"""One voice conversation: the Pipecat pipeline, its measurements and its end (docs/VOICE.md §4, §9, §13, §14).

``audio in -> Deepgram Flux (transcript + end of turn) -> user aggregator ->
mouth (Haiku on Bedrock; async tell_yuki, check_progress, more_of_result) ->
TTS (Gemini, voice Leda; or ElevenLabs / Deepgram Aura) -> audio out -> assistant aggregator``

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
from collections import deque
from pathlib import Path
import os
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable

from yuki.config import Settings
from yuki.voice.expressive import strip_tags
from yuki.voice.keys import DEEPGRAM, ELEVENLABS, GOOGLE, get_key
from yuki.voice.hands import HAND_TOOLS
from yuki.voice.recall import QUICK_MEMORY
from yuki.voice.mouth import (
    CHECK_PROGRESS, END_CONVERSATION, GO_QUIET, MORE_OF_RESULT, MOUTH_PROMPT, STAY_QUIET, TELL_YUKI, MouthMemory,
    ReplyChannel, mouth_system_blocks, volatile_block, with_markers,
)

#: The speech channels (``Settings.voice_speech_channel``, v3.16).
CHANNELS = ("guard", "marker")


def speech_channel(settings: Any) -> str:
    """The speech channel in force: ``"marker"`` or ``"guard"`` (anything else is the guard, as before v3.16)."""
    value = str(getattr(settings, "voice_speech_channel", "guard") or "guard").strip().lower()
    return value if value in CHANNELS else "guard"


def _with_text(event: Any, text: str) -> Any:
    """A text delta event of the talker's stream carrying ``text`` instead (the marker channel's speech, v3.16)."""
    delta = getattr(event, "delta", None)
    try:
        return event.model_copy(update={"delta": delta.model_copy(update={"text": text})})
    except Exception:  # noqa: BLE001 - a stand-in event (tests): the same shape, plain
        from types import SimpleNamespace

        return SimpleNamespace(type=getattr(event, "type", "content_block_delta"), index=getattr(event, "index", 0),
                               delta=SimpleNamespace(type="text_delta", text=text))

#: Default voices per provider when ``Settings.voice_tts_voice`` is empty.
DEFAULT_VOICES = {"elevenlabs": "cgSgspJ2msm6clMCkdW9", "deepgram": "aura-2-thalia-en"}

#: Yuki's own name, the wake word: a speech-recognition keyterm, never a name the user may be misheard saying.
WAKE_NAME = "Yuki"

#: The voice states the UI shows.
STATES = ("starting", "listening", "hearing", "thinking", "speaking", "working", "error", "off")

#: The pill's words when a reply had words to say and none of them could be played (v3.16: never the service's raw
#: text, and never for a reply that had nothing to say, 04 §4.4).
SILENT_REPLY_NOTE = "Couldn't say that one out loud"
#: The pill's words while a fatal service error ends the conversation (the session's notice then says why).
FATAL_NOTE = "Voice ran into a problem"


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


#: The one TLS context of every conversation's connections (speech recognition, TTS, the mouth's client).
_TLS: dict[str, Any] = {}
_TLS_LOCK = __import__("threading").Lock()


def shared_ssl_context() -> Any:
    """The TLS context voice connects with: built once per process, off the voice loop (v3.6).

    Live 2026-09-27 (04:46:41) a conversation's start stalled its event loop for 2,757 ms inside
    ``ssl.create_default_context`` (reading the Windows certificate store): websockets builds a new
    context for every ``wss://`` connection, on the loop, so the TTS connection held up speech
    recognition's. One context, built by the prewarm or the import thread, is shared by every
    connection (an ``SSLContext`` is safe to share).
    """
    with _TLS_LOCK:
        context = _TLS.get("context")
        if context is None:
            import ssl

            context = _TLS["context"] = ssl.create_default_context()
        return context


def use_shared_tls() -> None:
    """Make Pipecat's websocket services connect with :func:`shared_ssl_context` (once per process).

    Pipecat's ``WebsocketService._websocket_connect`` calls ``websockets``' ``connect`` without an
    ``ssl`` argument, so each ``wss://`` connection built its own context on the loop. Here a secure
    URI gets the shared one; an explicit ``ssl`` is left alone. Plumbing: how a socket is opened.
    """
    import pipecat.services.websocket_service as ws

    if getattr(ws, "_yuki_shared_tls", False):
        return
    original = ws.websocket_connect

    def connect(uri: Any, *args: Any, **kwargs: Any) -> Any:
        if str(uri).startswith("wss:") and "ssl" not in kwargs:
            kwargs["ssl"] = shared_ssl_context()
        return original(uri, *args, **kwargs)

    ws.websocket_connect = connect
    ws._yuki_shared_tls = True


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
    if cached and not refresh:
        # Stale: answer with what was read last and read it again in the background, so a conversation's
        # start never waits for it (live 2026-09-27 04:45 the check took 2.1 s of the start: the read of
        # the app's prewarm was older than QUOTA_TRUST_S).
        import threading

        threading.Thread(target=lambda: elevenlabs_quota(refresh=True), name="yuki-voice-quota",
                         daemon=True).start()
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
    if settings.voice_tts_provider == "gemini":
        from yuki.voice.gemini_tts import gemini_status

        ok, reason = gemini_status(get_key(GOOGLE), settings.voice_gemini_model)
        if not ok:
            if settings.voice_tts_fallback:
                facts["tts_provider"] = settings.voice_tts_fallback
                facts["tts_note"] = f"The Gemini voice can't speak ({reason}); using the {settings.voice_tts_fallback} voice"
            else:
                raise VoiceUnavailable(f"Voice can't speak: Gemini {reason}.")
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
    grounding_chars: int = 0            # the memory text attached (v3.14: the related block's size)
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
    route: str | None = None            # E3a (v3.17): the router's decision for this turn (thinker / talker / None)
    route_ms: float | None = None       # and how long it took from its request
    # v3.18: practice memory's lines in the volatile block (yuki.voice.practice): which lookup, its wait, and what the
    # block held (kinds and ids, size); the text itself only goes into the request
    practice: str = "off"               # final | partial | previous | none | off
    practice_wait_ms: float | None = None
    practice_block: str | None = None   # content: the request only, never a log
    talker_block: str = ""              # "pitfall:7;habit:9;skill:12"
    talker_block_chars: int = 0


#: Tools whose spoken acknowledgement travels in the call itself (``say_first``), said as soon as it is written.
SAY_FIRST_TOOLS = frozenset({"tell_yuki", "open_url", "open_app", "end_conversation", "run_skill"})


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


def early_flag(partial_json: str, name: str) -> bool | None:
    """A boolean field of a tool call whose arguments are still streaming, once its value is written.

    Parsing the call's own JSON (v3.9: ``screen``, the first field of ``tell_yuki``, decides whether the early
    thinker start gets the window read). ``None`` until ``true`` or ``false`` is there.
    """
    key = partial_json.find(f'"{name}"')
    if key < 0:
        return None
    colon = partial_json.find(":", key + len(name) + 2)
    if colon < 0:
        return None
    rest = partial_json[colon + 1:].lstrip(" \t\r\n")
    if rest.startswith("true"):
        return True
    if rest.startswith("false"):
        return False
    return None


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
#: misheard contacts' names 0.40-0.74 (the cases are in docs/, not in code); words of clear commands mostly
#: >= 0.9, a few 0.64-0.86).
LOW_WORD_CONFIDENCE = 0.7
#: ... but only for words of at least this many letters; a shorter word only below :data:`VERY_LOW_CONFIDENCE`.
#: Measured 2026-09-27 (v3.7) on 100 clear utterances through flux-general-multi (six Aura voices): below 0.7,
#: 12 of 100 turns were flagged, mostly on short words said right ("can", "the", "pass", "What", "made"), and
#: each flag cost the reply ~2 s (its words held, a second mouth run). With the length rule 7 of 100, and 6 of
#: those 7 were real mistranscriptions ("You can me OTP" for "you key in the OTP", "Soaki", "picoris"); the
#: misheard names of v3.5 are all 5+ letters.
LOW_WORD_MIN_LETTERS = 5
VERY_LOW_CONFIDENCE = 0.4
#: A heard word (or two adjacent ones) this similar to a keyterm, without being it, is named as sounding close
#: (measured: three mishearings of one contact's name 0.83-0.92 and the same name heard as two English words 0.67
#: against it, another contact's 0.83, "cloud" 0.73 against Claude; the words of clear commands <= 0.5 against every
#: name). A similarity measure, no word list.
NEAR_TERM_RATIO = 0.65


def heard_note(words: Any, keyterms: list[str], *, cut_before: bool = False,
               screen: list[str] | None = None) -> tuple[str | None, int, int]:
    """What speech recognition was unsure of in a final transcript, for the mouth: ``(note, low, near)``.

    ``words`` is Flux's TurnInfo word list (``{"word", "confidence"}``); ``keyterms`` the names memory
    holds. The note names the words below :data:`LOW_WORD_CONFIDENCE` and the heard words that sound
    close to a keyterm without being it; ``None`` when there is nothing to say. Measured signals only:
    the mouth decides from them and the meaning whether to ask back.

    ``cut_before``: the audio was cut right before these words (the first words after the wake word,
    whose name was kept out, v3.6). The first word's confidence then says more about the cut than about
    the word (measured 2026-09-27: "pause" 0.52-0.66 in "Yuki, pause the music." cut there, 0.99-1.00
    alone), so it is not named as unsure; a near-name is still named.

    ``screen``: the names on the screen in front (v3.10: its chats, tabs, links): a heard word close to one of
    them is named with it, and an unsure word that begins with a known name ("brochure" for "Bro chat", live
    2026-09-28) is named as that name run together with the next word. String measures, no word list.

    Letters said one by one (v3.13: "Open w a r p." heard as five words, answered "Was that Hindi?") are one word
    spelled out: a run of two or more one-letter words is joined (WARP) and named with the keyterm or screen name
    it is, or begins, or is closest to. Measured on the words' own lengths, no word list.
    """
    import difflib

    items = [w for w in (words or []) if isinstance(w, dict) and str(w.get("word") or "").strip()]
    rated = items[1:] if cut_before else items

    def key(text: str) -> str:
        return "".join(ch for ch in text.casefold() if ch.isalnum())

    def unsure(w: dict) -> bool:
        conf = w.get("confidence")
        if not isinstance(conf, (int, float)):
            return False
        long_enough = len(key(str(w["word"]))) >= LOW_WORD_MIN_LETTERS
        return float(conf) < (LOW_WORD_CONFIDENCE if long_enough else VERY_LOW_CONFIDENCE)

    low = [(str(w["word"]).strip(" ,.?!"), float(w["confidence"])) for w in rated if unsure(w)]
    terms = {key(t): t for t in keyterms if len(key(t)) >= 4}
    on_screen = {key(t): t for t in (screen or []) if len(key(t)) >= 3}
    terms.update({k: t for k, t in on_screen.items() if k not in terms})
    # An unsure word that begins with a known name of three letters or more is likely that name said together
    # with the next word ("brochure" for "Bro chat"): the name is kept at its start, the rest was merged.
    fused = []
    for word, _conf in low:
        k = key(word)
        best = max((t for t in {**{key(x): x for x in keyterms if len(key(x)) >= 3}, **on_screen}
                    if len(t) >= 3 and k.startswith(t) and len(k) > len(t)), key=len, default=None)
        if best is not None:
            fused.append((word, {**{key(x): x for x in keyterms}, **on_screen}.get(best, best)))
    near: list[tuple[str, str]] = []
    spoken = [str(w["word"]).strip(" ,.?!") for w in items]
    # Words that are a known name, alone or joined with the next ("eleven labs"), are not near misses.
    exact = {i for i, w in enumerate(spoken) if key(w) in terms}
    exact |= {j for i in range(len(spoken) - 1) if key(spoken[i] + spoken[i + 1]) in terms for j in (i, i + 1)}
    def closest(k: str) -> str | None:
        # The same first sound and most of the letters: misheard names keep their start ("tar niche" for
        # Tarnesh, "cloud" for Claude); a word that merely contains a name's letters does not
        # ("after" in "Father"). Measures of the two strings, no word list.
        best = max((t for t in terms if t[:1] == k[:1] and k not in t and t not in k),
                   key=lambda t: difflib.SequenceMatcher(None, k, t).ratio(), default=None)
        return best if best and difflib.SequenceMatcher(None, k, best).ratio() >= NEAR_TERM_RATIO else None

    # N9 (report 47): single words and pairs are both measured, and every near name is named, closest first:
    # recognition splits a name across two words ("hey meant" for Hemanth), and live a single word of the pair
    # ("niche") was named with another contact whose handle looks like it, which kept the pair from being measured.
    scored: list[tuple[float, str, str]] = []
    for size in (1, 2):
        for i in range(len(spoken) - size + 1):
            window = spoken[i:i + size]
            k = key(" ".join(window))
            if (len(k) < 5 or k in terms or any(j in exact for j in range(i, i + size))
                    or (size == 2 and any(len(key(w)) < 3 for w in window))):
                continue
            best = closest(k)
            if best and all(t != terms[best] for _r, _h, t in scored):
                scored.append((difflib.SequenceMatcher(None, k, best).ratio(), " ".join(window), terms[best]))
    near = [(h, t) for _r, h, t in sorted(scored, key=lambda s: -s[0])]
    spelled: list[tuple[str, str, str | None]] = []
    run: list[str] = []
    for w in [*spoken, ""]:
        if len(w) == 1 and w.isalpha():
            run.append(w)
            continue
        if len(run) >= 2:
            joined = "".join(run)
            k = key(joined)
            names = {key(t): t for t in [*keyterms, *(screen or [])] if len(key(t)) >= 3}
            match = names.get(k)
            if match is None:
                # A name it begins ("l l m o s" and "LLM OS agent harness revamp"), else the closest one.
                ratio = lambda t: difflib.SequenceMatcher(None, k, t[:len(k)] if len(t) > len(k) else t).ratio()
                best = max(names, key=ratio, default=None)
                if best is not None and ratio(best) >= NEAR_TERM_RATIO:
                    match = names[best]
            spelled.append((" ".join(run), joined.upper(), match))
        run = []
    parts = []
    if spelled:
        parts.append("; ".join(f"'{h}' is a word spelled out letter by letter: {w}"
                               + (f", which is {m}" if m and key(m) == key(w) else f", most likely {m}" if m else "")
                               for h, w, m in spelled[:2]))
    if low:
        parts.append("unsure of " + ", ".join(f"'{w}' ({c:.2f})" for w, c in low[:5]))
    if near:
        parts.append("; ".join(f"'{h}' sounds close to {t}, " + ("a name on the screen" if key(t) in on_screen
                                                                   else "a name you know") for h, t in near[:3]))
    if fused:
        parts.append("; ".join(f"'{h}' may be {t} said together with the next word" for h, t in fused[:2]))
    # A spelled word that is a known name exactly is no doubt (nothing to ask): only the others count.
    unsure_spelled = sum(1 for _h, w, m in spelled if not (m and key(m) == key(w)))
    return ("; ".join(parts) or None), len(low), len(near) + len(fused) + unsure_spelled


#: The user counts as at the PC when they touched the keyboard or mouse this recently (s): the dormant voice's
#: caches are refreshed only then (an empty room costs nothing).
PRESENT_S = 900.0


def user_present(within_s: float = PRESENT_S) -> bool:
    """Whether the keyboard or mouse was used in the last ``within_s`` (GetLastInputInfo: a time, not an input
    hook; ``True`` when it cannot be read)."""
    try:
        import ctypes

        class _Info(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

        info = _Info(ctypes.sizeof(_Info), 0)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return True
        idle_ms = (ctypes.windll.kernel32.GetTickCount() - info.dwTime) & 0xFFFFFFFF
        return idle_ms / 1000.0 < within_s
    except Exception:  # noqa: BLE001
        return True


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


#: Tools whose result is answered at once even while the voice is still saying the words the talker wrote before the
#: call (leftVoice: quick_memory's "Let me look."): see ``Conversation._answer_look_now`` and ``ClauseTTS``.
ANSWER_WHILE_SPEAKING = frozenset({"quick_memory"})


def bedrock_client(region: str, backend: str | None = None) -> Any:
    """The mouth's model client (Bedrock, or the Anthropic API per ``Settings.model_backend``: see
    :mod:`yuki.core.models`), keeping its connection between turns.

    httpx drops an idle connection after 5 s by default, so every turn after a pause
    paid a new TCP and TLS handshake: measured 2026-09-25 from this PC, time to the first
    token after 8-20 s idle p50 1,362 ms with the default against 860 ms with the
    connection kept (this was the "unexplained ~0.7 s" of docs/VOICE.md §14).
    """
    import httpx
    from anthropic import DefaultAsyncHttpxClient

    from yuki.core.models import async_client

    return async_client(region, backend=backend, http_client=DefaultAsyncHttpxClient(
        limits=httpx.Limits(max_connections=10, max_keepalive_connections=4, keepalive_expiry=300.0),
        verify=shared_ssl_context()))  # built off the loop (v3.6); httpx would load certifi's on it


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

    def hook(name: str, *args: Any, default: Any = None) -> Any:
        """Call one of the conversation's hooks the v3.6 service uses (a test's stand-in may lack it)."""
        fn = getattr(hooks, name, None)
        return fn(*args) if callable(fn) else default

    class LayeredAnthropicLLMService(base):  # type: ignore[misc, valid-type]
        _yuki_lookup: str | None = None
        _yuki_desk: str | None = None
        _yuki_call: LlmCall | None = None

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            #: Tool call ids of replies that said words before calling (see ``chronological``).
            self._yuki_spoke: set[str] = set()
            #: Tool call ids that were the only call of their reply (leftVoice review LV-1: Pipecat answers a reply's
            #: calls once, after the last; only a lone look may be answered at once on its own copy).
            self._yuki_solo: set[str] = set()
            #: Tool call ids whose say_first was spoken while the call was still being written (v3.5).
            self._yuki_early: set[str] = set()
            #: Tool calls of a speculative reply (eager end of turn), run when the turn is confirmed.
            self._yuki_held_calls: list[Any] | None = None
            #: The reply's words, held back while it streams on a turn whose words recognition was unsure
            #: of: said at its end, or never when a call of it is held until the user answers (v3.6, so a
            #: held action is never promised: live 04:46:47 "Settings coming up." and nothing opened).
            self._yuki_words: list[Any] | None = None
            #: A tell_yuki begun in a speculative reply: the thinker starts once the turn is confirmed.
            self._yuki_early_pending: str | None = None
            #: The context of the reply being made (the user's words for an early hand-off).
            self._yuki_context: Any = None
            #: Tool call ids of replies whose held words were dropped unsaid (never moved in the context).
            self._yuki_unspoken: set[str] = set()
            #: Whether each call of a reply is held until the user answers, decided once when the reply ends (v3.10:
            #: live 09:10:01 the words were dropped as "held", then the same call ran a moment later, because the
            #: turn's doubt had moved on in between; the user heard a question and the outcome).
            self._yuki_hold: dict[str, bool] = {}

        async def _yuki_release_words(self, *, drop: bool = False) -> None:
            """Say the reply's held words now, or drop them unsaid (``drop``: a call of the reply is held)."""
            from pipecat.processors.frame_processor import FrameDirection

            words, self._yuki_words = self._yuki_words, None
            if not words:
                return
            if drop:
                hook("words_dropped", len(words))
                return
            for frame in words:
                await super().push_frame(frame, FrameDirection.DOWNSTREAM)

        async def run_function_calls(self, function_calls: Any) -> None:
            for fc in function_calls or []:
                self._yuki_hold[str(getattr(fc, "tool_call_id", fc))] = bool(
                    hook("would_hold", getattr(fc, "function_name", ""), default=False))
            if len(self._yuki_hold) > 64:
                for old in list(self._yuki_hold)[:32]:
                    del self._yuki_hold[old]
            if function_calls and len(function_calls) == 1:
                if len(self._yuki_solo) > 64:
                    self._yuki_solo.clear()   # ids of long-settled calls; a live one is re-added just below
                self._yuki_solo.add(str(getattr(function_calls[0], "tool_call_id", function_calls[0])))
            if self._yuki_words is not None:
                # The reply is complete: its words are said, unless a call it makes will be held until the
                # user answers (then nothing of it was said or done, and the call's report says so).
                drop = bool(function_calls) and any(
                    self._yuki_hold.get(str(getattr(fc, "tool_call_id", fc)), False) for fc in function_calls)
                if drop and self._yuki_words:
                    # No words of this reply are in the context: nothing to put back before its calls.
                    self._yuki_unspoken.update(str(getattr(fc, "tool_call_id", fc)) for fc in function_calls)
                await self._yuki_release_words(drop=drop)
            if function_calls:
                hook("calls_made", {str(getattr(fc, "tool_call_id", fc)) for fc in function_calls})
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
                self._yuki_spoke.update(str(fc.tool_call_id) for fc in function_calls
                                        if str(fc.tool_call_id) not in self._yuki_unspoken)
            await super().run_function_calls(function_calls)

        async def push_frame(self, frame: Any, direction: Any = None) -> None:
            from pipecat.frames.frames import (
                EagerEndOfTurnCancelFrame, InterruptionFrame, LLMFullResponseEndFrame, LLMTextFrame,
                UserStoppedSpeakingFrame,
            )
            from pipecat.processors.frame_processor import FrameDirection

            direction = FrameDirection.DOWNSTREAM if direction is None else direction
            if direction == FrameDirection.DOWNSTREAM and self._yuki_words is not None:
                if isinstance(frame, LLMTextFrame):
                    self._yuki_words.append(frame)  # said when the reply is judged (run_function_calls, or its end)
                    return
                if isinstance(frame, LLMFullResponseEndFrame):
                    await self._yuki_release_words()
            await super().push_frame(frame, direction)
            if direction != FrameDirection.DOWNSTREAM:
                return
            if isinstance(frame, LLMFullResponseEndFrame):
                # E3a: the reply is over (its calls already ran): a router start it did not take is withdrawn, unless
                # the reply was speculative and its calls wait for the turn to be confirmed.
                hook("reply_done", bool(self._yuki_early_pending or self._yuki_held_calls))
            if isinstance(frame, UserStoppedSpeakingFrame):
                pending, self._yuki_early_pending = self._yuki_early_pending, None
                if pending is not None:
                    call_id, screen = pending if isinstance(pending, tuple) else (pending, False)
                    hook("handoff_began", call_id, screen)  # the turn is confirmed: the thinker starts now
            elif isinstance(frame, (EagerEndOfTurnCancelFrame, InterruptionFrame)):
                self._yuki_early_pending = None
                self._yuki_words = None  # a cut reply's held words are never said
                hook("early_withdrawn")  # a hand-off whose call never completed: its early start is void
            held = self._yuki_held_calls
            if held is None:
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
            if hook("may_ack", default=True) is False:
                return   # an acknowledgement was said for these words already (v3.12): this one stays unsaid
            hook("acked")
            self._yuki_spoke.add(tool_id)  # its words go before the call in the context (chronological)
            call.early_say = time.monotonic()
            await self.push_frame(TTSSpeakFrame(text=words))
            hooks.early_said(call, len(words))

        async def _process_context(self, context: Any) -> None:
            self._yuki_held_calls = None  # a new inference: whatever an older speculation held is void
            self._yuki_early_pending = None
            self._yuki_context = context
            call = LlmCall(entered=time.monotonic())
            self._yuki_call = call
            # E3a (v3.17): the router decides alongside this call whether the turn needs the thinker (off by default).
            call.user_turn = _last_role(context) == "user"
            hook("route_turn", context, call)
            try:
                self._yuki_lookup, self._yuki_desk = await hooks.before_call(context, call)
            except Exception:  # noqa: BLE001 - grounding is a hint; never block the reply on it
                self._yuki_lookup, self._yuki_desk = None, None
            call.grounded = time.monotonic()
            # On a turn whose words recognition was unsure of, the words wait for the whole reply (v3.6).
            self._yuki_words = [] if call.user_turn and hook("heard") else None
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
            channel = hook("channel", default="guard")
            params["system"] = mouth_system_blocks(memory, tail=[tail.strip()], call=call,
                                                   call_evidence=hook("call_evidence", default=None),
                                                   channel=channel, hands=bool(hook("hands", default=True)),
                                                   marks=bool(hook("marks", default=True)))
            messages = params.get("messages")
            if channel == "marker":
                messages = with_markers(messages)   # its earlier replies as the channel writes them (v3.16)
            params["messages"] = with_volatile(messages, volatile_block(
                lookup=self._yuki_lookup, desk=self._yuki_desk, call=call, heard=hooks.heard(),
                named=bool(hook("named", default=False)), work=hook("work", default=None),
                cut=hook("cut", default=None), after_goodbye=bool(hook("after_goodbye", default=False)),
                channel=channel, rerun=bool(hook("rerun", default=False)), state=hook("state", default=None),
                practice=getattr(self._yuki_call, "practice_block", None)))
            return params

        def _sync_cancel_tools(self) -> None:
            """Pipecat's cancel tools, with the talker's own description of when to cancel (v3.9)."""
            super()._sync_cancel_tools()
            from pipecat.adapters.schemas.function_schema import FunctionSchema

            from yuki.voice.mouth import CANCEL_TELL_YUKI_DESCRIPTION

            tools = getattr(getattr(self, "_adapter", None), "builtin_tools", None)
            for name in list(getattr(self, "_cancel_tool_names", ()) or ()):
                old = tools.get(name) if isinstance(tools, dict) else None
                if old is not None and getattr(old, "description", None) != CANCEL_TELL_YUKI_DESCRIPTION:
                    tools[name] = FunctionSchema(name=old.name, description=CANCEL_TELL_YUKI_DESCRIPTION,
                                                 properties=old.properties, required=old.required)

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
            # Settings → AI moved the talker: its client already retries there and then tries the user's backup, so
            # this layer makes one attempt (two layers would multiply the attempts and the wait before a reply)
            routed = getattr(hooks, "routed", None)
            moved = bool(callable(routed) and routed())
            tries = 1 if moved else 2
            last: BaseException | None = None
            for index in range(start, len(chain)):
                target = chain[index]
                for attempt in range(tries if index == 0 else 1):
                    try:
                        if target is None and moved:
                            # not Pipecat's timeout-and-send-again either (``retry_on_timeout``): the role client reads
                            # the first event inside this call, so that 5 s limit would cut its retries and backup
                            # short and start the whole chain over (it has its own first-event limit)
                            response = await api_call(**params)
                        elif target is None:
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
                        if kind == "retry" and attempt + 1 < tries and index == 0:
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
            # v3.16, the marker channel: only text after a say marker line reaches the voice (and so the context);
            # the marker lines and any scratch are taken out of the stream here, before anything downstream sees
            # them, so every stream fact (first text, words in the reply, an early say_first) is about speech only.
            last = hook("last_line", default=None) or {}
            channel = (ReplyChannel(cap_words=int(hook("reply_cap", default=0) or 0),
                                    repeat_of=last.get("written"), heard=last.get("heard"))
                       if hook("channel", default="guard") == "marker" else None)

            async def timed() -> Any:
                tool_id, args, said = None, "", True
                early_wait = False  # a tell_yuki whose early thinker start waits for its screen field (v3.9)
                last_text: Any = None   # the latest text delta (the shape a channel's leftover words go out in)
                try:
                    async for event in response:
                        now = time.monotonic()
                        if call.first_event is None:
                            call.first_event = now
                        kind = getattr(event, "type", "")
                        if call.first_content is None and kind in ("content_block_start", "content_block_delta"):
                            call.first_content = now
                        if channel is not None:
                            if kind == "content_block_delta" and hasattr(getattr(event, "delta", None), "text"):
                                last_text = event
                                speech = channel.feed(getattr(event.delta, "text", "") or "")
                                if not speech:
                                    continue   # a marker line or scratch: never spoken, never in the conversation
                                event = _with_text(event, speech)
                            elif kind in ("content_block_stop", "message_delta", "message_stop"):
                                leftover = channel.finish()
                                if leftover and last_text is not None:
                                    # A line start held while it could have been the more divider (v3.17), said now.
                                    if call.first_text is None:
                                        call.first_text = now
                                    yield _with_text(last_text, leftover)
                        if call.first_text is None and kind == "content_block_delta" and hasattr(event.delta, "text"):
                            call.first_text = now
                            if call.user_turn:
                                hook("acked")   # the reply's own words answer them: no second acknowledgement
                        if kind == "content_block_start" and getattr(event.content_block, "type", "") == "tool_use":
                            tool_id, args = str(event.content_block.id), ""
                            name = getattr(event.content_block, "name", "")
                            if call.first_text is not None and self._yuki_words is None:
                                # The reply's words are complete (it goes on to a call): the voice says the
                                # sentence waiting for its lookahead now, not when the call is written (v3.7).
                                await self.push_frame(_words_done_frame())
                            # On a doubtful turn nothing is said before the reply is judged (its words wait too).
                            said = name not in SAY_FIRST_TOOLS or self._yuki_words is not None
                            # The mouth has decided to hand over: the thinker starts on the user's own words while
                            # the call is still being written (v3.6), as soon as its first field says whether the
                            # window in front is to be read for it (v3.9, a few tokens in).
                            early_wait = name == "tell_yuki" and call.user_turn
                        elif (kind == "content_block_delta" and tool_id is not None
                              and hasattr(event.delta, "partial_json")):
                            args += event.delta.partial_json or ""
                            if early_wait:
                                flag = early_flag(args, "screen")
                                if flag is not None or '"say_first"' in args or '"request"' in args:
                                    early_wait = False
                                    if self._speculation_gate.is_speculating:
                                        self._yuki_early_pending = (tool_id, bool(flag))  # once the turn is confirmed
                                    else:
                                        hook("handoff_began", tool_id, bool(flag))
                            words = early_words(args) if not said else None
                            if words is not None:
                                said = True
                                if call.first_text is None:  # a reply that already spoke keeps its own words
                                    await self._yuki_say_early(call, tool_id, words)
                        yield event
                finally:
                    if channel is not None:
                        channel.finish()
                        hook("marker_report", call, channel)

            return timed()

    return LayeredAnthropicLLMService


