"""One voice conversation: the Pipecat pipeline, its measurements and its end (docs/VOICE.md §4, §9, §13, §14).

``audio in -> Deepgram Flux (transcript + end of turn) -> user aggregator ->
mouth (Haiku on Bedrock; async tell_yuki, check_progress, more_of_result) ->
TTS (ElevenLabs or Deepgram Aura) -> audio out -> assistant aggregator``

:class:`Conversation` builds it and runs it on the calling asyncio loop (the
voice thread's; never the Qt thread), with the conversation's
:class:`~yuki.voice.narrator.NarratorGate` (when the brain's news is said) and
:class:`~yuki.voice.grounding.Grounder` (the memory lookup on every turn). It
ends when nobody has spoken and nothing is owed for ``Settings.voice_idle_s``,
when asked to stop, or on a fatal service error. A running brain task (or a
result the gate still holds) keeps a quiet conversation open for at most
``voice_hold_max_s``.

In **call mode** (the user is in a voice call, docs/VOICE.md §25) the mouth gets the call
layer and a call line on every request and decides itself whether each utterance was for
Yuki or for the people on the call; the conversation then ends ``voice_call_idle_s`` after
the last exchange with Yuki, and talk to the call is neither answered nor kept in memory.

:class:`TurnObserver` watches the frames and writes the content-free turn
record (every stage's timing, tokens, cost, language), drives the voice state
the UI shows, gives the gate the conversation's live state, and hands each
spoken exchange to memory's turn log (encrypted), marked as voice:
``request_id`` ``voice-<n>`` and ``outcome`` ``voice``.
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
from yuki.voice.hands import HAND_TOOLS
from yuki.voice.mouth import (
    CHECK_PROGRESS, END_CONVERSATION, MORE_OF_RESULT, MOUTH_PROMPT, STAY_QUIET, TELL_YUKI, MouthMemory,
    mouth_system_blocks, volatile_block,
)

#: Default voices per provider when ``Settings.voice_tts_voice`` is empty.
DEFAULT_VOICES = {"elevenlabs": "cgSgspJ2msm6clMCkdW9", "deepgram": "aura-2-thalia-en"}

#: The voice states the UI shows.
STATES = ("starting", "listening", "hearing", "thinking", "speaking", "working", "error", "off")


class VoiceUnavailable(Exception):
    """Voice cannot start or went down: the text says why, for the UI and the log."""


def hold_until_connected(ready: Callable[[], bool], log: Any, max_s: float = 10.0,
                         on_connected: Callable[[], None] | None = None) -> Any:
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
                if on_connected is not None:
                    on_connected()  # the quiet-time clock starts now, not while Deepgram was connecting
                if log is not None:
                    log.event("stt_connected", waited_ms=round((time.monotonic() - self._since) * 1000),
                              held_ms=round(sum(len(f.audio) for f in held) / 32))
                for old in held:
                    await self.push_frame(old, direction)
            await self.push_frame(frame, direction)

    return _Hold()


#: ElevenLabs can-speak check, done once per process: ``(ok, reason)``.
_TTS_CHECK: dict[str, tuple[bool, str]] = {}


def forget_tts_check() -> None:
    """Check the TTS again before the next conversation (the voice id changed)."""
    _TTS_CHECK.clear()


#: ElevenLabs error statuses (``detail.status`` of its JSON error body) that refuse one voice,
#: not the key: another voice can still speak. Parsing the API's structured answer, not text.
_VOICE_REFUSALS = {"paid_plan_required", "voice_not_found", "voice_not_fine_tuned", "invalid_voice_id",
                   "voice_access_denied"}


def _elevenlabs_error(exc: Any) -> tuple[str, str]:
    """``(status, message)`` from an ElevenLabs HTTP error's JSON body (``("", "")`` if it has none)."""
    import json

    try:
        detail = json.loads(exc.read()[:2000].decode("utf-8", "replace")).get("detail")
    except (ValueError, AttributeError, OSError):
        return "", ""
    if isinstance(detail, dict):
        return str(detail.get("status") or ""), str(detail.get("message") or "")
    return "", str(detail or "")


def elevenlabs_can_speak(settings: Settings, voice: str | None = None) -> tuple[bool, str]:
    """Whether ElevenLabs will speak with ``voice`` (one 1-character request, once per voice per app run).

    The key can be valid yet lack the Text to Speech permission (a scoped key), or
    the account may not be allowed a voice (a library voice on the free plan answers
    402 ``paid_plan_required``); then every reply would be silent, so this is checked
    before a conversation rather than discovered in one. Returns ``(ok, reason)``,
    where a refusal of the voice itself starts with ``voice:``; never raises.
    """
    voice = voice or settings.voice_tts_voice or DEFAULT_VOICES["elevenlabs"]
    cache_key = f"elevenlabs:{voice}:{settings.voice_tts_model}"
    if cache_key in _TTS_CHECK:
        return _TTS_CHECK[cache_key]
    key = get_key(ELEVENLABS)
    if not key:
        result = (False, "ELEVENLABS_API_KEY is not set")
    else:
        import json
        import urllib.error
        import urllib.request

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
            status, message = _elevenlabs_error(exc)
            if status == "missing_permissions":
                result = (False, "the key lacks the Text to Speech permission")
            elif status in _VOICE_REFUSALS or exc.code in (402, 404):
                why = "it needs a paid plan" if status == "paid_plan_required" or exc.code == 402 else (
                    message[:120] or "it is not in the account's voices")
                result = (False, f"voice: {why}")
            else:
                result = (False, f"HTTP {exc.code}{' ' + status if status else ''}")
        except (urllib.error.URLError, OSError) as exc:
            return False, f"cannot reach ElevenLabs ({type(exc).__name__})"  # not cached: may be the network
    _TTS_CHECK[cache_key] = result
    return result


#: The ElevenLabs characters left this month, as last read: ``(remaining, limit, read at)`` (monotonic).
_TTS_QUOTA: dict[str, tuple[int, int, float]] = {}
#: Below this many characters left, a conversation speaks with the fallback voice (a reply is ~50-300).
QUOTA_FLOOR_CHARS = 400
#: How long a quota read is trusted before the next check reads it again (s).
QUOTA_TRUST_S = 600.0


def elevenlabs_quota(refresh: bool = False) -> tuple[int, int] | None:
    """``(characters left, limit)`` of the ElevenLabs account this month, or ``None`` when it cannot be read.

    Live 2026-09-26 the free tier's 10,000 characters ran out: every reply then came back with no audio,
    Pipecat gave the TTS up after three ("can no longer do its job") and Yuki went silent, while the
    one-character can-speak check still passed. Read from the account's subscription (content-free),
    cached for :data:`QUOTA_TRUST_S`; never raises.
    """
    import time as _time

    cached = _TTS_QUOTA.get("elevenlabs")
    if cached and not refresh and _time.monotonic() - cached[2] < QUOTA_TRUST_S:
        return cached[0], cached[1]
    key = get_key(ELEVENLABS)
    if not key:
        return None
    import json
    import urllib.request

    try:
        req = urllib.request.Request("https://api.elevenlabs.io/v1/user/subscription", headers={"xi-api-key": key})
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read()[:20000].decode("utf-8", "replace"))
        used, limit = int(data.get("character_count") or 0), int(data.get("character_limit") or 0)
    except Exception:  # noqa: BLE001 - a scoped key may not read it: then only the can-speak check applies
        return None
    _TTS_QUOTA["elevenlabs"] = (max(0, limit - used), limit, _time.monotonic())
    return max(0, limit - used), limit


def preflight(settings: Settings, *, need_headphones: bool = True) -> dict[str, Any]:
    """Check what a conversation needs before opening anything. Raises :class:`VoiceUnavailable`.

    Returns facts for the log, including ``tts_provider``: the configured one,
    or ``voice_tts_fallback`` (with ``tts_note`` saying why) when the configured
    one cannot speak. Speakers are no reason to refuse any more (the echo guard,
    docs/VOICE.md §11) unless ``voice_require_headphones`` asks for the v3.3 rule.
    """
    facts: dict[str, Any] = {"tts_provider": settings.voice_tts_provider}
    if not get_key(DEEPGRAM):
        raise VoiceUnavailable("Voice needs DEEPGRAM_API_KEY (a Windows user environment variable).")
    if settings.voice_tts_provider == "elevenlabs":
        ok, reason = elevenlabs_can_speak(settings)
        quota = elevenlabs_quota()
        if ok and quota is not None and quota[0] < QUOTA_FLOOR_CHARS:
            ok, reason = False, f"its monthly characters are used up ({quota[0]} of {quota[1]} left)"
            facts["tts_quota_left"] = quota[0]
        default = DEFAULT_VOICES["elevenlabs"]
        if not ok and reason.startswith("voice:") and (settings.voice_tts_voice or default) != default:
            # That voice is refused (e.g. a library voice on the free plan): this conversation
            # speaks with the default ElevenLabs voice and says why; the chosen one is kept,
            # so it just works once the account allows it.
            ok_default, _why = elevenlabs_can_speak(settings, default)
            if ok_default:
                facts["tts_voice"] = default
                facts["tts_note"] = (f"ElevenLabs won't use the chosen voice: {reason[6:].strip().rstrip('.')}. "
                                     f"Using the default voice for now")
                ok, reason = True, "ok"
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

        ok, heard = headphones_check(settings.voice_headphone_devices, device=settings.voice_output_device)
        facts["output"] = heard
        if not ok:
            where = heard.get("device") or "the speakers"
            raise VoiceUnavailable(
                f"Voice needs headphones for now: sound is going to {where}, and Yuki would hear itself."
            )
    return facts


_LOGGING_SET = False


@dataclass
class LlmCall:
    """Timestamps (monotonic) of one mouth call, stage by stage (docs/VOICE.md §14)."""

    entered: float                      # LLM service got the context
    grounded: float | None = None       # grounding wait over
    grounding: str = "off"              # final | partial | previous | none | off
    facts: int = 0
    sent: float | None = None           # the API request went out
    opened: float | None = None         # the stream was open (response headers)
    first_event: float | None = None    # message_start
    first_content: float | None = None  # first content block (Pipecat's TTFB ends here)
    first_text: float | None = None     # first text delta
    user_turn: bool = True
    desk: str = "off"                   # fresh | previous | none | off (the desktop summary, §5.6)
    desk_ms: float | None = None        # capturing it (fresh) or its age (previous)
    desk_chars: int = 0
    desk_tabs_ms: float | None = None   # reading the foreground window's tabs for it
    early_say: float | None = None      # a call's say_first went to the voice while the call was being written