#: Marks that end a reply's first clause: the voice starts on it while the rest is still being written (v3.7).
#: A comma or colon never ends a sentence, so no lookahead is needed; "." keeps Pipecat's own sentence rule
#: (it waits for the next word, "3.5" / "Dr.").
CLAUSE_MARKS = frozenset(",;:!?\u0964")
#: The first clause is said on its own once it has at least this many words ("Hey," alone waits for more).
CLAUSE_MIN_WORDS = 2


def clause_aggregator(base: type) -> type:
    """Pipecat's ``SimpleTextAggregator`` that lets a reply's first clause go to the voice at once (v3.7).

    Measured 2026-09-27 (offline, Aura): first text -> TTS request 255-362 ms p50, because the aggregator
    waits for a whole sentence and then for the first letter of the next one (and for a one-sentence reply,
    for the reply's end). Here the reply's first clause ("Hey, good morning!" / "Sure, one sec,") is handed
    over the moment its mark and a space arrive; everything after it is aggregated by sentence as before.
    Plumbing: where the voice may start, from punctuation of Yuki's own words.
    """
    from pipecat.utils.text.base_text_aggregator import Aggregation, AggregationType

    class ClauseAggregator(base):  # type: ignore[misc, valid-type]
        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self._yuki_started = False

        async def aggregate(self, text: str) -> Any:
            if self._aggregation_type == AggregationType.TOKEN:
                async for item in super().aggregate(text):
                    yield item
                return
            for char in text:
                self._text += char
                if (not self._yuki_started and char.isspace() and len(self._text) > 1
                        and self._text[-2] in CLAUSE_MARKS and len(self._text.split()) >= CLAUSE_MIN_WORDS
                        and self._text.count("[") <= self._text.count("]")  # not inside a [style: a, b] mark
                        and self._text.count("<") <= self._text.count(">")):
                    out, self._text, self._needs_lookahead = self._text.strip(" "), "", False
                    self._yuki_started = True
                    yield Aggregation(text=out.strip(), type=AggregationType.SENTENCE)
                    continue
                result = await self._check_sentence_with_lookahead(char)
                if result:
                    self._yuki_started = True
                    yield result

        def take_pending(self) -> Any:
            """The words waiting for a lookahead that will not come (the reply moved on to a tool call)."""
            text, self._text, self._needs_lookahead = self._text.strip(), "", False
            if not text:
                return None
            self._yuki_started = True
            return Aggregation(text=text, type=AggregationType.SENTENCE)

        async def flush(self) -> Any:
            self._yuki_started = False
            return await super().flush()

        async def reset(self) -> None:
            self._yuki_started = False
            await super().reset()

        async def handle_interruption(self) -> None:
            self._yuki_started = False
            await super().handle_interruption()

    return ClauseAggregator


def drop_unfinished_marks(text: str) -> str:
    """``text`` without a sound or style mark left open at its end (``"Sure [lau"``, a lone ``"["``): a reply cut at a
    stream boundary or a tool call never sends half a mark to the voice (v3.12). Plumbing on the marks' own syntax."""
    out = text or ""
    for left, right in (("[", "]"), ("<", ">")):
        at = out.rfind(left)
        if at >= 0 and out.find(right, at) < 0:
            out = out[:at]
    return out


def _words_done_frame() -> Any:
    """A frame the mouth pushes after a reply's words when the reply goes on to a tool call (v3.7)."""
    from pipecat.frames.frames import DataFrame

    global _WORDS_DONE
    if _WORDS_DONE is None:
        class WordsDoneFrame(DataFrame):
            """The reply's words are complete: say what is waiting for a lookahead (a tool call follows)."""

        _WORDS_DONE = WordsDoneFrame
    return _WORDS_DONE()


_WORDS_DONE: Any = None


def _clause_tts(base: type, *, strip_marks: bool = False) -> type:
    """``base`` (a Pipecat TTS service) with :func:`clause_aggregator` and the words-done flush (v3.7).

    ``strip_marks``: the voice cannot make Gemini's sounds or styles (Aura, ElevenLabs Flash), so the
    mouth's marks (:mod:`yuki.voice.expressive`) are cut out of what it is sent; the words stay.

    Before, a reply that spoke and then called a tool ("Sure, let me look." + tell_yuki) was heard only once
    the whole call was written: the last sentence waited for a next word that never came (live 0.7-2.1 s).
    The mouth pushes a words-done frame (in order, after the words) when the stream turns to a tool call,
    and what is waiting is said then.
    """
    from pipecat.utils.text.simple_text_aggregator import SimpleTextAggregator

    aggregator_type = clause_aggregator(SimpleTextAggregator)

    class ClauseTTS(base):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._text_aggregator = aggregator_type(aggregation_type=self._text_aggregation_mode)
            if not hasattr(self, "_yuki_guard"):
                self._yuki_guard = None
            #: Lines said by code for an owed outcome (v3.16): the thinker's own words, never held by the check.
            self._yuki_unguarded: set[str] = set()

        def set_guard(self, guard: Any) -> None:
            """The spoken-line check (yuki.voice.speechguard, v3.10): every line passes it before it is heard."""
            self._yuki_guard = guard

        def unguard(self, text: str) -> None:
            """These exact words (said by code for an owed outcome, v3.16) pass without the spoken-line check."""
            key = " ".join(strip_tags(text or "").split())
            if key:
                self._yuki_unguarded.add(key)
                if len(self._yuki_unguarded) > 16:
                    self._yuki_unguarded.pop()

        def _take_unguarded(self, text: str) -> bool:
            key = " ".join(strip_tags(text or "").split())
            if key and key in self._yuki_unguarded:
                self._yuki_unguarded.discard(key)
                return True
            return False

        async def process_ttfa_metrics(self, frame: Any) -> None:
            """Off (v3.15): Pipecat's time-to-first-audible metric re-scans the whole buffered reply with numpy on
            the loop for every chunk until it finds speech (quadratic on a quiet start); live 2026-09-30 the loop
            stalled 6.2 s inside it. Nothing here reads it: the turn rows time the first audio themselves."""
            return None

        async def _push_tts_frames(self, src_frame: Any, *args: Any, **kwargs: Any) -> None:
            text = str(getattr(src_frame, "text", "") or "")
            if text.strip() and not drop_unfinished_marks(text).strip(" .,;:!?-"):
                return   # a lone "[" or a mark cut off at a stream boundary: never a sound (live 13:32:34 "[")
            guard = getattr(self, "_yuki_guard", None)
            if guard is not None and not getattr(self, "_yuki_audio_gate", False):
                # A voice without an audio gate (Aura, ElevenLabs): the line is checked before it is sent (in shadow
                # mode only scored, never waited for; an owed outcome said by code is never checked, v3.16).
                text = strip_tags(str(getattr(src_frame, "text", "") or ""))
                if text.strip() and not self._take_unguarded(text) and not await guard.allows(text):
                    return
            await super()._push_tts_frames(src_frame, *args, **kwargs)

        async def _maybe_pause_frame_processing(self) -> None:
            if getattr(self, "_yuki_no_pause", False):
                return   # a lead before a look: the look's result goes on at once (process_frame below)
            await super()._maybe_pause_frame_processing()

        async def process_frame(self, frame: Any, direction: Any) -> None:
            from pipecat.frames.frames import (
                FunctionCallsStartedFrame, InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
            )

            if isinstance(frame, LLMFullResponseStartFrame):
                # A new reply: a look flagged after the last reply's end (a speculative reply's held calls run once
                # the turn is confirmed, after its end has passed) is not this reply's (review fixes).
                self._yuki_answer_next = False
            elif isinstance(frame, FunctionCallsStartedFrame):
                # leftVoice: Pipecat pauses this service's frames after a reply that had words until they have played
                # ("to avoid audio overlapping"), and the tool's result waits in that queue: with "Let me look."
                # before a quick_memory call the answer's run started only when the line ended (+1.0-1.2 s,
                # measured offline). A reply that ends on such a call is not paused: its result reaches the
                # conversation at once and the answer is written while the lead plays; its audio still plays after
                # the lead's, in order (the audio contexts are played one after another).
                names = {getattr(fc, "function_name", "") for fc in getattr(frame, "function_calls", None) or ()}
                self._yuki_answer_next = bool(names) and names <= ANSWER_WHILE_SPEAKING
            elif isinstance(frame, InterruptionFrame):
                self._yuki_answer_next = False
            elif isinstance(frame, LLMFullResponseEndFrame) and getattr(self, "_yuki_answer_next", False):
                self._yuki_answer_next = False
                self._yuki_no_pause = True
                try:
                    await super().process_frame(frame, direction)
                finally:
                    self._yuki_no_pause = False
                return
            if _WORDS_DONE is not None and isinstance(frame, _WORDS_DONE):
                from pipecat.frames.frames import AggregatedTextFrame

                take = getattr(self._text_aggregator, "take_pending", None)
                pending = take() if callable(take) else None
                if pending is not None:
                    await self.stop_text_aggregation_metrics()
                    await self._push_tts_frames(AggregatedTextFrame(pending.text, pending.type,
                                                                    raw_text=pending.text))
                return  # consumed here: nothing downstream needs it
            await super().process_frame(frame, direction)

        def _prepare_text_for_tts(self, text: str) -> str:
            text = drop_unfinished_marks(text) or " "
            if strip_marks:
                text = strip_tags(text) or " "
            return super()._prepare_text_for_tts(text)

    return ClauseTTS


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
        # enqueue: the file is written by loguru's own thread, never on the voice loop (live 2026-09-30 a burst of
        # 473 error lines stalled the loop 6.1 s inside the sink's write; v3.15).
        logger.add(str(path), level="WARNING", enqueue=True, backtrace=False, diagnose=False)
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
    reply_from: int = 0                  # where the newest mouth run's words start in reply (leftVoice)
    spoken: list[str] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    tts_chars: int = 0
    interrupted: bool = False
    error: str | None = None
    call: bool = False                   # the user was in a voice call (call mode)
    counted: bool = False                # counted as an exchange with Yuki
    cut: bool = False                    # the user spoke again before this turn's reply was heard
    rerun: bool = False                  # answered again: its reply was cut and nothing was heard (v3.9)
    heard_low: int = 0                   # words recognition was unsure of (below LOW_WORD_CONFIDENCE)
    heard_near: int = 0                  # heard words that sound close to a keyterm without being it
    guarded: int = 0                     # lines the spoken-line check kept from being said (v3.10)
    audible: list[str] = field(default_factory=list)   # words as they left the output: really heard (v3.12)
    audible_at: float | None = None      # when the last of them left it
    # v3.16: the marker channel, the rerun's outcome, and a voice that played nothing
    marker: str | None = None            # the reply's first marker line (say / act / quiet), marker mode only
    scratch: int = 0                     # characters of its text that were never spoken (no marker, act, quiet)
    unmarked: bool = False               # text came with no marker line at all: nothing of it was spoken
    responses_at_rerun: int = 0          # mouth runs finished when it was answered again
    rerun_at: float | None = None        # when it was answered again (monotonic)
    gave_up: bool = False                # the rerun was silent too: the error tone said so, the window was extended
    tts_silent: int = 0                  # replies of this turn whose voice played nothing at all
    repeat_dropped: bool = False         # V9: a reply to it was word for word the line said before, and was not said
    # vGame (5 Oct): said to Yuki by measured evidence, and what the fast path did with it
    named: bool = False                  # the name was heard with it (the wake word), or it came right after the name
    fast: str | None = None              # "dismiss" (ended at once) | "name" (the name alone, answered at once)
    ack_at: float | None = None          # when the instant acknowledgement started playing (monotonic)
    quiet_seen: bool = False             # its quiet choice was passed to the conversation already (once per turn)

    @property
    def heard(self) -> bool:
        """Words of a reply to it left the speaker (not a breath of silence, not marks alone)."""
        return bool(strip_tags("".join(self.audible)).strip())

    @property
    def quiet(self) -> bool:
        """Yuki chose silence on it: the stay_quiet call, or a quiet marker (v3.16)."""
        return "stay_quiet" in self.tools or self.marker == "quiet"


class TurnObserver:
    """Frame observer: turn timings, usage, state, and the memory turn log. Content goes only to memory.

    Also the conversation's live state for the narrator gate (who is speaking,
    whether Yuki owes a reply, when it last stopped talking, how many user turns).
    """

    #: A reply owed for longer than this is treated as given (a turn that ended in silence).
    OWES_REPLY_MAX_S = 8.0
    #: Words sent to the voice count as about to be heard for at most this long before Yuki starts
    #: speaking (a TTS that never answers must not hold the narrator forever).
    SPEECH_PENDING_MAX_S = 3.0

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
        #: When words went to the voice that are not audible yet (reply text, a line said by code), else
        #: ``None``: the narrator treats Yuki as about to speak until she does (v3.6).
        self.speech_pending_since: float | None = None
        self.last_bot_stop = 0.0
        self.user_turns = 0
        #: When the user last finished an utterance (its final transcript; v3.9, for "nobody spoke for a while").
        self.last_user_at = 0.0
        self._user_started_at = 0.0
        self._pending_call: LlmCall | None = None
        self._eot_at: float | None = None
        self._eager_at: float | None = None
        self._last_words = ""
        self._words_at: float | None = None
        self.grounder: Any = None
        #: Practice memory's lookups for the talker (:class:`yuki.voice.practice.PracticeLookup`, v3.18), or ``None``.
        self.practice: Any = None
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
        #: The note carries no doubt, only a fact (a name spelled out letter by letter, v3.13): nothing is held for it.
        self.heard_plain = False
        #: The names memory holds (the conversation's keyterms): a heard word close to one is named.
        self.keyterms: list[str] = []
        #: Whether the words being transcribed now begin right where the wake word's name was cut off (the
        #: first user turn of a wake-started conversation; set by the conversation, v3.6).
        self.cut_before: Callable[[], bool] = lambda: False
        self._said: list[str] = []
        self._said_turn: str | None = None
        #: The user's latest final words (for the spoken-line check's context; content, never logged).
        self.last_user_text = ""
        #: The user's last final utterances in this conversation, oldest first (v3.18: what a hand's trace says they
        #: asked, the words a learned way may type or send, and what the authorization check of a hand reads; content,
        #: RAM only).
        self.recent_words: deque[str] = deque(maxlen=8)
        #: ``(words)``: the user's turn ended with these words (the thinker's request pack is built then, v3.18).
        self.on_final_words: Callable[[str], None] | None = None
        #: The conversation's spoken-line check (yuki.voice.speechguard), or ``None``.
        self.guard: Any = None
        #: Names on the screen in front (yuki.voice.screennames, v3.10): a heard word close to one is named too.
        self.screen_terms: list[str] = []
        #: The last reply the user spoke over: what they heard and what was not said (v3.12), for the talker.
        self.last_cut: dict[str, Any] | None = None
        #: ``(now, turn)``: words left the speaker (v3.16, the owed-output ledger listens here). Set by the conversation.
        self.on_audible: Callable[[float, Any], None] | None = None
        #: The user's turn being answered again (``Conversation._reply_watch``): the rerun's reply stays in it (v3.16).
        self.rerun_turn: _Turn | None = None
        #: The turn whose look is being answered at once (``Conversation._answer_look_now``): that run's reply stays in
        #: it, so its lead's first audio and voice use are logged in it (leftVoice second review, LVC-2).
        self.look_now_turn: _Turn | None = None
        #: Words were heard since Yuki last started speaking: a stretch with none (the voice's breath of silence for a
        #: reply that had nothing audible) is no answer and no exchange (v3.16, 04 §4.1).
        self._stretch_words = False
        #: When words last left the speaker (monotonic, 0: never).
        self.words_heard_at = 0.0
        #: V9: the last turn whose words left the speaker: ``{"written": all it wrote, "heard": what left the speaker}``
        #: (content, RAM only), for the speech channel's repeat guard.
        self.last_said: dict[str, str] | None = None
        #: vGame: ``(turn)``: the talker chose silence on a user turn (stay_quiet or a quiet marker); set by the
        #: conversation (in a game or a call that ends the conversation at once, silently).
        self.on_quiet_turn: Callable[[Any], None] | None = None

    def fast_handled(self, kind: str) -> None:
        """vGame: the open user turn was answered by the fast path (``"dismiss"`` or ``"name"``): it was said to Yuki
        and answered, never "for others" (the talker never ran on it)."""
        turn = self.turn
        if turn is None or turn.trigger != "user":
            return
        turn.fast = kind
        turn.named = True
        turn.tools.append("end_conversation" if kind == "dismiss" else "answer_name")
        self.owes_reply_since = None
        self._took_part(time.monotonic())

    def _quiet_turn(self) -> None:
        """The talker chose silence on the open user turn (vGame): tell the conversation, once per turn."""
        turn = self.turn
        if turn is None or turn.trigger != "user" or turn.quiet_seen or turn.counted or not turn.quiet:
            return
        turn.quiet_seen = True
        if self.on_quiet_turn is not None:
            try:
                self.on_quiet_turn(turn)
            except Exception:  # noqa: BLE001 - a hint to the conversation only
                pass

    def cut_for_now(self) -> dict[str, str] | None:
        """The last reply they spoke over, as they heard it (v3.12), for the first user turn after it; else ``None``."""
        cut = self.last_cut
        if cut is None or self.user_turns > int(cut.get("turn", 0)) + 1:
            return None
        return {"heard": cut["heard"], "unsaid": cut["unsaid"]}

    #: Words a second of Yuki's voice carries (measured on Gemini Leda replies, 2026-09-28: 2.4-2.9): how much of the
    #: sentence being played when they spoke they heard (a sentence's words leave the output only after all of it).
    WORDS_PER_S = 2.6

    def _note_cut(self, turn: "_Turn", now: float) -> None:
        """A reply was spoken over: keep what was heard and what was not said (v3.12).

        Heard: the sentences whose audio played to the end (their words left the output), plus the part of the one
        playing when they spoke, estimated from how long it had played. Not said: the rest of what was written.
        """
        done = strip_tags("".join(turn.audible)).split()
        written = strip_tags("".join(turn.reply)).split()
        if written[:len(done)] != done:
            written = done + [w for w in written if w not in done]
        since = turn.audible_at or turn.first_audio or now
        partial = max(0, int((now - since) * self.WORDS_PER_S))
        heard = written[:len(done) + partial]
        unsaid = written[len(heard):]
        if not unsaid:
            return
        self.last_cut = {"heard": " ".join(heard), "unsaid": " ".join(unsaid), "turn": self.user_turns}
        if self.log is not None:
            self.log.event("interrupted", heard_words=len(heard), unsaid_words=len(unsaid), whole_words=len(done),
                           estimated_words=min(partial, len(heard) - len(done)))

    def guarded(self) -> None:
        """The spoken-line check kept a line of this turn from being said (the turn log keeps what was heard).

        The reply is given, as far as waiting goes: nothing of that line will be heard, so the narrator does not wait
        for it (a result held behind it would otherwise wait out the owed-reply time)."""
        if self.turn is not None:
            self.turn.guarded += 1
        self.owes_reply_since = None
        self._activity()

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
        from pipecat.frames.frames import BotStoppedSpeakingFrame, InterruptionFrame, TTSTextFrame

        frame = data.frame
        if (isinstance(frame, TTSTextFrame) and self.turn is not None and self.output_processor is not None
                and getattr(data, "source", None) is self.output_processor):
            words = strip_tags(frame.text or "")
            self.turn.audible.append(words + " ")
            self.turn.audible_at = time.monotonic()
            if words.strip():
                heard = " ".join("".join(self.turn.audible).split())
                written = " ".join(strip_tags("".join(self.turn.reply)).split())
                self.last_said = {"written": written if written.startswith(heard[:12]) else heard, "heard": heard}
                self._heard_words(self.turn.audible_at)
        if self.on_chat is None:
            return
        if isinstance(frame, TTSTextFrame) and self.output_processor is not None \
                and getattr(data, "source", None) is self.output_processor:
            turn = self.turn_id()
            if turn != self._said_turn:
                self._said, self._said_turn = [], turn
            self._said.append(strip_tags(frame.text or ""))
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

    def _heard_words(self, now: float) -> None:
        """Words of Yuki's left the speaker (v3.16): only this answers the user and counts as an exchange.

        Before, the start of any audio did (``BotStartedSpeakingFrame``), and the voice's 20 ms breath of silence
        for a fully dropped reply counted as an answer: live 2 Oct 21:24:08 "Open Chrome." got that breath, the
        unanswered-turn rerun never ran, and the call's idle clock restarted on it (04 §4.1).
        """
        self.words_heard_at = now
        self._stretch_words = True
        self.owes_reply_since = None
        self._took_part(now)
        if self.on_audible is not None:
            try:
                self.on_audible(now, self.turn)
            except Exception:  # noqa: BLE001 - bookkeeping never breaks the voice
                pass

    def tts_silent(self, speakable: int) -> None:
        """A reply's voice finished with nothing audible (v3.16; the voice's breath closed it, no ErrorFrame).

        The pill says so only when the reply had words to say (``speakable`` pieces) and none of this turn's were
        heard, in plain words; a reply of marks alone, or one whose words were dropped on purpose, shows nothing.
        """
        turn = self.turn
        if turn is None:
            return
        turn.tts_silent += 1
        if self.log is not None:
            self.log.event("tts_silent", turn=turn.n, trigger=turn.trigger, speakable=int(speakable))
        if speakable > 0 and not turn.heard:
            self.set_state("error", SILENT_REPLY_NOTE)

    # -- live state (the narrator gate) -------------------------------------------

    @property
    def owes_reply(self) -> bool:
        since = self.owes_reply_since
        return since is not None and time.monotonic() - since < self.OWES_REPLY_MAX_S

    @property
    def speech_pending(self) -> bool:
        since = self.speech_pending_since
        return since is not None and not self.bot_speaking and time.monotonic() - since < self.SPEECH_PENDING_MAX_S

    def live_state(self) -> dict[str, Any]:
        return {"user_speaking": self.user_speaking, "bot_speaking": self.bot_speaking,
                "generating": self.generating, "owes_reply": self.owes_reply,
                "speech_pending": self.speech_pending,
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
        # v3.18: practice memory is looked up on the final words only (their lookup, ~10-35 ms measured, fits inside the
        # grounding wait the talker's call has anyway); a lookup per interim transcript was CPU spent on every word of
        # every utterance, a call's talk to the others included (lightweight rule)
        if self.hands is not None and hasattr(self.hands, "prefetch_desk"):
            self.hands.prefetch_desk()  # the reply's desktop block, read while they talk (v3.7)

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

    def handoff_started(self, at: float) -> None:
        """The thinker was handed this turn's request (the early hand-off, v3.6): end of speech -> hand-off."""
        turn = self.turn
        if turn is not None and turn.trigger == "user" and turn.handoff_at is None:
            turn.handoff_at = at

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
        # Settings → AI may have moved the talker: the conversation prices the turn as the model(s) that answered
        price_turn = getattr(self, "price_turn", None)
        if callable(price_turn):
            cost = price_turn(usage)
        else:
            from yuki.agent.providers.client import talker_price_model

            cost = self.settings.estimate_cost(talker_price_model(self.settings), usage)
        # What memory and the chat keep is the words: the voice's marks (<sigh>, <style: ...>) are cut out.
        # What was heard: a line the spoken-line check dropped was never said, so it is never "the reply" either.
        spoken = "".join(turn.spoken).strip()
        reply = strip_tags(spoken if turn.guarded else (spoken or "".join(turn.reply).strip()))
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
            "grounding_chars": c.grounding_chars if c else None,
            "grounding_wait_ms": ms(c.entered, c.grounded) if c else None,
            "desk": c.desk if c else None, "desk_ms": c.desk_ms if c else None,
            "desk_chars": c.desk_chars if c else None,
            "desk_tabs_ms": c.desk_tabs_ms if c else None,
            # v3.18: practice memory's lines in the talker's volatile block (kinds and ids, size; never their text)
            "practice": c.practice if c else None,
            "practice_wait_ms": c.practice_wait_ms if c else None,
            "talker_block": (c.talker_block or None) if c else None,
            "talker_block_chars": c.talker_block_chars if c else None,
            "llm_prep_ms": ms(c.grounded, c.sent) if c else None,
            "llm_open_ms": ms(c.sent, c.opened) if c else None,
            "llm_first_event_ms": ms(c.sent, c.first_event) if c else None,
            "llm_api_ttft_ms": ms(c.sent, c.first_content) if c else None,
            "llm_first_text_ms": ms(c.sent, c.first_text) if c else None,
            "llm_early_say_ms": ms(c.sent, c.early_say) if c else None,
            "text_to_tts_ms": ms(c.first_text if c else None, turn.tts_started),
            "tts_audio_to_speaking_ms": ms(turn.tts_audio, turn.first_audio),
            "eager": bool(c and turn.transcript_at and c.entered < turn.transcript_at),
            # Wall-clock time the turn opened (the row itself is written when the next turn opens: live
            # 2026-09-27 a result turn spoken at 04:47:33 was logged at 04:47:56 and read as a late repeat).
            "opened_at": datetime.fromtimestamp(time.time() - (time.monotonic() - turn.opened)).isoformat(
                timespec="milliseconds"),
        }
        others = self.for_others(turn, reply)
        addressed = None
        if turn.trigger == "user":
            addressed = ("others" if others or (not reply and turn.quiet and not turn.counted)
                         else "yuki")
            self.chat({"type": "turn_closed", "turn_id": f"{self.session}-{turn.n}", "addressed": addressed,
                       "replied": bool(reply), "cut_by_user": turn.cut})
        # v3.16: what was really heard, the marker channel, and an unheard turn answered again (content-free)
        row["audible_chars"] = len(strip_tags("".join(turn.audible)).strip())
        if c is not None and (c.route is not None or c.route_ms is not None):
            row.update(router=c.route, router_ms=c.route_ms)   # E3a (v3.17)
        if turn.marker is not None or turn.unmarked or turn.scratch:
            row.update(marker=turn.marker, unmarked=turn.unmarked, scratch_chars=turn.scratch)
        if turn.rerun:
            row.update(rerun=True, gave_up=turn.gave_up)
        if turn.trigger == "user" and (turn.named or turn.fast or turn.ack_at is not None):
            # vGame: said to Yuki by measured evidence; what the fast path did; end of speech -> the instant "Mm-hm"
            row.update(named=turn.named, fast=turn.fast, eos_to_ack_ms=ms(turn.speech_end, turn.ack_at))
        if turn.call:
            row["call_mode"] = True
            if turn.trigger == "user":
                row["addressed"] = ("yuki" if not others else
                                    "others" if turn.quiet else "unanswered")
                # Why a turn was taken as not for Yuki (content-free, v3.15): the talker's stay_quiet in call mode,
                # or their next words came before any reply.
                if others:
                    row["for_others_why"] = ("talker_stay_quiet" if turn.quiet
                                             else "next_words_first")
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
            memory, log = self.memory, self.log
            # v3.18: the turn is dated when it opened (the user's final words, or the reply a result turn began), not
            # when this row is written (the next turn's opening): practice memory's learners read "what the user said
            # next" after a hand's trace by these times, and a turn dated at its close came after its own hand.
            opened_at = time.time() - (time.monotonic() - turn.opened)
            fields = dict(request_id=f"voice-{self.session}-{turn.n}", at=opened_at, user_text=turn.user_text,
                          reply_text=reply, actions=[f"{name} (by voice)" for name in turn.tools], outcome="voice",
                          addressed=addressed)

            def write() -> None:
                try:
                    memory.log_turn(**fields)
                except Exception as exc:  # noqa: BLE001 - memory never breaks voice
                    if log is not None:
                        log.event("memory_turn", error=f"{type(exc).__name__}: {exc}")

            try:
                asyncio.get_running_loop()
            except RuntimeError:
                write()   # not on a loop (a test's observer, a closing conversation): as before
            else:
                # Off the voice loop, in order (v3.14): memory's first log_turn starts its writer thread, and a
                # thread's start waits for the interpreter lock (0.5 s on the loop, offline 29 Sept).
                from yuki.voice.workers import ordered

                ordered().submit(write)

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
            note, low, near = heard_note(result.get("words") if isinstance(result, dict) else None, self.keyterms,
                                         cut_before=self.cut_before(), screen=self.screen_terms)
            self.heard, self.heard_plain = note, bool(note and not (low or near))
            return
        if isinstance(frame, UserStartedSpeakingFrame):
            self.user_speaking = True
            self._user_started_at = now
            self.heard, self.heard_plain = None, False  # a new turn: the last one's doubts are not about these
            if self.bot_speaking and self.turn is not None:
                self.turn.interrupted = True
            if (self.turn is not None and self.turn.trigger == "user" and not self.turn.heard
                    and self.owes_reply):
                # They went on talking before hearing a reply (a pause inside what they were saying): that
                # reply is dropped and the next one answers everything (logged: silent turns, 26 Sept).
                self.turn.cut = True
            if self.grounder is not None:
                self.grounder.new_turn()
            if self.practice is not None:
                self.practice.new_turn()
            if self.hands is not None:
                self.hands.prefetch()  # the tabs for the reply's desktop block, read while they talk
            self.set_state("hearing")
            self._activity()
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self._activity()
        elif isinstance(frame, TranscriptionFrame):
            if self.turn is not None and self.turn.trigger == "user" and self.turn.fast == "dismiss":
                self.turn.user_text = frame.text or self.turn.user_text   # vGame: ended on its eager words already
                return
            turn = self._open("user")
            turn.user_text = frame.text or ""
            self.chat({"type": "user_utterance", "text": turn.user_text, "ts": time.time(), "turn_id": self.turn_id()})
            turn.transcript_at = now
            turn.speech_end, turn.eos_source = self._end_of_speech(now)
            result = getattr(frame, "result", None)
            self.heard, turn.heard_low, turn.heard_near = heard_note(
                result.get("words") if isinstance(result, dict) else None, self.keyterms,
                cut_before=self.cut_before(), screen=self.screen_terms)
            self.heard_plain = bool(self.heard and not (turn.heard_low or turn.heard_near))
            self.last_user_text = turn.user_text
            if turn.user_text.strip():
                self.recent_words.append(turn.user_text.strip())
            if self.guard is not None:
                self.guard.new_turn()
            self._last_words, self._words_at = "", None
            turn.eot_at = now  # Flux pushes the final transcript at its end of turn
            turn.eager_at, self._eager_at = self._eager_at, None
            if self.grounder is not None:
                self.grounder.final(turn.user_text)  # before the aggregator hands the context over
            if self.practice is not None:
                self.practice.final(turn.user_text)
            if self.on_final_words is not None and turn.user_text.strip():
                try:
                    self.on_final_words(turn.user_text)   # only puts work on a pre-started thread
                except Exception:  # noqa: BLE001 - a head start only
                    pass
            if self.hands is not None:
                self.hands.new_user_turn()  # a hand halted by a wrong effect may act again
            pending, self._pending_call = self._pending_call, None
            if pending is not None and pending.entered >= self._user_started_at:
                turn.llm = pending  # the eager (speculative) reply, confirmed by this transcript
            lang = getattr(frame, "language", None)
            turn.language = str(getattr(lang, "value", lang)) if lang else None
            self.user_turns += 1
            self.last_user_at = now
            self.owes_reply_since = now
            self.set_state("thinking")
            self._activity()
        elif isinstance(frame, LLMContextFrame):
            if self.turn is not None and self.turn.context_at is None and self.turn.transcript_at is not None:
                self.turn.context_at = now
        elif isinstance(frame, LLMFullResponseStartFrame):
            self.generating = True
            rerun, self.rerun_turn = self.rerun_turn, None
            look_now, self.look_now_turn = self.look_now_turn, None
            if rerun is not None and rerun is self.turn:
                pass   # the user's turn answered again (v3.16): its new reply is still that turn's answer
            elif look_now is not None and look_now is self.turn:
                pass   # a look answered at once while its lead is still on its way (leftVoice): the same answer
            elif self.turn is None or self.turn.first_audio is not None or self.turn.responses:
                # A new mouth run after this turn already had one (a result, a progress line,
                # a follow-up to a tool-only reply) is its own turn, with its own stages.
                self._open("result")
            if self.turn.llm is None and self._pending_call is not None:
                self.turn.llm, self._pending_call = self._pending_call, None
            self.turn.reply_from = len(self.turn.reply)
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
            self._quiet_turn()   # vGame: a quiet marker is known by now (a stay_quiet call comes just after)
            self._activity()
        elif isinstance(frame, LLMTextFrame):
            if self.turn is not None:
                self.turn.reply.append(frame.text or "")
            if not self.bot_speaking and (frame.text or "").strip():
                self.speech_pending_since = now  # on its way to the voice
        elif isinstance(frame, TTSStartedFrame):
            if self.turn is not None and self.turn.tts_started is None:
                self.turn.tts_started = now
            if not self.bot_speaking:
                self.speech_pending_since = now
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
                else:
                    self._quiet_turn()   # vGame
        elif isinstance(frame, BotStartedSpeakingFrame):
            self.bot_speaking = True
            self.speech_pending_since = None
            self._stretch_words = False
            if self.turn is not None and self.turn.first_audio is None:
                self.turn.first_audio = now
            # v3.16: the exchange is counted by the first words heard (``_heard_words``), not by the start of any
            # audio: a breath of silence closing a reply with nothing audible is not an answer.
            self.set_state("speaking")
            self._activity()
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self.turn is not None and self.turn.interrupted:
                self._note_cut(self.turn, now)
            self.bot_speaking = False
            self.speech_pending_since = None
            self.last_bot_stop = now
            if self._stretch_words:
                self.last_exchange = now
                self.owes_reply_since = None
            self.set_state(self.idle_state())
            self._activity()
        elif isinstance(frame, InterruptionFrame):
            self.speech_pending_since = None  # whatever was on its way is dropped
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
            fatal = bool(getattr(frame, "fatal", False))
            if self.turn is not None:
                self.turn.error = text[:200]
            if self.log is not None:
                self.log.event("error", fatal=fatal, error=text[:500])
            if fatal:
                self.fatal = text
                self.set_state("error", FATAL_NOTE)   # the session's notice says why, in words the user can act on
            elif self.turn is not None:
                # v3.16: a non-fatal error (a TTS context with no audio, a hiccup) is never shown as the service's
                # raw text; the pill says something only when the reply had words and none were heard.
                speakable = bool(strip_tags("".join(self.turn.reply)).strip())
                if speakable and not self.turn.heard and not self.bot_speaking:
                    self.set_state("error", SILENT_REPLY_NOTE)
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
        job = getattr(self.c, "_recent_job", None)
        if job is not None:
            self.c._recent_job = None
        hands = getattr(self.c.bridge, "hands", None) if self.c.settings.voice_hands else None
        desk_task = (asyncio.ensure_future(hands.for_call(self.c.settings.voice_desk_wait_ms))
                     if hands is not None else None)
        # vfix4: a woken conversation's first call waits a moment for the last hour, read again on the wake, alongside
        # the desktop summary and the lookup (review F3: never in front of them)
        recent_task = (asyncio.ensure_future(self._recent(job, getattr(self.c, "_recent_close", None)))
                       if job is not None else None)
        practice = getattr(self.c, "practice", None)
        if practice is not None and user_turn:
            # the final words' lookup starts here, with the call (the observer sees the transcript a moment later:
            # Pipecat calls observers from a queue of its own); a lookup of the same words already running is reused
            words = _last_user_text(context)
            if words.strip():
                practice.final(words)
        practice_task = (asyncio.ensure_future(self._practice_for(practice, user_turn))
                         if practice is not None else None)
        lookup_text = await self._lookup(call, user_turn)
        if recent_task is not None:
            await recent_task
        if practice_task is not None:
            self._practice(call, *(await practice_task))
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

    async def _recent(self, job: Any, close: Any) -> None:
        """Wait at most :attr:`Conversation.RECENT_WAIT_S` for the wake's read of the last hour, then close it: a read
        that lands later is dropped, so the system prompt never changes after this call (review F4); this
        conversation goes with the view read when it was built."""
        try:
            if not job.done():
                await asyncio.wait_for(asyncio.shield(asyncio.wrap_future(job)), self.c.RECENT_WAIT_S)
        except Exception:  # noqa: BLE001 - past the bound: the view read at the build
            pass
        finally:
            if callable(close):
                close()

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
        text = lookup.block if lookup.block is not None else format_facts(lookup.facts)
        call.grounding_chars = len(text or "")   # (lookup.block: the whole-memory block for the talker, v3.14)
        return text

    async def _practice_for(self, practice: Any, user_turn: bool) -> tuple[Any, str, float]:
        """The practice lookup for this call, waiting at most as long as the grounding lookup: ``(lookup, how, ms)``."""
        started = time.monotonic()
        try:
            found, how = await practice.for_call(self.c.settings.voice_grounding_wait_ms if user_turn else 0.0,
                                                 user_turn=user_turn)
        except Exception:  # noqa: BLE001 - a hint; never block the reply on it
            found, how = None, "none"
        return found, how, (time.monotonic() - started) * 1000

    def _practice(self, call: LlmCall, found: Any, how: str, wait_ms: float) -> None:
        """Practice memory's lines for this call (v3.18): into the volatile block, offered skills registered for
        run_skill, and what the block held on the call (kinds, ids, size) for the turn log; a content-free event."""
        call.practice = how
        call.practice_wait_ms = round(wait_ms, 1)
        self.c._shown_ids = []
        if found is None:
            return
        block = found.block
        call.practice_block = block or None
        call.talker_block = found.kinds_ids()
        call.talker_block_chars = len(block or "")
        self.c._shown_ids = found.ids()
        practice = getattr(self.c, "practice", None)
        if practice is not None and found.skills:
            practice.offer(found, self.c.observer.user_turns)
        facts = found.facts()
        self.c.log.event("practice", op="block", session=self.c.session, how=how, user_turn=call.user_turn,
                         wait_ms=call.practice_wait_ms, **facts)

    def call_started(self, call: LlmCall) -> None:
        self.c.observer.call_started(call)

    def early_said(self, call: LlmCall, chars: int) -> None:
        """A call's say_first went to the voice while the call was being written (content-free log)."""
        self.c.log.event("early_say", chars=chars,
                         after_request_ms=round((call.early_say - call.sent) * 1000) if call.sent and call.early_say
                         else None)

    # -- the mouth's model failover (v3.5) ----------------------------------------

    def routed(self) -> bool:
        """Whether Settings → AI moved the talker to another service (its client then does the retries)."""
        routed = getattr(getattr(self.c, "_client", None), "routed", None)
        return bool(callable(routed) and routed())

    def fallbacks(self) -> list[dict[str, Any]]:
        if self.routed():
            return []   # Settings → AI moved the talker: its client retries there and uses only the user's backup
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
            clients[region] = bedrock_client(region, getattr(self.c.settings, "model_backend", None))
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

    def would_hold(self, name: str) -> bool:
        """Whether a call of the reply now ending will be held until the user answers (see ``Conversation._held``)."""
        return self.c._held(name)

    def words_dropped(self, frames: int) -> None:
        """A doubtful turn's reply made a call that is held: its words were never said (content-free log)."""
        self.c.log.event("held_words", op="dropped", frames=frames)

    def handoff_began(self, call_id: str, screen: bool = False) -> None:
        """The mouth's reply has begun a tell_yuki (its first field is written): start the thinker now."""
        self.c._early_handoff(call_id, screen)

    def work(self) -> str | None:
        """What the thinker is doing now, for the talker's volatile block (v3.9), or ``None``."""
        line = getattr(self.c.bridge, "work_line", None)
        try:
            return line() if callable(line) else None
        except Exception:  # noqa: BLE001 - a hint only
            return None

    def early_withdrawn(self) -> None:
        """The reply was cut (a barge-in, a withdrawn speculation): an early start whose call never ran is void."""
        self.c._cancel_route()
        self.c.bridge.drop_early()

    def calls_made(self, ids: set[str]) -> None:
        """The reply's calls are complete: an early start for a call that is not among them is void."""
        self.c.bridge.settle_early(ids)

    def heard(self) -> str | None:
        """What recognition was unsure of in the latest user words, for a call answering them (else ``None``)."""
        call = getattr(self.c._llm, "_yuki_call", None) if getattr(self.c, "_llm", None) is not None else None
        if call is not None and not call.user_turn:
            return None
        return self.c.observer.heard

    def call(self) -> str | None:
        """The app of the voice call the user is in (call mode), else ``None``."""
        return self.c.call_app

    def after_goodbye(self) -> bool:
        c = self.c
        call = getattr(getattr(c, "_llm", None), "_yuki_call", None)
        return bool(getattr(c, "after_goodbye", False) and (call is None or call.user_turn) and c.observer.user_turns <= 1)

    def may_ack(self) -> bool:
        fn = getattr(self.c.bridge, "may_ack", None)
        return bool(fn()) if callable(fn) else True

    def acked(self) -> None:
        fn = getattr(self.c.bridge, "acked", None)
        if callable(fn):
            fn()

    def cut(self) -> dict[str, str] | None:
        """What they heard of the reply they spoke over, and what was not said yet (the first user turn after it)."""
        call = getattr(getattr(self.c, "_llm", None), "_yuki_call", None)
        if call is not None and not call.user_turn:
            return None
        return self.c.observer.cut_for_now()

    def call_evidence(self) -> str | None:
        """How sure memory is of that call: ``"fresh"`` or ``"stale"`` (v3.10), ``None`` without a call."""
        return self.c.call_evidence if self.c.call_app else None

    def named(self) -> bool:
        """Whether the call answers words said to Yuki by name: the first words after the wake word (the name was cut
        from them, v3.6), or (vGame) a turn the wake word heard the name in, or the turn right after the name alone."""
        c = self.c
        call = getattr(getattr(c, "_llm", None), "_yuki_call", None)
        if call is not None and not call.user_turn:
            return False
        turn = c.observer.turn
        if turn is not None and turn.trigger == "user" and turn.named:
            return True
        return bool(c.woken_by_name and c.observer.user_turns <= 1)

    # -- v3.16: the speech channel and an unheard turn answered again --------------------------------------

    def channel(self) -> str:
        """``"marker"`` or ``"guard"`` (``Settings.voice_speech_channel``)."""
        return self.c.channel

    def rerun(self) -> bool:
        """Whether this call answers the user's turn again because nothing of the last reply to it was heard."""
        turn = self.c.observer.turn
        call = getattr(getattr(self.c, "_llm", None), "_yuki_call", None)
        return bool(turn is not None and turn.rerun and turn.trigger == "user" and (call is None or call.user_turn))

    def marker_report(self, call: LlmCall, channel: Any) -> None:
        """A reply's text was read by the marker channel: record it on its turn and log it (content-free)."""
        self.c._marker_report(call, channel)

    # -- v3.17: the prompt by measured state (H1) and the answer's length (H2) --------------------------------

    def hands(self) -> bool:
        """Whether the talker has hands this conversation (its hands block is in the prompt only then)."""
        return bool(self.c.settings.voice_hands and getattr(self.c.bridge, "hands", None) is not None)

    def marks(self) -> bool:
        """vfix4: whether this conversation's voice performs the talker's marks (Gemini); Deepgram Aura and ElevenLabs
        strip them (``_clause_tts(strip_marks=True)``), so their talker's core goes without the marks section."""
        return str(getattr(self.c, "tts_provider", "") or "").strip().lower() == "gemini"

    def reply_cap(self) -> int:
        """The word cap of a spoken reply (``voice_reply_words_cap``; 0: none); vGame: shorter in a game or a call
        (``voice_focus_reply_words``)."""
        cap = int(getattr(self.c.settings, "voice_reply_words_cap", 0) or 0)
        focus_cap = int(getattr(self.c.settings, "voice_focus_reply_words", 0) or 0)
        if self.c.focus and focus_cap:
            return min(cap, focus_cap) if cap else focus_cap
        return cap

    def last_line(self) -> dict[str, str] | None:
        """V9: Yuki's last spoken line (``{"written", "heard"}``), so a reply that repeats it word for word is not
        said (content, RAM only); ``None`` before anything was heard. ``None`` too on the rerun of a turn whose reply
        was held as a repeat: that rerun was told so, and decides by itself whether to say the line again (they may
        have asked to hear it again: review F10, a code rule never overrides that)."""
        obs = self.c.observer
        turn = getattr(obs, "turn", None)
        if turn is not None and getattr(turn, "repeat_dropped", False):
            return None
        return getattr(obs, "last_said", None)

    def state(self) -> dict[str, Any]:
        """The measured states that bring their guidance into this call's volatile block (``mouth.volatile_block``)."""
        return self.c._prompt_state()

    # -- v3.17: the hand-off router (E3a) ---------------------------------------------------------------------

    def route_turn(self, context: Any, call: LlmCall) -> None:
        self.c._route_turn(context, call)

    def reply_done(self, pending: bool = False) -> None:
        self.c._reply_done(pending)


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