#: Tools whose spoken acknowledgement travels in the call itself (``say_first``), said as soon as it is written.
SAY_FIRST_TOOLS = frozenset({"tell_yuki", "open_url", "open_app", "end_conversation"})


def early_words(partial_json: str) -> str | None:
    """The ``say_first`` value of a tool call whose arguments are still streaming, once that string is whole.

    The mouth writes ``say_first`` first (its schema asks it to); the moment its string is complete in
    the streamed JSON it can be spoken, while the rest of the call is still being written (v3.5,
    docs/VOICE.md §5.2). Parsing the call's own JSON: ``None`` until the value is complete, or when
    the arguments have no ``say_first``.
    """
    import json

    key = partial_json.find('"say_first"')
    if key < 0:
        return None
    colon = partial_json.find(":", key + len('"say_first"'))
    if colon < 0:
        return None
    start = colon + 1
    while start < len(partial_json) and partial_json[start] in " \t\r\n":
        start += 1
    if start >= len(partial_json) or partial_json[start] != '"':
        return None
    try:
        value, _end = json.JSONDecoder().raw_decode(partial_json, start)
    except ValueError:
        return None  # the string is not complete yet
    return value.strip() if isinstance(value, str) and value.strip() else None


def one_stop_per_turn(base: type) -> type:
    """``base`` (Pipecat's ``EagerUserTurnStopStrategy``) resolving each user turn's end once.

    Found 2026-09-26 with Pipecat's debug log (eager end of turn on, the latency926 replay): Flux's
    proposed turn stop and the stop strategy's transcript timer resolve the same turn within a
    millisecond. The eager strategy awaits inside its resolution (stopping its speculation timer), so
    the second call found no speculation and ran a second inference, whose turn-stop frame released
    the speculative reply, and the first call then judged the speculation "missed" and ran a third:
    two or three replies to one "Hello?" (and two answers to "what's the CLI setup?", one without the
    thinker). Here the resolutions are serialized and a turn is resolved once; the next turn's start
    re-arms it. Plumbing: when a turn ends, not what was said.
    """

    class OneStopPerTurn(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._yuki_lock = asyncio.Lock()
            self._yuki_resolved = False

        async def handle_user_turn_started(self) -> None:
            self._yuki_resolved = False
            await super().handle_user_turn_started()

        async def trigger_user_turn_stopped(self, **kwargs: Any) -> None:
            async with self._yuki_lock:
                if self._yuki_resolved:
                    return
                self._yuki_resolved = True
                await super().trigger_user_turn_stopped(**kwargs)

    return OneStopPerTurn


#: A word recognized with less confidence than this is named to the mouth (measured 2026-09-27 on 44 clips:
#: misheard names 0.40-0.74, e.g. "harsh" 0.40, "Hemish" 0.48, "Varez" 0.49; words of clear commands mostly
#: >= 0.9, a few 0.64-0.86).
LOW_WORD_CONFIDENCE = 0.7
#: A heard word (or two adjacent ones) this similar to a keyterm, without being it, is named as sounding close
#: (measured: "Hemish" / "harsh" / "Hemant" 0.83-0.92 and "hey meant" 0.67 against Hemanth, "Komala" 0.83
#: against Komal, "cloud" 0.73 against Claude; the words of clear commands <= 0.5 against every name). A
#: similarity measure, no word list.
NEAR_TERM_RATIO = 0.65


def heard_note(words: Any, keyterms: list[str]) -> tuple[str | None, int, int]:
    """What speech recognition was unsure of in a final transcript, for the mouth: ``(note, low, near)``.

    ``words`` is Flux's TurnInfo word list (``{"word", "confidence"}``); ``keyterms`` the names memory
    holds. The note names the words below :data:`LOW_WORD_CONFIDENCE` and the heard words that sound
    close to a keyterm without being it; ``None`` when there is nothing to say. Measured signals only:
    the mouth decides from them and the meaning whether to ask back.
    """
    import difflib

    items = [w for w in (words or []) if isinstance(w, dict) and str(w.get("word") or "").strip()]

    def key(text: str) -> str:
        return "".join(ch for ch in text.casefold() if ch.isalnum())

    low = [(str(w["word"]).strip(" ,.?!"), float(w["confidence"])) for w in items
           if isinstance(w.get("confidence"), (int, float)) and float(w["confidence"]) < LOW_WORD_CONFIDENCE]
    terms = {key(t): t for t in keyterms if len(key(t)) >= 4}
    near: list[tuple[str, str]] = []
    spoken = [str(w["word"]).strip(" ,.?!") for w in items]
    # Words that are a known name, alone or joined with the next ("eleven labs"), are not near misses.
    exact = {i for i, w in enumerate(spoken) if key(w) in terms}
    exact |= {j for i in range(len(spoken) - 1) if key(spoken[i] + spoken[i + 1]) in terms for j in (i, i + 1)}
    def closest(k: str) -> str | None:
        # The same first sound and most of the letters: misheard names keep their start ("Hemish", "hey
        # meant" for Hemanth, "cloud" for Claude); a word that merely contains a name's letters does not
        # ("after" in "Father"). Measures of the two strings, no word list.
        best = max((t for t in terms if t[:1] == k[:1] and k not in t and t not in k),
                   key=lambda t: difflib.SequenceMatcher(None, k, t).ratio(), default=None)
        return best if best and difflib.SequenceMatcher(None, k, best).ratio() >= NEAR_TERM_RATIO else None

    flagged: set[int] = set()
    for size in (1, 2):  # single words first; a pair only when neither of its words was named alone
        for i in range(len(spoken) - size + 1):
            window = spoken[i:i + size]
            k = key(" ".join(window))
            if (len(k) < 5 or k in terms or any(j in exact or j in flagged for j in range(i, i + size))
                    or (size == 2 and any(len(key(w)) < 3 for w in window))):
                continue
            best = closest(k)
            if best:
                near.append((" ".join(window), terms[best]))
                flagged.update(range(i, i + size))
    parts = []
    if low:
        parts.append("unsure of " + ", ".join(f"'{w}' ({c:.2f})" for w, c in low[:5]))
    if near:
        parts.append("; ".join(f"'{h}' sounds close to {t}, a name you know" for h, t in near[:3]))
    return ("; ".join(parts) or None), len(low), len(near)


def keyterm_list(base: list[str], extra: list[str], cap: int) -> list[str]:
    """Speech recognition's keyterms: ``base`` then ``extra``, each once (case-insensitive), at most ``cap``."""
    out: list[str] = []
    seen: set[str] = set()
    for term in [*base, *extra]:
        term = " ".join(str(term or "").split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out[: max(1, cap)]


def with_volatile(messages: Any, block: dict[str, Any]) -> Any:
    """``messages`` with ``block`` appended to the last user message, after its cache breakpoint.

    The time and the grounding lookup change on every call. In the system prompt they
    would sit before the whole conversation and break its cache (measured: every turn
    rewrote 1-2k tokens of history); as the last block of the newest user message they
    come after every breakpoint, so the next call reads everything before them from the
    cache. Only the copy sent is changed, never the conversation's context.
    """
    if not isinstance(messages, list):
        return messages
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if isinstance(message, dict) and message.get("role") == "user":
            content = message.get("content")
            parts = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
            out = list(messages)
            out[i] = {**message, "content": [*parts, block]}
            return out
    return messages


def chronological(messages: list[Any], spoke: set[str]) -> list[Any]:
    """``messages`` with each reply's spoken words back before the tool calls it made.

    Pipecat writes a reply that spoke and then called a tool as the tool call, its
    placeholder, and only then the words (the words are committed when the reply ends).
    A result that settles in the placeholder therefore sits *before* the words that
    announced it, the conversation ends on the mouth's own words, and the Anthropic
    adapter pads it with a user message "." (models past 4.5 take no prefill; the
    ``global.`` id is not in Pipecat's prefill list). Measured 2026-09-25: that "." made
    the mouth answer a finished job with "What's up?" / "I'm here. What's up?", and in the
    offline replay read out a "<thinking>" about the time. Moving each reply's words back
    before its own calls restores the order they were said in, so a result is the newest
    thing and no "." is added. ``spoke`` holds the tool call ids of replies that had words
    (a stream fact, recorded by the service). Plumbing: only the order changes.
    """
    out = list(messages)
    i = 0
    while i < len(out):
        message = out[i]
        calls = message.get("tool_calls") if isinstance(message, dict) and message.get("role") == "assistant" else None
        if calls and any(isinstance(c, dict) and c.get("id") in spoke for c in calls):
            j = i + 1
            while j < len(out) and isinstance(out[j], dict) and (
                    out[j].get("role") == "tool" or (out[j].get("role") == "assistant" and out[j].get("tool_calls"))):
                j += 1
            if (j < len(out) and isinstance(out[j], dict) and out[j].get("role") == "assistant"
                    and not out[j].get("tool_calls") and out[j].get("content")):
                out.insert(i, out.pop(j))
                i += 1
        i += 1
    return out


def settle_only_when_newest(aggregator: Any) -> None:
    """A tool's result settles in place only while its placeholder is the newest message.

    Pipecat settles an async result in place (overwriting its "started" placeholder)
    unless a user or developer message follows it; assistant messages, other calls and
    their results do not count. Live 2026-09-25 18:56:57: a thinker's result arrived
    after the mouth had spoken and called stay_quiet on another task's result; it
    settled in place, in the middle of the conversation, the newest thing the mouth saw
    was its own stay_quiet, and it answered the run with "What's up?". Here a result
    goes in place only when nothing at all came after its placeholder; otherwise it is
    appended as Pipecat's deferred final message, so a result is always the newest
    thing when the mouth is asked to speak. Plumbing: where a message goes, not what it
    says. ``aggregator`` is Pipecat 1.11's ``LLMAssistantAggregator``.
    """
    context = aggregator._context

    def _is_deferred(in_progress_frame: Any) -> bool:
        if in_progress_frame.cancel_on_interruption:
            return False  # a synchronous call: always in place, as Pipecat does
        messages = context.messages
        for i, message in enumerate(messages):
            if (isinstance(message, dict) and message.get("role") == "tool"
                    and message.get("tool_call_id") == in_progress_frame.tool_call_id):
                return i != len(messages) - 1
        return True  # the placeholder is gone (the context was rebuilt): deferred, as Pipecat does

    aggregator._is_deferred = _is_deferred


def bedrock_client(region: str) -> Any:
    """The mouth's Bedrock client, keeping its connection between turns.

    httpx drops an idle connection after 5 s by default, so every turn after a pause
    paid a new TCP and TLS handshake: measured 2026-09-25 from this PC, time to the first
    token after 8-20 s idle p50 1,362 ms with the default against 860 ms with the
    connection kept (this was the "unexplained ~0.7 s" of docs/VOICE.md §14).
    """
    import httpx
    from anthropic import AsyncAnthropicBedrock, DefaultAsyncHttpxClient

    return AsyncAnthropicBedrock(aws_region=region, http_client=DefaultAsyncHttpxClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=4, keepalive_expiry=300.0)))