def _holds_words_since_user(messages: list[Any], words: str) -> bool:
    """Whether an assistant message after the newest user message already says ``words`` (whitespace aside)."""
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            return False
        content = message.get("content")
        if message.get("role") == "assistant" and isinstance(content, str) and " ".join(content.split()) == words:
            return True
    return False


def _said_count(context: Any) -> int | None:
    """How many user and developer messages the talker's context holds (a newer one means the user said more)."""
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return None
    return sum(1 for m in messages or [] if isinstance(m, dict) and m.get("role") in ("user", "developer"))


def _reply_words(observer: Any) -> str:
    """The current reply's words so far (the observer's turn, from where the newest run began), tags stripped."""
    turn = getattr(observer, "turn", None)
    if turn is None:
        return ""
    return " ".join(strip_tags("".join(turn.reply[getattr(turn, "reply_from", 0):])).split())


async def _add_asked_more(conv: Any, context: Any, result: Any, call_id: str) -> bool:
    """Whether the words a code-said add came from asked for more that is not answered yet (third check LVV-1).

    :func:`yuki.voice.recall.rest_asked`: one short isolated check by the fast model, run while the add's line plays,
    with their words and Yuki's words so far (its lead, then the line). ``conv`` is the :class:`Conversation`. No
    client (a bare conversation), no verdict in time or an error: more, so the talker runs and decides (never
    silent)."""
    from yuki.voice.recall import rest_asked

    observer = getattr(conv, "observer", None)
    said = " ".join(str(result.get("said") or "").split())
    words = _reply_words(observer)
    if said and said not in words:
        words = f"{words} {said}".strip()
    user = _last_user_text(context) or str(getattr(observer, "last_user_text", "") or "")
    client = getattr(conv, "_client", None)
    settings = getattr(conv, "settings", None)
    started = time.monotonic()
    if client is None or settings is None:
        more, how = True, "no_client"
    else:
        more, how = await rest_asked(client, settings.voice_mouth_model, user=user, said=words)
    if conv.log is not None:
        conv.log.event("quick_memory", session=conv.session, op="add_rest", call=call_id, more=more, how=how,
                       ms=round((time.monotonic() - started) * 1000))
    return more


def _last_user_text(context: Any) -> str:
    """The newest user message's words in the talker's context (``""`` when the newest message is not the user's)."""
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return ""
    for message in reversed(messages or []):
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant", "developer", "tool"):
            continue
        if message.get("role") != "user":
            return ""
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return " ".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
        return ""
    return ""