def _layered_service(base: type, memory: MouthMemory, hooks: Any) -> type:
    """``base`` (Pipecat's ``AnthropicLLMService``) sending the mouth's prompt as cached layers, instrumented.

    Pipecat passes ``system`` as one string. This subclass swaps it for the blocks of
    :func:`~yuki.voice.mouth.mouth_system_blocks`: the composed core (our prompt plus
    Pipecat's async-tool paragraphs, frozen), the profile, the standing rules, and a
    volatile tail with the time now and the grounding lookup for this call (after every
    cache breakpoint, so the cached layers stay byte-identical). Before each call it
    waits (bounded) for the grounding lookup, and it records every stage of the call
    (context in, request out, stream open, first event, first content, first text) for
    the turn log. Made on first use so importing this module loads no Pipecat.
    """

    class LayeredAnthropicLLMService(base):  # type: ignore[misc, valid-type]
        _yuki_lookup: str | None = None
        _yuki_desk: str | None = None
        _yuki_call: LlmCall | None = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            #: Tool call ids of replies that said words before calling (see ``chronological``).
            self._yuki_spoke: set[str] = set()
            #: Tool call ids whose say_first was spoken while the call was still being written (v3.5).
            self._yuki_early: set[str] = set()
            #: Tool calls of a speculative reply (eager end of turn), run when the turn is confirmed.
            self._yuki_held_calls: list[Any] | None = None

        async def run_function_calls(self, function_calls: Any) -> None:
            if function_calls and self._speculation_gate.is_speculating:
                # A reply to an eager end of turn wants a tool. Pipecat withdraws the whole reply and
                # asks again after the committed turn (a second call: the latency eager was for).
                # Here the calls wait, as the reply's words wait in the gate: they run the moment the
                # turn is confirmed and are dropped with the words if it is withdrawn (v3.5).
                self._yuki_held_calls = list(function_calls)
                hooks.speculation("held_calls", len(function_calls))
                return
            call = self._yuki_call
            if call is not None and call.first_text is not None:
                self._yuki_spoke.update(str(fc.tool_call_id) for fc in function_calls)
            await super().run_function_calls(function_calls)

        async def push_frame(self, frame: Any, direction: Any = None) -> None:
            from pipecat.frames.frames import EagerEndOfTurnCancelFrame, InterruptionFrame, UserStoppedSpeakingFrame
            from pipecat.processors.frame_processor import FrameDirection

            direction = FrameDirection.DOWNSTREAM if direction is None else direction
            await super().push_frame(frame, direction)
            held = self._yuki_held_calls
            if held is None or direction != FrameDirection.DOWNSTREAM:
                return
            if isinstance(frame, UserStoppedSpeakingFrame):
                # The turn is confirmed: the gate has just released the reply's words; its calls follow.
                self._yuki_held_calls = None
                hooks.speculation("released_calls", len(held))
                self.create_task(self.run_function_calls(held), "yuki-held-calls")
            elif isinstance(frame, (EagerEndOfTurnCancelFrame, InterruptionFrame)):
                self._yuki_held_calls = None
                hooks.speculation("dropped_calls", len(held))

        async def _yuki_say_early(self, call: LlmCall, tool_id: str, words: str) -> None:
            """Speak a call's say_first now, while the rest of the call is still being written."""
            from pipecat.frames.frames import TTSSpeakFrame

            self._yuki_early.add(tool_id)
            self._yuki_spoke.add(tool_id)  # its words go before the call in the context (chronological)
            call.early_say = time.monotonic()
            await self.push_frame(TTSSpeakFrame(text=words))
            hooks.early_said(call, len(words))

        async def _process_context(self, context: Any) -> None:
            self._yuki_held_calls = None  # a new inference: whatever an older speculation held is void
            call = LlmCall(entered=time.monotonic())
            self._yuki_call = call
            try:
                self._yuki_lookup, self._yuki_desk = await hooks.before_call(context, call)
            except Exception:  # noqa: BLE001 - grounding is a hint; never block the reply on it
                self._yuki_lookup, self._yuki_desk = None, None
            call.grounded = time.monotonic()
            hooks.call_started(call)
            await super()._process_context(context)

        def _get_llm_invocation_params(self, context: Any) -> Any:
            if self._yuki_spoke:
                from pipecat.processors.aggregators.llm_context import LLMContext

                messages = context.get_messages()
                ordered = chronological(messages, self._yuki_spoke)
                if ordered != messages:
                    context = LLMContext(messages=ordered, tools=context.tools, tool_choice=context.tool_choice)
            params = super()._get_llm_invocation_params(context)
            composed = self._settings.system_instruction or MOUTH_PROMPT
            tail = composed[len(MOUTH_PROMPT):] if composed.startswith(MOUTH_PROMPT) else composed
            call = hooks.call()
            params["system"] = mouth_system_blocks(memory, tail=[tail.strip()], call=call)
            params["messages"] = with_volatile(params.get("messages"), volatile_block(
                lookup=self._yuki_lookup, desk=self._yuki_desk, call=call, heard=hooks.heard()))
            return params

        async def _cancel_tool_handler(self, params: Any) -> None:
            # A successful cancel settles without another turn: the mouth says it is
            # stopping in the same reply as the call (measured: the extra turn made it
            # greet the user out of nowhere). A refusal still gets a turn, to react to it.
            from pipecat.frames.frames import FunctionCallResultProperties

            original = params.result_callback

            async def settle(result: Any, *, properties: Any = None) -> None:
                if isinstance(result, dict) and result.get("cancelled"):
                    properties = FunctionCallResultProperties(run_llm=False)
                await original(result, properties=properties)

            params.result_callback = settle
            await super()._cancel_tool_handler(params)

        async def _yuki_open(self, api_call: Any, params: Any) -> tuple[Any, Any]:
            """Open the reply's stream and read its first event, with failover (v3.5).

            The mouth had none (the reliability builder's note, 2026-09-27). As ``yuki.agent.failover``
            does for the brain: a failure that says nothing about the request (throttled, overloaded, 5xx,
            timeout, no connection) is retried once on the same target after a short backoff, a missing
            model or profile (404/424) moves on at once, then the chain of ``Settings.model_fallbacks`` for
            the mouth's model (the global profile from us-east-1, the US profile, then Sonnet). The first
            event is read here because Bedrock often reports throttling as the stream's first event. A
            target that worked is kept for ``failover_sticky_s``. Logged ``model_retry`` /
            ``model_failover`` (content-free).
            """
            from yuki.agent.failover import adapt_params, classify

            chain = [None, *hooks.fallbacks()]
            start = hooks.sticky_index(len(chain))
            last: BaseException | None = None
            for index in range(start, len(chain)):
                target = chain[index]
                for attempt in range(2 if index == 0 else 1):
                    try:
                        if target is None:
                            response = await super()._create_message_stream(api_call, params)
                        else:
                            client = hooks.client_for(target.get("region"))
                            response = await client.beta.messages.create(**adapt_params(params, target["model"]))
                        stream = response.__aiter__()
                        try:
                            first = await stream.__anext__()
                        except StopAsyncIteration:
                            first = None
                        hooks.model_ok(index, target)
                        return stream, first
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:  # noqa: BLE001 - classified from the SDK's own types
                        last = exc
                        kind = classify(exc)
                        if kind == "fatal":
                            raise
                        hooks.model_problem(index, target, exc, kind)
                        if kind == "retry" and attempt == 0 and index == 0:
                            await asyncio.sleep(0.4)
                            continue
                        break
            assert last is not None
            raise last

        async def _create_message_stream(self, api_call: Any, params: Any) -> Any:
            call = self._yuki_call
            if call is not None:
                call.sent = time.monotonic()
            stream, first = await self._yuki_open(api_call, params)

            async def events() -> Any:
                if first is not None:
                    yield first
                async for event in stream:
                    yield event

            response = events()
            if call is None:
                return response
            call.opened = time.monotonic()

            async def timed() -> Any:
                tool_id, args, said = None, "", True
                async for event in response:
                    now = time.monotonic()
                    if call.first_event is None:
                        call.first_event = now
                    kind = getattr(event, "type", "")
                    if call.first_content is None and kind in ("content_block_start", "content_block_delta"):
                        call.first_content = now
                    if call.first_text is None and kind == "content_block_delta" and hasattr(event.delta, "text"):
                        call.first_text = now
                    if kind == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                        tool_id, args = str(event.content_block.id), ""
                        said = getattr(event.content_block, "name", "") not in SAY_FIRST_TOOLS
                    elif (kind == "content_block_delta" and not said and tool_id is not None
                          and hasattr(event.delta, "partial_json")):
                        args += event.delta.partial_json or ""
                        words = early_words(args)
                        if words is not None:
                            said = True
                            if call.first_text is None:  # a reply that already spoke keeps its own words
                                await self._yuki_say_early(call, tool_id, words)
                    yield event

            return timed()

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
    eos_source: str = "none"             # mic | stt | none (how the end of speech is known)
    responses: int = 0                   # mouth runs finished in this turn
    eot_at: float | None = None          # Flux's end-of-turn event arrived
    eager_at: float | None = None        # Flux's eager end-of-turn (a speculative reply started)
    transcript_at: float | None = None
    context_at: float | None = None      # the user aggregator pushed the context to the mouth
    llm: LlmCall | None = None
    llm_ttfb: float | None = None
    tts_started: float | None = None
    tts_ttfb: float | None = None
    tts_audio: float | None = None
    first_audio: float | None = None
    action_at: float | None = None       # a hand's action went out (v3.3: end of speech -> action)
    handoff_at: float | None = None      # a tell_yuki call started: the job is submitted now (v3.5)
    reply: list[str] = field(default_factory=list)
    spoken: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    tts_chars: int = 0
    interrupted: bool = False
    error: str | None = None
    call: bool = False                   # the user was in a voice call (call mode)
    counted: bool = False                # counted as an exchange with Yuki
    cut: bool = False                    # the user spoke again before this turn's reply was heard
    heard_low: int = 0                   # words recognition was unsure of (below LOW_WORD_CONFIDENCE)
    heard_near: int = 0                  # heard words that sound close to a keyterm without being it