def fast_path_processor(conversation: Any) -> Any:
    """vGame: the frame processor between speech recognition and the talker's context (``Conversation._fast_turn``).

    It sees each transcript before the user aggregator does: a final one the fast path takes (a dismissal, the name
    alone) never reaches the talker; an eager one that is a dismissal starts no speculative talker call. Every other
    frame passes untouched. The turn observer saw the transcript already (observers see a frame as it is pushed here),
    so the turn is logged as usual."""
    from pipecat.frames.frames import EagerTranscriptionFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    class FastPath(FrameProcessor):
        async def process_frame(self, frame: Any, direction: Any) -> None:
            await super().process_frame(frame, direction)
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, (TranscriptionFrame,
                                                                             EagerTranscriptionFrame)):
                try:
                    taken = conversation._fast_turn(frame.text or "",
                                                    final=not isinstance(frame, EagerTranscriptionFrame))
                except Exception as exc:  # noqa: BLE001 - the talker then, as before
                    taken = False
                    conversation.log.event("fast_path", op="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                if taken:
                    return
            await self.push_frame(frame, direction)

    return FastPath(name="yuki-fast-path")


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
                 on_chat: Callable[[dict[str, Any]], None] | None = None, call_evidence: str | None = None,
                 on_quiet: Callable[[float], None] | None = None, dormant: bool = False) -> None:
        self.settings = settings
        #: Built ahead and waiting for the name (v3.11): the pipeline runs, speech recognition is connected and the
        #: caches are warm, but no audio is sent anywhere and nothing is said until :meth:`activate`.
        self.dormant = dormant
        #: Called on the voice loop once the conversation is live (built awake, or woken), for the session's timings.
        self.on_awake: Callable[[dict[str, Any]], None] | None = None
        #: When it was woken (monotonic), ``None`` while dormant or built awake.
        self.woken_at: float | None = None
        #: Whether the shared bridge is wired to this conversation (only once it is live).
        self._bound = False
        #: Woken by the name moments after the user ended the last conversation (v3.12): the talker judges whether
        #: it was for her (their talk may just mention her), and stays quiet unless it plainly was.
        self.after_goodbye = False
        #: Names and terms from memory that speech recognition is told to expect (v3.5, docs/VOICE.md §9).
        self.keyterms = list(keyterms or [])
        self.call_app = call_app
        #: How sure memory is of the call (v3.10): "fresh" (seen on the screen during this hold) or "stale" (seen a
        #: while ago): with stale evidence plain instructions are always for Yuki and only ambiguous words get a
        #: short "was that for me?".
        self.call_evidence = call_evidence
        #: ``(minutes)``: the user asked Yuki to go quiet for a while (``go_quiet``); set by the session.
        self.on_quiet = on_quiet
        self._on_closing = on_closing
        #: When speech recognition was connected (the idle clocks start there, not while connecting).
        self.stt_ready_at: float | None = None
        #: The conversation was started by the wake word and what speech recognition hears begins after the
        #: name (v3.6): its first words are said to Yuki (set by the session).
        self.woken_by_name = False
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
        #: The thinker's step lines (yuki.voice.steplines, v3.14), made with the gate.
        self.steps: Any = None
        self.grounder: Any = None
        self.observer = TurnObserver(
            settings=settings, log=log, memory=memory, session=session, speech_end=speech_end,
            on_state=on_state, tts_provider=self.tts_provider, activity=self._on_activity,
            call_mode=lambda: bool(self.call_app),
        )
        self.observer.on_chat = on_chat
        self.observer.price_turn = self._price_turn
        self.observer.cut_before = lambda: bool(self.woken_by_name and self.observer.user_turns == 0)
        # The names a heard word may be a mishearing of: the user's people and names, never Yuki's own name
        # (v3.6: live 04:46:47 "yucky" was flagged as close to Yuki, the mouth asked about it and held the
        # action; a word like the name is the user calling her, and the wake word's timing keeps the name
        # itself out of what recognition hears).
        self.observer.keyterms = [t for t in keyterm_list(["Yuki", *settings.user_names], self.keyterms,
                                                          settings.voice_keyterms_max)
                                  if t.casefold() != WAKE_NAME.casefold()]
        self.worker: Any = None
        #: The spoken-line check (yuki.voice.speechguard, v3.10), made with the services.
        self.guard: Any = None
        self._stt: Any = None
        self.started_at = time.monotonic()
        self._extra_observers = list(extra_observers or [])
        self.progress_cost_usd = 0.0
        # -- v3.16 -------------------------------------------------------------------------------------------
        #: Which of the talker's words are spoken: ``"marker"`` or ``"guard"`` (``Settings.voice_speech_channel``).
        self.channel = speech_channel(settings)
        #: Every outcome handed to the talker until it is heard (:class:`yuki.voice.narrator.OwedLedger`), made in run.
        self.ledger: Any = None
        #: ``(kind)``: play an earcon (:func:`yuki.voice.echo.tone`) through this conversation's output; set by the
        #: session (a test's stand-in records it). Non-speech only: Yuki never speaks first.
        self.on_tone: Callable[[str], None] | None = None
        #: Loop stalls seen while live, ``(start, end)`` monotonic (the lag monitor's and the watch's own): the idle
        #: clock does not count them (04 F4).
        self._stalls: list[tuple[float, float]] = []
        #: One more idle window from here (the rerun of an unheard turn was silent too, 04 F1).
        self._grace_since: float | None = None
        #: Why and how the conversation ended (content-free, for the ``session stop`` event; 04 F3).
        self.stop_facts: dict[str, Any] = {}
        self.observer.on_audible = self._on_audible
        # -- v3.18 -------------------------------------------------------------------------------------------
        #: Practice memory's lookups for the talker (lessons, learned ways; :mod:`yuki.voice.practice`), made in run.
        self.practice: Any = None
        #: The playbook ids the talker's latest call was shown (a hand's trace carries them as ``injected``).
        self._shown_ids: list[int] = []
        # -- vfix4 -------------------------------------------------------------------------------------------
        #: The last hour read again on the wake (:meth:`_refresh_recent`), until the first call has waited for it.
        self._recent_job: Any = None
        #: Closes that read once the first call has stopped waiting (a later read is dropped).
        self._recent_close: Any = None
        #: The talker's quick memory tool (:class:`yuki.voice.recall.QuickMemory`), made with the services.
        self.quick_memory: Any = None
        # -- vGame (5 Oct) -----------------------------------------------------------------------------------
        #: The full-screen game in front (its name from memory's game voice session, or ``"fullscreen"``), else
        #: ``None``; with a call (fresh evidence) this is focus mode (:attr:`focus`). Set by the session.
        self.game_app: str | None = None
        #: When the wake word heard the name during this conversation (monotonic; the spotter keeps listening while a
        #: conversation runs): a turn the name was heard in is said to Yuki (measured, never words).
        self._names: deque[float] = deque(maxlen=16)
        #: Until when the next turn counts as said to Yuki: the user said the name alone and was answered "Yeah?".
        self._name_carry_until = 0.0
        #: The instant acknowledgement lines in this conversation's voice (:class:`yuki.voice.fastpath.AckVoice`).
        self.ack: Any = None
        #: ``(pcm, kind)``: play a short clip of Yuki's voice now (24 kHz int16 mono) on the conversation's output,
        #: outside the pipeline (the acknowledgement); set by the session.
        self.on_sound: Callable[[bytes, str], None] | None = None
        #: ``()``: the user dismissed Yuki on the fast path: the end cue now (the session's); set by the session.
        self.on_dismissed: Callable[[], None] | None = None
        self.observer.on_quiet_turn = self._quiet_turn

    # -- vGame: the fast path, focus mode, the name heard while talking ---------------------------------------------

    #: After the name alone ("Yuki." answered "Yeah?"), the next turn within this long is said to Yuki (s).
    NAME_CARRY_S = 12.0
    #: A name the wake word heard this long before the turn's start of speech still belongs to it (s): Flux reports a
    #: start of turn ~0.3-1 s into speech, and the spotter hears the name ~0.35-0.7 s after it ends (measured 5 Oct).
    NAME_BEFORE_S = 1.2
    #: In focus mode Yuki's voice is this much quieter (dB): a game's sound and the friends stay on top.
    FOCUS_GAIN_DB = -6.0

    @property
    def _call_focus(self) -> bool:
        """A call with fresh evidence (a stale one may be over: then plain instructions are for Yuki, v3.10)."""
        return bool(self.call_app and self.call_evidence != "stale")

    @property
    def focus(self) -> bool:
        """vGame focus mode: a full-screen game in front or a call on, and the setting on."""
        return bool(getattr(self.settings, "voice_focus_mode", True) and (self._call_focus or self.game_app))

    def set_game(self, app: str | None) -> None:
        """A full-screen game came to the front (``app``) or went (``None``). Thread-safe."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._set_game, app)
        else:
            self.game_app = app

    def _set_game(self, app: str | None) -> None:
        if bool(app) != bool(self.game_app):
            self.log.event("session", state="game_on" if app else "game_off", session=self.session)
        self.game_app = app
        self._apply_gain()
        self._activity.set()

    def _apply_gain(self) -> None:
        output = self.transport.output() if self.transport is not None else None
        if output is not None and hasattr(output, "gain_db"):
            output.gain_db = self.FOCUS_GAIN_DB if self.focus else 0.0

    def name_heard(self, at: float) -> None:
        """The wake word heard the name while this conversation runs (``at``: monotonic). Thread-safe."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._names.append, float(at))

    def _named_turn(self) -> tuple[bool, str]:
        """Whether the open user turn was said to Yuki by measured evidence, and which: the first words after a wake by
        name, the turn right after the name alone, or the wake word heard the name in it."""
        obs = self.observer
        now = time.monotonic()
        if self.woken_by_name and obs.user_turns <= 1:
            return True, "wake"
        if now < self._name_carry_until:
            self._name_carry_until = 0.0
            return True, "after_name"
        start = float(getattr(obs, "_user_started_at", 0.0) or 0.0)
        if any(start - self.NAME_BEFORE_S <= t <= now for t in self._names):
            return True, "spotter"
        return False, ""

    def _busy_for_dismiss(self) -> bool:
        """Something runs that "stop" could mean (a voice task, a scroll, a hand at work)."""
        try:
            return bool(self.bridge.open_voice_jobs() or self._scrolling_line() or getattr(self.bridge, "busy_tools", 0))
        except Exception:  # noqa: BLE001 - when unsure, "stop" is the talker's
            return True

    def _fast_turn(self, text: str, *, final: bool) -> bool:
        """vGame: the fast path on a transcript before the talker (``FastPath``). ``True``: the frame is taken.

        A dismissal (:func:`yuki.voice.fastpath.dismissal`) ends the conversation at once with no model call, already on
        Flux's eager end of turn (~0.15 s before the final words; if they were going on, closing costs nothing: the name
        opens it again). The name alone, said to Yuki, is answered "Yeah?" at once and the next turn counts as said to
        her. A turn said to her by name gets the instant "Mm-hm" and goes on to the talker as before."""
        from yuki.voice.fastpath import bare_name, dismissal

        if self._stop.is_set():
            return self.reason == "user_asked"   # after a dismissal nothing more reaches the talker
        if self.dormant:
            return False
        s = self.settings
        phrase = None
        busy = False
        if getattr(s, "voice_fast_dismiss", True):
            busy = self._busy_for_dismiss()
            phrase = dismissal(text, busy=busy)
        if phrase is not None:
            self._fast_dismiss(phrase, busy, eager=not final, text=text)
            return True
        if not final:
            return False
        named, how = self._named_turn()
        turn = self.observer.turn
        if turn is not None and turn.trigger == "user" and named:
            turn.named = True
        if not named or not getattr(s, "voice_fast_ack", True):
            return False
        if bare_name(text):
            self.observer.fast_handled("name")
            self._name_carry_until = time.monotonic() + self.NAME_CARRY_S
            self._ack("name", how)
            return True
        self._ack("request", how)
        return False

    def _fast_dismiss(self, phrase: str, busy: bool, eager: bool = False, text: str = "") -> None:
        obs = self.observer
        turn = obs.turn
        if eager:
            # the final transcript has not come yet, so its turn is not open: the dismissal opens it now with the eager
            # words (logged as usual, said to Yuki); a final transcript after it only updates them (TurnObserver)
            now = time.monotonic()
            turn = obs._open("user")
            turn.user_text = text
            turn.transcript_at = now
            turn.speech_end, turn.eos_source = obs._end_of_speech(now)
            obs.user_turns += 1
        obs.fast_handled("dismiss")
        now = time.monotonic()
        self.log.event("fast_path", op="dismiss", session=self.session, phrase=phrase, busy=busy, eager=eager,
                       call_mode=bool(self.call_app), game=bool(self.game_app),
                       eos_to_end_ms=round((now - turn.speech_end) * 1000) if turn is not None and turn.speech_end
                       else None)
        if self.on_dismissed is not None:
            try:
                self.on_dismissed()
            except Exception:  # noqa: BLE001 - the cue is a courtesy; the end is not
                pass
        self.log.event("session", state="end_asked", session=self.session, waited_ms=0, fast=True)
        self._set_stop("user_asked")

    def _ack(self, kind: str, how: str) -> None:
        """Play the instant acknowledgement (``"request"``: "Mm-hm.", ``"name"``: "Yeah?") unless Yuki is speaking."""
        obs = self.observer
        turn = obs.turn
        if obs.bot_speaking or obs.speech_pending or (self.after_goodbye and obs.user_turns <= 1):
            return
        sound = self.on_sound
        pcm = self.ack.pcm(kind) if self.ack is not None else None
        now = time.monotonic()
        played = "none"
        try:
            if pcm and sound is not None:
                from yuki.voice.fastpath import scale

                sound(scale(pcm, self.FOCUS_GAIN_DB if self.focus else 0.0), kind)
                played = "voice"
            elif self.on_tone is not None:
                self.on_tone("tick")   # no line in this voice (yet): a soft tick, never silence
                played = "tick"
        except Exception as exc:  # noqa: BLE001 - an acknowledgement never breaks the turn
            self.log.event("fast_path", op="ack_error", error=f"{type(exc).__name__}: {str(exc)[:120]}")
            return
        if played == "none":
            return
        if turn is not None and turn.trigger == "user":
            turn.ack_at = now
        self.log.event("fast_path", op="ack", session=self.session, kind=kind, by=how, played=played,
                       focus=self.focus, eos_to_ack_ms=round((now - turn.speech_end) * 1000)
                       if turn is not None and turn.speech_end else None)

    def _quiet_turn(self, turn: Any) -> None:
        """vGame: the talker chose silence on the user's words. In a game or a call (focus mode) with nothing owed or
        running, the conversation ends at once, silently: talk not said to Yuki is ignored, and what follows is not
        transcribed or judged any more (live, 13 of 37 dismissals were judged as said to the friends and it stayed;
        an accidental wake listened on). The name opens it again in ~0.2 s (the standby)."""
        if not self.focus or not getattr(self.settings, "voice_focus_close_on_quiet", True):
            return
        if self._stop.is_set() or self.dormant:
            return
        now = time.monotonic()
        owed = (self.bridge.open_voice_jobs() + (self.gate.holding if self.gate is not None else 0)
                + int(bool(self._held_pending(now))) + self._owed_pending() + int(self._scrolling_line() is not None))
        if owed:
            self.log.event("session", state="focus_quiet_kept", session=self.session, owed=int(owed))
            return
        self.log.event("session", state="focus_quiet_end", session=self.session, game=bool(self.game_app),
                       call_mode=self._call_focus, turn=getattr(turn, "n", None))
        self._set_stop("not_for_yuki")

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

    def set_call(self, app: str | None, evidence: str | None = None) -> None:
        """A call started (``app``) or ended (``None``) while the conversation runs. Thread-safe."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._set_call, app, evidence)
        else:
            self.call_app, self.call_evidence = app, evidence

    def _set_call(self, app: str | None, evidence: str | None = None) -> None:
        if bool(app) != bool(self.call_app) or (app and evidence != self.call_evidence):
            self.log.event("session", state="call_mode_on" if app else "call_mode_off", session=self.session,
                           **({"evidence": evidence} if app else {}))
        self.call_app, self.call_evidence = app, (evidence if app else None)
        self._apply_gain()   # vGame: quieter in a call
        self._activity.set()

    #: vGame: silence sent to speech recognition once a standby's socket is up (s). Measured 5 Oct (a Flux socket idle
    #: 25-60 s, then 0.6 s of words at once and the rest live, 7 runs each): with no audio since it opened, its first
    #: answer came 0.34-1.17 s after the words (4 of 7 near a second) and the end of the turn 0.62-1.57 s after the
    #: speech ended (median 1.30); with half a second of silence sent when it opened, 0.37-0.48 s and 0.66-1.03 s
    #: (median 0.85). Billed as half a second of audio per standby.
    STT_WARM_S = 0.5

    async def _warm_stt(self) -> None:
        """Send :data:`STT_WARM_S` of silence on the standby's speech recognition socket (never raises)."""
        stt = getattr(self, "_stt", None)
        if stt is None or not getattr(self.settings, "voice_stt_warm", True):
            return
        started = time.monotonic()
        try:
            await stt._transport_send_audio(b"\0\0" * int(16_000 * self.STT_WARM_S))
            self._stt_warmed_at = time.monotonic()
            self.log.event("stt_warm", session=self.session, ms=round((time.monotonic() - started) * 1000))
        except Exception as exc:  # noqa: BLE001 - only a head start
            self.log.event("stt_warm", session=self.session, error=f"{type(exc).__name__}: {str(exc)[:120]}")

    def _stt_connected(self) -> None:
        self.stt_ready_at = time.monotonic()
        self._activity.set()
        if self.on_stt_connected is not None:
            try:
                self.on_stt_connected()
            except Exception:  # noqa: BLE001 - timings only
                pass

    # -- standby (v3.11) -----------------------------------------------------------

    @property
    def ready(self) -> bool:
        """Dormant with speech recognition connected: a wake can go live at once."""
        stt = getattr(self, "_stt", None)
        try:
            connected = bool(stt is not None and stt._transport_is_active())
        except Exception:  # noqa: BLE001
            connected = False
        return bool(self.dormant and self.stt_ready_at is not None and connected and not self._stop.is_set())

    def activate(self, *, listen_from: float | None, woken_by_name: bool = False, call_app: str | None = None,
                 call_evidence: str | None = None, heard_at: float | None = None) -> bool:
        """Go live now (thread-safe): the words said since ``listen_from`` and the live mic go to speech recognition.

        Returns ``False`` when it cannot (not dormant, not connected, stopping): the caller starts one cold.
        """
        loop = self._loop
        if loop is None or loop.is_closed() or not self.ready:
            return False
        self.dormant = False   # decided now, on the caller's thread: a second wake never takes it twice
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._activate(
            listen_from, woken_by_name, call_app, call_evidence, heard_at)))
        return True

    async def _activate(self, listen_from: float | None, woken_by_name: bool, call_app: str | None,
                        call_evidence: str | None, heard_at: float | None) -> None:
        now = time.monotonic()
        self.woken_at = now
        self.woken_by_name = woken_by_name
        self.call_app, self.call_evidence = call_app, (call_evidence if call_app else None)
        # The clocks of a conversation start at the wake, not when it was built.
        self.started_at = now
        self.stt_ready_at = now
        self.observer.last_bot_stop = 0.0
        self._refresh_recent()
        self._bind_bridge()
        self._boost()
        self._apply_gain()   # vGame: quieter in a game or a call
        stream = getattr(self.transport.input(), "go_live", None)
        facts: dict[str, Any] = {}
        if callable(stream):
            facts = await stream(listen_from)
        # The pill says "hearing" the moment their words stream (they are usually mid-sentence right after the name).
        voiced = False
        if self._voice_activity is not None:
            try:
                _since, last = self._voice_activity()
                voiced = bool(last and now - last < 0.6)
            except Exception:  # noqa: BLE001
                voiced = False
        self.observer.set_state("hearing" if voiced else "listening")
        self._awake()
        facts.update(wake_to_live_ms=round((time.monotonic() - heard_at) * 1000) if heard_at else None,
                     voiced=voiced)
        self.log.event("session", state="woken", session=self.session, **facts)
        self._activity.set()

    #: How long the first call of a woken conversation waits for the last hour to be read again (s; measured tens of ms).
    RECENT_WAIT_S = 0.4

    def _refresh_recent(self) -> None:
        """vfix4: read the last hour with the user again now that the conversation is woken (it was built dormant,
        maybe long before: live 4 Oct the standby was built two seconds after the last conversation ended and woken
        33 s later). Off the loop, on the voice pool. The first call waits for it at most :attr:`RECENT_WAIT_S`
        (``_Hooks._recent``, alongside the desktop summary and the lookup); the Earlier block takes it only when it lands
        by then and the read worked, else it keeps the view read at the build. Content-free log."""
        memory, mouth_memory = self.memory, self.mouth_memory
        if memory is None or mouth_memory is None:
            return
        import concurrent.futures
        import threading

        from yuki.voice.workers import shared

        job: concurrent.futures.Future[Any] = concurrent.futures.Future()
        # Open until the first call has stopped waiting for it (``_Hooks._recent`` closes it): a read that lands
        # later is dropped, so the system prompt never changes in the middle of the conversation (review F4).
        gate = {"open": True}
        lock = threading.Lock()

        def close() -> None:
            with lock:
                gate["open"] = False

        self._recent_job, self._recent_close = job, close
        log, session, started = self.log, self.session, time.monotonic()

        def work() -> None:
            try:
                from yuki.voice.recall import fetch_recent

                failed = object()
                text = fetch_recent(memory, failed=failed)
                ok = text is not failed   # a failed read keeps the view read at the build (review F4)
                with lock:
                    before = mouth_memory.recent
                    used = ok and gate["open"]
                    if used:
                        mouth_memory.recent = text
                if log is not None:
                    log.event("recent", op="woken", session=session, ok=ok, used=used,
                              chars=len(text or "") if ok else 0,
                              changed=used and (text or None) != (before or None),
                              ms=round((time.monotonic() - started) * 1000))
            finally:
                job.set_result(None)

        shared().submit(work)

    def _bind_bridge(self) -> None:
        """Wire the shared bridge (and the talker's hands) to this conversation: once it is live, never while dormant."""
        if self._bound:
            return
        self._bound = True
        llm, context, summarizer = self._llm, self._llm_context, getattr(self, "_summarizer", None)
        bridge = self.bridge
        bridge.attach(self._loop)
        # Whether the mouth's reply now making a tool call said anything aloud (a stream fact).
        bridge.spoke_in_reply = lambda: bool(getattr(llm, "_yuki_call", None) is not None
                                             and llm._yuki_call.first_text is not None)
        tts = getattr(self, "_tts", None)
        if tts is not None and hasattr(tts, "_yuki_stale") and callable(getattr(bridge, "waiting_reply_stale", None)):
            steps = self.steps

            def stale(text: str = "") -> bool:
                # Both asked every time (each answers once per line it sent): a "still on it" reply whose job has
                # finished, or a step line whose step has returned, is never heard (v3.14b). The step line is known
                # by its own words, so no other reply is ever taken for it.
                waiting = bool(bridge.waiting_reply_stale())
                step = bool(steps.line_stale(text)) if steps is not None else False
                return waiting or step

            tts._yuki_stale = stale
        # The say_first words spoken for a silent hand-over belong before its call, too.
        bridge.mark_spoken = lambda call_id: llm._yuki_spoke.add(str(call_id))
        bridge.said_early = lambda call_id: str(call_id) in llm._yuki_early
        # Whether the mouth run now calling a tool was the user's turn (not a result's).
        bridge.user_turn = lambda: bool(getattr(llm, "_yuki_call", None) is None or llm._yuki_call.user_turn)
        # The mouth run answers the first words after the wake word: they were said to Yuki (v3.6).
        bridge.named_turn = lambda: _Hooks(self).named()
        bridge.call_mode = lambda: bool(self.call_app)
        bridge.call_evidence = lambda: self.call_evidence if self.call_app else None
        bridge.quiet_requested = self.on_quiet
        # The summary is a user-role message nobody said: never "what the user said" to the thinker;
        # nor, in a call, what the mouth judged was said to the people on it (it stayed quiet).
        bridge.not_heard = lambda message: (message is getattr(summarizer, "_summary_message", None)
                                            or (context is not None and id(message) in quiet_answered(
                                                context.get_messages())))
        bridge.on_jobs_changed = self._activity.set
        bridge.announce = self._announce
        bridge.gate = self.gate
        bridge.ledger = self.ledger   # v3.16: every outcome handed to the talker stays owed until it is heard
        bridge.steps = self.steps
        bridge.progress_answerer = self._progress_answer
        bridge.on_remembered = self._refresh_rules
        bridge.end_requested = lambda quick=False: asyncio.ensure_future(self._end_after_speech(quick=quick))
        # v3.9: the bridge binds each job to the user turn it answers, paces its "still on it" line by real
        # silence, and says a job's own finishing line by code when no tell_yuki call carries it.
        bridge.user_turns = lambda: self.observer.user_turns
        bridge.quiet_for = self._quiet_for
        bridge.speak = self._speak
        # v3.17: a short or verified result said by code (C1), its sentences fetched by the voice ahead (C2).
        bridge.speak_result = self._speak_result
        fetch = getattr(tts, "prefetch", None) if tts is not None else None
        bridge.prefetch_speech = fetch if callable(fetch) else None
        bridge.more_held = self.more_held   # H2: the talker's own held words, for more_of_result
        bridge.in_exchange = lambda: bool(self.observer.bot_speaking or (
            self.observer.last_bot_stop and time.monotonic() - self.observer.last_bot_stop < self.EXCHANGE_LIVE_S))
        # v3.18: the thinker's request pack (Agent.build_pack) built when the user's turn ends, when the agent takes one
        prefetch = getattr(bridge, "prefetch_pack", None)
        self.observer.on_final_words = prefetch if callable(prefetch) else None
        hands = getattr(bridge, "hands", None)
        if hands is not None:
            hands.on_action = self.observer.hand_acted
            hands.on_step = lambda name, what, ok, report, ms: self.observer.chat({
                "type": "hand_step", "turn_id": self.observer.turn_id(), "name": name,
                "args": strip_tags(what) if isinstance(what, str) else what, "ok": ok, "result": report, "ms": ms})
            # v3.18: practice memory sees the hands (their run traces) and the hands run the learned ways offered
            hands.memory = self.memory if getattr(self.settings, "voice_hand_traces", True) else None
            hands.words = lambda: list(self.observer.recent_words)
            hands.turn_key = self.observer.turn_id
            hands.shown = lambda: list(self._shown_ids)
            hands.user_turn = lambda: self.observer.user_turns
            hands.practice = self.practice

    def _awake(self) -> None:
        """What a live conversation starts with (built awake, or woken from standby): all in the background."""
        s = self.settings
        warm_brain = getattr(self.bridge, "warm_brain", None)
        if callable(warm_brain):
            try:
                warm_brain(float(getattr(s, "voice_cache_refresh_s", 3000.0)))  # the thinker's cache, if gone cold
            except TypeError:  # a stand-in bridge without the argument
                warm_brain()
        hands = getattr(self.bridge, "hands", None)
        if s.voice_hands and hands is not None and hasattr(hands, "prefetch_desk"):
            hands.prefetch_desk()   # the first reply's desktop block, read while they talk
        tts = getattr(self, "_tts", None)
        warm = getattr(tts, "warm_now", None) or getattr(tts, "_maybe_warm", None)
        if callable(warm) and self.woken_at is not None:   # built awake, the voice warms itself as it starts
            try:
                warm()   # v3.16: the voice's connections at every wake (live 2 Oct its first byte timed out 8 s in)
            except Exception:  # noqa: BLE001 - only a head start
                pass
        if self.woken_at is not None and self._loop is not None:
            # The talker's connection is cold after a quiet spell (its client lets idle connections go): one cached
            # one-token call opens it while they are still talking, and reads the cache (keeping it warm).
            asyncio.ensure_future(self._warm())
        if self.on_awake is not None:
            try:
                self.on_awake({"woken": self.woken_at is not None})
            except Exception:  # noqa: BLE001 - timings only
                pass

    async def _dormant_upkeep(self) -> bool:
        """While dormant: keep speech recognition connected and the caches warm. ``False``: this standby is lost.

        Nothing is sent to speech recognition (it gets no audio at all, so nothing is heard or billed); an idle
        socket stays open on its own (measured: Flux kept one with no audio open for 15 min). A dropped socket ends
        this standby: the session builds a fresh one in the background, never on a wake's path.
        """
        s = self.settings
        refresh_s = float(getattr(s, "voice_cache_refresh_s", 3000.0))
        stt = getattr(self, "_stt", None)
        lost_since: float | None = None
        last_warm = time.monotonic()
        while self.dormant and not self._stop.is_set():
            self._activity.clear()
            try:
                await asyncio.wait_for(self._activity.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass
            if not self.dormant or self._stop.is_set():
                return True
            try:
                connected = bool(stt is None or stt._transport_is_active())
            except Exception:  # noqa: BLE001
                connected = False
            now = time.monotonic()
            if not connected and self.stt_ready_at is not None:
                lost_since = lost_since or now
                if now - lost_since >= 3.0:
                    self.log.event("session", state="standby_lost", session=self.session, why="stt_closed",
                                   dormant_s=round(now - self.started_at))
                    return False
            else:
                lost_since = None
            if now - last_warm >= refresh_s and user_present():
                # One cached call each an hour keeps both halves warm (their stable layers have a 1 h cache).
                last_warm = now
                asyncio.ensure_future(self._warm())
                warm_brain = getattr(self.bridge, "warm_brain", None)
                if callable(warm_brain):
                    try:
                        warm_brain(refresh_s)
                    except TypeError:
                        warm_brain()
        return True

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

        async def on_connected(_service: Any) -> None:
            if self.dormant:
                self._stt_connected()   # a standby is ready once its socket is up (no audio flows to say so)
                asyncio.ensure_future(self._warm_stt())   # vGame: its first words after the wake come back sooner

        stt.add_event_handler("on_connected", on_connected)
        stt.add_event_handler("on_update", on_update)
        stt.add_event_handler("on_eager_end_of_turn", on_eager)
        stt.add_event_handler("on_end_of_turn", on_eot)
        # Settings → AI: the talker's calls go to the client it wraps while "Voice replies" is on Claude (as before),
        # else to the chosen provider and model (yuki.agent.providers.client.AsyncRoleClient)
        from yuki.agent.providers.client import role_async_client

        self._client = role_async_client("talker", bedrock_client(s.aws_region, getattr(s, "model_backend", None)), s,
                                         log=lambda event, **fields: self.log.event(event, **fields))
        llm = _layered_service(AnthropicLLMService, self.mouth_memory, _Hooks(self))(
            api_key="unused",  # the client passed in carries the auth (Bedrock token or API key)
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
        llm.register_function("go_quiet", self.bridge.go_quiet)
        hands = getattr(self.bridge, "hands", None)
        if hands is not None:
            self.observer.hands = hands   # the hands' own callbacks are bound with the bridge (_bind_bridge)
        if s.voice_hands and hands is not None:
            # The mouth's own one-step hands (§5.6). Not cancelled by a barge-in: the action
            # may already have happened, and its report must reach the context either way.
            for tool in HAND_TOOLS:
                llm.register_function(tool["name"], self._hand_or_hold, cancel_on_interruption=False,
                                      timeout_secs=30)
        if self.memory is not None:
            # vfix4: the talker's quick look into memory (its to-do list, today's talk, a search) and its to-do adds;
            # not cancelled by a barge-in (an add may already be saved, and its report must reach the context).
            from yuki.voice.recall import QuickMemory

            self.quick_memory = QuickMemory(self.memory, log=self.log, session=self.session,
                                            alone=lambda call_id: call_id in getattr(self._llm, "_yuki_solo", ()),
                                            mark_spoken=lambda call_id: getattr(self._llm, "_yuki_spoke", set()).add(
                                                str(call_id)))
            llm.register_function(QUICK_MEMORY["name"], self._quick_memory_call, cancel_on_interruption=False,
                                  timeout_secs=20)
        provider = self.tts_provider
        voice = (s.voice_tts_voice if provider == s.voice_tts_provider else "") or DEFAULT_VOICES.get(provider, "")
        if provider == "gemini":
            from yuki.voice.gemini_tts import service_class

            log = self.log
            session = self.session

            def tts_log(event: str, **facts: Any) -> None:
                if log is not None:
                    log.event(event, session=session, **facts)

            fallback_key = get_key(DEEPGRAM) if s.voice_tts_fallback == "deepgram" else None
            cache = None
            if getattr(s, "voice_tts_cache", True):
                # v3.14: short lines she says often come back from disk at once (yuki.voice.ttscache)
                from yuki.voice.ttscache import TTSCache

                cache = TTSCache(Path(s.voice_tts_cache_dir) if getattr(s, "voice_tts_cache_dir", "") else None)
                self._tts_cache = cache
                from yuki.voice.workers import shared as _workers

                _workers().submit(cache.preload)   # its index read off the loop, before the first reply
            tts = _clause_tts(service_class())(
                api_key=get_key(GOOGLE) or "", model=s.voice_gemini_model, voice=s.voice_gemini_voice,
                style=s.voice_gemini_style, first_audio_timeout_s=s.voice_gemini_first_audio_s,
                fallback_key=fallback_key, fallback_voice=DEFAULT_VOICES["deepgram"], log=tts_log,
                ssl=shared_ssl_context(), cache=cache,
                # v3.16: hedge a slow first byte with Aura, a per-piece circuit breaker instead of a sticky switch
                hedge_s=float(getattr(s, "voice_tts_hedge_s", 1.8) or 0.0),
                breaker=(int(getattr(s, "voice_tts_breaker_failures", 2)),
                         float(getattr(s, "voice_tts_breaker_window_s", 60.0)),
                         float(getattr(s, "voice_tts_breaker_open_s", 60.0))))
            tts._yuki_on_silent = self.observer.tts_silent   # a reply that played nothing: its breath, no error
        elif provider == "deepgram":
            from pipecat.services.deepgram.tts import DeepgramTTSService

            tts = _clause_tts(DeepgramTTSService, strip_marks=True)(
                api_key=get_key(DEEPGRAM) or "", settings=DeepgramTTSService.Settings(voice=voice))
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
            tts = _clause_tts(ElevenLabsTTSService, strip_marks=True)(
                api_key=get_key(ELEVENLABS) or "",
                settings=ElevenLabsTTSService.Settings(voice=voice, model=s.voice_tts_model, **tuning))
        self._stt = stt
        if getattr(s, "voice_fast_ack", True):
            # vGame: the instant acknowledgement in this voice, from disk or rendered once, off the voice loop
            from yuki.voice.fastpath import AckVoice
            from yuki.voice.ttscache import TTSCache
            from yuki.voice.workers import shared as _ack_workers

            ack_cache = TTSCache(Path(s.voice_tts_cache_dir) if getattr(s, "voice_tts_cache_dir", "") else None)
            self.ack = AckVoice(provider=provider, voice=voice,
                                model=s.voice_tts_model if provider == "elevenlabs" else "", cache=ack_cache,
                                log=self.log)
            _ack_workers().submit(self.ack.load)
        if getattr(s, "voice_speech_guard", True) and callable(getattr(tts, "set_guard", None)):
            # Every line passes one cheap check before it is heard (v3.10): only what a person says to the user.
            from yuki.voice.speechguard import SpeechGuard

            guard = SpeechGuard(self._client, s.voice_mouth_model, log=self.log, session=self.session,
                                timeout_s=float(getattr(s, "voice_speech_guard_timeout_s", 2.5)),
                                context=lambda: {"user": self.observer.last_user_text},
                                shadow=self.channel == "marker")   # v3.16: with the marker channel it only measures
            guard.on_drop = self.observer.guarded
            self.guard = guard
            self.observer.guard = guard
            tts.set_guard(guard)
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
        self._tts = tts
        self._llm_context: Any = None
        hand_tools = HAND_TOOLS if (self.settings.voice_hands and getattr(self.bridge, "hands", None)) else []
        if not (getattr(self.settings, "voice_skills", True) and getattr(self.settings, "voice_practice", True)
                and self.memory is not None):
            # v3.18: run_skill only where learned ways can be offered (fixed for the conversation: the tools head the
            # cached prefix)
            hand_tools = [t for t in hand_tools if t["name"] != "run_skill"]
        tools = ToolsSchema(standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["properties"],
                           required=t["required"])
            for t in (TELL_YUKI, CHECK_PROGRESS, MORE_OF_RESULT, STAY_QUIET, END_CONVERSATION, GO_QUIET,
                      *([QUICK_MEMORY] if self.memory is not None else []), *hand_tools)
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
        self._summarizer = getattr(assistant_agg, "_summarizer", None)
        hold = hold_until_connected(lambda: bool(getattr(stt, "_transport_is_active", lambda: True)()), self.log,
                                    on_connected=self._stt_connected)
        output = self.transport.output()
        self.observer.output_processor = output
        # vGame: the fast path sits between speech recognition and the talker's context ("go away" ends here, the name
        # alone is answered here, a request by name gets its instant "Mm-hm" here)
        fast = fast_path_processor(self)
        pipeline = Pipeline([self.transport.input(), hold, stt, fast, user_agg, llm, tts, output, assistant_agg])
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
    #: stretch of it has gone on this long without speech recognition hearing a turn (noise). v3.14: 6 -> 2 s. Flux
    #: reports a start of turn ~0.5-1 s into speech (docs/VOICE.md §6.4), so sound going on longer with no turn is a
    #: video or the room; on the live logs of 27-29 Sept this held ready results 22.6 s in all (3-4 s each).
    SOUND_RECENT_S = 0.35
    SOUND_MAX_STRETCH_S = 2.0

    def _gate_state(self) -> dict[str, Any]:
        state = self.observer.live_state()
        state["busy_tools"] = self.bridge.busy_tools
        # the user is in a voice call (the attention gate; v3.5), or (vGame) a full-screen game is in front
        state["call_mode"] = bool(self.call_app or self.focus)
        state["jobs_open"] = self.bridge.open_voice_jobs()
        if self._voice_activity is not None:
            since, last = self._voice_activity()
            now = time.monotonic()
            state["user_sound"] = bool(last and now - last < self.SOUND_RECENT_S
                                       and (last - since) < self.SOUND_MAX_STRETCH_S)
        return state

    def _step_state(self) -> dict[str, Any]:
        """What the step-line voice needs to know now (v3.14): who is speaking, and how long nobody has."""
        obs = self.observer
        gate = self._gate_state()
        return {"user_speaking": obs.user_speaking, "user_sound": bool(gate.get("user_sound")),
                "bot_busy": bool(obs.bot_speaking or obs.generating or obs.speech_pending or obs.owes_reply
                                 or self.bridge.busy_tools),
                "call_mode": bool(self.call_app or self.focus), "quiet_for": self._quiet_for()}   # vGame: no steps

    def _price_turn(self, usage: dict[str, int]) -> float | None:
        """One reply turn's cost (the observer's): as before on Claude; after Settings → AI moved the talker, each
        reply stream as the model that answered it (yuki.agent.providers.client.price_talker_turn)."""
        from yuki.agent.providers.client import price_talker_turn

        return price_talker_turn(self.settings, getattr(self, "_client", None), usage)

    async def _progress_answer(self, log_text: str, question: str) -> tuple[str | None, dict[str, Any]]:
        from yuki.agent.providers.client import talker_price_model
        from yuki.voice.progress import answer_progress

        text, facts = await answer_progress(self._client, self.settings.voice_mouth_model, log_text, question)
        cost = self.settings.estimate_cost(talker_price_model(self.settings, getattr(self, "_client", None)), {
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
        use_shared_tls()  # the context itself was built off this loop (the import thread, or the prewarm)
        import threading

        s = self.settings
        self._loop = asyncio.get_running_loop()
        # v3.14: every thread the loop's work runs on exists before anything is live (starting one from the loop
        # stalled it 0.4-3.4 s under a busy interpreter lock; yuki.voice.workers).
        from yuki.voice.workers import prestart_default_executor

        executor_ms = await prestart_default_executor(self._loop)
        self.log.event("session", state="executor", session=self.session, ms=round(executor_ms))
        self.gate = NarratorGate(self._gate_state, settle_s=s.voice_settle_s, settle_max_s=s.voice_settle_max_s,
                                 cooldown_s=s.voice_cooldown_s, stale_turns=s.voice_stale_turns, log=self.log,
                                 final_gap_s=s.voice_final_gap_s, progress_pause_s=s.voice_progress_pause_s)
        from yuki.voice.narrator import OwedLedger

        self.ledger = OwedLedger(stale_turns=s.voice_stale_turns, log=self.log)
        if getattr(s, "voice_step_lines", True):
            # v3.14: the thinker's step and say lines, said by code in Yuki's one voice when a pause would be noticed.
            from yuki.voice.steplines import StepLines

            self.steps = StepLines(state=self._step_state, speak=self._speak,
                                   on_said=getattr(self.bridge, "step_said", None), log=self.log,
                                   silence_s=float(getattr(s, "voice_step_silence_s", 2.5)),
                                   after_s=float(getattr(s, "voice_step_after_s", 0.8)),
                                   long_silence_s=float(getattr(s, "voice_step_long_silence_s", 5.0)))
        if s.voice_grounding and self.memory is not None:
            from yuki.voice.grounding import Grounder

            self.grounder = Grounder(self.memory, facts=s.voice_grounding_facts, log=self.log,
                                     source=str(getattr(s, "voice_grounding_source", "relevant") or "relevant"))
            self.grounder.warm()
            self.observer.grounder = self.grounder
        if getattr(s, "voice_practice", True) and self.memory is not None:
            # v3.18: practice memory for the talker (lessons from its mistakes, learned ways its hands can run)
            from yuki.voice.practice import LESSONS_CHARS, SKILL_MIN_CONFIDENCE, SKILL_MIN_COSINE, PracticeLookup

            hands = getattr(self.bridge, "hands", None)
            self.practice = PracticeLookup(
                self.memory, apps=self._apps_in_play, log=self.log,
                min_cosine=float(getattr(s, "voice_skill_min_cosine", SKILL_MIN_COSINE)),
                min_confidence=float(getattr(s, "voice_skill_min_confidence", SKILL_MIN_CONFIDENCE)),
                lessons_chars=int(getattr(s, "voice_lessons_chars", LESSONS_CHARS)),
                skills=bool(getattr(s, "voice_skills", True) and s.voice_hands and hands is not None))
            self.practice.warm()
            self.observer.practice = self.practice
        self.observer.idle_state = lambda: "working" if self.bridge.open_voice_jobs() else "listening"
        runner = WorkerRunner(handle_sigint=False, check_dangling_tasks=False)
        worker = self._build()
        # While Deepgram connects (2-3 s), off the loop; done long before the first reply. Started
        # after the services are built: they import anthropic, and importing it on two threads at
        # once raised an import deadlock (a stand-in-brain run, where nothing had imported it yet).
        threading.Thread(target=_preimport, name="yuki-voice-preimport", daemon=True).start()
        if not self.dormant:
            self._bind_bridge()   # a dormant one binds when woken: it must never take the bridge from a live one
        await runner.add_workers(worker)
        if not self.dormant:
            self.observer.set_state("listening")
            self._awake()   # the thinker's cache, the first desktop block (v3.6, v3.7), in the background
            self._boost()
            self._apply_gain()   # vGame
        self.gate.start()
        warm = asyncio.create_task(self._warm())
        watch = asyncio.create_task(self._watch(runner))
        lag = asyncio.create_task(self._lag_monitor())
        replies = asyncio.create_task(self._reply_watch())
        names = asyncio.create_task(self._screen_terms_loop())
        self._scroll_hold(True)
        try:
            await runner.run()
        finally:
            self._scroll_hold(False)
            self._cancel_route()
            self._unboost()
            self._log_audio_qos()
            watch.cancel()
            warm.cancel()
            lag.cancel()
            replies.cancel()
            names.cancel()
            if self.guard is not None:
                self.guard.cancel()
                summary = self.guard.summary()
                from yuki.agent.providers.client import talker_price_model

                cost = self.settings.estimate_cost(talker_price_model(self.settings, getattr(self, "_client", None)), {
                    "input_tokens": summary.get("input_tokens") or 0, "output_tokens": summary.get("output_tokens") or 0,
                    "cache_read_tokens": 0, "cache_write_tokens": 0})
                self.log.event("speech_guard", op="summary", session=self.session, cost_usd=round(cost or 0.0, 6),
                               **summary)
            await self.gate.stop()
            if self.steps is not None:
                await self.steps.stop()
            self.observer.close()
            if self._bound:
                self.bridge.closing = True
                self.bridge.attach(None)
                self.bridge.announce = None
                self.bridge.ledger = None
            if self.ledger is not None and (self.ledger.items or self.ledger.stats["owed"]):
                self.log.event("owed", op="summary", session=self.session, **self.ledger.summary())
            if self.grounder is not None:
                unloaded = self.grounder.release(busy=self.bridge.brain_busy())
                timings = sorted(self.grounder.timings)
                self.log.event("grounding", op="summary", lookups=len(timings), unloaded=unloaded,
                               p50_ms=round(timings[len(timings) // 2], 1) if timings else None,
                               max_ms=round(timings[-1], 1) if timings else None)
            if self.practice is not None:
                self.practice.release()
                timings = sorted(self.practice.timings)
                hands = getattr(self.bridge, "hands", None)
                self.log.event("practice", op="summary", lookups=len(timings),
                               p50_ms=round(timings[len(timings) // 2], 1) if timings else None,
                               max_ms=round(timings[-1], 1) if timings else None,
                               traces=getattr(hands, "traces_queued", None))
                if self._bound and hands is not None and getattr(hands, "practice", None) is self.practice:
                    hands.practice = None
            self.log.event("narrator", op="summary", **self.gate.stats)
            cache = getattr(self, "_tts_cache", None)
            if cache is not None:
                self.log.event("tts_cache", op="summary", **cache.stats)   # counts only (v3.14)
                from yuki.voice.workers import shared as _workers

                _workers().submit(cache.flush)   # approvals not yet written with a put, off the loop
        if self.observer.fatal and self.reason == "running":
            self.reason = "error"
        return self.reason

    #: How often the names on the screen in front are looked at again (s): a cheap look at the window list,
    #: and one bounded window read only when the window in front, its title or the action log changed.
    SCREEN_TERMS_EVERY_S = 1.5
    #: Speech recognition's keyterms are at most this many in all (the memory names, then the screen's).
    KEYTERMS_TOTAL_MAX = 60

    async def _screen_terms_loop(self) -> None:
        """Keep the names on the screen in front as recognition's keyterms and the talker's (v3.10).

        Live 2026-09-28 "open the res chat" with WhatsApp's chat list in front was heard as "address. chart". The
        names in front are what the user says next; Flux takes new keyterms mid-stream (a Configure message).
        """
        hands = getattr(self.bridge, "hands", None)
        cap = int(getattr(self.settings, "voice_screen_terms", 30) or 0)
        if hands is None or cap <= 0 or not hasattr(hands, "refresh_screen_names"):
            return
        loop = asyncio.get_running_loop()

        def changed(names: list[str]) -> None:
            try:
                loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._apply_screen_terms(names[:cap])))
            except RuntimeError:  # the loop closed in between
                pass

        memory = self.memory
        cache: dict[str, tuple[float, list[str]]] = {}

        def titles(process: str) -> list[str]:
            """The app's recent window titles from memory's timeline (cached a minute; bounded; never raises)."""
            hit = cache.get(process)
            if hit is not None and time.monotonic() - hit[0] < 60.0:
                return hit[1]
            got: list[str] = []
            if memory is not None and hasattr(memory, "call"):
                try:
                    got = list(memory.call("recent titles", lambda c: c.recent_titles(process, hours=12.0, limit=12),
                                           timeout_s=1.0) or [])
                except Exception:  # noqa: BLE001
                    got = []
            cache[process] = (time.monotonic(), got)
            return got

        try:
            while not self._stop.is_set():
                if not self.dormant:   # no window reads while waiting for the name
                    try:
                        hands.refresh_screen_names(changed, titles=titles)
                    except TypeError:  # a stand-in without the keyword
                        hands.refresh_screen_names(changed)
                await asyncio.sleep(self.SCREEN_TERMS_EVERY_S)
        except asyncio.CancelledError:
            return

    async def _apply_screen_terms(self, names: list[str]) -> None:
        """New names on the screen: the Speech line's near names and recognition's keyterms (memory names first)."""
        self.observer.screen_terms = list(names)
        s = self.settings
        memory_terms = keyterm_list(["Yuki", *s.user_names], self.keyterms, s.voice_keyterms_max)
        terms = keyterm_list(memory_terms, list(names), self.KEYTERMS_TOTAL_MAX)
        stt = getattr(self, "_stt", None)
        if stt is None or self.worker is None or terms == getattr(self, "_terms_sent", None):
            return
        try:
            from pipecat.frames.frames import STTUpdateSettingsFrame
            from pipecat.services.deepgram.flux.stt import DeepgramFluxSTTService

            await self.worker.queue_frame(STTUpdateSettingsFrame(
                delta=DeepgramFluxSTTService.Settings(keyterm=terms), service=stt))
            self._terms_sent = terms
            self.log.event("keyterms", op="screen", terms=len(terms), screen=len(names))
        except Exception as exc:  # noqa: BLE001 - recognition works without them
            self.log.event("keyterms", op="screen", error=type(exc).__name__)

    def _boost(self) -> None:
        """While the conversation is live, the process runs above normal priority (v3.14b, yuki.voice.priority), out of
        Windows' power throttling, with the voice loop's thread in the MMCSS "Audio" task (D7, v3.17, yuki.voice.qos).
        Called on the voice loop's own thread (MMCSS is per thread)."""
        if getattr(self, "_boosted", False):
            return
        self._boosted = True
        if getattr(self.settings, "voice_priority_boost", True):
            from yuki.voice import priority

            self._prioritised = True
            self.log.event("priority", **priority.boost())
        if getattr(self.settings, "voice_qos", True):
            from yuki.voice import qos

            self._loop_qos = qos.ThreadQos()
            self.log.event("qos", **qos.process_high_qos())
            self.log.event("qos", op="loop", **self._loop_qos.apply(mmcss=bool(getattr(self.settings, "voice_mmcss",
                                                                                      True))))

    def _unboost(self) -> None:
        if not getattr(self, "_boosted", False):
            return
        self._boosted = False
        if getattr(self, "_prioritised", False):
            from yuki.voice import priority

            self._prioritised = False
            self.log.event("priority", **priority.restore())
        handle = getattr(self, "_loop_qos", None)
        if handle is not None:
            from yuki.voice import qos

            self._loop_qos = None
            self.log.event("qos", op="loop_end", **handle.revert())
            self.log.event("qos", **qos.process_default_qos())

    def _log_audio_qos(self) -> None:
        """What D7 did for the speaker's writer and the microphone's callback threads (content-free)."""
        try:
            out = self.transport.output() if self.transport is not None else None
            mic = getattr(self.transport.input(), "_mic", None) if self.transport is not None else None
        except Exception:  # noqa: BLE001
            return
        speaker, heard = getattr(out, "qos_facts", None), getattr(mic, "qos_facts", None)
        if speaker or heard:
            self.log.event("qos", op="audio", speaker=speaker, mic=heard)
        tts = getattr(self, "_tts", None)
        stats = getattr(tts, "stats", None)
        if isinstance(stats, dict) and stats.get("prefetched"):
            self.log.event("tts_prefetch", op="summary", prefetched=stats.get("prefetched"),
                           used=stats.get("prefetch_used"), wasted=stats.get("prefetch_wasted"))

    async def _lag_monitor(self, every_s: float = 0.05, report_ms: float = 100.0) -> None:
        """How late the voice loop wakes up (a blocked loop delays every stage). Logs stalls and a summary."""
        lags: list[float] = []
        import threading

        state = {"tick": time.monotonic(), "stop": False, "stacks": []}
        loop_thread = threading.get_ident()
        watchdog = threading.Thread(target=self._stall_watch, args=(state, loop_thread), name="yuki-voice-watch",
                                    daemon=True)
        watchdog.start()
        from yuki.voice import gcwatch

        gcwatch.watch()   # long garbage collections are recorded from here on (v3.14)
        stall: Any = None
        try:
            while True:
                if self.dormant:   # nothing runs on the loop while dormant: look once a second, measure nothing
                    await asyncio.sleep(1.0)
                    state["tick"] = time.monotonic() + 1.0
                    continue
                if stall is None:
                    # v3.14b: every thread's stack during a stall (a native dump) and the CPU each used over it
                    from yuki.voice.stallwatch import StallWatch

                    try:
                        stall = StallWatch(folder=getattr(self.log, "log_dir", None))
                    except Exception:  # noqa: BLE001 - diagnostics never break the voice
                        stall = False
                if stall:
                    stall.tick()
                started = time.monotonic()
                await asyncio.sleep(every_s)
                state["tick"] = time.monotonic()
                late = (state["tick"] - started - every_s) * 1000
                lags.append(late)
                if late >= report_ms:
                    self._note_stall(started + every_s, state["tick"])   # the idle clock does not count it (F4)
                    stacks, state["stacks"] = state["stacks"], []
                    from yuki.voice.gcwatch import pauses_between

                    # v3.14: a garbage collection that overlapped the stall (it holds the interpreter lock)
                    gcs = pauses_between(started, state["tick"])
                    during = stall.report(started, state["tick"]) if stall else {}
                    self.log.event("loop_lag", ms=round(late), **({"where": stacks} if stacks else {}),
                                   **({"gc": gcs} if gcs else {}), **during)
        except asyncio.CancelledError:
            state["stop"] = True
            if stall:
                stall.stop()
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

        Settings → AI: a talker moved to a service with no prompt cache at all gets no warm-up (it would only spend
        a call, and on a free model a share of its daily limit).
        """
        started = time.monotonic()
        caching = getattr(self._client, "prompt_caching", None)
        if callable(caching) and caching() == "none":
            self.log.event("warm", note="skipped: the chosen voice model's service keeps no prompt cache")
            return
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

    def _held(self, name: str, call_id: str | None = None) -> bool:
        """Whether this call is held: recognition was unsure of the latest words and the mouth spoke with it."""
        from yuki.voice.hands import AUDIO_HANDS

        decided = getattr(getattr(self, "_llm", None), "_yuki_hold", None) or {}
        if call_id is not None and str(call_id) in decided:
            return decided[str(call_id)]  # decided once, when the reply ended (its words went with that decision)
        call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
        spoke = call is not None and call.first_text is not None and call.user_turn
        return bool(spoke and self.observer.heard and not getattr(self.observer, "heard_plain", False)
                    and name not in AUDIO_HANDS)

    async def _hold(self, params: Any) -> None:
        self.log.event("hands" if params.function_name != "tell_yuki" else "bridge", op=params.function_name,
                       held="asked_first")
        self.bridge.held_at = time.monotonic()
        self.bridge.held_call = (params.function_name, dict(params.arguments or {}), params.context)
        self.bridge._held_refused = False
        # The reply's words were held with it and never said (v3.6): the user heard nothing yet, so nothing
        # was promised. The mouth now asks plainly, or acts if the words were clear after all.
        await params.result_callback({"held": (
            "Not done: nothing was done, so never say it is done, open or playing. The words you wrote with it were "
            "not said either: speech recognition was unsure of their "
            "words (the Speech line) and you wrote words and this call together. If something that matters "
            "may be misheard, ask them now in a few words, the question alone, with no call and no promise. "
            "If their words were clear after all, call it again now, alone, with its say_first.")})

    async def _tell_or_hold(self, params: Any) -> None:
        """tell_yuki, held like a hand when the mouth asked and handed over in one reply on doubtful words."""
        if self._held("tell_yuki", params.tool_call_id):
            self.bridge.drop_early(str(params.tool_call_id))
            await self._hold(params)
            return
        await self.bridge.tell_yuki(params)

    def _early_handoff(self, call_id: str, screen: bool = False) -> None:
        """Start the thinker on the user's words the moment the mouth's reply begins a hand-off (v3.6).

        The user asked for the two halves to run in parallel ("the thinker should also get the same
        request and then run right"). The mouth is the one that decides whether a turn is for the thinker
        (chit-chat and one-step hands cost no thinker run); its decision is the first thing its reply
        streams, the tell_yuki block, about 0.6-0.9 s before the call is written out (say_first, request,
        context). Not on a turn whose words recognition was unsure of (the call may be held until they
        answer), and not while a voice task is open (a follow-up routes by that task's state once the call
        is complete). Exactly one side acts: from here the thinker owns the request, and the talker's window
        hands wait while it runs (``VoiceBridge.desk_job``).
        """
        adopt = getattr(self.bridge, "adopt_routed", None)
        if callable(adopt) and adopt(str(call_id)):
            self.log.event("router", op="adopted", call=call_id)   # E3a: the router had started it already
            return
        if (self.observer.heard and not getattr(self.observer, "heard_plain", False)) or self.bridge.has_open_voice_job():
            return
        from yuki.voice.bridge import heard_words

        from yuki.voice.bridge import spoken_words

        context = getattr(getattr(self, "_llm", None), "_yuki_context", None) or getattr(self, "_llm_context", None)
        heard = heard_words(context, skip=self.bridge.not_heard)
        try:
            started = self.bridge.begin_early(call_id, heard, screen=screen, said=spoken_words(context))
        except TypeError:  # a stand-in bridge without the v3.9 arguments
            started = self.bridge.begin_early(call_id, heard)
        if started:
            self.observer.handoff_started(time.monotonic())

    # -- E3a (v3.17): the hand-off router ------------------------------------------------------------------------

    def _route_turn(self, context: Any, call: Any) -> None:
        """The talker's call for the user's turn begins: the router decides alongside it whether the turn needs the
        thinker (``voice_router``; yuki.voice.router). Not on words recognition was unsure of (a call may be held until
        they answer), not while a voice task is open (a follow-up routes by that task's state), not while dormant."""
        if not getattr(self.settings, "voice_router", False) or self.dormant or not getattr(call, "user_turn", False):
            return
        turn = self.observer.turn
        if self.focus and not (turn is not None and turn.named):
            return   # vGame: in a game or a call only words said to Yuki by name start the thinker early
        self._cancel_route()
        pending = getattr(self.bridge, "routed_job", None)
        if callable(pending) and pending() is not None:
            # an earlier start the talker never took (this is a new run for the turn): withdrawn before a new one
            self.log.event("router", op="withdrawn", why="new_run")
            self.bridge.drop_early(getattr(self.bridge, "ROUTE_KEY", "router"))
        if (self.observer.heard and not getattr(self.observer, "heard_plain", False)) or self.bridge.has_open_voice_job():
            return
        from yuki.voice.bridge import heard_words
        from yuki.voice.router import router_request

        heard = heard_words(context, skip=getattr(self.bridge, "not_heard", None))
        if not heard:
            return
        history: list[tuple[str, str]] = []
        try:
            messages = list(context.get_messages() or [])
        except Exception:  # noqa: BLE001
            messages = []
        for message in messages[:-1][-8:]:
            if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
                continue
            content = message.get("content")
            text = content if isinstance(content, str) else " ".join(
                str(p.get("text") or "") for p in (content or []) if isinstance(p, dict) and p.get("type") == "text")
            if str(text or "").strip():
                history.append(("they" if message["role"] == "user" else "Yuki", str(text)))
        hands = getattr(self.bridge, "hands", None)
        snap = getattr(hands, "last", None) if hands is not None else None
        s = self.settings
        request = router_request(heard[-1], history=history, desk=getattr(snap, "text", None) or None,
                                 call=self.call_app,
                                 model=str(getattr(s, "voice_router_model", "") or s.voice_mouth_model))
        turn = self.observer.user_turns
        self._router_task = asyncio.ensure_future(self._route(request, heard, context, call, turn))

    async def _route(self, request: dict[str, Any], heard: list[str], context: Any, call: Any, turn: int) -> None:
        from yuki.voice.bridge import spoken_words
        from yuki.voice.router import decide

        route = await decide(self._client, request)
        call.route, call.route_ms = route.to, route.ms
        self.log.event("router", op="decided", to=route.to, screen=route.screen, ms=route.ms, error=route.error)
        if route.to != "thinker":
            return
        llm = getattr(self, "_llm", None)
        waited = 0.0
        while llm is not None and getattr(getattr(llm, "_speculation_gate", None), "is_speculating", False):
            if waited >= 3.0:
                return   # a speculation that is neither confirmed nor withdrawn: let the talker's own hand-off decide
            await asyncio.sleep(0.02)
            waited += 0.02
        if self._stop.is_set() or self.observer.user_turns != turn or self.bridge.has_open_voice_job():
            return   # they spoke again, or the talker already handed it over
        begin = getattr(self.bridge, "begin_early", None)
        key = getattr(self.bridge, "ROUTE_KEY", "router")
        try:
            started = bool(callable(begin) and begin(key, heard, screen=route.screen, said=spoken_words(context),
                                                     routed=True))
        except TypeError:   # a stand-in bridge without routed starts
            started = False
        if not started:
            return
        self.observer.handoff_started(time.monotonic())
        self.log.event("router", op="started", screen=route.screen, after_call_ms=round(
            (time.monotonic() - call.entered) * 1000))
        adopt_s = float(getattr(self.settings, "voice_router_adopt_s", 2.5) or 2.5)
        loop = asyncio.get_running_loop()
        self._route_timer = loop.call_later(adopt_s, self._route_timeout)

    def _route_timeout(self) -> None:
        """The talker did not take the router's start in time: withdrawn before the thinker can act on it."""
        self._route_timer = None
        routed = getattr(self.bridge, "routed_job", None)
        if callable(routed) and routed() is not None:
            self.log.event("router", op="withdrawn", why="not_adopted_in_time")
            self.bridge.drop_early(getattr(self.bridge, "ROUTE_KEY", "router"))

    def _reply_done(self, pending: bool) -> None:
        """The talker's reply ended: a router start it did not take (it answered, used a hand, stayed quiet) is void."""
        if pending:
            return
        routed = getattr(self.bridge, "routed_job", None)
        if callable(routed) and routed() is not None:
            task = getattr(self, "_router_task", None)
            self.log.event("router", op="withdrawn", why="talker_chose_otherwise")
            self.bridge.drop_early(getattr(self.bridge, "ROUTE_KEY", "router"))
            if task is not None and not task.done():
                task.cancel()
        else:
            task = getattr(self, "_router_task", None)
            if task is not None and not task.done():
                task.cancel()   # the talker answered first: a late "thinker" must start nothing
                self.log.event("router", op="cancelled", why="reply_first")

    def _cancel_route(self) -> None:
        task, self._router_task = getattr(self, "_router_task", None), None
        if task is not None and not task.done():
            task.cancel()
        timer, self._route_timer = getattr(self, "_route_timer", None), None
        if timer is not None:
            timer.cancel()

    async def _hand_or_hold(self, params: Any) -> None:
        """A hand, unless recognition was unsure of the user's words and the mouth spoke with the call.

        Measured 2026-09-27 (clarify replay, 4 of 6 runs): "close the cloud tabs" got "The Claude tabs, three
        and four?" and the close in the same reply, before the answer. A window hand called with words while
        the latest words were doubtful (the Speech line) is held, not run (hand-offs too): its report tells
        the mouth to wait for the answer if it asked (stay_quiet), else to call it again, so nothing is
        lost. Sound hands touch no window and run. State only (the note, the stream, which hand), never what was said.
        """
        if self._held(params.function_name, params.tool_call_id):
            await self._hold(params)
            return
        await self.bridge.hands.handle(params)

    def _quiet_for(self) -> float:
        """Seconds since anyone spoke (Yuki, or the user by the mic or a transcript); 0 while someone speaks."""
        obs = self.observer
        if obs.bot_speaking or obs.user_speaking or obs.generating or obs.speech_pending:
            return 0.0
        now = time.monotonic()
        marks = [obs.last_bot_stop or 0.0, getattr(obs, "last_user_at", 0.0) or 0.0, self.stt_ready_at or 0.0]
        if self._voice_activity is not None:
            try:
                since, last = self._voice_activity()
                if last and now - last < self.SOUND_RECENT_S and (last - since) < self.SOUND_MAX_STRETCH_S:
                    return 0.0
                marks.append(last or 0.0)
            except Exception:  # noqa: BLE001
                pass
        return max(0.0, now - max(marks))

    async def _quick_memory_call(self, params: Any) -> None:
        """The talker's quick memory tool (vfix4's :class:`yuki.voice.recall.QuickMemory`), its answer written while the
        words before the call are still being said (leftVoice).

        The talker says a few words before a look ("Let me look."). Pipecat keeps every frame after a reply's words in
        order behind their audio (the voice's queue pauses until they have played, the speaker plays frames in order),
        so the look's report reached the conversation, and the run that answers it started, only once "Let me look."
        had played: measured offline 4 Oct, the answer's run began 1.0-1.2 s after the report was ready. When the reply
        that made the call spoke, the report is settled without a run (``run_llm`` false: it still goes into the
        conversation in its place) and the answer's run starts at once on a copy of the conversation that already
        holds the call and its report (:meth:`_answer_look_now`); its words queue behind the lead in the voice. A reply
        that called it with no words first waits behind nothing, so it settles as before. A clean add said by code
        (:class:`~yuki.voice.recall.QuickMemory`) waits behind its own line, lead or not (third check LVV-1): when the
        same words asked for more (:func:`_add_asked_more`, checked while the line plays) the run that does the rest
        starts at once the same way; when the add was all of it, the turn ends with the line, with no run.
        Plumbing: when the run starts, never what anyone said.

        Review fixes (LV-1, LV-2): only a look that was the only call of its reply is answered this way (Pipecat answers
        a reply's several calls once, after the last, with every report), and only when, as the report comes back, the
        user is not speaking and has said nothing since the call (no user or developer message newer than it). Else the
        report settles as on main and Pipecat's aggregator decides, so Yuki never talks over the user or answers twice.
        """
        llm = getattr(self, "_llm", None)
        call_id = str(getattr(params, "tool_call_id", "") or "")
        spoke = bool(call_id) and llm is not None and call_id in getattr(llm, "_yuki_spoke", ())
        solo = bool(call_id) and llm is not None and call_id in getattr(llm, "_yuki_solo", ())
        if solo:
            original = params.result_callback
            # Second review (LVC-1): what the user says is watched in the live conversation. The run's own context
            # is often a frozen copy: an eager end of turn runs on Pipecat's provisional copy (the conversation plus
            # the speculated turn), the answer-now run on its own copy, and neither ever grows. The user and
            # developer messages of the context the run answered are the turn being answered and everything before
            # it; more of them in the live conversation is the user going on. Counted on the run's context, so the
            # eager turn's own commit (it can land just after its held call starts) is not taken for new words.
            ran_on = getattr(params, "context", None)
            live = getattr(self, "_llm_context", None) or ran_on
            said = _said_count(ran_on)

            async def settle(result: Any, *, properties: Any = None) -> None:
                if properties is not None and getattr(properties, "run_llm", None) is False:
                    await original(result, properties=properties)   # settled with no run: nothing to answer
                    return
                said_by_code = isinstance(result, dict) and bool(result.get("said"))
                if not spoke and not said_by_code:
                    await original(result, properties=properties)   # no words before it to wait behind
                    return
                if said_by_code and not await _add_asked_more(self, ran_on, result, call_id):
                    # Third check LVV-1: a clean add that was all they asked ends the turn with its line, no run
                    # (a run there wandered: "What's up?", an offer about the screen, once a pause).
                    from pipecat.frames.frames import FunctionCallResultProperties

                    await original(result, properties=FunctionCallResultProperties(run_llm=False))
                    return
                user_speaking = bool(getattr(self.observer, "user_speaking", False))
                now_said = _said_count(live)
                newer = said is None or now_said is None or now_said > said
                if user_speaking or newer:
                    # The user is talking, or has said more since the call: as on main, the report just goes in and
                    # their own turn (or Pipecat, once they stop) answers with it.
                    if self.log is not None:
                        self.log.event("quick_memory", session=self.session, op="answer_later", call=call_id,
                                       user_speaking=user_speaking, newer=newer)
                    await original(result, properties=properties)
                    return
                from pipecat.frames.frames import FunctionCallResultProperties

                await original(result, properties=FunctionCallResultProperties(run_llm=False))
                await self._answer_look_now(params, result)

            params.result_callback = settle
        await self.quick_memory.handle(params)

    async def _answer_look_now(self, params: Any, result: Any) -> None:
        """Run the talker now on the conversation as it will be once the look's call and report are in it (leftVoice).

        The shared conversation gets them only when their frames have passed the lead's audio; the copy has them now:
        the conversation so far without this call's own messages (a placeholder already there is replaced), then the
        call and its report. The run's words reach the conversation in order, after the report, as any answer's do.

        The copy is made from the context the look's run answered, not the live conversation (second review, LVC-1):
        the live one gets the call, its lead and an earlier look's report only once their audio has played, so it
        would lack them; that the user has said nothing since is checked on the live one before this runs."""
        import json

        from pipecat.frames.frames import LLMContextFrame
        from pipecat.processors.aggregators.llm_context import LLMContext

        llm = getattr(self, "_llm", None)
        context = getattr(params, "context", None)
        if llm is None or context is None:
            return
        call_id = str(params.tool_call_id)

        def mine(message: Any) -> bool:
            if not isinstance(message, dict):
                return False
            if message.get("tool_call_id") == call_id:
                return True
            calls = message.get("tool_calls") if message.get("role") == "assistant" else None
            return bool(calls) and any(isinstance(c, dict) and c.get("id") == call_id for c in calls)

        messages = [m for m in context.get_messages() if not mine(m)]
        observer = getattr(self, "observer", None)
        turn = getattr(observer, "turn", None)
        # Second review: the reply's own words before the call ("It's ten to eleven. Adding it now.") go in too, as
        # ``chronological`` orders them, so the answer knows what was already said (offline 4 Oct: without them the
        # add-and-ask answer gave the time a second time). Words the conversation already holds are not repeated.
        words = _reply_words(observer)
        said = result.get("said") if isinstance(result, dict) else None
        if isinstance(said, str) and said.strip() and " ".join(said.split()) not in words:
            # third check LVV-1: a clean add's line, said by code, is words of this reply before the answer too (text
            # replays 4 Oct: only in the report, the answer said the add again or chatted; before the call, it did not)
            words = f"{words} {' '.join(said.split())}".strip()
        if words and not _holds_words_since_user(messages, words):
            messages.append({"role": "assistant", "content": words})
        args = params.arguments if isinstance(params.arguments, dict) else {}
        messages.append({"role": "assistant", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": params.function_name, "arguments": json.dumps(args, ensure_ascii=False)}}]})
        messages.append({"role": "tool", "tool_call_id": call_id,
                         "content": json.dumps(result, ensure_ascii=False) if result else "COMPLETED"})
        copy = LLMContext(messages=messages, tools=context.tools, tool_choice=context.tool_choice)
        if observer is not None and turn is not None:
            observer.look_now_turn = turn   # LVC-2: the run's reply stays in the turn whose lead it follows
        if self.log is not None:
            self.log.event("quick_memory", session=self.session, op="answer_now", call=call_id,
                           bot_speaking=bool(self.observer.bot_speaking))
        await llm.queue_frame(LLMContextFrame(context=copy))

    async def _speak(self, text: str) -> None:
        """Say ``text`` now by code (no mouth run): a job's own finishing line with no tell_yuki call to carry it."""
        llm = getattr(self, "_llm", None)
        if llm is None or not text.strip():
            return
        from pipecat.frames.frames import TTSSpeakFrame

        await llm.push_frame(TTSSpeakFrame(text=text.strip()))

    #: Yuki spoke this recently (s): the user's next words are most likely to her (a stay_quiet on them is refused
    #: once, v3.9).
    EXCHANGE_LIVE_S = 20.0

    #: A user turn that got nothing audible (its reply was cut by a sound that brought no words) is answered
    #: again once the user has been quiet this long after it (s), and at least this long after its words.
    REPLY_WATCH_QUIET_S = 0.9
    REPLY_WATCH_AFTER_S = 1.2
    #: A rerun whose reply has not even started this long after it was asked for counts as silent (s, v3.16).
    RERUN_WAIT_S = 8.0

    def _unanswered(self) -> Any:
        """The latest turn when it is the user's, was said to Yuki and got nothing audible, else ``None``.

        v3.16 (04 F1): "audible" is words that left the speaker (``turn.heard``), never the start of any audio: a
        fully dropped or marks-only reply ends with a breath of silence, which is no answer. A turn Yuki acted on
        (a hand, a hand-off), chose silence on (stay_quiet, a quiet marker), or gave up on after a silent rerun is
        not unanswered.
        """
        obs = self.observer
        turn = obs.turn
        if turn is None or turn.trigger != "user" or not (turn.user_text or "").strip():
            return None
        if turn.heard or turn.tools or turn.quiet or turn.gave_up:
            return None  # heard something, or acted, or handed over, or chose silence, or already given up on
        if not (turn.cut or turn.responses):
            return None  # its reply may still be on its way
        return turn

    async def _reply_watch(self, every_s: float = 0.2) -> None:
        """Every utterance said to Yuki gets an audible answer (v3.9), and every outcome owed is heard (v3.16).

        Live 2026-09-27 14:17:01-14:17:10 "uke", "Yuki." and a third utterance each got a reply that was cut
        before a sound was heard (the next words, or a start of turn that brought no words) and nothing was
        said for ten seconds: the user thought Yuki was dead. When the newest turn is the user's, got no words
        heard, no hand, no hand-off and no stay_quiet, and the user has since been quiet (no speech, no mic sound,
        no reply being written or spoken), the mouth runs again on the same context, once per turn. If that rerun
        is silent too, the error tone says so (a non-speech notice) and the conversation stays open one more idle
        window (v3.16, 04 F1). A fact about the stream, never about the words. Each tick also gives the owed-output
        ledger its turn (:meth:`_owed_tick`).
        """
        from pipecat.frames.frames import LLMRunFrame

        try:
            while True:
                await asyncio.sleep(every_s if not self.dormant else 1.0)
                if self.dormant or self._stop.is_set():
                    continue
                try:
                    await self._owed_tick()
                except Exception as exc:  # noqa: BLE001 - the ledger never stops the watch
                    self.log.event("owed", op="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")
                turn = self._unanswered()
                obs = self.observer
                if turn is None or self.worker is None:
                    continue
                if obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending:
                    continue
                now = time.monotonic()
                if turn.transcript_at is None or now - turn.transcript_at < self.REPLY_WATCH_AFTER_S:
                    continue
                if self._quiet_for() < self.REPLY_WATCH_QUIET_S:
                    continue
                if turn.rerun:
                    started = turn.rerun_at or now
                    if turn.responses <= turn.responses_at_rerun and now - started < self.RERUN_WAIT_S:
                        continue   # the rerun's reply is still on its way
                    self._give_up(turn)
                    continue
                turn.rerun = True
                turn.responses_at_rerun = turn.responses
                turn.rerun_at = now
                obs.rerun_turn = turn
                self.log.event("reply_watch", op="rerun", turn=turn.n, cut=bool(turn.cut),
                               responses=turn.responses, after_ms=round((now - turn.transcript_at) * 1000),
                               marker=turn.marker, unmarked=turn.unmarked, tts_silent=turn.tts_silent,
                               guarded=turn.guarded)
                await self.worker.queue_frame(LLMRunFrame())
        except asyncio.CancelledError:
            return

    def _give_up(self, turn: Any) -> None:
        """The rerun of an unheard turn was silent too: the error tone, and one more idle window (04 F1)."""
        turn.gave_up = True
        self._grace_since = time.monotonic()
        self.log.event("reply_watch", op="gave_up", turn=turn.n, responses=turn.responses, marker=turn.marker,
                       unmarked=turn.unmarked, tts_silent=turn.tts_silent, guarded=turn.guarded,
                       repeat=bool(getattr(turn, "repeat_dropped", False)))
        if not getattr(turn, "repeat_dropped", False):
            self._tone("error")   # a line kept back as a repeat was heard already: nothing went wrong (V9)
        self._activity.set()

    def _tone(self, kind: str) -> None:
        """An earcon through this conversation's output (non-speech; the session plays it)."""
        if self.on_tone is None:
            return
        try:
            self.on_tone(kind)
        except Exception:  # noqa: BLE001 - a notice never breaks the voice
            pass

    # -- the owed-output ledger (v3.16, 07 §6 A) -----------------------------------------------------------------

    def _on_audible(self, now: float, turn: Any) -> None:
        if self.ledger is not None:
            self.ledger.heard(now, getattr(turn, "opened", None), getattr(turn, "trigger", None))

    async def _owed_tick(self) -> None:
        """Say by code (or show with a sound) an outcome whose talker run was not heard, at a quiet moment."""
        ledger = self.ledger
        if ledger is None or not ledger.pending:
            return
        obs = self.observer
        ledger.expire(obs.user_turns)
        now = time.monotonic()
        busy = bool(obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending or obs.owes_reply
                    or getattr(self.bridge, "busy_tools", 0))
        item = ledger.due(now, last_response_end=obs.last_response_end, quiet_for=self._quiet_for(), busy=busy)
        if item is None:
            return
        if ledger.attempt(item, now) == "code":
            await self._speak_owed(item.text)
            return
        # Said twice by code and still not heard (the voice itself is failing): on the pill, with a sound.
        obs.set_state("note", item.text)
        self._tone("error" if item.kind == "failed" else "note")

    async def _speak_owed(self, text: str) -> None:
        """An owed outcome said by code in the one voice: the thinker's own words, never held by the line check."""
        tts = getattr(self, "_tts", None)
        unguard = getattr(tts, "unguard", None)
        if callable(unguard):
            unguard(text)
        await self._speak(text)

    async def _speak_result(self, lines: list[str]) -> None:
        """C1 (v3.17): a result's sentences said by code in the one voice, each its own piece (so each starts the moment
        its audio is ready, and a sentence fetched ahead, C2, is found by its words); the thinker's own words, never
        held by a blocking line check (as an owed outcome's)."""
        tts = getattr(self, "_tts", None)
        unguard = getattr(tts, "unguard", None)
        if lines and not self.observer.bot_speaking:
            self.observer.speech_pending_since = time.monotonic()   # words on their way: Yuki is about to speak
        for line in lines:
            if not line.strip():
                continue
            if callable(unguard):
                unguard(line)
            await self._speak(line)

    def _owed_pending(self) -> int:
        return len(self.ledger.pending) if self.ledger is not None else 0

    # -- v3.17: the talker's prompt by measured state (H1) and its held words (H2) --------------------------------

    #: A hand of the talker's that did not take effect this recently (s) brings its guidance into the next calls.
    HAND_TROUBLE_S = 60.0
    #: A close this recently (s) brings the guidance on getting a closed tab back into the talker's calls.
    CLOSED_S = 900.0
    #: Held words of a reply are offered for this many user turns after it.
    HELD_TURNS = 2

    def _prompt_state(self) -> dict[str, Any]:
        """Measured states for the talker's volatile block (``mouth.volatile_block``): never the words."""
        state: dict[str, Any] = {}
        now = time.monotonic()
        hands = getattr(self.bridge, "hands", None)
        if hands is not None:
            if getattr(hands, "halted", None):
                state["hand_trouble"] = True
            else:
                at = getattr(hands, "trouble_at", None)
                if at is not None and now - at <= self.HAND_TROUBLE_S:
                    state["hand_trouble"] = True
        closed = [x for x in (getattr(hands, "closed_at", None), getattr(self.bridge, "closed_at", None))
                  if isinstance(x, (int, float))]
        if closed and now - max(closed) <= self.CLOSED_S:
            state["closed"] = True
        held = getattr(self, "_held_more", None)
        if held and self.observer.user_turns - int(getattr(self, "_held_at_turn", 0)) <= self.HELD_TURNS:
            state["held"] = held
        turn = self.observer.turn
        if turn is not None and getattr(turn, "repeat_dropped", False):
            state["repeat"] = True   # V9: the rerun is told its last reply repeated the line before
        scrolling = self._scrolling_line()
        if scrolling:
            state["scrolling"] = scrolling   # V7: a page scrolling on by itself; the talker stops it
        dictating = getattr(hands, "dictating", None) if hands is not None else None
        started = getattr(self.observer, "_user_started_at", None)
        if isinstance(dictating, dict) and started is not None and float(dictating.get("since") or 0) >= started - 1.0:
            # N7 (report 47): another app's text field changed while they spoke this turn (typing or dictation)
            state["dictating"] = (f"{dictating.get('app')} \"{dictating.get('title')}\" (the field "
                                  f"{dictating.get('field')})")
        if self.focus:
            # vGame: a full-screen game in front or a call on: Yuki stays out of the way (mouth.FOCUS_GUIDE)
            state["focus"] = {"game": self.game_app or None, "call": self._call_focus}
        return state

    #: V7: a continuous scroll waits while the user talks and this long after their words (they may be "stop").
    SCROLL_TALK_HOLD_S = 1.5

    def _scroll_hold(self, live: bool) -> None:
        """V7: while this conversation runs, a continuous scroll pauses while the user talks (and while a reply to them
        is being written); when it ends, a scroll still running is stopped (nobody could say stop any more)."""
        try:
            from yuki.actions import scrolling
        except Exception:  # noqa: BLE001
            return
        if live:
            obs = self.observer
            scrolling.set_voice_hold(lambda: bool(
                obs.user_speaking or obs.generating
                or time.monotonic() - float(getattr(obs, "last_user_at", 0.0) or 0.0) < self.SCROLL_TALK_HOLD_S))
            return
        scrolling.set_voice_hold(None)
        report = scrolling.stop_continuous("ended", wait_s=0.0)
        if report is not None:
            self.log.event("hands", op="scroll_keep", stopped="ended", steps=report.get("steps"),
                           seconds=report.get("seconds"))

    def _scrolling_line(self) -> str | None:
        """V7: what the continuous scroll is doing now (``yuki.actions.scrolling.continuous``), or ``None``.

        Read from the module only when it is already loaded: a scroll can only run in this process through it, and
        the watch asks on every pass, the first one included. Importing it there cost ~0.5 s on the event loop
        (``yuki.actions`` pulls in pyautogui and comtypes), which the watch measured as a loop stall and added to the
        call-mode idle: call mode ended at 5.7-5.9 s against its 5 s (1 s idle + speech grace)."""
        scrolling = sys.modules.get("yuki.actions.scrolling")
        if scrolling is None:
            return None
        try:
            return scrolling.continuous_state()
        except Exception:  # noqa: BLE001 - a hint only
            return None

    # -- v3.18: practice memory and the thinker's request pack -----------------------------------------------------

    def _apps_in_play(self) -> list[str]:
        """The open windows' processes from the hands' latest desktop summary, the one in front first (practice
        memory's apps in play; a summary read while they talked, no read here)."""
        hands = getattr(self.bridge, "hands", None)
        snap = getattr(hands, "last", None)
        apps = dict(getattr(snap, "apps", None) or {})
        front = getattr(snap, "foreground", None)
        ordered = ([apps.pop(front)] if front in apps else []) + list(apps.values())
        return list(dict.fromkeys(a for a in ordered if a))

    def more_held(self, max_sentences: int = 2, max_chars: int = 280) -> tuple[str, bool] | None:
        """H2: the next part of the talker's held words (``(part, more left)``), taken; ``None`` when nothing is held."""
        held = getattr(self, "_held_more", None)
        if not held:
            return None
        from yuki.voice.narrator import condense

        head, rest = condense(held, max_sentences=max_sentences, max_chars=max_chars)
        self._held_more = rest or None
        self._held_at_turn = self.observer.user_turns
        return head, bool(rest)

    def _held_pending(self, now: float) -> int:
        """1 while a hand the talker held until the user answers waits for that answer (04 F1), else 0.

        Held (doubtful words, ``_hold``) and the user has not spoken since: their next words are what it waits for.
        """
        bridge = self.bridge
        if getattr(bridge, "held_call", None) is None:
            return 0
        at = getattr(bridge, "held_at", None)
        if at is None or now - at > float(self.settings.voice_hold_max_s):
            return 0
        return 0 if (self.observer.last_user_at or 0.0) > at else 1

    def _marker_report(self, call: Any, channel: Any) -> None:
        """A reply's text read by the marker channel (v3.16): kept on its turn, logged content-free. H2 (v3.17): what it
        held back (after its more line, or past the word cap) is kept for the next turn and for more_of_result."""
        facts = channel.facts()
        held = str(getattr(channel, "held", "") or "").strip()
        if held:
            self._held_more = held
            self._held_at_turn = self.observer.user_turns
        turn = self.observer.turn
        if turn is not None:
            turn.marker = facts.get("marker") or turn.marker
            turn.scratch += int(facts.get("scratch_chars") or 0)
            turn.unmarked = bool(turn.unmarked or facts.get("unmarked"))
            if facts.get("repeat_dropped"):
                turn.repeat_dropped = True   # V9: answered again with the repeat note, never with the same line
        if facts.get("marker") is not None or facts.get("scratch_chars"):
            self.log.event("marker", session=self.session, user_turn=bool(getattr(call, "user_turn", False)),
                           turn=turn.n if turn is not None else None, **facts)
        if turn is not None and turn.marker == "quiet" and getattr(call, "user_turn", False):
            self.observer._quiet_turn()   # vGame: silence chosen on the user's words

    # -- loop stalls (04 F4) -------------------------------------------------------------------------------------

    #: A wake-up later than asked by more than this, or a watch pass that took this long, was a loop stall (s).
    STALL_MIN_S = 0.25

    def _note_stall(self, start: float, end: float) -> None:
        if end - start <= 0:
            return
        self._stalls.append((start, end))
        if len(self._stalls) > 64:
            del self._stalls[:-64]

    def _stalled_since(self, since: float, now: float) -> float:
        """Seconds of loop stalls between ``since`` and ``now`` (overlapping measurements counted once)."""
        spans = sorted((max(a, since), min(b, now)) for a, b in self._stalls if b > since and a < now)
        total, end = 0.0, since
        for a, b in spans:
            a = max(a, end)
            if b > a:
                total += b - a
                end = b
        return total

    #: With no goodbye to say (V1: "shut up", "go away"), the conversation ends this soon after the call (s): only a
    #: word already on its way to the speaker plays out, nothing new is said.
    QUICK_END_S = 0.3

    async def _end_after_speech(self, max_s: float = 6.0, quick: bool = False) -> None:
        """The user asked to end the conversation: let the goodbye play, then end it (the wake word stays on).
        ``quick`` (V1): there is no goodbye, so it ends at once."""
        started = time.monotonic()
        heard = False
        while time.monotonic() - started < max_s:
            obs = self.observer
            heard = heard or obs.bot_speaking
            if heard and not obs.bot_speaking:
                break
            if not heard and time.monotonic() - started > (self.QUICK_END_S if quick else 2.0):
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
        """An item of a task no tool call carries (a hand's try passed to the thinker by code, or a correction that
        came too late): into the context as the thinker's message, said if ``run_llm``."""
        import json

        from pipecat.frames.frames import LLMMessagesAppendFrame

        if self.worker is not None:
            message = {"role": "developer", "content": json.dumps({"from_the_thinker": payload}, ensure_ascii=False)}
            await self.worker.queue_frame(LLMMessagesAppendFrame(messages=[message], run_llm=run_llm))

    #: In call mode, a user still talking when the call idle runs out gets this much longer
    #: (a sentence to Yuki that started just before the end is not cut off).
    CALL_SPEECH_GRACE_S = 4.0
    #: A mouth reply that ended this recently may still be starting its tool call (Pipecat runs
    #: the calls after the stream ends): the conversation does not end in between (measured: a
    #: call-mode run ended 0.4 s before the "turn it up" hand it had just decided on).
    CALL_TOOL_START_S = 1.0
    #: In call mode the microphone's own voiced signal this recent (s) means they are still talking: the call idle
    #: never ends the conversation then (v3.16, 04 F1: live 2 Oct 21:28:00 it closed on their "Hello?", which Flux had
    #: not reported yet; its start of turn comes ~0.5-1 s into speech). A measured level, never words.
    CALL_VOICED_HOLD_S = 0.6
    #: A stop the watch decided is confirmed on a second look this long later, with the loop free in between, so a
    #: frame that waited out a stall (a transcript, a reply) is handled before the end (v3.16, 04 F4).
    STOP_CONFIRM_S = 0.15

    def idle_limit(self) -> float:
        """Quiet seconds that end a conversation: longer the more the user has been talking with Yuki."""
        s = self.settings
        extra = s.voice_idle_per_exchange_s * max(0, self.observer.exchanges - 1)
        return max(s.voice_idle_s, min(s.voice_idle_max_s, s.voice_idle_s + extra))

    def _user_voiced(self, now: float) -> bool:
        """The microphone hears them talking right now (its voiced level), not a long stretch of noise."""
        if self._voice_activity is None:
            return False
        try:
            since, last = self._voice_activity()
        except Exception:  # noqa: BLE001
            return False
        if not last or now - last >= self.CALL_VOICED_HOLD_S:
            return False
        return bool((last - since) < self.SOUND_MAX_STRETCH_S or self.observer.user_speaking)

    async def _watch(self, runner: Any) -> None:
        """End the conversation when it has been quiet long enough, or on stop / fatal error.

        Normal mode: after :meth:`idle_limit` of nobody speaking (longer while a task runs, up
        to ``voice_hold_max_s``). Call mode: ``voice_call_idle_s`` after the last exchange with
        Yuki, however much the user goes on talking to the people on the call.

        v3.16 (04 F1, F3, F4): never while the user's newest turn has had no audible answer (it is answered again
        first), while a held hand waits for their answer or an outcome is still owed (then the hold limit applies),
        or, in a call, while the microphone hears them talking. The quiet time does not count loop stalls, and a stop
        is confirmed on a second look a moment later. Why it ended is kept in :attr:`stop_facts`.
        """
        s = self.settings
        if self.dormant and not await self._dormant_upkeep():
            self._set_stop("standby_lost")
        last = time.monotonic()
        confirming = False
        held = unanswered = False
        window = last
        while True:
            obs = self.observer
            began = time.monotonic()
            held = bool(self._held_pending(began))
            scrolling = self._scrolling_line() is not None   # V7: open while a page scrolls on (they say stop)
            owed = (self.bridge.open_voice_jobs() + (self.gate.holding if self.gate is not None else 0)
                    + int(held) + self._owed_pending() + int(scrolling))
            unanswered = self._unanswered() is not None
            now = time.monotonic()
            if now - began > self.STALL_MIN_S:
                self._note_stall(began, now)   # this pass itself was held up: a stall, not quiet
            if self.call_app:
                busy = (obs.bot_speaking or obs.generating or obs.owes_reply or unanswered or self._user_voiced(now)
                        or now - obs.last_response_end < self.CALL_TOOL_START_S)
                window = max(x for x in (self.stt_ready_at, obs.last_exchange, self.started_at, self._grace_since)
                             if x is not None)
                limit = s.voice_hold_max_s if owed else s.voice_call_idle_s
                if obs.user_speaking and not owed:
                    limit += self.CALL_SPEECH_GRACE_S
                remaining = limit - ((now - window) - self._stalled_since(window, now))
                wait = 1.0 if busy else max(0.05, remaining)  # owes_reply expires by time, not by an event
            else:
                busy = obs.user_speaking or obs.bot_speaking or obs.generating or unanswered
                window = max(last, self._grace_since or 0.0)
                limit = s.voice_hold_max_s if owed else self.idle_limit()
                remaining = limit - ((now - window) - self._stalled_since(window, now))
                wait = (1.0 if unanswered else None) if busy else max(0.05, remaining)
            if scrolling and (wait is None or wait > 1.0):
                wait = 1.0   # the scroll ends on its own thread with no event here: look again each second
            if not busy and not obs.owes_reply and obs.state in ("thinking", "working", "listening"):
                # The pill says "Working on it" only while a job really runs (queued or running); a
                # result folded in silently, a cancelled job or a stay_quiet leaves it "Listening".
                obs.set_state(obs.idle_state())
            if self._stop.is_set() or obs.fatal:
                break
            if not busy and remaining <= 0:
                if not confirming:
                    # Look again in a moment with the loop free in between: frames that waited out a stall are
                    # handled first (a transcript, a reply on its way), and a stall in between is measured.
                    confirming = True
                    before = time.monotonic()
                    await asyncio.sleep(self.STOP_CONFIRM_S)
                    after = time.monotonic()
                    if after - before > self.STOP_CONFIRM_S + self.STALL_MIN_S:
                        self._note_stall(before + self.STOP_CONFIRM_S, after)
                    continue
                self._set_stop("held_too_long" if owed else ("call_idle" if self.call_app else "idle"))
                break
            confirming = False
            self._activity.clear()
            slept_from = time.monotonic()
            try:
                await asyncio.wait_for(self._activity.wait(), timeout=wait)
            except asyncio.TimeoutError:
                woke = time.monotonic()
                if wait is not None and woke - slept_from > wait + self.STALL_MIN_S:
                    self._note_stall(slept_from + wait, woke)   # it woke late: the loop was stalled
                continue
            except asyncio.CancelledError:
                return
            last = time.monotonic()
            if self._stop.is_set():
                break
        self._record_stop(window, held=held, unanswered=unanswered)
        if self._on_closing is not None:
            try:
                self._on_closing(self.reason)
            except Exception:  # noqa: BLE001 - only a hint to the session
                pass
        if self._bound:
            self.bridge.closing = True
        await runner.cancel()

    def _record_stop(self, window: float, *, held: bool, unanswered: bool) -> None:
        """What ended the conversation, content-free, for the session's ``session stop`` event (04 F3)."""
        obs = self.observer
        now = time.monotonic()
        exchange = obs.last_exchange or self.stt_ready_at or self.started_at
        self.stop_facts = {
            "reason": self.reason,
            "since_exchange_s": round(now - exchange, 1),
            "exchanges": obs.exchanges,
            "unanswered": bool(unanswered),
            "held": bool(held),
            "owed": self._owed_pending(),
            "stall_ms_in_window": round(self._stalled_since(window, now) * 1000),
            "call_mode": bool(self.call_app),
        }


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
    try:
        # v3.14: Pipecat's sentence splitter loads NLTK and its punkt data the first time a reply is split, on the
        # voice loop: measured 2026-09-29 in a fresh process 0.8 s to import and 1.2 s for the first sentence (disk
        # and path checks), and live a 3.5 s loop stall inside nltk.pathsec. Loaded here once per process.
        from pipecat.utils.string import match_endofsentence

        match_endofsentence("Warm up. Then the rest")
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