class TurnObserver:
    """Frame observer: turn timings, usage, state, and the memory turn log. Content goes only to memory.

    Also the conversation's live state for the narrator gate (who is speaking,
    whether Yuki owes a reply, when it last stopped talking, how many user turns).
    """

    #: A reply owed for longer than this is treated as given (a turn that ended in silence).
    OWES_REPLY_MAX_S = 8.0

    def __init__(self, *, settings: Settings, log: Any, memory: Any, session: str,
                 speech_end: Callable[[], float | None], on_state: Callable[[str, str], None],
                 tts_provider: str, activity: Callable[[], None],
                 call_mode: Callable[[], bool] = lambda: False) -> None:
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
        self.owes_reply_since: float | None = None
        self.last_bot_stop = 0.0
        self.user_turns = 0
        self._user_started_at = 0.0
        self._pending_call: LlmCall | None = None
        self._eot_at: float | None = None
        self._eager_at: float | None = None
        self._last_words = ""
        self._words_at: float | None = None
        self.grounder: Any = None
        self.hands: Any = None
        self.state = "starting"
        self.fatal: str | None = None
        self.totals = {"turns": 0, "mouth_cost_usd": 0.0, "tts_chars": 0, "for_others": 0}
        self._call_mode = call_mode
        #: User turns Yuki answered (words or an action; not stay_quiet), and when Yuki last took
        #: part (spoke, acted, handed over): the idle clocks run from these (docs/VOICE.md §25).
        self.exchanges = 0
        self.last_exchange: float | None = None
        #: When the mouth's last reply stream ended (its tool calls start just after it).
        self.last_response_end = 0.0
        #: The state to show when nobody is talking and nothing is being generated: "working" only while
        #: a job really runs, else "listening" (set by the conversation; v3.5, the pill bug of 26 Sept).
        self.idle_state: Callable[[], str] = lambda: "listening"
        #: ``(event dict)``: the chat UI's events (words: for the screen, never the log; v3.5). Set by the session.
        self.on_chat: Callable[[dict[str, Any]], None] | None = None
        #: The output transport: a word Yuki says counts as heard when it leaves this processor.
        self.output_processor: Any = None
        #: What recognition was unsure of in the latest final transcript (:func:`heard_note`), for the mouth.
        self.heard: str | None = None
        #: The names memory holds (the conversation's keyterms): a heard word close to one is named.
        self.keyterms: list[str] = []
        self._said: list[str] = []
        self._said_turn: str | None = None

    def as_observer(self) -> Any:
        outer = self

        class _Obs(self._base):
            async def on_push_frame(self, data: Any) -> None:
                outer.on_played(data)
                outer.on_frame(data.frame)

        return _Obs()

    # -- the chat UI's events (docs/VOICE.md §12) --------------------------------------

    def turn_id(self) -> str:
        """The id of the turn now open (``<session>-<n>``), for the chat UI."""
        return f"{self.session}-{self.turn.n if self.turn is not None else self._n}"

    def chat(self, event: dict[str, Any]) -> None:
        if self.on_chat is not None:
            try:
                self.on_chat(event)
            except Exception:  # noqa: BLE001 - the screen never breaks voice
                pass

    def on_played(self, data: Any) -> None:
        """Yuki's words as they leave the output (audible), and where speech ended or was cut."""
        if self.on_chat is None:
            return
        from pipecat.frames.frames import BotStoppedSpeakingFrame, InterruptionFrame, TTSTextFrame

        frame = data.frame
        if isinstance(frame, TTSTextFrame) and self.output_processor is not None \
                and getattr(data, "source", None) is self.output_processor:
            turn = self.turn_id()
            if turn != self._said_turn:
                self._said, self._said_turn = [], turn
            self._said.append(frame.text or "")
            self.chat({"type": "assistant_speech", "turn_id": turn, "text": " ".join(w.strip() for w in self._said
                                                                                      if w.strip()),
                       "final": False, "interrupted": False})
        elif isinstance(frame, (BotStoppedSpeakingFrame, InterruptionFrame)) and self._said:
            if isinstance(frame, InterruptionFrame) and not self.bot_speaking:
                return
            self.chat({"type": "assistant_speech", "turn_id": self._said_turn,
                       "text": " ".join(w.strip() for w in self._said if w.strip()), "final": True,
                       "interrupted": isinstance(frame, InterruptionFrame)})
            self._said = []

    # -- live state (the narrator gate) -------------------------------------------

    @property
    def owes_reply(self) -> bool:
        since = self.owes_reply_since
        return since is not None and time.monotonic() - since < self.OWES_REPLY_MAX_S

    def live_state(self) -> dict[str, Any]:
        return {"user_speaking": self.user_speaking, "bot_speaking": self.bot_speaking,
                "generating": self.generating, "owes_reply": self.owes_reply,
                "last_bot_stop": self.last_bot_stop, "user_turns": self.user_turns}

    # -- STT events (Flux), called from the STT service's event handlers --------------

    def on_eot(self, transcript: str) -> None:
        """Flux's end-of-turn event (it comes after the transcript frame, which is pushed at end of turn)."""

    def on_eager(self, transcript: str) -> None:
        self._eager_at = time.monotonic()
        if self.grounder is not None:
            self.grounder.interim(transcript)

    def on_update(self, transcript: str) -> None:
        words = " ".join((transcript or "").split())
        if words and words != self._last_words:
            self._last_words = words
            self._words_at = time.monotonic()  # when speech recognition last heard a new word
        if self.grounder is not None:
            self.grounder.interim(transcript)

    def _end_of_speech(self, now: float) -> tuple[float | None, str]:
        """When the user stopped talking this turn, and how that is known.

        ``mic``: the microphone's last voiced block (its level against the room's floor);
        ``stt``: speech recognition's last new word, when the microphone saw no speech
        in this turn (a very quiet mic). Measurement only.
        """
        mic = self._speech_end()
        if mic is not None and self._user_started_at - 0.3 <= mic <= now:
            return mic, "mic"
        words = self._words_at
        if words is not None and self._user_started_at - 0.3 <= words <= now:
            return words, "stt"
        return None, "none"

    def hand_acted(self, sent_at: float | None) -> None:
        """A hand's action went out (monotonic): the turn's end of speech -> action (v3.3)."""
        turn = self.turn
        if turn is not None and sent_at is not None and turn.action_at is None:
            turn.action_at = sent_at

    def call_started(self, call: LlmCall) -> None:
        """A mouth call began (the LLM service's hook): attach it to the turn it answers."""
        turn = self.turn
        if (turn is not None and turn.llm is None and turn.first_audio is None
                and turn.transcript_at is not None and call.entered >= turn.transcript_at):
            turn.llm = call
        else:
            self._pending_call = call  # speculative (eager) or a result turn: attached when it opens

    # -- helpers -----------------------------------------------------------------

    def set_state(self, state: str, detail: str = "") -> None:
        if state != self.state or detail:
            self.state = state
            self._on_state(state, detail)

    def _open(self, trigger: str) -> _Turn:
        if self.turn is not None:
            self.close()
        self._n += 1
        self.turn = _Turn(n=self._n, trigger=trigger, opened=time.monotonic(), call=bool(self._call_mode()))
        return self.turn

    def _took_part(self, now: float) -> None:
        """Yuki spoke, acted or handed something over: an exchange with her is going on."""
        self.last_exchange = now
        turn = self.turn
        if turn is not None and turn.trigger == "user" and not turn.counted:
            turn.counted = True
            self.exchanges += 1

    @staticmethod
    def for_others(turn: _Turn, reply: str) -> bool:
        """A call-mode user turn Yuki took no part in: the mouth stayed quiet (its judgement that the words
        were for the people on the call), or the next words came before it said or did anything."""
        return turn.call and turn.trigger == "user" and not reply and not turn.counted

    @staticmethod
    def _ms(a: float | None, b: float | None) -> float | None:
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
        c = turn.llm
        ms = self._ms
        row = {
            "session": self.session, "turn": turn.n, "trigger": turn.trigger, "language": turn.language,
            "user_chars": len(turn.user_text), "reply_chars": len(reply),
            "eos_source": turn.eos_source if turn.trigger == "user" else None,
            "eos_to_transcript_ms": ms(turn.speech_end, turn.transcript_at),
            "eos_to_first_token_ms": ms(turn.speech_end, c.first_text if c else None),
            "mouth_ttft_ms": round(turn.llm_ttfb * 1000, 1) if turn.llm_ttfb is not None else None,
            "tts_ttfb_ms": round(turn.tts_ttfb * 1000, 1) if turn.tts_ttfb is not None else None,
            "eos_to_first_audio_ms": ms(turn.speech_end, turn.first_audio),
            "eos_to_action_ms": ms(turn.speech_end, turn.action_at),
            "eos_to_handoff_ms": ms(turn.speech_end, turn.handoff_at),
            "mouth_input_tokens": usage["input_tokens"], "mouth_cache_read_tokens": usage["cache_read_tokens"],
            "mouth_cache_write_tokens": usage["cache_write_tokens"], "mouth_output_tokens": usage["output_tokens"],
            "mouth_cost_usd": round(cost, 6) if cost is not None else None,
            "tts_chars": turn.tts_chars, "tts_cost_usd": tts_cost,
            "tool_calls": ";".join(turn.tools), "interrupted": turn.interrupted, "error": turn.error,
            "cut_by_user": turn.cut, "heard_low": turn.heard_low, "heard_near": turn.heard_near,
            # stage by stage (docs/VOICE.md §14)
            "eos_to_eot_ms": ms(turn.speech_end, turn.eot_at),
            "eos_to_eager_ms": ms(turn.speech_end, turn.eager_at),
            "transcript_to_context_ms": ms(turn.transcript_at, turn.context_at),
            "llm_start_after_eos_ms": ms(turn.speech_end, c.entered) if c else None,
            "grounding": c.grounding if c else None, "grounding_facts": c.facts if c else None,
            "grounding_wait_ms": ms(c.entered, c.grounded) if c else None,
            "desk": c.desk if c else None, "desk_ms": c.desk_ms if c else None,
            "desk_chars": c.desk_chars if c else None,
            "desk_tabs_ms": c.desk_tabs_ms if c else None,
            "llm_prep_ms": ms(c.grounded, c.sent) if c else None,
            "llm_open_ms": ms(c.sent, c.opened) if c else None,
            "llm_first_event_ms": ms(c.sent, c.first_event) if c else None,
            "llm_api_ttft_ms": ms(c.sent, c.first_content) if c else None,
            "llm_first_text_ms": ms(c.sent, c.first_text) if c else None,
            "llm_early_say_ms": ms(c.sent, c.early_say) if c else None,
            "text_to_tts_ms": ms(c.first_text if c else None, turn.tts_started),
            "tts_audio_to_speaking_ms": ms(turn.tts_audio, turn.first_audio),
            "eager": bool(c and turn.transcript_at and c.entered < turn.transcript_at),
        }
        others = self.for_others(turn, reply)
        addressed = None
        if turn.trigger == "user":
            addressed = ("others" if others or (not reply and "stay_quiet" in turn.tools and not turn.counted)
                         else "yuki")
            self.chat({"type": "turn_closed", "turn_id": f"{self.session}-{turn.n}", "addressed": addressed,
                       "replied": bool(reply), "cut_by_user": turn.cut})
        if turn.call:
            row["call_mode"] = True
            if turn.trigger == "user":
                row["addressed"] = ("yuki" if not others else
                                    "others" if "stay_quiet" in turn.tools else "unanswered")
        self.totals["turns"] += 1
        self.totals["for_others"] += int(others)
        self.totals["mouth_cost_usd"] += cost or 0.0
        self.totals["tts_chars"] += turn.tts_chars
        if self.log is not None:
            self.log.turn(row)
        if self.memory is not None and (turn.user_text or reply):
            # Every turn is logged, marked with who it was addressed to (v3.5, the memory builder's relay):
            # talk the mouth judged was for someone else (the people on a call, dictation to another app)
            # is stored as "others" and never extracted into memory; the thinker is never handed it either
            # (``quiet_answered``).
            try:
                self.memory.log_turn(
                    request_id=f"voice-{self.session}-{turn.n}", at=time.time(),
                    user_text=turn.user_text, reply_text=reply,
                    actions=[f"{name} (by voice)" for name in turn.tools], outcome="voice", addressed=addressed,
                )
            except Exception as exc:  # noqa: BLE001 - memory never breaks voice
                if self.log is not None:
                    self.log.event("memory_turn", error=f"{type(exc).__name__}: {exc}")

    # -- frames ------------------------------------------------------------------

    def on_frame(self, frame: Any) -> None:  # noqa: C901 - one switch over frame types
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame, BotStoppedSpeakingFrame, EagerTranscriptionFrame, ErrorFrame,
            FunctionCallInProgressFrame,
            InterruptionFrame, LLMContextFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
            MetricsFrame, TranscriptionFrame, TTSAudioRawFrame, TTSStartedFrame, TTSTextFrame,
            UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
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

        if isinstance(frame, TTSAudioRawFrame):
            if self.turn is not None and self.turn.tts_audio is None:
                self.turn.tts_audio = now
            return
        if isinstance(frame, EagerTranscriptionFrame):
            # A speculative reply starts from these words: what recognition was unsure of in them (v3.5).
            result = getattr(frame, "result", None)
            self.heard = heard_note(result.get("words") if isinstance(result, dict) else None, self.keyterms)[0]
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            self._user_started_at = now
            self.heard = None  # a new turn: the last one's doubts are not about these words
            if self.bot_speaking and self.turn is not None:
                self.turn.interrupted = True
            if (self.turn is not None and self.turn.trigger == "user" and self.turn.first_audio is None
                    and self.owes_reply):
                # They went on talking before hearing a reply (a pause inside what they were saying): that
                # reply is dropped and the next one answers everything (logged: silent turns, 26 Sept).
                self.turn.cut = True
            if self.grounder is not None:
                self.grounder.new_turn()
            if self.hands is not None:
                self.hands.prefetch()  # the tabs for the reply's desktop block, read while they talk
            self.set_state("hearing")
            self._activity()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self._activity()
        elif isinstance(frame, TranscriptionFrame):
            turn = self._open("user")
            turn.user_text = frame.text or ""
            self.chat({"type": "user_utterance", "text": turn.user_text, "ts": time.time(), "turn_id": self.turn_id()})
            turn.transcript_at = now
            turn.speech_end, turn.eos_source = self._end_of_speech(now)
            result = getattr(frame, "result", None)
            self.heard, turn.heard_low, turn.heard_near = heard_note(
                result.get("words") if isinstance(result, dict) else None, self.keyterms)
            self._last_words, self._words_at = "", None
            turn.eot_at = now  # Flux pushes the final transcript at its end of turn
            turn.eager_at, self._eager_at = self._eager_at, None
            if self.grounder is not None:
                self.grounder.final(turn.user_text)  # before the aggregator hands the context over
            if self.hands is not None:
                self.hands.new_user_turn()  # a hand halted by a wrong effect may act again
            pending, self._pending_call = self._pending_call, None
            if pending is not None and pending.entered >= self._user_started_at:
                turn.llm = pending  # the eager (speculative) reply, confirmed by this transcript
            lang = getattr(frame, "language", None)
            turn.language = str(getattr(lang, "value", lang)) if lang else None
            self.user_turns += 1
            self.owes_reply_since = now
            self.set_state("thinking")
            self._activity()
        elif isinstance(frame, LLMContextFrame):
            if self.turn is not None and self.turn.context_at is None and self.turn.transcript_at is not None:
                self.turn.context_at = now
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.generating = True
            if self.turn is None or self.turn.first_audio is not None or self.turn.responses:
                # A new mouth run after this turn already had one (a result, a progress line,
                # a follow-up to a tool-only reply) is its own turn, with its own stages.
                self._open("result")
            if self.turn.llm is None and self._pending_call is not None:
                self.turn.llm, self._pending_call = self._pending_call, None
            self._activity()
        elif isinstance(frame, LLMFullResponseEndFrame):
            self.generating = False
            self.last_response_end = now
            if self.turn is not None:
                self.turn.responses += 1
            if self.turn is not None and not "".join(self.turn.reply).strip() and not self.bot_speaking:
                self.owes_reply_since = None  # a turn that only called a tool: nothing to wait for
                if self.state == "thinking":
                    self.set_state(self.idle_state())  # stay_quiet, a hand, a hand-off: not "Thinking" forever
            self._activity()
        elif isinstance(frame, LLMTextFrame):
            if self.turn is not None:
                self.turn.reply.append(frame.text or "")
        elif isinstance(frame, TTSStartedFrame):
            if self.turn is not None and self.turn.tts_started is None:
                self.turn.tts_started = now
        elif isinstance(frame, TTSTextFrame):
            if self.turn is not None:
                self.turn.spoken.append((frame.text or "") + " ")
        elif isinstance(frame, FunctionCallInProgressFrame):
            call = getattr(frame, "tool_call_id", None)
            if self.turn is not None and call not in self._calls:
                self._calls.add(call)
                self.turn.tools.append(frame.function_name)
                if frame.function_name == "tell_yuki" and self.turn.handoff_at is None:
                    self.turn.handoff_at = now
                if frame.function_name != "stay_quiet":
                    self._took_part(now)
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            if self.turn is not None and self.turn.first_audio is None:
                self.turn.first_audio = now
            self._took_part(now)
            self.set_state("speaking")
            self._activity()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.bot_speaking = False
            self.last_bot_stop = now
            self.last_exchange = now
            self.owes_reply_since = None
            self.set_state(self.idle_state())
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


class _Hooks:
    """What the mouth's LLM service asks of the conversation around each call."""

    def __init__(self, conversation: "Conversation") -> None:
        self.c = conversation

    async def before_call(self, context: Any, call: LlmCall) -> tuple[str | None, str | None]:
        """Wait (bounded) for the grounding lookup and the desktop summary, both at once.

        Returns ``(lookup block text or None, desktop summary or None)``.
        """
        user_turn = _last_role(context) == "user"
        call.user_turn = user_turn
        hands = getattr(self.c.bridge, "hands", None) if self.c.settings.voice_hands else None
        desk_task = (asyncio.ensure_future(hands.for_call(self.c.settings.voice_desk_wait_ms))
                     if hands is not None else None)
        lookup_text = await self._lookup(call, user_turn)
        desk_text = None
        if desk_task is not None:
            snap, how = await desk_task
            call.desk = how if snap is not None else "none"
            if snap is not None:
                desk_text = snap.text
                call.desk_chars = len(snap.text)
                call.desk_ms = round(snap.ms if how == "fresh" else (time.monotonic() - snap.at) * 1000, 1)
                call.desk_tabs_ms = snap.tabs_ms
        return lookup_text, desk_text

    async def _lookup(self, call: LlmCall, user_turn: bool) -> str | None:
        grounder = self.c.grounder
        if grounder is None:
            call.grounding = "off"
            return None
        lookup, how = await grounder.for_call(self.c.settings.voice_grounding_wait_ms if user_turn else 0.0)
        if lookup is None or (user_turn and how == "previous"):
            call.grounding = "none"
            return None
        from yuki.voice.grounding import format_facts

        call.grounding, call.facts = how, len(lookup.facts)
        return format_facts(lookup.facts)

    def call_started(self, call: LlmCall) -> None:
        self.c.observer.call_started(call)

    def early_said(self, call: LlmCall, chars: int) -> None:
        """A call's say_first went to the voice while the call was being written (content-free log)."""
        self.c.log.event("early_say", chars=chars,
                         after_request_ms=round((call.early_say - call.sent) * 1000) if call.sent and call.early_say
                         else None)

    # -- the mouth's model failover (v3.5) ----------------------------------------

    def fallbacks(self) -> list[dict[str, Any]]:
        s = self.c.settings
        return [dict(t) for t in (getattr(s, "model_fallbacks", None) or {}).get(s.voice_mouth_model, [])
                if isinstance(t, dict) and t.get("model")]

    def sticky_index(self, size: int) -> int:
        index, until = getattr(self.c, "_failover_sticky", (0, 0.0))
        return index if 0 < index < size and time.monotonic() < until else 0

    def client_for(self, region: str | None) -> Any:
        region = region or self.c.settings.aws_region
        clients = self.c.__dict__.setdefault("_failover_clients", {})
        if region not in clients:
            clients[region] = bedrock_client(region)
        return clients[region]

    def model_problem(self, index: int, target: dict[str, Any] | None, exc: BaseException, kind: str) -> None:
        where = "primary" if target is None else f"{target.get('model')}@{target.get('region') or 'home'}"
        self.c.log.event("model_retry" if kind == "retry" and index == 0 else "model_failover", target=where,
                         kind=kind, status=getattr(exc, "status_code", None), error=type(exc).__name__)

    def model_ok(self, index: int, target: dict[str, Any] | None) -> None:
        if index:
            sticky_s = float(getattr(self.c.settings, "failover_sticky_s", 300.0))
            self.c._failover_sticky = (index, time.monotonic() + sticky_s)
            self.c.log.event("model_failover_ok", target=f"{target.get('model')}@{target.get('region') or 'home'}")

    def speculation(self, op: str, calls: int) -> None:
        """An eager reply's tool calls were held, released on the confirmed turn, or dropped."""
        self.c.log.event("speculation", op=op, calls=calls)

    def heard(self) -> str | None:
        """What recognition was unsure of in the latest user words, for a call answering them (else ``None``)."""
        call = getattr(self.c._llm, "_yuki_call", None) if getattr(self.c, "_llm", None) is not None else None
        if call is not None and not call.user_turn:
            return None
        return self.c.observer.heard

    def call(self) -> str | None:
        """The app of the voice call the user is in (call mode), else ``None``."""
        return self.c.call_app


def quiet_answered(messages: list[Any] | None) -> set[int]:
    """Ids of the user messages the mouth answered only with ``stay_quiet`` (in a call: talk to the others).

    Read from the context's own structure (which tool the next assistant message called), so the
    thinker is never handed those words as what the user said to Yuki. Plumbing, no words read.
    """
    out: set[int] = set()
    items = list(messages or [])
    for i, message in enumerate(items):
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        for nxt in items[i + 1:]:
            if not isinstance(nxt, dict) or nxt.get("role") == "user":
                break
            if nxt.get("role") == "assistant":
                calls = nxt.get("tool_calls") or []
                names = [(c.get("function") or {}).get("name") for c in calls if isinstance(c, dict)]
                if names and all(n == "stay_quiet" for n in names) and not nxt.get("content"):
                    out.add(id(message))
                break
    return out


def _last_role(context: Any) -> str | None:
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return None
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") in ("user", "assistant", "developer", "tool"):
            return str(message.get("role"))
    return None


class Conversation:
    """One conversation's pipeline, run on the current asyncio loop.

    Args:
        settings: Voice settings.
        bridge: :class:`~yuki.voice.bridge.VoiceBridge` (its handlers are the mouth's tools).
        transport: Input and output processors (:class:`~yuki.voice.transport.PairTransport`).
        memory: :class:`~yuki.agent.memory.MemoryAccess` for the turn log and the grounding
            lookup, or ``None``.
        mouth_memory: Rules and memory block, fetched before the loop started.
        log: The voice log.
        speech_end: Returns the monotonic time of the user's last voiced audio.
        on_state: ``(state, detail)`` callback (called on the voice thread).
        session: Id of this conversation, for the logs.
        voice_activity: ``(start of the current voiced stretch, end of the last voiced
            audio)`` from the microphone (monotonic), so results never start over the user
            before speech recognition notices them.
        call_app: The app of the voice call the user is in (call mode, docs/VOICE.md §25), or
            ``None``; :meth:`set_call` changes it while the conversation runs.
        on_closing: Called (on the voice loop) the moment the conversation decides to end, with
            the reason, before its teardown: the wake word listens again from then on.
    """

    def __init__(self, *, settings: Settings, bridge: Any, transport: Any, memory: Any, mouth_memory: MouthMemory,
                 log: Any, speech_end: Callable[[], float | None], on_state: Callable[[str, str], None],
                 session: str, extra_observers: list[Any] | None = None, tts_provider: str | None = None,
                 voice_activity: Callable[[], tuple[float, float]] | None = None, call_app: str | None = None,
                 on_closing: Callable[[str], None] | None = None, keyterms: list[str] | None = None,
                 on_chat: Callable[[dict[str, Any]], None] | None = None) -> None:
        self.settings = settings
        #: Names and terms from memory that speech recognition is told to expect (v3.5, docs/VOICE.md §9).
        self.keyterms = list(keyterms or [])
        self.call_app = call_app
        self._on_closing = on_closing
        #: When speech recognition was connected (the idle clocks start there, not while connecting).
        self.stt_ready_at: float | None = None
        #: Called once (on the voice loop) when speech recognition is connected (the session's timings).
        self.on_stt_connected: Callable[[], None] | None = None
        self._voice_activity = voice_activity
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
        self.gate: Any = None
        self.grounder: Any = None
        self.observer = TurnObserver(
            settings=settings, log=log, memory=memory, session=session, speech_end=speech_end,
            on_state=on_state, tts_provider=self.tts_provider, activity=self._on_activity,
            call_mode=lambda: bool(self.call_app),
        )
        self.observer.on_chat = on_chat
        self.observer.keyterms = keyterm_list(["Yuki", *settings.user_names], self.keyterms,
                                              settings.voice_keyterms_max)
        self.worker: Any = None
        self.started_at = time.monotonic()
        self._extra_observers = list(extra_observers or [])
        self.progress_cost_usd = 0.0

    def _on_activity(self) -> None:
        self._activity.set()
        if self.gate is not None:
            self.gate.poke()

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

    def set_call(self, app: str | None) -> None:
        """A call started (``app``) or ended (``None``) while the conversation runs. Thread-safe."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._set_call, app)
        else:
            self.call_app = app

    def _set_call(self, app: str | None) -> None:
        if bool(app) != bool(self.call_app):
            self.log.event("session", state="call_mode_on" if app else "call_mode_off", session=self.session)
        self.call_app = app
        self._activity.set()

    def _stt_connected(self) -> None:
        self.stt_ready_at = time.monotonic()
        self._activity.set()
        if self.on_stt_connected is not None:
            try:
                self.on_stt_connected()
            except Exception:  # noqa: BLE001 - timings only
                pass

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
        from pipecat.processors.aggregators import async_tool_messages
        from pipecat.services.anthropic.llm import AnthropicLLMService
        from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService
        from pipecat.transcriptions.language import Language

        s = self.settings
        # Pipecat's standing async-tool paragraph says a result must be added to whatever
        # reply is in progress and is "owed whatever the conversation moved on to"; here the
        # narrator gate decides when a result is said, and the user's rules ban em dashes.
        llm_service_module.ASYNC_TOOL_INSTRUCTIONS = async_tool_instructions()
        # Pipecat's final-result note says a result must be spoken whatever the
        # conversation moved on to. Here the narrator gate decides that, and a
        # stale result arrives marked held_back; the note defers to the payload.
        async_tool_messages._FINAL_DESCRIPTION = FINAL_DESCRIPTION
        hints = [Language(code) for code in s.voice_stt_languages] if s.voice_stt_model.endswith("multi") else None
        stt = DeepgramFluxSTTService(
            api_key=get_key(DEEPGRAM) or "",
            enable_eager_end_of_turn=s.voice_eager_eot_threshold is not None,
            settings=DeepgramFluxSTTService.Settings(
                model=s.voice_stt_model, language_hints=hints, eot_threshold=s.voice_eot_threshold,
                eager_eot_threshold=s.voice_eager_eot_threshold, eot_timeout_ms=s.voice_eot_timeout_ms,
                keyterm=keyterm_list(["Yuki", *s.user_names], self.keyterms, s.voice_keyterms_max),
            ),
        )
        obs = self.observer

        async def on_update(_service: Any, transcript: str) -> None:
            obs.on_update(transcript)

        async def on_eager(_service: Any, transcript: str) -> None:
            obs.on_eager(transcript)

        async def on_eot(_service: Any, transcript: str) -> None:
            obs.on_eot(transcript)

        stt.add_event_handler("on_update", on_update)
        stt.add_event_handler("on_eager_end_of_turn", on_eager)
        stt.add_event_handler("on_end_of_turn", on_eot)
        self._client = bedrock_client(s.aws_region)
        llm = _layered_service(AnthropicLLMService, self.mouth_memory, _Hooks(self))(
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
        llm.register_function("tell_yuki", self._tell_or_hold, cancel_on_interruption=False,
                              cancellable_by_llm=True, timeout_secs=None)
        llm.register_function("check_progress", self.bridge.check_progress)
        llm.register_function("more_of_result", self.bridge.more_of_result)
        llm.register_function("stay_quiet", self.bridge.stay_quiet)
        llm.register_function("end_conversation", self.bridge.end_conversation)
        hands = getattr(self.bridge, "hands", None)
        if hands is not None:
            hands.on_action = self.observer.hand_acted
            self.observer.hands = hands
            hands.on_step = lambda name, what, ok, report, ms: self.observer.chat({
                "type": "hand_step", "turn_id": self.observer.turn_id(), "name": name, "args": what, "ok": ok,
                "result": report, "ms": ms})
        if s.voice_hands and hands is not None:
            # The mouth's own one-step hands (§5.6). Not cancelled by a barge-in: the action
            # may already have happened, and its report must reach the context either way.
            for tool in HAND_TOOLS:
                llm.register_function(tool["name"], self._hand_or_hold, cancel_on_interruption=False,
                                      timeout_secs=30)
        provider = self.tts_provider
        voice = (s.voice_tts_voice if provider == s.voice_tts_provider else "") or DEFAULT_VOICES.get(provider, "")
        if provider == "deepgram":
            from pipecat.services.deepgram.tts import DeepgramTTSService

            tts = DeepgramTTSService(api_key=get_key(DEEPGRAM) or "",
                                     settings=DeepgramTTSService.Settings(voice=voice))
        else:
            from pipecat.services.elevenlabs.tts import ElevenLabsTTSService

            # Voice settings only when set (Settings / voice.json); unset, ElevenLabs uses the
            # voice's own defaults and speed 1.0. Flash v2.5 honours speed, stability,
            # similarity_boost and use_speaker_boost; ElevenLabs documents style for its v2 and
            # later models, so it is passed through when set and costs nothing when not.
            tuning = {name: value for name, value in (
                ("speed", s.voice_tts_speed), ("stability", s.voice_tts_stability),
                ("similarity_boost", s.voice_tts_similarity_boost), ("style", s.voice_tts_style),
                ("use_speaker_boost", s.voice_tts_speaker_boost)) if value is not None}
            tts = ElevenLabsTTSService(api_key=get_key(ELEVENLABS) or "",
                                       settings=ElevenLabsTTSService.Settings(voice=voice, model=s.voice_tts_model,
                                                                              **tuning))
        return stt, llm, tts

    def _build(self) -> Any:
        from pipecat.adapters.schemas.function_schema import FunctionSchema
        from pipecat.adapters.schemas.tools_schema import ToolsSchema
        from pipecat.pipeline.pipeline import Pipeline
        from pipecat.pipeline.worker import PipelineParams, PipelineWorker
        from pipecat.processors.aggregators.llm_context import LLMContext
        from pipecat.processors.aggregators.llm_response_universal import (
            LLMAssistantAggregatorParams, LLMContextAggregatorPair, LLMUserAggregatorParams,
        )
        from pipecat.utils.context.llm_context_summarization import LLMAutoContextSummarizationConfig

        stt, llm, tts = self._services()
        self._llm = llm
        # Whether the mouth's reply now making a tool call said anything aloud (a stream fact).
        self.bridge.spoke_in_reply = lambda: bool(getattr(llm, "_yuki_call", None) is not None
                                                  and llm._yuki_call.first_text is not None)
        # The say_first words spoken for a silent hand-over belong before its call, too.
        self.bridge.mark_spoken = lambda call_id: llm._yuki_spoke.add(str(call_id))
        self.bridge.said_early = lambda call_id: str(call_id) in llm._yuki_early
        self._llm_context: Any = None
        # Whether the mouth run now calling a tool was the user's turn (not a result's).
        self.bridge.user_turn = lambda: bool(getattr(llm, "_yuki_call", None) is None or llm._yuki_call.user_turn)
        self.bridge.call_mode = lambda: bool(self.call_app)
        hand_tools = HAND_TOOLS if (self.settings.voice_hands and getattr(self.bridge, "hands", None)) else []
        tools = ToolsSchema(standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["properties"],
                           required=t["required"])
            for t in (TELL_YUKI, CHECK_PROGRESS, MORE_OF_RESULT, STAY_QUIET, END_CONVERSATION, *hand_tools)
        ])
        self._tools = tools
        context = LLMContext(tools=tools)
        self._llm_context = context
        # Flux's own turn strategies from the start. Left to default, the aggregator first
        # builds Pipecat's local ones, which load the Smart Turn ONNX model and onnxruntime
        # (measured 2026-09-25: imported in every conversation, never used), before Flux's
        # recommendation replaces them.
        strategies = stt.recommended_user_turn_strategies(enable_interruptions=True)
        if self.settings.voice_eager_eot_threshold is not None:
            from pipecat.turns.user_stop.eager_user_turn_stop_strategy import EagerUserTurnStopStrategy

            eager = [x for x in strategies.stop if isinstance(x, EagerUserTurnStopStrategy)]
            if eager:
                strategies.stop = [one_stop_per_turn(EagerUserTurnStopStrategy)(
                    match_policy=eager[0].match_policy, speculation_timeout=eager[0]._speculation_timeout)]
        user_agg, assistant_agg = LLMContextAggregatorPair(
            context,
            user_params=LLMUserAggregatorParams(user_turn_strategies=strategies),
            # Summaries by size only: Pipecat's default also summarises every 20 messages, and
            # each hand or hand-off adds three, so a 4-minute live conversation (2026-09-25) was
            # summarised twice by the mouth's model, losing what was done.
            assistant_params=LLMAssistantAggregatorParams(
                enable_auto_context_summarization=True,
                auto_context_summarization_config=LLMAutoContextSummarizationConfig(
                    max_context_tokens=8000, max_unsummarized_messages=None)),
        )
        settle_only_when_newest(assistant_agg)
        summarizer = getattr(assistant_agg, "_summarizer", None)
        # The summary is a user-role message nobody said: never "what the user said" to the thinker;
        # nor, in a call, what the mouth judged was said to the people on it (it stayed quiet).
        self.bridge.not_heard = lambda message: (message is getattr(summarizer, "_summary_message", None)
                                                 or id(message) in quiet_answered(context.get_messages()))
        hold = hold_until_connected(lambda: bool(getattr(stt, "_transport_is_active", lambda: True)()), self.log,
                                    on_connected=self._stt_connected)
        output = self.transport.output()
        self.observer.output_processor = output
        pipeline = Pipeline([self.transport.input(), hold, stt, user_agg, llm, tts, output, assistant_agg])
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

    #: Raw sound counts as the user talking only this soon after it (s), and not when a
    #: stretch of it has gone on this long without speech recognition hearing a turn (noise).
    SOUND_RECENT_S = 0.35
    SOUND_MAX_STRETCH_S = 6.0

    def _gate_state(self) -> dict[str, Any]:
        state = self.observer.live_state()
        state["busy_tools"] = self.bridge.busy_tools
        state["call_mode"] = bool(self.call_app)  # the user is in a voice call (the attention gate; v3.5)
        state["jobs_open"] = self.bridge.open_voice_jobs()
        if self._voice_activity is not None:
            since, last = self._voice_activity()
            now = time.monotonic()
            state["user_sound"] = bool(last and now - last < self.SOUND_RECENT_S
                                       and (last - since) < self.SOUND_MAX_STRETCH_S)
        return state

    async def _progress_answer(self, log_text: str, question: str) -> tuple[str | None, dict[str, Any]]:
        from yuki.voice.progress import answer_progress

        text, facts = await answer_progress(self._client, self.settings.voice_mouth_model, log_text, question)
        cost = self.settings.estimate_cost(self.settings.voice_mouth_model, {
            "input_tokens": facts.get("input_tokens") or 0, "output_tokens": facts.get("output_tokens") or 0,
            "cache_read_tokens": 0, "cache_write_tokens": 0})
        self.progress_cost_usd += cost or 0.0
        facts["cost_usd"] = round(cost or 0.0, 6)
        return text, facts

    async def run(self) -> str:
        """Run until idle, stopped or failed. Returns why it ended."""
        from pipecat.workers.runner import WorkerRunner

        from yuki.voice.narrator import NarratorGate

        quiet_pipecat_logging(self.settings.voice_log_dir)
        import threading

        s = self.settings
        self._loop = asyncio.get_running_loop()
        self.gate = NarratorGate(self._gate_state, settle_s=s.voice_settle_s, settle_max_s=s.voice_settle_max_s,
                                 cooldown_s=s.voice_cooldown_s, stale_turns=s.voice_stale_turns, log=self.log,
                                 final_gap_s=s.voice_final_gap_s, progress_pause_s=s.voice_progress_pause_s)
        if s.voice_grounding and self.memory is not None:
            from yuki.voice.grounding import Grounder

            self.grounder = Grounder(self.memory, facts=s.voice_grounding_facts, log=self.log)
            self.grounder.warm()
            self.observer.grounder = self.grounder
        self.bridge.attach(self._loop)
        self.bridge.on_jobs_changed = self._activity.set
        self.observer.idle_state = lambda: "working" if self.bridge.open_voice_jobs() else "listening"
        self.bridge.announce = self._announce
        self.bridge.gate = self.gate
        self.bridge.progress_answerer = self._progress_answer
        self.bridge.on_remembered = self._refresh_rules
        self.bridge.end_requested = lambda: asyncio.ensure_future(self._end_after_speech())
        runner = WorkerRunner(handle_sigint=False, check_dangling_tasks=False)
        worker = self._build()
        # While Deepgram connects (2-3 s), off the loop; done long before the first reply. Started
        # after the services are built: they import anthropic, and importing it on two threads at
        # once raised an import deadlock (a stand-in-brain run, where nothing had imported it yet).
        threading.Thread(target=_preimport, name="yuki-voice-preimport", daemon=True).start()
        await runner.add_workers(worker)
        self.observer.set_state("listening")
        self.gate.start()
        warm = asyncio.create_task(self._warm())
        watch = asyncio.create_task(self._watch(runner))
        lag = asyncio.create_task(self._lag_monitor())
        try:
            await runner.run()
        finally:
            watch.cancel()
            warm.cancel()
            lag.cancel()
            await self.gate.stop()
            self.observer.close()
            self.bridge.closing = True
            self.bridge.attach(None)
            self.bridge.announce = None
            if self.grounder is not None:
                unloaded = self.grounder.release(busy=self.bridge.brain_busy())
                timings = sorted(self.grounder.timings)
                self.log.event("grounding", op="summary", lookups=len(timings), unloaded=unloaded,
                               p50_ms=round(timings[len(timings) // 2], 1) if timings else None,
                               max_ms=round(timings[-1], 1) if timings else None)
            self.log.event("narrator", op="summary", **self.gate.stats)
        if self.observer.fatal and self.reason == "running":
            self.reason = "error"
        return self.reason

    async def _lag_monitor(self, every_s: float = 0.05, report_ms: float = 100.0) -> None:
        """How late the voice loop wakes up (a blocked loop delays every stage). Logs stalls and a summary."""
        lags: list[float] = []
        import threading

        state = {"tick": time.monotonic(), "stop": False, "stacks": []}
        loop_thread = threading.get_ident()
        watchdog = threading.Thread(target=self._stall_watch, args=(state, loop_thread), name="yuki-voice-watch",
                                    daemon=True)
        watchdog.start()
        try:
            while True:
                started = time.monotonic()
                await asyncio.sleep(every_s)
                state["tick"] = time.monotonic()
                late = (state["tick"] - started - every_s) * 1000
                lags.append(late)
                if late >= report_ms:
                    stacks, state["stacks"] = state["stacks"], []
                    self.log.event("loop_lag", ms=round(late), **({"where": stacks} if stacks else {}))
        except asyncio.CancelledError:
            state["stop"] = True
            if lags:
                ordered = sorted(lags)
                self.log.event("loop_lag", op="summary", n=len(ordered), p50_ms=round(ordered[len(ordered) // 2], 1),
                               p99_ms=round(ordered[int(len(ordered) * 0.99)], 1), max_ms=round(ordered[-1], 1))
            raise

    @staticmethod
    def _stall_watch(state: dict[str, Any], loop_thread: int, every_s: float = 0.1, stall_s: float = 0.3) -> None:
        """While the voice loop is stalled, note where it is and what the other threads run.

        One sample per stall, 0.3 s into it: the loop thread's innermost frames and the
        innermost frame of every other thread (file, line, function and thread name
        only: content-free). Logged with the ``loop_lag`` event that ends the stall
        (measured 2026-09-25: a 3 s stall before the first live reply had no explanation).
        """
        import os
        import sys
        import threading

        sampled_for = None
        while not state["stop"]:
            time.sleep(every_s)
            tick = state["tick"]
            if time.monotonic() - tick < stall_s or sampled_for == tick:
                continue
            sampled_for = tick
            names = {t.ident: t.name for t in threading.enumerate()}
            frames = sys._current_frames()

            def where(frame: Any, depth: int) -> list[str]:
                out = []
                while frame is not None and len(out) < depth:
                    code = frame.f_code
                    out.append(f"{os.path.basename(code.co_filename)}:{frame.f_lineno}:{code.co_name}")
                    frame = frame.f_back
                return out

            sample = {"loop": where(frames.get(loop_thread), 14)}
            others = {}
            for ident, frame in frames.items():
                if ident in (loop_thread, threading.get_ident()):
                    continue
                top = where(frame, 1)
                if top and len(others) < 16:
                    others[names.get(ident, str(ident))] = top[0]
            sample["threads"] = others
            state["stacks"] = [sample]

    async def _warm(self) -> None:
        """Open the Bedrock connection and write the mouth's prompt cache while Deepgram connects.

        Each conversation has a fresh client (it belongs to this event loop), so
        without this the first reply pays the TLS handshake. The one-token request
        carries the mouth's real system layers and tools, so it also writes their
        cache: the first turn then reads it instead (measured 2026-09-25, 4 pairs:
        first-turn time to first token median 1,031 ms vs 1,186 ms, max 1,090 vs
        2,715 ms; the cache write is the one the first turn would have made). Logged
        as ``warm`` with the tokens it wrote.
        """
        started = time.monotonic()
        request: dict[str, Any] = {"messages": [{"role": "user", "content": "."}]}
        try:
            from pipecat.processors.aggregators.llm_context import LLMContext

            llm = self._llm
            # Pipecat settles its built-in cancel tool and the composed instruction on the
            # first context frame; do it now, or this prefix differs from the real calls'
            # (measured: the first turn then missed the cache and wrote it again).
            llm._sync_cancel_tools()
            llm._compose_system_instruction()
            params = llm._get_llm_invocation_params(LLMContext(messages=[{"role": "user", "content": "."}],
                                                               tools=self._tools))
            request = {"system": params["system"], "tools": params["tools"], "messages": params["messages"]}
        except Exception as exc:  # noqa: BLE001 - then only the connection is warmed
            self.log.event("warm", note=f"plain warm-up ({type(exc).__name__})")
        try:
            response = await self._client.messages.create(model=self.settings.voice_mouth_model, max_tokens=1,
                                                          **request)
            usage = getattr(response, "usage", None)
            self.log.event("warm", ms=round((time.monotonic() - started) * 1000),
                           cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
                           cache_read_tokens=getattr(usage, "cache_read_input_tokens", None))
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            self.log.event("warm", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def _held(self, name: str) -> bool:
        """Whether this call is held: recognition was unsure of the latest words and the mouth spoke with it."""
        from yuki.voice.hands import AUDIO_HANDS

        call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
        spoke = call is not None and call.first_text is not None and call.user_turn
        return bool(spoke and self.observer.heard and name not in AUDIO_HANDS)

    async def _hold(self, params: Any) -> None:
        self.log.event("hands" if params.function_name != "tell_yuki" else "bridge", op=params.function_name,
                       held="asked_first")
        await params.result_callback({"held": (
            "Not done yet: speech recognition was unsure of their words and you spoke with this call. If your "
            "words asked them something, call stay_quiet now and wait for their answer, then call it again. If "
            "they asked nothing, call it again now.")})

    async def _tell_or_hold(self, params: Any) -> None:
        """tell_yuki, held like a hand when the mouth asked and handed over in one reply on doubtful words."""
        if self._held("tell_yuki"):
            await self._hold(params)
            return
        await self.bridge.tell_yuki(params)

    async def _hand_or_hold(self, params: Any) -> None:
        """A hand, unless recognition was unsure of the user's words and the mouth spoke with the call.

        Measured 2026-09-27 (clarify replay, 4 of 6 runs): "close the cloud tabs" got "The Claude tabs, three
        and four?" and the close in the same reply, before the answer. A window hand called with words while
        the latest words were doubtful (the Speech line) is held, not run (hand-offs too): its report tells
        the mouth to wait for the answer if it asked (stay_quiet), else to call it again, so nothing is
        lost. Sound hands touch no window and run. State only (the note, the stream, which hand), never what was said.
        """
        if self._held(params.function_name):
            await self._hold(params)
            return
        await self.bridge.hands.handle(params)

    async def _end_after_speech(self, max_s: float = 6.0) -> None:
        """The user asked to end the conversation: let the goodbye play, then end it (the wake word stays on)."""
        started = time.monotonic()
        heard = False
        while time.monotonic() - started < max_s:
            obs = self.observer
            heard = heard or obs.bot_speaking
            if heard and not obs.bot_speaking:
                break
            if not heard and time.monotonic() - started > 2.0:
                break  # no goodbye was spoken
            await asyncio.sleep(0.05)
        self.log.event("session", state="end_asked", session=self.session, waited_ms=round(
            (time.monotonic() - started) * 1000))
        self._set_stop("user_asked")

    async def _refresh_rules(self) -> None:
        """Re-read the user's standing rules after a spoken rule was saved (off the loop, bounded).

        The mouth's rules layer was read when the conversation started; a rule the user
        just gave or withdrew by voice must hold from the next reply, not the next
        conversation. One cache write of the rules layer; logged as ``rules_refreshed``.
        """
        if self.memory is None:
            return
        try:
            text = await asyncio.to_thread(self.memory.standing, timeout_s=3.0)
        except Exception as exc:  # noqa: BLE001 - the mouth keeps the rules it has
            self.log.event("rules_refreshed", error=f"{type(exc).__name__}: {str(exc)[:160]}")
            return
        changed = (text or None) != (self.mouth_memory.standing or None)
        if text is not None:
            self.mouth_memory.standing = text
        self.log.event("rules_refreshed", changed=changed, chars=len(text or ""))

    async def _announce(self, payload: dict[str, Any], run_llm: bool = True) -> None:
        """A result with no open tool call (a correction that came too late): into the context, said if ``run_llm``."""
        import json

        from pipecat.frames.frames import LLMMessagesAppendFrame

        if self.worker is not None:
            message = {"role": "developer", "content": json.dumps({"late_result": payload}, ensure_ascii=False)}
            await self.worker.queue_frame(LLMMessagesAppendFrame(messages=[message], run_llm=run_llm))

    #: In call mode, a user still talking when the call idle runs out gets this much longer
    #: (a sentence to Yuki that started just before the end is not cut off).
    CALL_SPEECH_GRACE_S = 4.0
    #: A mouth reply that ended this recently may still be starting its tool call (Pipecat runs
    #: the calls after the stream ends): the conversation does not end in between (measured: a
    #: call-mode run ended 0.4 s before the "turn it up" hand it had just decided on).
    CALL_TOOL_START_S = 1.0

    def idle_limit(self) -> float:
        """Quiet seconds that end a conversation: longer the more the user has been talking with Yuki."""
        s = self.settings
        extra = s.voice_idle_per_exchange_s * max(0, self.observer.exchanges - 1)
        return max(s.voice_idle_s, min(s.voice_idle_max_s, s.voice_idle_s + extra))

    async def _watch(self, runner: Any) -> None:
        """End the conversation when it has been quiet long enough, or on stop / fatal error.

        Normal mode: after :meth:`idle_limit` of nobody speaking (longer while a task runs, up
        to ``voice_hold_max_s``). Call mode: ``voice_call_idle_s`` after the last exchange with
        Yuki, however much the user goes on talking to the people on the call.
        """
        s = self.settings
        last = time.monotonic()
        while True:
            obs = self.observer
            now = time.monotonic()
            owed = self.bridge.open_voice_jobs() + (self.gate.holding if self.gate is not None else 0)
            if self.call_app:
                busy = (obs.bot_speaking or obs.generating or obs.owes_reply
                        or now - obs.last_response_end < self.CALL_TOOL_START_S)
                since = max(x for x in (self.stt_ready_at, obs.last_exchange, self.started_at) if x is not None)
                limit = s.voice_hold_max_s if owed else s.voice_call_idle_s
                if obs.user_speaking and not owed:
                    limit += self.CALL_SPEECH_GRACE_S
                remaining = limit - (now - since)
                wait = 1.0 if busy else max(0.05, remaining)  # owes_reply expires by time, not by an event
            else:
                busy = obs.user_speaking or obs.bot_speaking or obs.generating
                limit = s.voice_hold_max_s if owed else self.idle_limit()
                remaining = limit - (now - last)
                wait = None if busy else max(0.05, remaining)
            if not busy and not obs.owes_reply and obs.state in ("thinking", "working", "listening"):
                # The pill says "Working on it" only while a job really runs (queued or running); a
                # result folded in silently, a cancelled job or a stay_quiet leaves it "Listening".
                obs.set_state(obs.idle_state())
            if self._stop.is_set() or obs.fatal:
                break
            if not busy and remaining <= 0:
                self._set_stop("held_too_long" if owed else ("call_idle" if self.call_app else "idle"))
                break
            self._activity.clear()
            try:
                await asyncio.wait_for(self._activity.wait(), timeout=wait)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            last = time.monotonic()
            if self._stop.is_set():
                break
        if self._on_closing is not None:
            try:
                self._on_closing(self.reason)
            except Exception:  # noqa: BLE001 - only a hint to the session
                pass
        self.bridge.closing = True
        await runner.cancel()


def _preimport() -> None:
    """Import what the first turn would otherwise import on the voice loop, before anything is live.

    anyio builds its lazy-import maps by reading and parsing its own source when a
    module of it is first imported; that happened on the voice loop during the first
    user turn (measured offline 2026-09-25: a 669 ms stall inside ``inspect.getsource``;
    live, a 2,964 ms stall right before the first reply). Tens of milliseconds here.

    The bigger one: the Bedrock stream decoder loads botocore's ``bedrock-runtime``
    service model (a large JSON) the first time a streamed reply is parsed, and it did
    that on the voice loop inside the first reply (measured: a 403 ms stall offline in
    ``botocore.loaders``; live, llm_open 2,433 ms on the first turn only). It is cached
    process-wide, so loading it here once costs the first turn nothing.
    """
    import importlib

    for name in ("anyio.abc", "anyio._backends._asyncio", "anyio.from_thread", "anyio.to_thread",
                 "anyio.streams.memory", "botocore.parsers", "botocore.eventstream"):
        try:
            importlib.import_module(name)
        except Exception:  # noqa: BLE001 - only an optimisation
            pass
    try:
        from anthropic.lib.bedrock._stream_decoder import get_response_stream_shape

        get_response_stream_shape()
    except Exception:  # noqa: BLE001
        pass


def async_tool_instructions() -> str:
    """The standing async-tool paragraph composed into the mouth's prompt (replaces Pipecat's)."""
    return (
        "ASYNC TOOLS:\n"
        "tell_yuki keeps running after you have replied. What it sends back (progress, questions, "
        "the result) arrives as messages in the conversation when it is a good moment to say it: "
        "after the user has finished and you have answered them. Say each thing once, in its own "
        "fields' terms, and never repeat it in a later reply; a later \"okay\" or \"thanks\" is not "
        "a cue to say it again. Your hands report back the same way, as soon as they are done."
    )


#: Replaces Pipecat's final-result note (see ``Conversation._services``).
FINAL_DESCRIPTION = (
    "This is the final result for the asynchronous task associated with this tool_call_id. The task "
    "has completed and no further results will arrive for it. Handle it as its own fields say: a "
    "result or failed is to be said now, briefly; a held_back one is kept for reference and not "
    "brought up. Convey it once; do not repeat it in later responses."
)
