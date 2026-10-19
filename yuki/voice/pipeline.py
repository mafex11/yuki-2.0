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
import contextvars
import itertools
import weakref
from collections import deque
from pathlib import Path
import os
import sys
import time
from datetime import datetime
from dataclasses import dataclass, field
from typing import Any, Callable

from yuki.config import Settings, thinking_off
from yuki.voice.expressive import strip_tags
from yuki.voice.keys import DEEPGRAM, ELEVENLABS, GOOGLE, get_key
from yuki.voice.keyterms import name_terms, own_names, said_site, vocabulary
from yuki.voice.hands import talker_hand_tools
from yuki.voice.recall import QUICK_MEMORY
from yuki.voice.mouth import (
    CHECK_PROGRESS, END_CONVERSATION, GO_QUIET, HAND_OVER, HANDOFF_TOOLS, LEAN_MOUTH_PROMPT, MORE_OF_RESULT,
    MOUTH_PROMPT, OPENED_QUIET, STAY_QUIET, TELL_YUKI, MouthMemory, ReplyChannel, mouth_system_blocks, router_on,
    say_first_words,
    talker_kind, voice_on_brain, voice_one, volatile_block, with_markers,
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


async def read_head(stream: Any) -> tuple[list[Any], bool]:
    """The talker's stream up to its first content block (``message_start``, pings), and whether the reply was
    declined before any output (kMind, review M2: a ``message_delta`` with ``stop_reason`` ``refusal`` before any
    content). A failure the stream reports on its way there raises here, where the failover catches it."""
    head: list[Any] = []
    while True:
        try:
            event = await stream.__anext__()
        except StopAsyncIteration:
            return head, False
        head.append(event)
        kind = getattr(event, "type", "")
        if kind == "message_delta":
            return head, getattr(getattr(event, "delta", None), "stop_reason", None) == "refusal"
        if kind in ("content_block_start", "message_stop"):
            return head, False

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


def stt_provider(settings: Any) -> str:
    """Speech recognition in force: ``"local"`` (Parakeet on this PC, vLocal) or ``"deepgram"`` (Flux, the default)."""
    value = str(getattr(settings, "voice_stt_provider", "deepgram") or "deepgram").strip().lower()
    return "local" if value == "local" else "deepgram"


#: vLocal: what the pill says when a game or a call is on and the models on this PC are not loaded (review F3).
LOCAL_BUSY_NOTE = "You're in a game or a call, so I'm using the cloud this time; speech on this PC would slow it down"


def local_fallbacks(settings: Any, facts: dict[str, Any], *, busy: bool = False) -> None:
    """vLocal: speech on this PC that cannot run yet runs in the cloud for this conversation, with a note saying so:
    Yuki is never deaf or silent because of the setting.

    * Not downloaded (the chosen voice's own model, not just the plain fallback voice): the cloud. The plain voice
      speaks only for a chosen voice that is downloaded but fails to load this run.
    * Failed to load this run (recognition, or both the voice and the plain voice): the cloud.
    * ``busy`` (a game or a call is on) and the model is not loaded already: the cloud, when the cloud can work (a
      Deepgram key); loading about 1 GB and decoding on this PC would slow the game (``facts["local_busy"]``)."""
    stt_local, tts_local = facts.get("stt_provider") == "local", facts.get("tts_provider") == "local"
    if not (stt_local or tts_local):
        return
    from yuki.voice import local_models as lm

    root = str(getattr(settings, "voice_local_models_dir", "") or "") or None
    eng = lm.engine()
    broken = eng.broken
    loaded = eng.loaded() if busy else {}
    cloud_ok = bool(get_key(DEEPGRAM))

    def ready(key: str) -> bool:
        return lm.installed(key, root) and key not in broken

    if stt_local:
        if not ready("parakeet"):
            facts["stt_provider"] = "deepgram"
            facts["stt_note"] = ("Speech recognition on this PC isn't downloaded yet; using the cloud for now"
                                 if "parakeet" not in broken else
                                 "Speech recognition on this PC couldn't start; using the cloud for now")
        elif busy and cloud_ok and not loaded.get("parakeet"):
            facts["stt_provider"], facts["stt_note"], facts["local_busy"] = "deepgram", LOCAL_BUSY_NOTE, True
    if tts_local:
        voice = lm.parse_voice(getattr(settings, "voice_tts_voice", ""))
        fallback = str(getattr(settings, "voice_tts_fallback", "") or "deepgram")
        cloud = fallback if fallback != "local" else "deepgram"
        if not lm.installed(voice.family, root):
            facts["tts_provider"] = cloud
            facts["tts_note"] = "The voice on this PC isn't downloaded yet; using the cloud voice for now"
        elif voice.family in broken and not ready(lm.FALLBACK_VOICE.family):
            facts["tts_provider"] = cloud
            facts["tts_note"] = "The voice on this PC couldn't start; using the cloud voice for now"
        elif busy and cloud_ok and not loaded.get(voice.family):
            facts["tts_provider"], facts["local_busy"] = cloud, True
            facts.setdefault("tts_note", LOCAL_BUSY_NOTE)


def preflight(settings: Settings, *, need_headphones: bool = True, busy: bool = False) -> dict[str, Any]:
    """Check what a conversation needs before opening anything. Raises :class:`VoiceUnavailable`.

    Returns facts for the log, including ``tts_provider``: the configured one,
    or ``voice_tts_fallback`` (with ``tts_note`` saying why) when the configured
    one cannot speak. Speakers are no reason to refuse any more (the echo guard,
    docs/VOICE.md §11) unless ``voice_require_headphones`` asks for the v3.3 rule.
    ``busy``: a game or a call is on (speech on this PC then runs only if already loaded, :func:`local_fallbacks`).
    """
    facts: dict[str, Any] = {"tts_provider": settings.voice_tts_provider, "stt_provider": stt_provider(settings)}
    local_fallbacks(settings, facts, busy=busy)
    if (facts["stt_provider"] != "local" or facts["tts_provider"] == "deepgram") and not get_key(DEEPGRAM):
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
    tool: bool = False                 # kMind: a tool call began in this reply (the claim gate stops checking)
    one_round: int = 0                 # oVoice: this call's round in a ONE turn (0: no ONE round)
    one_fresh: bool = False            # oVoice: it answers the user's turn again (a rerun: the turn's block made again)
    one_thoughts: int = 0              # oVoice: thinking blocks an adaptive ONE round kept for the turn
    one_eager: bool = False            # oVoice: it answers Flux's eager end of turn (its final words still to come)
    #: final-B (one-voice RC A): the marker channel's reading of this reply's own text (content-free): no marker line
    #: (none of it spoken), the marker it began with, and whether it was held back as a verbatim repeat
    one_unmarked: bool = False
    one_marker: str | None = None
    one_repeat: bool = False
    #: voice20 review L1: what of this reply was spoken ends a sentence as a question (its own closing mark)
    one_asks: bool = False
    #: voice22 (review L3): the ids of the tool results its context ends with (:func:`_trailing_tool_ids`): the run a
    #: tool result started (``Conversation._opened_answer``)
    after_tools: tuple[str, ...] = ()


#: Tools whose spoken acknowledgement travels in the call itself (``say_first``), said as soon as it is written.
SAY_FIRST_TOOLS = frozenset({"tell_yuki", "open_url", "open_app", "end_conversation", "run_skill", "hand_over"})


def early_words(partial_json: str) -> str | None:
    """The ``say_first`` value of a tool call whose arguments are still streaming, once that string is whole.

    The mouth writes ``say_first`` first (its schema asks it to); the moment its string is complete in
    the streamed JSON it can be spoken, while the rest of the call is still being written (v3.5,
    docs/VOICE.md §5.2). Parsing the call's own JSON: ``None`` until the value is complete, or when
    the arguments have no ``say_first``. sVoiceFix (F6): only its words (:func:`~yuki.voice.mouth.say_first_words`: up
    to a line break, a ``<``, or a stray closing quote with only the call's ``,`` / ``}`` after it): a call that ran
    the rest of itself into the string is never spoken whole.
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
    words = say_first_words(value) if isinstance(value, str) else ""
    return words or None


def early_text(partial_json: str, name: str) -> str | None:
    """voice22 (A25): a string field of a tool call whose arguments are still streaming, once its value is whole
    (``None`` until then, or when it is not a string). Parsing the call's own JSON."""
    import json

    key = str(partial_json or "").find(f'"{name}"')
    if key < 0:
        return None
    colon = partial_json.find(":", key + len(name) + 2)
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
        return None
    return value if isinstance(value, str) else None


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


class ReplyToolCalls:
    """Every ``tool_use`` block of one streamed talker reply, in the order written (hotTools, 18 Oct).

    Pipecat 1.11's Anthropic service (``pipecat/services/anthropic/llm.py``, ``_process_context``) keeps one
    ``tool_use_block`` and one JSON accumulator, overwritten at each new tool_use block, and queues a single call when
    the reply stops for tools: a reply of two calls ("pause the music and mute the PC") ran only its last, and the first
    was lost without a trace. The talker's stream passes through :meth:`feed` (its events unchanged), which keeps each
    block's id, name and streamed arguments by the block's index; :meth:`complete` then puts the calls Pipecat dropped
    back in front of the one it queued, so the runner gets the reply's every call in order. Plumbing; never the words.
    """

    def __init__(self) -> None:
        #: ``{"id", "name", "json"}`` per tool_use block, in the order the reply wrote them.
        self.blocks: list[dict[str, Any]] = []
        self._at: dict[Any, dict[str, Any]] = {}

    def feed(self, event: Any) -> None:
        kind = getattr(event, "type", "")
        if kind == "content_block_start":
            block = getattr(event, "content_block", None)
            if getattr(block, "type", "") == "tool_use":
                entry = {"id": str(getattr(block, "id", "") or ""), "name": str(getattr(block, "name", "") or ""),
                         "json": ""}
                self.blocks.append(entry)
                self._at[getattr(event, "index", None)] = entry
        elif kind == "content_block_delta" and self.blocks:
            part = getattr(getattr(event, "delta", None), "partial_json", None)
            if isinstance(part, str):
                # by the block's index (a stream without one: the latest tool_use block, as Pipecat does)
                self._at.get(getattr(event, "index", None), self.blocks[-1])["json"] += part

    def last_id(self) -> str | None:
        return self.blocks[-1]["id"] if self.blocks else None

    def complete(self, calls: list[Any], make: Callable[..., Any]) -> tuple[list[Any], int, int]:
        """``calls`` (what Pipecat queued for this reply) with the reply's earlier tool_use blocks it dropped put back
        in front, in the order written, each made by ``make`` (Pipecat's ``FunctionCallFromLLM``) on the same context:
        ``(calls, put back, left out)``; a block whose arguments do not parse is left out (as Pipecat's own parse of
        the last one would fail the reply), counted. ``calls`` unchanged when it is not the tail of this reply's blocks
        (Pipecat already gave every call, or the list is another reply's)."""
        import json

        ids = [str(getattr(fc, "tool_call_id", "")) for fc in calls]
        known = [b["id"] for b in self.blocks]
        if not ids or len(ids) >= len(known) or known[-len(ids):] != ids:
            return list(calls), 0, 0
        context = getattr(calls[-1], "context", None)
        front: list[Any] = []
        bad = 0
        for block in self.blocks[:len(known) - len(ids)]:
            try:
                args = json.loads(block["json"]) if block["json"] else {}
            except ValueError:
                bad += 1
                continue
            front.append(make(context=context, tool_call_id=block["id"], function_name=block["name"],
                              arguments=args))
        return front + list(calls), len(front), bad


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
#: Final-B (all-questions RC5): a name's first sound can go into the word heard before it ("bring up the scored" for
#: "bring up discord"): a heard word of 5+ letters is also measured against the name with its first one or two letters
#: dropped (at most this many), when it and the word before it together are at most this much longer than the name.
NEAR_TAIL_HEAD = 2
#: ... and with part of the name gone, the rest must match closely: measured "scored" against Discord's tail 0.91 (the
#: final-B clip), "after" against Father's 0.80 (a clear sentence of the v3.5 checks, never named).
NEAR_TAIL_RATIO = 0.85
#: voice20 (final-D S12): a running app's name is measured by its tail with exactly this many first letters gone (the
#: first sound the word before took: "di|scord"). With one letter gone a prefixed brand leaves an everyday word: on the
#: user's 793 spoken turns (memory copy, every app of the timeline) "the cloud" was named iCloud, once; with two, none.
APP_TAIL_HEAD = 2
#: voice21 (final-D decision 3, 19 Oct: "not flag, but it should automatically understand it and work"): what a near
#: name of the Speech line is, by where it came from (a measured source, never the words).
NEAR_KINDS = {"screen": "a name on the screen", "running": "an app running now",
              "own": "one of their apps, sites or channels", "known": "a name you know"}
#: ... and what the note says of the closest near name of each heard stretch: it is what they most likely said, to be
#: used and named, never asked about. A farther one for the same heard words is named as less likely.
#: A person still goes as heard: whom a message goes to is the message skill's to settle (a send is never matched by
#: sound there). Review L1/L2 (voice21): the name goes in the call's own lines (say_first, say_when_done), never in
#: words written beside the call (a window call written with words on a Speech-line turn is held: ``_held``), and only
#: for an undoable act; something lasting keeps each brain's own lasting rule.
NEAR_ACT = ("for anything undoable, take each most likely name as what they said: act on it and name it in the call's "
            "say_first or say_when_done, never ask about it; a person still goes as heard, and anything else lasting "
            "keeps its own rule")


def heard_note(words: Any, keyterms: list[str], *, cut_before: bool = False,
               screen: list[str] | None = None, apps: list[str] | None = None,
               own: list[str] | None = None, measured: list[str] | None = None) -> tuple[str | None, int, int]:
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

    ``apps`` (voice20, final-D S12): the apps running on the desk (:func:`desk_app_names`). A heard word is named as
    close to one only by the tail measure (:data:`NEAR_TAIL_RATIO`: the word before took the name's first sound, "the
    scored" for "discord"), and only when no name above matched it: the looser whole-word measure of apps is what made
    plain command words brand notes ("you turn" ~ YouTube, lKeys review L1).

    ``own`` (voice21, final-D decision 3): the user's own apps and sites (contract C11, read with the keyterms:
    :func:`yuki.voice.keyterms.own_names`), measured exactly as ``apps`` (the tail shape, :data:`APP_TAIL_HEAD`), after
    them. A tail match names the word before with the heard word ("'the scored'"): it took the name's first sound.

    voice21 (decision 3): the note says each near name as what they most likely said (:data:`NEAR_ACT`: to be used and
    named in the reply, never asked about); for the same heard words, a farther name is named as less likely.

    ``measured`` (voice22, A4 / final-D S18, contract C14): the user's own apps, sites and channels for names21's
    measured match (:func:`measured_near`: ``names.best_match`` with ``as_name`` on each stretch of the heard words,
    never a name heard as written), so a garble the tail shape misses ("Porbex Kaladra" for Excalidraw) is named as
    what they most likely said before the model picks an exact wrong name itself.
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

    def tail_ratio(k: str, t: str, before: str, heads: Any = None) -> float:
        # Final-B (all-questions RC5): the name's first sound went into the word before ("the|scored" for
        # "dis|cord"): the heard word keeps the start of the name's tail (its first one or two letters dropped,
        # :data:`NEAR_TAIL_HEAD`) and most of its letters, and the two words together span about the name. Review fix
        # F5: the word before must have at least as many letters as the head it took ("the|scored" yes; "I|scored",
        # "a|..." no). Measures of the strings' letters and lengths only, no word list.
        if len(k) < LOW_WORD_MIN_LETTERS or not before or len(before) + len(k) > len(t) + NEAR_TAIL_HEAD:
            return 0.0
        tails = [t[j:] for j in (range(1, NEAR_TAIL_HEAD + 1) if heads is None else heads)
                 if len(t) - j >= LOW_WORD_MIN_LETTERS and len(before) >= j]
        return max((difflib.SequenceMatcher(None, k, tail).ratio() for tail in tails if tail[:1] == k[:1]
                    and k != t), default=0.0)

    def tail_closest(k: str, before: str, among: Any = None, heads: Any = None) -> str | None:
        pool = terms if among is None else among
        best = max(pool, key=lambda t: tail_ratio(k, t, before, heads), default=None)
        return best if best and tail_ratio(k, best, before, heads) >= NEAR_TAIL_RATIO else None

    # voice20 (final-D S12): the running apps, by the tail measure only, with exactly APP_TAIL_HEAD first letters gone
    # (a name too short for such a tail of LOW_WORD_MIN_LETTERS letters is never measured); a name already above is
    # measured as that
    running = {key(t): t for t in (apps or [])
               if len(key(t)) - APP_TAIL_HEAD >= LOW_WORD_MIN_LETTERS and key(t) not in terms}
    # voice21 (decision 3): the user's own apps and sites (contract C11), the same tail measure, after the running ones
    owned = {key(t): t for t in (own or [])
             if len(key(t)) - APP_TAIL_HEAD >= LOW_WORD_MIN_LETTERS and key(t) not in terms and key(t) not in running}
    named = {**owned, **running, **terms}
    # N9 (report 47): single words and pairs are both measured, and every near name is named, closest first:
    # recognition splits a name across two words ("hey meant" for Hemanth), and live a single word of the pair
    # ("niche") was named with another contact whose handle looks like it, which kept the pair from being measured.
    # voice21: each with the heard words' places (a tail match takes the word before too: it took the name's start)
    scored: list[tuple[float, str, str, set[int]]] = []
    for size in (1, 2):
        for i in range(len(spoken) - size + 1):
            window = spoken[i:i + size]
            k = key(" ".join(window))
            if (len(k) < 5 or k in terms or any(j in exact for j in range(i, i + size))
                    or (size == 2 and any(len(key(w)) < 3 for w in window))):
                continue
            best = closest(k)
            ratio = difflib.SequenceMatcher(None, k, best).ratio() if best else 0.0
            span = set(range(i, i + size))
            if best is None and size == 1 and i > 0:
                before = key(spoken[i - 1])
                best = tail_closest(k, before)
                ratio = tail_ratio(k, best, before) if best else 0.0
                for pool in ((running, owned) if k not in running and k not in owned else ()):
                    # voice21 (re-measure): an app or site name is often a prefix and an everyday word ("Re|anime"):
                    # a heard word that is its tail letter for letter is that word, never the name misheard (a
                    # mishearing re-spells the sound: "scored" for "scord")
                    pool = {t: v for t, v in pool.items() if t[APP_TAIL_HEAD:] != k}
                    if best is None and pool:
                        best = tail_closest(k, before, pool, (APP_TAIL_HEAD,))
                        ratio = tail_ratio(k, best, before, (APP_TAIL_HEAD,)) if best else 0.0
                if best is not None:
                    window, span = spoken[i - 1:i + 1], {i - 1, i}
            if best and all(t != named[best] for _r, _h, t, _s in scored):
                scored.append((ratio, " ".join(window), named[best], span))
    # voice22 (A4): names21's measured match of the user's own names, on each stretch of the heard words (never words
    # that are a known name as heard, never a name already named above)
    measured_names: set[str] = set()
    for ratio, h, t, span in measured_near(items, measured or (), skip=exact):
        if all(key(t) != key(x) for _r, _h, x, _s in scored):
            scored.append((ratio, h, t, span))
            measured_names.add(key(t))
    # voice21 (decision 3): of the names for the same heard words, the one that accounts for more of them (N9: the pair
    # "hey meant" is Hemanth, though "niche" alone spells closer to another contact), then the closest, is what they
    # most likely said; another for words of the same stretch is less likely
    near: list[tuple[str, str, bool]] = []
    taken: set[int] = set()
    for _r, h, t, span in sorted(scored, key=lambda s: (-len(s[3]), -s[0])):
        near.append((h, t, not (span & taken)))
        taken |= span
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
    parts: list[str] = []
    if spelled:
        parts.append("; ".join(f"'{h}' is a word spelled out letter by letter: {w}"
                               + (f", which is {m}" if m and key(m) == key(w) else f", most likely {m}" if m else "")
                               for h, w, m in spelled[:2]))
    if low:
        parts.append("unsure of " + ", ".join(f"'{w}' ({c:.2f})" for w, c in low[:5]))
    if near:
        def kind(t: str) -> str:
            k = key(t)
            return NEAR_KINDS["screen" if k in on_screen else "running" if k in running else "own"
                              if k in owned or k in measured_names else "known"]

        parts.append("; ".join(f"'{h}' sounds close to {t}, {kind(t)}"
                               + (f": they most likely said {t}" if first else ", less likely")
                               for h, t, first in near[:3]))
    if fused:
        parts.append("; ".join(f"'{h}' may be {t} said together with the next word" for h, t in fused[:2]))
    # A spelled word that is a known name exactly is no doubt (nothing to ask): only the others count.
    unsure_spelled = sum(1 for _h, w, m in spelled if not (m and key(m) == key(w)))
    note = "; ".join(parts)
    if near:
        note += ". " + NEAR_ACT[:1].upper() + NEAR_ACT[1:]   # voice21 (decision 3): a near name leads to action
    return (note or None), len(low), len(near) + len(fused) + unsure_spelled


#: voice22 (A4): marks recognition writes at a word's end that close a stretch of what was said (its own punctuation:
#: a clause or a sentence ends there). Each stretch is measured as a name given at its end (``names.best_match``
#: ``as_name``: the stretch's last words, never a part before them).
STRETCH_MARKS = ",.?!;:\u0964"


#: voice22 fix (review M1): an own name of at least this many letters standing as written in the words measured_near
#: would name means nothing was misheard there (names21's shortest name, ``names.MIN_LETTERS``).
LITERAL_MIN_LETTERS = 3


def measured_near(items: Any, names: Any, *, skip: Any = ()) -> list[tuple[float, str, str, set[int]]]:
    """voice22 (A4, final-D S18; names21 merge note 3, contract C14): names21's measured match of the user's own apps,
    sites and channels (``names``: :func:`yuki.voice.keyterms.own_names`) on the heard words (``items``: Flux's word
    list, ``{"word", ...}``): ``[(score, heard words, name, word places)]``, at most one a stretch.

    The words are cut into stretches at recognition's own punctuation (:data:`STRETCH_MARKS`); each stretch is measured
    as a name given at its end (``best_match(..., as_name=True, literal_ok=False)``: the stretch whole, or its last
    words, never a part before them; a name standing in it as written is no mishearing and is left out). The heard
    words named are the fewest last words of the stretch that hold the winner's score (``names.score``). A stretch
    whose named words include a place in ``skip`` (a known name heard as written) is left out, and so are named words
    in which any of ``names`` of :data:`LITERAL_MIN_LETTERS` letters or more stands as written (``names.literal``;
    review M1: "Open Python." with Pythonw on their list is Python said right, never a mishearing of Pythonw). The
    bar, the margin and the measure are names21's (calibrated on the user's turns); re-measured by voice22 on the
    user's past spoken turns before shipping (docs/research/final-test-2026-10-19/builds/voice22.md). ``best_match``,
    ``score`` and ``literal`` are imported inside ``try/except ImportError`` (contract C14 and its merge note):
    without them, nothing; ``TURN_BAR`` is read with names21's value as the default. Never raises."""
    try:
        from yuki.skills import names as names_mod
        from yuki.skills.names import best_match, literal, score
    except ImportError:
        return []
    try:
        turn_bar = float(getattr(names_mod, "TURN_BAR", 0.85) or 0.85)
        pool = [str(n) for n in (names or ()) if str(n or "").strip()]
        whole = [n for n in pool if sum(ch.isalnum() for ch in n) >= LITERAL_MIN_LETTERS]
        words = [str(w.get("word") or "") for w in (items or ()) if isinstance(w, dict)
                 and str(w.get("word") or "").strip()]
        if not pool or not words:
            return []
        clean = [w.strip(" ,.?!;:\"'()\u0964") for w in words]
        stretches: list[list[int]] = [[]]
        for i, w in enumerate(words):
            stretches[-1].append(i)
            if w.rstrip("\"')").endswith(tuple(STRETCH_MARKS)):
                stretches.append([])
        skip = set(skip or ())
        out: list[tuple[float, str, str, set[int]]] = []
        for places in stretches:
            places = [i for i in places if clean[i]]
            if not places:
                continue
            text = " ".join(clean[i] for i in places)
            got = best_match(text, pool, as_name=True, literal_ok=False)
            if got is None:
                continue
            name, top = str(got[0]), float(got[1])
            span: list[int] = places
            for n in range(1, len(places) + 1):
                tail = places[-n:]
                if score(" ".join(clean[i] for i in tail), name, as_name=True) >= top - 1e-9:
                    span = tail
                    break
            if skip & set(span):
                continue
            heard = " ".join(clean[i] for i in span)
            if any(literal(heard, n) for n in whole):
                continue   # review M1: one of their own names was said as written there: nothing was misheard
            if top < turn_bar and not _tail_shaped(span, clean, name, top, score):
                continue
            out.append((top, heard, name, set(span)))
        return out
    except Exception:  # noqa: BLE001 - the Speech line goes without it
        return []


def _tail_shaped(span: list[int], clean: list[str], name: str, top: float, score: Callable[..., float]) -> bool:
    """voice22 (A4): whether a measured match of two heard words came from names21's tail shape, with the name's first
    letters standing at the end of the word before as written ("Porb|ex Kaladra" for "Ex|calidraw": the word boundary
    moved, the letters stayed): the word before ends in the name's first one or two letters (:data:`APP_TAIL_HEAD`),
    and the pair scores more than its last word alone and more than its two words read as one (names21's own
    ``score``). names21's other head rule, a short word that sounds like the head ("the" for "di"), is left to its
    whole-turn bar: re-measured, below that bar it made "are saying" a channel whose spoken form is an everyday word.
    Letters and names21's measure only."""
    if len(span) != 2:
        return False
    fold = lambda text: "".join(ch for ch in str(text or "").casefold() if ch.isalpha())   # noqa: E731
    said = said_site(name) if " " not in name.strip() and "." in name.strip(".") else name.split("(")[0]
    before, last, letters = fold(clean[span[0]]), fold(clean[span[-1]]), fold(said)
    if not last or not any(len(letters) - j >= LOW_WORD_MIN_LETTERS - 1 and len(before) > j
                           and before.endswith(letters[:j]) for j in range(1, APP_TAIL_HEAD + 1)):
        return False
    joined = clean[span[0]] + clean[span[-1]]
    return top > max(score(clean[span[-1]], name, as_name=True), score(joined, name, as_name=True)) + 1e-9


#: voice20 (final-D S12): at most this many running apps are measured (a busy desk's own list, never cut by name).
DESK_APPS_MAX = 40


def desk_app_names(overview: Any) -> list[str]:
    """voice20 (final-D S12): the apps running on the desk as the talker's desk block names them, from a desktop read
    already made (the hands' last ``Snapshot.overview``; no new read): each window's program (its image name without
    ".exe", as ``situation`` names it) and the apps running with no window (their Start menu names), each once, in that
    order, at most :data:`DESK_APPS_MAX`. The Speech line's app names (:func:`heard_note` ``apps``). Never raises."""
    if not hasattr(overview, "get"):
        return []
    out: list[str] = []
    seen: set[str] = set()

    def add(name: Any) -> None:
        text = " ".join(str(name or "").split())
        if text.casefold().endswith(".exe"):
            text = text[:-4].strip()
        if text and text.casefold() not in seen:
            seen.add(text.casefold())
            out.append(text)

    try:
        for w in overview.get("windows") or []:
            if hasattr(w, "get"):
                add(w.get("process_name") or w.get("app"))
        for a in overview.get("background_apps") or []:
            if hasattr(a, "get"):
                add(a.get("name"))
    except Exception:  # noqa: BLE001 - a desk read of another shape: what was named so far
        pass
    return out[:DESK_APPS_MAX]


#: The result of a call held on unsure words (``Conversation._hold``) when recognition named nothing (as before).
HELD_PLAIN = (
    "Not done: nothing was done, so never say it is done, open or playing. The words you wrote with it were "
    "not said either: speech recognition was unsure of their "
    "words (the Speech line) and you wrote words and this call together. If something that matters "
    "may be misheard, ask them now in a few words, the question alone, with no call and no promise. "
    "If their words were clear after all, call it again now, alone, with its say_first.")
#: lSpeed (live test 17 Oct, problem 4.4): the held call's result names the unsure words, so the talker confirms only
#: those. Live, "Right to harness on Discord asking when he'll join the VC" was held on "harness" and the talker asked
#: "what do you want to ask Hemanth about the VC?", which they had just said.
HELD_UNSURE = (
    "Not done: nothing was done, so never say it is done, open or playing. The words you wrote with it were not said "
    "either: you wrote words and this call together on a turn whose words speech recognition was unsure of, and it "
    "was unsure only of this: {unsure}. Everything else they said was heard clearly: never ask about any of it again "
    "(what they want, who it is for, what to say). A name the Speech line says they most likely said settles that "
    "word. If one of those unsure words matters to this call and nothing on the screen or in their words settles it, "
    "confirm that word alone in a few words (\"Sam, right?\"): that question "
    "is your whole reply, with no call and no promise; a person's name too. If it is settled after all, call it again "
    "now, alone, with no words, with its say_first. One or the other, never both in one reply: a call beside words "
    "is held again, and the words are never said.")


def held_note(heard: str | None) -> str:
    """The result of a held call: :data:`HELD_UNSURE` naming what recognition was unsure of (the observer's Speech
    line, :func:`heard_note`), else :data:`HELD_PLAIN`."""
    unsure = " ".join(str(heard or "").split())[:300]
    return HELD_UNSURE.format(unsure=unsure) if unsure else HELD_PLAIN


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


def near_terms(user_names: Any, keyterms: Any, cap: int, names: Any = ()) -> list[str]:
    """The names a heard word may be a mishearing of (the Speech line's near test, :func:`heard_note`): xGlue (lKeys
    review r1 L1, its interface note for this file) the people's names and the corrections' right names among this
    conversation's keyterms (``names``: :func:`yuki.voice.keyterms.name_terms`, the last read's tags), plus the user's
    own names; with none of them tagged (no read yet, or a read that fell back to ``speech_names``) the whole
    :func:`~yuki.voice.keyterms.vocabulary`, as before. Measured on the user's 858 turns: 76 -> 48 flagged, the brand
    near-matches on plain commands gone ("you turn" ~ YouTube), each of which held a hand and skipped the early router
    start. Identity of our own lists, never a reading of words."""
    def fold(term: Any) -> str:
        return " ".join(str(term or "").split()).casefold()

    near = vocabulary(user_names, keyterms, cap)
    tagged = {fold(t) for t in (names or ())} & {fold(t) for t in (keyterms or ())}
    if not tagged:
        return near
    own = {fold(n) for n in (user_names or ())}
    return [t for t in near if fold(t) in tagged or fold(t) in own]


#: voice20 (final-D S22j): Deepgram's own limit on one keyterm: "Each keyterm must be 100 characters or fewer"; one
#: longer term makes Flux reject the whole mid-call update (live final-D: 7 + 5 rejected updates; a screen name taken
#: from a drawn app's recent window titles has no 32-character cap).
KEYTERM_MAX_CHARS = 100


def keyterm_list(base: list[str], extra: list[str], cap: int) -> list[str]:
    """Speech recognition's keyterms: ``base`` then ``extra``, each once (case-insensitive), at most ``cap``; a term
    longer than :data:`KEYTERM_MAX_CHARS` is skipped (voice20: the recogniser's own limit, a length, never words)."""
    out: list[str] = []
    seen: set[str] = set()
    for term in [*base, *extra]:
        term = " ".join(str(term or "").split())
        if len(term) > KEYTERM_MAX_CHARS:
            continue
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


#: vRound2 (sVoiceFix "Left", lean #10): the reply-level rule. Pipecat runs a reply's calls in parallel, so when one
#: run_skill is refused before it runs and goes to the thinker by code with their whole request, a call beside it still
#: ran: "put something on YouTube and ARC browser" got web.open_video (refused: nothing offered, to the thinker) and the
#: learned "Open Excalidraw in Arc", which opened Excalidraw and asked "Keep this as your usual?". Now such a reply's
#: other calls are left out before Pipecat runs them, and each gets this result. Calls that do nothing on the desktop
#: or for a job (stay quiet, a look into memory, a cancel) are never left out.
SIBLING_SKIPPED: dict[str, str] = {
    "not_run": "Left out, not run: another call of this same reply passes their whole request to the thinker by "
               "code, so nothing is done twice.",
    "how_to_handle_it": "Say nothing about it.",
}
#: The talker's calls that never act or start a job (never left out by :func:`reply_skips`).
NOT_ACTING: frozenset[str] = frozenset({"stay_quiet", "end_conversation", "go_quiet", "more_of_result",
                                        "check_progress", QUICK_MEMORY["name"], "cancel_hand_over",
                                        "cancel_tell_yuki"})


#: pVoice (vRound2 "found, not built"): a hand called in a talker run that no user turn started (a result's, a
#: thinker's line) while the thinker holds the user's current request (``VoiceBridge.turn_handed_over``: a job made for
#: this very turn, still open) is that same request: the thinker does all of it, so the hand is not run. vRound2's
#: layer 1 saw the talker scroll, or bring Spotify up with a "then", in 3 of 11 such runs. A call in the reply the
#: user's own words started (a hand beside the hand-off) and anything after their next words run as before.
THINKER_HOLDS: dict[str, str] = {
    "not_run": "Not run: the thinker has this request now and does all of it, so nothing was done here.",
    "how_to_handle_it": "Say nothing about it.",
}
#: Hands never stopped by that rule (an answer to the talker's own question, which acts on nothing).
THINKER_HOLDS_SPARES: frozenset[str] = frozenset({"keep_usual"})


def thinker_holds(name: str, bridge: Any) -> bool:
    """pVoice: whether the hand ``name`` is stopped by :data:`THINKER_HOLDS`: the talker's run that called it was not
    started by the user's words (``bridge.user_turn()`` false) and the user's current turn is with the thinker, open
    (``bridge.turn_handed_over()``). State only, never the words; anything unknown runs the hand (``False``)."""
    if name in THINKER_HOLDS_SPARES or bridge is None:
        return False
    try:
        by_user = getattr(bridge, "user_turn", None)
        held = getattr(bridge, "turn_handed_over", None)
        if not callable(by_user) or not callable(held) or by_user():
            return False
        return bool(held())
    except Exception:  # noqa: BLE001 - unknown: the hand runs as before
        return False


def reply_skips(calls: Any, hands: Any) -> dict[str, str]:
    """vRound2: the calls of one reply that are left out (their tool call ids, each with a code word), by the
    reply-level rule (:data:`SIBLING_SKIPPED`). ``calls``: ``(tool call id, name, arguments)`` of one reply, in order;
    ``hands``: the talker's hands (``yuki.voice.hands.Hands.refused_first``: a run_skill that will be refused before
    running and go to the thinker by code with their whole request, by its data).

    When the reply has such a run_skill and another call that acts or hands over, the whole request goes to the thinker
    once and nothing else of the reply runs: a hand-off of the same reply carries it (``"handoff"``: it may have begun
    the thinker already while its call was written, so the refused run_skill is left out too), else the refused run_skill
    does by its own fallback (``"refused_skill"``). Names and data only, never the words. Never raises (``{}``)."""
    try:
        calls = [(str(cid), str(name or ""), args) for cid, name, args in calls or ()]
        if len(calls) < 2 or hands is None or not callable(getattr(hands, "refused_first", None)):
            return {}
        acting = [c for c in calls if c[1] not in NOT_ACTING]
        lead = next((c for c in acting if c[1] == "run_skill" and hands.refused_first(c[2])), None)
        if lead is None or len(acting) < 2:
            return {}
        handoff = next((c for c in acting if c[1] in HANDOFF_TOOLS), None)
        keep = handoff or lead
        why = "handoff" if handoff is not None else "refused_skill"
        return {cid: why for cid, _name, _args in acting if cid != keep[0]}
    except Exception:  # noqa: BLE001 - the reply runs as it is
        return {}


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
            #: hotTools: every tool_use block of the reply being streamed (Pipecat queues only the last; see
            #: :class:`ReplyToolCalls`), taken by :meth:`run_function_calls` when that reply's calls come in.
            self._yuki_reply_tools: ReplyToolCalls | None = None
            #: integ22 (desk22 merge note 1, D4): the names of the reply's function calls (every one, the hand-offs
            #: included), given to the hands when the reply ends, then cleared (a plain answer gives none).
            self._yuki_reply_names: tuple[str, ...] = ()

        def _yuki_every_call(self, function_calls: Any) -> Any:
            """hotTools: the reply's every call, in the order written, where Pipecat queued only its last (its stream
            loop keeps one tool_use block). Only for the reply just streamed (its last block is Pipecat's last call);
            a list from elsewhere (an eager reply's held calls, already whole) is left as it is."""
            tools = self._yuki_reply_tools
            if tools is None or not function_calls:
                return function_calls
            last = str(getattr(function_calls[-1], "tool_call_id", ""))
            if last != tools.last_id():
                return function_calls
            self._yuki_reply_tools = None
            from pipecat.services.llm_service import FunctionCallFromLLM

            calls, back, bad = tools.complete(list(function_calls), FunctionCallFromLLM)
            if back or bad:
                hook("calls_restored", len(calls), back, bad)
            return calls

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
            # hotTools: Pipecat hands over only the reply's last call; the ones it dropped go back in front, so
            # everything below (holds, words, the reply rule, the order gate) sees the whole reply as written
            function_calls = self._yuki_every_call(function_calls)
            self._yuki_reply_names = tuple(str(getattr(fc, "function_name", "") or "") for fc in function_calls or [])
            for fc in function_calls or []:
                # review r1 L3: the call's arguments decide too (a sound-only skill runs like the sound hands); a
                # stand-in without would_hold_call answers by the name alone
                held = hook("would_hold_call", getattr(fc, "function_name", ""), getattr(fc, "arguments", None),
                            default=None)
                if held is None:
                    held = hook("would_hold", getattr(fc, "function_name", ""), default=False)
                self._yuki_hold[str(getattr(fc, "tool_call_id", fc))] = bool(held)
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
            if function_calls and len(function_calls) > 1:
                # vRound2: the reply-level rule (reply_skips), decided before Pipecat runs the calls in parallel; a
                # call held until the user answers is no part of it (nothing of it runs now anyway)
                hook("reply_calls", [(str(getattr(fc, "tool_call_id", fc)), getattr(fc, "function_name", ""),
                                      getattr(fc, "arguments", None)) for fc in function_calls
                                     if not self._yuki_hold.get(str(getattr(fc, "tool_call_id", fc)), False)])
            if function_calls:
                hook("one_reply", function_calls)   # oVoice: the ONE driver keeps the reply's calls, in order
            await super().run_function_calls(function_calls)

        async def push_frame(self, frame: Any, direction: Any = None) -> None:
            from pipecat.frames.frames import (
                EagerEndOfTurnCancelFrame, InterruptionFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
                LLMTextFrame, UserStoppedSpeakingFrame,
            )
            from pipecat.processors.frame_processor import FrameDirection

            direction = FrameDirection.DOWNSTREAM if direction is None else direction
            if direction == FrameDirection.DOWNSTREAM and isinstance(frame, LLMFullResponseStartFrame):
                # integ22 (D4): a new reply's call names start empty (a speculative reply's held calls, released after
                # its end, never stand for the next reply)
                self._yuki_reply_names = ()
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
                # integ22 (desk22 merge note 1, D4): the hands learn the reply ended and which calls it made (a
                # deferred two-part tidy step is taken, left to a hand of it, or dropped on a hand-off)
                names, self._yuki_reply_names = self._yuki_reply_names, ()
                hook("hands_reply_done", names)
                # oVoice: a ONE round's reply with no call ends the turn (the agent decided it is done)
                hook("one_reply_end", self._yuki_call,
                     bool(getattr(getattr(self, "_speculation_gate", None), "is_speculating", False)))
            if isinstance(frame, UserStoppedSpeakingFrame):
                pending, self._yuki_early_pending = self._yuki_early_pending, None
                if pending is not None:
                    call_id, screen = pending if isinstance(pending, tuple) else (pending, False)
                    hook("handoff_began", call_id, screen)  # the turn is confirmed: the thinker starts now
            elif isinstance(frame, (EagerEndOfTurnCancelFrame, InterruptionFrame)):
                self._yuki_early_pending = None
                self._yuki_words = None  # a cut reply's held words are never said
                if isinstance(frame, EagerEndOfTurnCancelFrame):
                    hook("words_withdrawn")  # integ23 S2 (c): a router decision on these partial words goes whole
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

        async def _yuki_say_early(self, call: LlmCall, tool_id: str, words: str, name: str = "",
                                  args: str = "") -> None:
            """Speak a call's say_first now, while the rest of the call is still being written. voice22 (A25): not
            while a game is in front and the gate holds the act (``bridge.say_waits_for_game``, read off the loop;
            ``args``: the call's JSON so far, for a skill's key): the call decides again once it is whole, before it
            runs (``VoiceBridge._ack_if_silent``)."""
            from pipecat.frames.frames import TTSSpeakFrame

            from yuki.voice.bridge import GAME_GATED_SAYS, say_waits_for_game

            if name in GAME_GATED_SAYS:
                try:
                    waits = await asyncio.to_thread(say_waits_for_game, name, early_text(args, "skill") or "")
                except Exception:  # noqa: BLE001 - no read: as before
                    waits = False
                if waits:
                    held = getattr(hooks, "early_held", None)
                    if callable(held):
                        held(call, name)
                    return
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
            self._yuki_reply_tools = None   # hotTools: set again when this reply's stream opens
            self._yuki_context = context
            call = LlmCall(entered=time.monotonic())
            self._yuki_call = call
            # E3a (v3.17): the router decides alongside this call whether the turn needs the thinker (off by default).
            call.user_turn = _last_role(context) == "user"
            call.after_tools = _trailing_tool_ids(context)
            hook("route_turn", context, call)
            one = hook("one")
            if one is not None:
                # oVoice: the ONE turn this call belongs to, and the budget's round before every model call of it;
                # spent (the rounds, the hard stop): code said its line and no model call is made
                call.one_fresh = bool(call.user_turn and hook("rerun", default=False))
                # the gate's speculation flag is set for this inference before this method runs (process_frame)
                call.one_eager = bool(getattr(getattr(self, "_speculation_gate", None), "is_speculating", False))
                try:
                    go = await one.begin(context, call)
                except Exception as exc:  # noqa: BLE001 - the reply still goes (every act fails closed at its gate)
                    one._log("begin_error", error=type(exc).__name__)
                    go = True
                if not go:
                    return
            try:
                if one is None or one.needs_reads(call):
                    self._yuki_lookup, self._yuki_desk = await hooks.before_call(context, call)
                else:
                    self._yuki_lookup, self._yuki_desk = None, None   # oVoice: the turn's block is frozen already
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
            one = hook("one")
            if one is not None:
                return self._yuki_one_params(params, one)
            # bTalk: the lean talker's core (design 3) when the conversation has one; else the full one, as before
            talker = "lean" if hook("talker", default="full") == "lean" else "full"
            base = LEAN_MOUTH_PROMPT if talker == "lean" else MOUTH_PROMPT
            composed = self._settings.system_instruction or base
            tail = composed[len(base):] if composed.startswith(base) else composed
            call = hooks.call()
            channel = hook("channel", default="guard")
            params["system"] = mouth_system_blocks(memory, tail=[tail.strip()], call=call,
                                                   call_evidence=hook("call_evidence", default=None),
                                                   channel=channel, hands=bool(hook("hands", default=True)),
                                                   marks=bool(hook("marks", default=True)), talker=talker)
            messages = params.get("messages")
            if channel == "marker":
                messages = with_markers(messages)   # its earlier replies as the channel writes them (v3.16)
            params["messages"] = with_volatile(messages, volatile_block(
                lookup=self._yuki_lookup, desk=self._yuki_desk, call=call, heard=hooks.heard(),
                named=bool(hook("named", default=False)), work=hook("work", default=None),
                cut=hook("cut", default=None), after_goodbye=bool(hook("after_goodbye", default=False)),
                channel=channel, rerun=bool(hook("rerun", default=False)), state=hook("state", default=None),
                talker=talker))
            return params

        def _yuki_one_params(self, params: Any, one: Any) -> Any:
            """oVoice (``brain = "one"``): the ONE agent's request. System: ``yuki.one.prompt.voice_blocks`` (the lean
            talker's layers and cache breakpoints with the ONE core and tools paragraph; the you-card; the last hour).
            Messages: the turn's block (time, Situation, lookup, notes) made once at round 1 and riding on their own
            message every round (``OneVoice.volatile``, :func:`with_volatile_at`). Thinking: the turn's mode from round 2
            (``OneVoice.thinking``)."""
            from yuki.one.prompt import ONE_CORE, voice_blocks

            composed = self._settings.system_instruction or ONE_CORE
            tail = composed[len(ONE_CORE):].strip() if composed.startswith(ONE_CORE) else ""
            call = hooks.call()
            channel = hook("channel", default="guard")
            params["system"] = voice_blocks(memory, tail=[tail] if tail else [], call=call,
                                            call_evidence=hook("call_evidence", default=None),
                                            marker=channel == "marker", marks=bool(hook("marks", default=True)))
            messages = params.get("messages")
            if channel == "marker":
                messages = with_markers(messages)   # its earlier replies as the channel writes them (v3.16)
            current = None if getattr(self, "_yuki_warming", False) else self._yuki_call

            def make() -> dict[str, Any]:
                return volatile_block(
                    lookup=self._yuki_lookup, desk=self._yuki_desk, call=call, heard=hooks.heard(),
                    named=bool(hook("named", default=False)), work=None, cut=hook("cut", default=None),
                    after_goodbye=bool(hook("after_goodbye", default=False)), channel=channel,
                    rerun=bool(hook("rerun", default=False)), state=one.state(hook("state", default=None)),
                    talker="lean")

            block, anchor = one.volatile(current, make)
            params["messages"] = (with_volatile_at(messages, block, anchor) if anchor
                                  else with_volatile(messages, block))
            one.thinking(params, current)
            return params

        def _sync_cancel_tools(self) -> None:
            """Pipecat's cancel tools, with the talker's own description of when to cancel (v3.9)."""
            super()._sync_cancel_tools()
            from pipecat.adapters.schemas.function_schema import FunctionSchema

            from yuki.voice.mouth import CANCEL_HAND_OVER_DESCRIPTION, CANCEL_TELL_YUKI_DESCRIPTION

            tools = getattr(getattr(self, "_adapter", None), "builtin_tools", None)
            for name in list(getattr(self, "_cancel_tool_names", ()) or ()):
                old = tools.get(name) if isinstance(tools, dict) else None
                # bTalk: the lean talker's cancel_hand_over gets its own words (it names hand_over)
                wanted = CANCEL_HAND_OVER_DESCRIPTION if name == "cancel_hand_over" else CANCEL_TELL_YUKI_DESCRIPTION
                if old is not None and getattr(old, "description", None) != wanted:
                    tools[name] = FunctionSchema(name=old.name, description=wanted,
                                                 properties=old.properties, required=old.required)

        async def _cancel_tool_handler(self, params: Any) -> None:
            # A successful cancel settles without another turn: the mouth says it is
            # stopping in the same reply as the call (measured: the extra turn made it
            # greet the user out of nowhere). A refusal still gets a turn, to react to it.
            from pipecat.frames.frames import FunctionCallResultProperties

            original = params.result_callback
            target = str(getattr(params, "function_name", "") or "").removeprefix("cancel_")
            if target in HANDOFF_TOOLS and not self._yuki_running(target):
                # final-B (contract C4, all-messaging RC3): no hand-over call is running, but a run_skill of this
                # turn may have passed its request on to the bridge (that job is invisible to Pipecat's cancel):
                # the bridge cancels it (``VoiceBridge.cancel_passed_on``, guarded). voice20 (final-D S11): it settles
                # with no talker run either way; when this reply said no words before the call (a stream fact: its
                # words went to the voice, ``_yuki_spoke``), the bridge says the job's own "not sent" ending by code
                said = str(getattr(params, "tool_call_id", "") or "") in self._yuki_spoke
                if hook("cancel_passed_on", said, default=None):
                    note = ("Stopped: the request it passed on will not go on." if said else
                            "Stopped: the request it passed on will not go on; Yuki's voice says so by code.")
                    await original({"cancelled": True, "note": note},
                                   properties=FunctionCallResultProperties(run_llm=False))
                    return
                if hook("passed_on_sent", default=None):
                    # integration review VC-2: its message went before the cancel: nothing was stopped, and the talker
                    # says so in its own words (a turn of its own), never that it will not go on
                    await original({"cancelled": False, "note": "Not stopped: what it passed on had already been "
                                                                "sent before the cancel."})
                    return

            async def settle(result: Any, *, properties: Any = None) -> None:
                if isinstance(result, dict) and result.get("cancelled"):
                    properties = FunctionCallResultProperties(run_llm=False)
                await original(result, properties=properties)

            params.result_callback = settle
            await super()._cancel_tool_handler(params)

        def _yuki_running(self, target: str) -> bool:
            """Whether a call of ``target`` that Pipecat's cancel tool can stop is running (its own task table, read
            as its handler reads it); unknown: running (Pipecat's own handler then answers)."""
            try:
                return any(item.function_name == target and self._is_cancellable(item.registry_item)
                           for item in list(self._function_call_tasks.values()))
            except Exception:  # noqa: BLE001 - another Pipecat: its own handler decides
                return True

        async def _yuki_open(self, api_call: Any, params: Any) -> tuple[Any, list[Any]]:
            """Open the reply's stream and read its head (the events before its first content block), with failover
            (v3.5).

            The mouth had none (the reliability builder's note, 2026-09-27). As ``yuki.agent.failover``
            does for the brain: a failure that says nothing about the request (throttled, overloaded, 5xx,
            timeout, no connection) is retried once on the same target after a short backoff, a missing
            model or profile (404/424) moves on at once, then the chain of ``Settings.model_fallbacks`` for
            the mouth's model (the global profile from us-east-1, the US profile, then Sonnet). The head
            is read here because Bedrock often reports throttling as the stream's first event. A
            target that worked is kept for ``failover_sticky_s``. Logged ``model_retry`` /
            ``model_failover`` (content-free).

            kMind (review M2): a reply the model declined before any output (Sonnet 5.5's safety classifiers: HTTP 200,
            ``stop_reason`` ``refusal``, no content) is run once more on the first target of the chain with another
            model (thinking off as that model takes it), never made sticky (a decline is about this request); logged
            ``model_refusal`` (content-free). With no other model, or a second decline, the declined reply goes on as
            before (silent: the reply watch answers the turn again).
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
            declined: tuple[str, Any, list[Any]] | None = None   # (the model, its stream, its head)
            for index in range(start, len(chain)):
                target = chain[index]
                model = str((target or {}).get("model") or params.get("model") or "")
                if declined is not None and model == declined[0]:
                    continue   # the same model declines the same request again
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
                            moved_params = adapt_params(params, target["model"])
                            off = params.get("thinking")
                            if isinstance(off, dict) and off.get("type") in ("disabled", "between_tools"):
                                moved_params["thinking"] = thinking_off(target["model"])   # kMind: each model's own
                            response = await client.beta.messages.create(**moved_params)
                        stream = response.__aiter__()
                        head, refused = await read_head(stream)
                        if refused and declined is None:
                            declined = (model, stream, head)
                            hook("model_declined", "declined", target)
                            break   # once, on the next target with another model
                        if declined is not None:
                            hook("model_declined", "refused_again" if refused else "answered", target)
                        else:
                            hooks.model_ok(index, target)
                        return stream, head
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
            if declined is not None:
                return declined[1], declined[2]   # no other model answered: the declined reply, as before
            assert last is not None
            raise last

        async def _create_message_stream(self, api_call: Any, params: Any) -> Any:
            call = self._yuki_call
            if call is not None:
                call.sent = time.monotonic()
            stream, head = await self._yuki_open(api_call, params)
            # hotTools: every tool_use block of this reply, seen before anything below drops or edits an event
            tools = ReplyToolCalls()
            self._yuki_reply_tools = tools

            async def events() -> Any:
                for event in head:
                    tools.feed(event)
                    yield event
                async for event in stream:
                    tools.feed(event)
                    yield event

            response = events()
            if call is None:
                return response
            call.opened = time.monotonic()
            one = hook("one")
            # oVoice: an adaptive round of a ONE turn keeps its thinking blocks (Pipecat stores them with their
            # signatures) until the turn ends; every other reply drops them, as before
            keep_thoughts = bool(one is not None and one.keeps_thoughts(call))
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
                thoughts: set[Any] = set()   # kMind (review L1): the indexes of thinking blocks, dropped
                try:
                    async for event in response:
                        now = time.monotonic()
                        if call.first_event is None:
                            call.first_event = now
                        kind = getattr(event, "type", "")
                        if keep_thoughts and kind == "content_block_start" and getattr(
                                event.content_block, "type", "") in ("thinking", "redacted_thinking"):
                            call.one_thoughts += 1
                        elif kind == "content_block_start" and getattr(event.content_block, "type", "") in (
                                "thinking", "redacted_thinking"):
                            # kMind (review L1): Sonnet 5.5's notes between tool calls (``between_tools``) are never
                            # spoken, and kept out of the conversation: its history is edited every turn (volatile
                            # block, markers, chronological order), and a replayed thinking block after an edit is
                            # a 400 on accounts that enforce the history check; without the blocks there is none
                            thoughts.add(getattr(event, "index", None))
                            continue
                        if kind in ("content_block_delta", "content_block_stop") and thoughts and \
                                getattr(event, "index", None) in thoughts:
                            continue
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
                            call.tool = True
                            if call.first_text is not None and self._yuki_words is None:
                                # The reply's words are complete (it goes on to a call): the voice says the
                                # sentence waiting for its lookahead now, not when the call is written (v3.7).
                                await self.push_frame(_words_done_frame())
                            # On a doubtful turn nothing is said before the reply is judged (its words wait too).
                            said = name not in SAY_FIRST_TOOLS or self._yuki_words is not None
                            # The mouth has decided to hand over: the thinker starts on the user's own words while
                            # the call is still being written (v3.6), as soon as its first field says whether the
                            # window in front is to be read for it (v3.9, a few tokens in).
                            # kMind: hand_over starts when its call is complete (its understanding, written last,
                            # goes with the request: contract C3); tell_yuki on its first field, as before
                            early_wait = name == "tell_yuki" and call.user_turn
                        elif (kind == "content_block_delta" and tool_id is not None
                              and hasattr(event.delta, "partial_json")):
                            args += event.delta.partial_json or ""
                            if early_wait:
                                flag = early_flag(args, "screen")
                                if flag is not None or '"say_first"' in args:
                                    early_wait = False
                                    if self._speculation_gate.is_speculating:
                                        self._yuki_early_pending = (tool_id, bool(flag))  # once the turn is confirmed
                                    else:
                                        hook("handoff_began", tool_id, bool(flag))
                            words = early_words(args) if not said else None
                            if words is not None:
                                said = True
                                if call.first_text is None:  # a reply that already spoke keeps its own words
                                    await self._yuki_say_early(call, tool_id, words, name, args)
                        yield event
                finally:
                    if channel is not None:
                        channel.finish()
                        hook("marker_report", call, channel)

            return timed()

    return LayeredAnthropicLLMService


def skills_on(settings: Any) -> bool:
    """Phase S: whether the skill path is on (``Settings.skills``, default on)."""
    try:
        from yuki.skills import enabled

        return enabled(settings)
    except Exception:  # noqa: BLE001 - no skill package: the old path
        return False


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
                TTSSpeakFrame,
            )

            if isinstance(frame, TTSSpeakFrame):
                # kMind (review L6): words said by code, not the talker's reply: never held by the claim check
                note = getattr(getattr(self, "_yuki_guard", None), "code_line", None)
                if callable(note):
                    note(strip_tags(str(getattr(frame, "text", "") or "")))
            elif isinstance(frame, LLMFullResponseStartFrame):
                # A new reply: a look flagged after the last reply's end (a speculative reply's held calls run once
                # the turn is confirmed, after its end has passed) is not this reply's (review fixes).
                self._yuki_answer_next = False
                # voice20 (final-D S22h): its lines get their own one claim-gate wait (the voices checking a line
                # before sending it know a reply's start only here)
                new_reply = getattr(getattr(self, "_yuki_guard", None), "new_reply", None)
                if callable(new_reply):
                    new_reply()
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
    claim_dropped: bool = False          # kMind: a line of its reply claimed something done with nothing done (dropped)
    claim_rerun: bool = False            # ... and it was answered again for that (the rerun is told so)
    opened_quiet: bool = False           # voice21 (S10): its quiet marker on their opening words was waived once
    quiet_refused: bool = False          # integration F4: its quiet on their opening words was refused (S10, any path)
    # vGame (5 Oct): said to Yuki by measured evidence, and what the fast path did with it
    named: bool = False                  # the name was heard with it (the wake word), or it came right after the name
    fast: str | None = None              # "name" (the name alone, answered at once) | "end" (D1: the router ended it)
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
        #: x3 (IF-words): the user's final turns that reached the talker (the fast path's own, the name alone, never
        #: do), with when each was heard (monotonic), oldest first: the current exchange for the hands'
        #: commit check and a held call's words is cut from these (``Conversation._turn_reached``; content, RAM only).
        self.recent_heard: deque[tuple[float, str]] = deque(maxlen=8)
        #: ``(words)``: the user's turn ended with these words (the thinker's request pack is built then, v3.18).
        self.on_final_words: Callable[[str], None] | None = None
        #: The conversation's spoken-line check (yuki.voice.speechguard), or ``None``.
        self.guard: Any = None
        #: Names on the screen in front (yuki.voice.screennames, v3.10): a heard word close to one is named too.
        self.screen_terms: list[str] = []
        #: voice20 (final-D S12): ``() -> [app names]``, the apps running on the desk (``desk_app_names``, from the
        #: hands' last desktop read; set by the conversation): a heard word is named with one by the tail measure only.
        self.desk_apps: Callable[[], list[str]] | None = None
        #: voice21 (final-D decision 3): ``() -> [names]``, the user's own apps and sites (contract C11, read beside the
        #: keyterms: ``keyterms.own_names``; set by the conversation): measured as ``desk_apps`` are, after them.
        self.own_names: Callable[[], list[str]] | None = None
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
        #: oVoice (phase 4): ``()`` at Flux's start of turn, ``(words)`` at each final transcript (any words): a
        #: ONE send countdown pauses on their speech and their words decide (set by the conversation under one only).
        self.on_user_started: Callable[[], None] | None = None
        self.on_user_words: Callable[[str], None] | None = None

    def _desk_apps(self) -> list[str]:
        """voice20 (final-D S12): the apps running on the desk for the Speech line (:attr:`desk_apps`), ``[]`` when
        unknown. Never raises."""
        fn = self.desk_apps
        if fn is None:
            return []
        try:
            return [str(a) for a in (fn() or [])]
        except Exception:  # noqa: BLE001 - the Speech line goes without them
            return []

    def _own_names(self) -> list[str]:
        """voice21 (final-D decision 3): the user's own apps and sites for the Speech line (:attr:`own_names`), ``[]``
        when unknown. Never raises."""
        fn = self.own_names
        if fn is None:
            return []
        try:
            return [str(a) for a in (fn() or [])]
        except Exception:  # noqa: BLE001 - the Speech line goes without them
            return []

    def fast_handled(self, kind: str) -> None:
        """The open user turn was answered with no talker reply: ``"name"`` (vGame: the name alone, answered "Yeah?"
        at once: said to Yuki by measured evidence) or ``"end"`` (D1, 20 Oct: the router's model judged that the words
        end the conversation). Either way it was answered, never "for others"."""
        turn = self.turn
        if turn is None or turn.trigger != "user":
            return
        turn.fast = kind
        if kind == "name":
            turn.named = True
        turn.tools.append("end_conversation" if kind == "end" else "answer_name")
        self.owes_reply_since = None
        self._took_part(time.monotonic())
        if self.state == "thinking":
            self.set_state(self.idle_state())   # review F9: answered at once; the pill waits for the command
        self._activity()

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
    def addressed(turn: _Turn, reply: str) -> str:
        """Who a user turn was said to, as memory keeps it: "others" when Yuki chose silence on it (the talker's
        stay_quiet: its judgement that the words were for someone else), "unanswered" when, in a call, the next
        words came before Yuki said or did anything - nobody judged those words, so memory's extractor reads them
        and judges from their content (report 77b: 15 of 23 such turns were followed by a request Yuki answered,
        and were kept out of memory as talk to the others by this timing alone), else "yuki". Integration F4: a turn
        whose quiet on their opening words was refused (S10: said to Yuki; the answer came in a turn of its own) is
        never "others"."""
        if not reply and not turn.counted:
            if turn.quiet and not getattr(turn, "quiet_refused", False):
                return "others"
            if turn.call:
                return "unanswered"
        return "yuki"

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
            addressed = self.addressed(turn, reply)
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
            # (``quiet_answered``). A call turn nobody judged (the next words came first) is "unanswered" (77b).
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
            own = self._own_names()
            note, low, near = heard_note(result.get("words") if isinstance(result, dict) else None, self.keyterms,
                                         cut_before=self.cut_before(), screen=self.screen_terms,
                                         apps=self._desk_apps(), own=own, measured=own)
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
            if self.hands is not None:
                self.hands.prefetch()  # the tabs for the reply's desktop block, read while they talk
            self.set_state("hearing")
            self._activity()
            if self.on_user_started is not None:
                try:
                    self.on_user_started()
                except Exception:  # noqa: BLE001 - the countdown's own pause limit then
                    pass
        elif isinstance(frame, UserStoppedSpeakingFrame):
            self.user_speaking = False
            self._activity()
        elif isinstance(frame, TranscriptionFrame):
            if self.on_user_words is not None:
                try:
                    self.on_user_words(frame.text or "")
                except Exception:  # noqa: BLE001 - the countdown's own pause limit then
                    pass
            turn = self._open("user")
            turn.user_text = frame.text or ""
            self.chat({"type": "user_utterance", "text": turn.user_text, "ts": time.time(), "turn_id": self.turn_id()})
            turn.transcript_at = now
            turn.speech_end, turn.eos_source = self._end_of_speech(now)
            result = getattr(frame, "result", None)
            own = self._own_names()   # voice22 (A4): the same list for names21's measured match (``measured``)
            self.heard, turn.heard_low, turn.heard_near = heard_note(
                result.get("words") if isinstance(result, dict) else None, self.keyterms,
                cut_before=self.cut_before(), screen=self.screen_terms, apps=self._desk_apps(), own=own,
                measured=own)
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
                if frame.function_name in HANDOFF_TOOLS and self.turn.handoff_at is None:
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
        lookup_text = await self._lookup(call, user_turn)
        if recent_task is not None:
            await recent_task
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

    def call_started(self, call: LlmCall) -> None:
        self.c.observer.call_started(call)

    def early_said(self, call: LlmCall, chars: int) -> None:
        """A call's say_first went to the voice while the call was being written (content-free log)."""
        self.c.log.event("early_say", chars=chars,
                         after_request_ms=round((call.early_say - call.sent) * 1000) if call.sent and call.early_say
                         else None)

    def early_held(self, call: LlmCall, tool: str) -> None:
        """voice22 (A25): a call's say_first was kept from the voice while it was written: a game is in front and the
        gate holds its act (content-free log: the tool's name only)."""
        self.c.log.event("early_say", held="game", tool=str(tool or ""))

    # -- the mouth's model failover (v3.5) ----------------------------------------

    def routed(self) -> bool:
        """Whether Settings → AI moved the talker to another service (its client then does the retries)."""
        routed = getattr(getattr(self.c, "_client", None), "routed", None)
        return bool(callable(routed) and routed())

    def fallbacks(self) -> list[dict[str, Any]]:
        if self.routed():
            return []   # Settings → AI moved the talker: its client retries there and uses only the user's backup
        s = self.c.settings
        model = getattr(self.c, "talker_model", None) or s.voice_mouth_model
        chain = [dict(t) for t in (getattr(s, "model_fallbacks", None) or {}).get(model, [])
                 if isinstance(t, dict) and t.get("model")]
        if getattr(self.c, "one", False):
            # phase 4 (addendum must-fix 9, oTyped's note): the ONE agent acts, so its chain stops after Sonnet 5 and
            # holds no Haiku (``brain.settings.one_chain``, as typed's ``one.host.one_client``)
            from yuki.brain.settings import one_chain

            return one_chain(chain)
        return chain

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

    def model_declined(self, op: str, target: dict[str, Any] | None) -> None:
        """kMind (review M2): the reply was declined before any output (``declined``), and how the run on another model
        went (``answered`` / ``refused_again``). Content-free."""
        where = "primary" if target is None else f"{target.get('model')}@{target.get('region') or 'home'}"
        self.c.log.event("model_refusal", op=op, target=where)

    def model_ok(self, index: int, target: dict[str, Any] | None) -> None:
        if index:
            sticky_s = float(getattr(self.c.settings, "failover_sticky_s", 300.0))
            self.c._failover_sticky = (index, time.monotonic() + sticky_s)
            self.c.log.event("model_failover_ok", target=f"{target.get('model')}@{target.get('region') or 'home'}")

    def speculation(self, op: str, calls: int) -> None:
        """An eager reply's tool calls were held, released on the confirmed turn, or dropped."""
        self.c.log.event("speculation", op=op, calls=calls)

    def calls_restored(self, calls: int, restored: int, bad: int) -> None:
        """hotTools: a reply of several calls, whose earlier calls Pipecat dropped, runs whole (content-free log)."""
        self.c.log.event("talker_calls", op="restored", calls=calls, restored=restored, bad_args=bad)

    def would_hold(self, name: str) -> bool:
        """Whether a call of the reply now ending will be held until the user answers (see ``Conversation._held``)."""
        return self.c._held(name)

    def would_hold_call(self, name: str, args: Any = None) -> bool:
        """:meth:`would_hold` with the call's arguments (a run_skill of a sound-only skill is never held)."""
        return self.c._held(name, args=args)

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
        """The reply was cut (a barge-in, a withdrawn speculation): an early start whose call never ran is void.
        integ23 S2: the router's decision on a send's stop keeps that part (newer speech never voids a stop; a
        withdrawn speculation's own decision went whole just before, :meth:`words_withdrawn`)."""
        self.c._cancel_route(keep_stop=True)
        self.c.bridge.drop_early()

    def words_withdrawn(self) -> None:
        """integ23 S2 (c): an eager end of turn was withdrawn (the same turn went on): the router's decision on those
        partial words is void whole, its stop part too (the turn's final words get a decision of their own)."""
        self.c._cancel_route()

    def calls_made(self, ids: set[str]) -> None:
        """The reply's calls are complete: an early start for a call that is not among them is void."""
        self.c.bridge.settle_early(ids)

    def reply_calls(self, calls: list[tuple[str, str, Any]]) -> None:
        """vRound2: a reply of several calls, about to run in parallel: the ones the reply-level rule leaves out."""
        self.c._note_reply(calls)

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
        return bool(getattr(c, "after_goodbye", False) and (call is None or call.user_turn) and c._call_turns() <= 1)

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
        from them, v3.6), or (vGame) a turn the wake word heard the name in, or the turn right after the name alone.

        Review F4: decided for the words this call answers (the fast path's mark for the utterance), never from the
        observer's open turn, which during a speculative call on Flux's eager end of turn is still the last one."""
        c = self.c
        call = getattr(getattr(c, "_llm", None), "_yuki_call", None)
        if call is not None and not call.user_turn:
            return False
        return c._utterance_named()

    # -- v3.16: the speech channel and an unheard turn answered again --------------------------------------

    def channel(self) -> str:
        """``"marker"`` or ``"guard"`` (``Settings.voice_speech_channel``)."""
        return self.c.channel

    def rerun(self) -> bool:
        """Whether this call answers the user's turn again because nothing of the last reply to it was heard; voice22
        (A9): or answers again the talker's run for the refusal of a quiet on their opening words that gave them
        nothing (``Conversation._opened_answer``: its call follows that refusal's result, not their words)."""
        turn = self.c.observer.turn
        call = getattr(getattr(self.c, "_llm", None), "_yuki_call", None)
        if turn is None or not turn.rerun:
            return False
        if turn.trigger == "user":
            return bool(call is None or call.user_turn)
        opened = getattr(self.c, "_opened_answer", None)
        return bool(callable(opened) and opened(turn))

    def marker_report(self, call: LlmCall, channel: Any) -> None:
        """A reply's text was read by the marker channel: record it on its turn and log it (content-free)."""
        self.c._marker_report(call, channel)

    # -- v3.17: the prompt by measured state (H1) and the answer's length (H2) --------------------------------

    def talker(self) -> str:
        """bTalk: the talker in force this conversation, ``"full"`` or ``"lean"`` (``mouth.talker_kind``)."""
        return str(getattr(self.c, "talker", "full") or "full")

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

    def hands_reply_done(self, calls: tuple[str, ...] = ()) -> None:
        """integ22 (desk22 merge note 1, D4): the talker's reply ended, ``calls`` the names of every function call it
        made (``tell_yuki`` / ``hand_over`` included; empty for a plain answer): ``Hands.reply_done``. Not under
        ``brain = "one"`` (the ONE driver ends its own turns). Sync, never raises; safe while the calls still run."""
        if getattr(self.c, "one", False):
            return
        hands = getattr(self.c.bridge, "hands", None) if self.c.settings.voice_hands else None
        done = getattr(hands, "reply_done", None) if hands is not None else None
        if not callable(done):
            return
        try:
            done(tuple(calls or ()))
        except Exception:  # noqa: BLE001 - the deferred step is dropped at their next words, as before
            pass

    # -- oVoice (phase 4, brain = "one"): the ONE driver ------------------------------------------------------------

    def one(self) -> Any:
        """The conversation's ONE driver (``brain = "one"``), else ``None`` (every other brain: nothing changes)."""
        return getattr(self.c, "one_driver", None)

    def cancel_passed_on(self, said: bool = True) -> bool | None:
        """Contract C4: the bridge cancels the job a ``run_skill`` of the current turn passed on
        (``VoiceBridge.cancel_passed_on() -> bool``); ``None`` when the bridge has no such call (before hands19's
        change). voice20 (final-D S11): ``said``: the talker's cancelling reply said words before the call; when it
        did not, the bridge says the job's own ending by code. Never raises."""
        cancel = getattr(self.c.bridge, "cancel_passed_on", None)
        if not callable(cancel):
            return None
        try:
            try:
                return bool(cancel(said=bool(said)))
            except TypeError:   # a bridge before voice20: no ``said``
                return bool(cancel())
        except Exception:  # noqa: BLE001 - Pipecat's own refusal then
            return None

    def passed_on_sent(self) -> bool:
        """Integration review VC-2: the last :meth:`cancel_passed_on` found that job's send gone already
        (``VoiceBridge.cancel_found_sent``). Never raises."""
        return getattr(self.c.bridge, "cancel_found_sent", False) is True

    def one_reply(self, calls: Any) -> None:
        driver = getattr(self.c, "one_driver", None)
        if driver is not None:
            driver.reply(calls)

    def one_reply_end(self, call: Any, speculating: bool = False) -> None:
        driver = getattr(self.c, "one_driver", None)
        if driver is not None:
            driver.reply_end(call, speculating)


def quiet_answered(messages: list[Any] | None) -> set[int]:
    """Ids of the user messages the mouth answered only with ``stay_quiet`` (in a call: talk to the others).

    Read from the context's own structure (which tool the next assistant message called), so the
    thinker is never handed those words as what the user said to Yuki. Plumbing, no words read.
    Integration F1 (voice21 S10): a stay_quiet whose own result refused it (a ``not_quiet`` key: the opened-first,
    held and owed refusals, ONE's) answers nothing; the next assistant message (the rerun's reply) decides.
    """
    out: set[int] = set()
    items = list(messages or [])
    for i, message in enumerate(items):
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        for j, nxt in enumerate(items[i + 1:], start=i + 1):
            if not isinstance(nxt, dict) or nxt.get("role") == "user":
                break
            if nxt.get("role") == "assistant":
                calls = nxt.get("tool_calls") or []
                names = [(c.get("function") or {}).get("name") for c in calls if isinstance(c, dict)]
                if names and all(n == "stay_quiet" for n in names) and not nxt.get("content"):
                    ids = {str(c.get("id")) for c in calls if isinstance(c, dict)}
                    if _quiet_refused(items[j + 1:], ids):
                        continue
                    out.add(id(message))
                break
    return out


def _quiet_refused(items: list[Any], call_ids: set[str]) -> bool:
    """Integration F1: whether one of ``call_ids`` (stay_quiet calls) got a result that refused it: a tool message for
    it whose JSON content is a mapping with the ``not_quiet`` key. A structural key, never words. Never raises."""
    import json

    for m in items:
        if not (isinstance(m, dict) and m.get("role") == "tool" and str(m.get("tool_call_id")) in call_ids):
            continue
        content = m.get("content")
        try:
            body = content if isinstance(content, dict) else json.loads(content or "")
        except (TypeError, ValueError):
            continue
        if isinstance(body, dict) and "not_quiet" in body:
            return True
    return False


def _last_role(context: Any) -> str | None:
    try:
        messages = context.get_messages()
    except Exception:  # noqa: BLE001
        return None
    for message in reversed(messages or []):
        if isinstance(message, dict) and message.get("role") in ("user", "assistant", "developer", "tool"):
            return str(message.get("role"))
    return None


def _trailing_tool_ids(context: Any) -> tuple[str, ...]:
    """voice22 (review L3): the ``tool_call_id`` of each tool result the context ends with: the results a mouth run
    answers. Read back from the newest message: the reply text Pipecat writes at a response's end (an assistant
    message with no calls) is passed over until a result is met, then the run of calls and their results (Pipecat's
    in-progress pairs, the parallel calls of one reply) is read; it stops at anything else. ``()`` when the context
    ends with their words, a thinker or job line (a developer message), or cannot be read. Structure only. Never
    raises."""
    try:
        messages = list(context.get_messages() or [])
    except Exception:  # noqa: BLE001
        return ()
    out: list[str] = []
    for message in reversed(messages):
        role = message.get("role") if isinstance(message, dict) else None
        if role == "tool":
            if message.get("tool_call_id"):
                out.append(str(message.get("tool_call_id")))
        elif role == "assistant" and message.get("tool_calls"):
            if not out:
                break   # a call with no result after it yet: not a run a result started
        elif not (role == "assistant" and not out):
            break
    return tuple(out)


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


def _user_text(message: Any) -> str:
    """A user-role message's text as the hand-offs read it (``""`` for any other message)."""
    if not isinstance(message, dict) or message.get("role") != "user":
        return ""
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    else:
        text = ""
    return " ".join(text.split())


def _turn_spans(text: str, turns: list[str]) -> list[tuple[int, int]]:
    """Which of ``turns`` (the user's heard turns, oldest first) a user message's text is, as half-open spans
    ``(k, m)``: one turn exactly (``m = k + 1``), or the aggregator's merge of two or more of them in a row (the text is
    their join; the oldest of them may follow older turns of theirs merged before them, outside the snapshot). Review
    r1 M1: a single turn that merely starts or ends with the same words never holds one ("Dude, just do it." holds no
    "do it."). String identity only, never the words' meaning."""
    n = len(turns)
    out = [(j, j + 1) for j in range(n) if text == turns[j]]
    for k in range(n):
        for m in range(k + 2, n + 1):
            joined = " ".join(turns[k:m])
            if text == joined or (k == 0 and text.endswith(" " + joined)):
                out.append((k, m))
    return out


def _message_text(message: Any) -> str:
    """Any message's own words (its text content, whitespace collapsed; ``""`` when it has none: a call, a result)."""
    content = message.get("content") if isinstance(message, dict) else None
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(str(p.get("text") or "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    else:
        text = ""
    return " ".join(text.split())


def _call_index(messages: list[Any], call_id: str | None) -> int | None:
    """Where a call is in a conversation: the assistant message that made it, else its result (``None``: not there)."""
    if not call_id:
        return None
    for i, message in enumerate(messages):
        if isinstance(message, dict) and message.get("role") == "assistant":
            calls = message.get("tool_calls") or ()
            if any(isinstance(c, dict) and str(c.get("id")) == str(call_id) for c in calls):
                return i
    for i, message in enumerate(messages):
        if (isinstance(message, dict) and message.get("role") == "tool"
                and str(message.get("tool_call_id")) == str(call_id)):
            return i
    return None


def _spoken_before(messages: list[Any], end: int) -> int:
    """The index of Yuki's last reply with words before ``end`` (``-1``: none). A held reply's words never reach the
    conversation (they are dropped unsaid, and nothing is said early on a doubtful turn), so any assistant message
    with words before a held call was said before the turn that set it off."""
    for i in range(min(end, len(messages)) - 1, -1, -1):
        message = messages[i]
        if isinstance(message, dict) and message.get("role") == "assistant" and _message_text(message):
            return i
    return -1


def _tail_held(users: list[tuple[int, str]], spans: dict[int, list[tuple[int, int]]], n: int, lo: int,
               hi: int) -> int:
    """How many of the ``n`` snapshot turns, newest first and in order, the user's messages strictly between indexes
    ``lo`` and ``hi`` hold back from ``hi``, the newest in the last of those messages (``0``: that message does not hold
    it). Found as :func:`with_heard_turns` finds them: a message holds a turn when it is that turn or a merge of these
    turns (:func:`_turn_spans`)."""
    count, bound, newer = 0, hi - 1, None
    for j in range(n - 1, -1, -1):
        found = None
        for i, _t in reversed(users):
            if i > bound or i <= lo:
                continue
            if i == bound and newer is not None:
                ok = any(k <= j and newer < m for k, m in spans[i])   # the newer turn's own message: a merge of both
            else:
                ok = any(k <= j < m for k, m in spans[i])
            if ok:
                found = i
                break
            if newer is None:
                break   # the newest turn: their last message there, or none
        if found is None:
            break
        count, bound, newer = count + 1, found, j
    return count


def with_heard_turns(messages: list[Any], turns: list[str], held_id: str | None = None) -> list[Any]:
    """x3 (fix-plan 3e): ``messages`` (a conversation's, oldest first) with each of ``turns`` (the user's own heard
    turns of the moment a call was held, oldest first) that no user message holds put back as a user message where it
    was said. ``held_id``: the held call's id; the moment is where that call stands (the end when it is not there).

    Review r1 M1: the newest turn (the one that set the call off, or was heard while it was being written) is looked
    for only among the user's messages after Yuki's last reply with words before the held call (:func:`_spoken_before`),
    or in one that landed right after the call, before Yuki said anything more; it is never matched to an older "Yeah."
    said long before. Review r2 L1: when a line said by code stands between their turns and the call, their last
    message before it counts for the newest turn when it and the one before it hold the snapshot's last two turns in
    order (:func:`_tail_held`). Each older turn is looked for from where the newer one was found, back. A message holds a turn
    only when it is that turn or a merge of these turns (:func:`_turn_spans`). A missing turn goes right before the
    message holding the next turn of theirs that is there; a missing newest turn right after their last message before
    the held call (after Yuki's last reply when there is none). Never between a call and its result. The messages
    themselves are kept as they are (the same objects); nothing is added when every turn is there. String identity and
    order only, never the words' meaning."""
    turns = [" ".join(str(t or "").split()) for t in turns or ()]
    turns = [t for t in turns if t]
    messages = list(messages)
    if not turns:
        return messages
    users = [(i, _user_text(m)) for i, m in enumerate(messages)]
    users = [(i, t) for i, t in users if t]
    if not users:
        return [*messages, *({"role": "user", "content": t} for t in turns)]
    at = _call_index(messages, held_id)
    end = len(messages) if at is None else at
    floor = _spoken_before(messages, end)
    spans = {i: _turn_spans(t, turns) for i, t in users}
    n = len(turns)
    newest = n - 1
    place: list[int | None] = [None] * n
    hit = next((i for i, _t in reversed(users) if floor < i < end and any(k <= newest < m for k, m in spans[i])), None)
    if hit is None and at is not None:
        # it landed right after the held call (before Yuki said anything more): there, not missing
        after = next((i for i in range(end + 1, len(messages)) if isinstance(messages[i], dict)
                      and messages[i].get("role") == "assistant" and _message_text(messages[i])), len(messages))
        hit = next((i for i, _t in users if end < i < after and any(k <= newest < m for k, m in spans[i])), None)
    if hit is None and n >= 2 and floor >= 0 and not any(floor < i < end for i, _t in users):
        # review r2 L1: a line said by code after their turns (an older job's announcement lands in the context like a
        # reply) can stand between the turn that set the call off and the call. When their last message before that
        # line holds the newest turn and the one before it holds the turn before (or one message is the merge of
        # both), with nothing of theirs after the line, the newest is there, not missing: put back after the line, it
        # would hide the earlier turns from "their words since Yuki last spoke". Two turns in a row the same as an old
        # exchange's, both missing, is not a case that happens; a single turn is never taken this way (an old "Yeah."
        # before a reply may be just that, and a new one put back after the reply loses nothing)
        lo = _spoken_before(messages, floor)
        if _tail_held(users, spans, n, lo, floor) >= 2:
            hit = next(i for i, _t in reversed(users) if lo < i < floor)
    place[newest] = hit
    bound, newer = (hit, newest) if hit is not None else (end - 1, None)
    for j in range(n - 2, -1, -1):
        found = None
        for i, _t in reversed(users):
            if i > bound:
                continue
            if i == bound and newer is not None:
                ok = any(k <= j and newer < m for k, m in spans[i])   # the newer turn's own message: a merge of both
            else:
                ok = any(k <= j < m for k, m in spans[i])
            if ok:
                found = i
                break
        place[j] = found
        if found is not None:
            bound, newer = found, j
    before: dict[int, list[str]] = {}
    tail: list[str] = []
    for j, turn in enumerate(turns):
        if place[j] is not None:
            continue
        nxt = next((place[k] for k in range(j + 1, n) if place[k] is not None), None)
        if nxt is not None:
            before.setdefault(nxt, []).append(turn)
        else:
            tail.append(turn)
    if not before and not tail:
        return messages
    spot = len(messages)
    if tail:
        last = next((i for i, _t in reversed(users) if floor < i < end), None)
        spot = last + 1 if last is not None else (floor + 1 if floor >= 0 else end)
        while spot < len(messages) and isinstance(messages[spot], dict) and messages[spot].get("role") == "tool":
            spot += 1   # never between a call and its result
    out: list[Any] = []
    for i, message in enumerate(messages):
        if i == spot:
            out.extend({"role": "user", "content": t} for t in tail)
        out.extend({"role": "user", "content": t} for t in before.get(i, ()))
        out.append(message)
    if tail and spot >= len(messages):
        out.extend({"role": "user", "content": t} for t in tail)
    return out


class HeldWords:
    """x3 (fix-plan 3e): a held call's conversation as its hand-off reads it: the live context, with any of the user's
    own turns of the moment it was held that the context lacks put back where they were said (:func:`with_heard_turns`,
    placed by the held call's own id). Everything else is the live context's own (attributes pass through). r16 20:47
    the turn that triggered a held hand-off never reached the conversation before it, so the call made again carried
    only the turns before it."""

    def __init__(self, context: Any, turns: list[str], held_id: str | None = None) -> None:
        self._context = context
        self._turns = list(turns or ())
        self._held_id = str(held_id) if held_id else None

    def __getattr__(self, name: str) -> Any:
        if name in ("_context", "_turns", "_held_id"):
            raise AttributeError(name)   # not set yet (a copy): never a lookup on itself
        return getattr(self._context, name)

    def get_messages(self) -> list[Any]:
        try:
            messages = list(self._context.get_messages() or [])
        except Exception:  # noqa: BLE001 - as a context that cannot be read: the turns alone
            messages = []
        return with_heard_turns(messages, self._turns, self._held_id)


def fast_path_processor(conversation: Any) -> Any:
    """vGame: the frame processor between speech recognition and the talker's context (``Conversation._fast_turn``).

    It sees each transcript before the user aggregator does: a final one the fast path takes (the name alone, said to
    Yuki: answered "Yeah?" at once) never reaches the talker, and an eager one of the name alone starts no speculative
    talker call. Every other frame passes untouched. The turn observer saw the transcript already (observers see a
    frame as it is pushed here), so the turn is logged as usual. Keyword audit A1-A5 (20 Oct): nothing here acts on what
    the words say any more (no dismiss word list, no learned reflex): every other turn reaches the talker whole."""
    from pipecat.frames.frames import EagerTranscriptionFrame, TranscriptionFrame
    from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

    def answered(frame: Any) -> None:
        # x3 (IF-words, fix-plan 3g IF-answer): a final transcript that goes on to the talker is the user's turn: kept
        # for the current exchange, and offered to the voice job waiting for their answer (the fast path's own turn,
        # the name alone, never is)
        if isinstance(frame, TranscriptionFrame) and not isinstance(frame, EagerTranscriptionFrame):
            try:
                conversation._turn_reached(frame.text or "")
            except Exception as exc:  # noqa: BLE001 - the turn goes on to the talker either way
                conversation.log.event("fast_path", op="error", error=f"{type(exc).__name__}: {str(exc)[:160]}")

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
                answered(frame)
                await self.push_frame(frame, direction)
                return
            await self.push_frame(frame, direction)

    return FastPath(name="yuki-fast-path")


#: D2 (20 Oct, keyword audit A5): the retired reflex book's row in memory's ``nudge_state`` (encrypted: the user's
#: words and the hand each ran): deleted once by code (:func:`drop_old_reflexes`), never read again.
OLD_REFLEX_STATE = "voice_reflex"
#: Whether this process deleted it (or found none) already: tried once per process, again after a failure.
_OLD_REFLEXES: dict[str, bool] = {"done": False}


def drop_old_reflexes(store: Any) -> bool:
    """D2: delete the retired reflex book's stored row from ``store`` (an open ``yuki.memory.store.Store``),
    idempotently: a database without it changes nothing. ``True``: a row was deleted. Raises only when the store itself
    cannot be read or written (the caller tries again later)."""
    if store.nudge_state(OLD_REFLEX_STATE) is None:
        return False
    store.set_nudge_state(OLD_REFLEX_STATE, None)
    return True


# ---------------------------------------------------------------------------------------------------------------------
# yConsumers (state-design.md §3.5): the talker's "you right now" line, from a background-refreshed cache
# ---------------------------------------------------------------------------------------------------------------------


class TalkerYou:
    """The voice talker's one "you right now" line (yConsumers; state-design.md §3.5, §7 yConsumers items 1-2).

    One per process (:func:`talker_you`). A daemon thread keeps a copy of the memory service's record (the ``focus``
    and ``changed`` sections only, through ``NowClient.you``: version-cached, an unchanged answer is ~100 bytes) and
    refreshes it

    * when the service sets the ``talk`` hold event (``yuki.memory.now.you_event_name``: only when the set of holds
      changes, a game or a call starting or ending),
    * when a conversation goes live (a kick: one ``SetEvent`` from the voice loop, microseconds, never a pipe read),
    * every :attr:`POLL_S` while a conversation is live (so "Spotify started 2 min ago" is there).

    :meth:`line` renders the cached copy in RAM (``younow.render(you, "talker")``, at most 300 characters): no fetch per
    turn and never a pipe read on the wake path (``voice/session.py`` is untouched). It is ``""`` when the copy is older
    than :attr:`FRESH_S` (a hint: better none than an old "in VALORANT"), before the thread has imported the renderer
    (an import on the voice loop stalls it), with ``[you] enabled = false`` (the service answers "off"), or with
    nothing to say. The thread is started by :meth:`start` before a conversation is live (a thread started from the
    loop stalled it, v3.14). Counters are content-free (:attr:`stats`).
    """

    #: While a conversation is live, the record is asked again this often (s).
    POLL_S = 3.0
    #: The cached copy is used for at most this long after its last answer (s).
    FRESH_S = 10.0
    #: One ask's wait (s; the background thread's, never the loop's).
    ASK_WAIT_S = 0.3
    #: With no conversation live the thread waits for the hold event or a kick at most this long between checks (s).
    IDLE_WAIT_S = 60.0

    def __init__(self, client_fn: Callable[[], Any] | None = None) -> None:
        import threading

        self._client_fn = client_fn
        self._lock = threading.Lock()
        self._live: set[int] = set()
        self._you: Any = None
        self._version: Any = None
        self._at: float | None = None
        self._render: Callable[..., str] | None = None
        self._thread: Any = None
        self._kick: Any = None
        self._hold: Any = None
        self._own: Any = None   # its own NowClient (the refresh thread only)
        self.stats: dict[str, int] = {"asks": 0, "changed": 0, "failed": 0, "off": 0, "holds": 0, "kicks": 0,
                                      "lines": 0}

    def start(self) -> None:
        """Start the refresh thread once (call it before anything is live). Never raises."""
        import threading

        with self._lock:
            if self._thread is not None:
                return
            try:
                from yuki.memory.now import NamedEvent, you_event_name

                self._kick = NamedEvent(None)
                hold = NamedEvent(you_event_name(None, "talk"))   # the default database, as shared_client()'s
                self._hold = hold if hold.handle else None
            except Exception:  # noqa: BLE001 - no events: it polls while live, and stays idle otherwise
                self._kick, self._hold = None, None
            self._thread = threading.Thread(target=self._run, name="yuki-voice-you", daemon=True)
            self._thread.start()

    def attach(self, owner: Any, live: bool) -> None:
        """A conversation went live (``True``: a refresh is kicked at once) or ended (``False``). Cheap; never
        raises."""
        with self._lock:
            if live:
                self._live.add(id(owner))
            else:
                self._live.discard(id(owner))
            kick = self._kick if live else None
        if kick is not None and kick.set():
            self.stats["kicks"] += 1

    def line(self, now: float | None = None) -> str:
        """The talker's line from the cached copy (RAM only), or ``""``."""
        with self._lock:
            you, at, render = self._you, self._at, self._render
        if you is None or at is None or render is None or time.monotonic() - at > self.FRESH_S:
            return ""
        try:
            text = str(render(you, "talker", now=time.time() if now is None else float(now)) or "")
        except Exception:  # noqa: BLE001 - a hint: none rather than a broken turn
            return ""
        if text:
            self.stats["lines"] += 1
        return text

    def _client(self) -> Any:
        """Its own ``NowClient`` (its own pipe connection and lock, made once by this thread): an ask waits up to
        :attr:`ASK_WAIT_S`, and on the process-wide client that wait would hold the lock the hands' desk read
        (``prefetch_desk``, 50 ms) needs right after a wake."""
        try:
            if self._client_fn is not None:
                return self._client_fn()
            if self._own is None:   # no key yet (the service never ran): tried again next time
                from yuki.memory.now import own_client

                self._own = own_client()
            return self._own
        except Exception:  # noqa: BLE001
            return None

    def _run(self) -> None:
        try:
            import yuki.core.appnames  # noqa: F401 - the renderer's lazy import, done here too
            from yuki.memory.now import wait_any
            from yuki.memory.younow import TALKER_SECTIONS, render   # here, never on the voice loop
        except Exception:  # noqa: BLE001 - no record to read: the line stays empty
            return
        with self._lock:
            self._render = render
        events = [e for e in (self._hold, self._kick) if e is not None]
        while True:
            with self._lock:
                live = bool(self._live)
            got = wait_any(events, self.POLL_S if live else self.IDLE_WAIT_S)
            fired = events[got] if got is not None else None
            if fired is not None and fired is self._hold:
                self.stats["holds"] += 1
            if fired is None and not live:
                continue   # idle and nothing happened: nothing to read
            self._refresh(TALKER_SECTIONS)

    def _refresh(self, sections: tuple[str, ...]) -> None:
        client = self._client()
        if client is None or not callable(getattr(client, "you", None)):
            return
        self.stats["asks"] += 1
        try:
            you = client.you(sections=sections, wait_s=self.ASK_WAIT_S)
        except Exception:  # noqa: BLE001
            you = None
        if you is None:
            off = getattr(client, "you_error", None) == "off"
            self.stats["off" if off else "failed"] += 1
            if off:
                with self._lock:
                    self._you, self._at = None, None
            return
        with self._lock:
            if you.get("version") != self._version:
                self.stats["changed"] += 1
            self._you, self._version, self._at = you, you.get("version"), time.monotonic()


_talker_you: TalkerYou | None = None


def talker_you() -> TalkerYou:
    """This process's :class:`TalkerYou` (one cache for every conversation)."""
    global _talker_you
    if _talker_you is None:
        _talker_you = TalkerYou()
    return _talker_you


# ---------------------------------------------------------------------------------------------------------------------
# oVoice (phase 4, ``brain = "one"``): the ONE agent's voice driver
# ---------------------------------------------------------------------------------------------------------------------
#
# Design: docs/research/audit-2026-10-18/phase4-design.md with phase4-addendum.md (binding). Under ``brain = "one"``
# one Sonnet 5.5 agent owns the whole spoken turn: it talks, acts with the generic verbs (``yuki.one.tools``), gets
# what changed with every result (the runner, ``yuki.one.runner``), and decides itself when it is done; every act
# passes the one gate (``yuki.one.policy``, called by the runner); the budget (``yuki.one.budget``) is the runaway
# guard; sends wait in the one countdown (``yuki.one.countdown``). Nothing here runs under any other brain value.

#: The voice controls: the bridge's own handlers, never the runner (sync, as before).
ONE_CONTROLS: frozenset[str] = frozenset({"stay_quiet", "end_conversation", "go_quiet", "more_of_result"})
#: Controls that end the conversation: no model run after them.
ONE_ENDS: frozenset[str] = frozenset({"end_conversation", "go_quiet"})
#: A runner result's keys that are the driver's alone, never sent to the model (a dict result; a ``OneResult`` gives
#: the model its own ``for_model()``).
ONE_PRIVATE: frozenset[str] = frozenset({"settle", "silent", "fallback", "speak", "owed", "decision", "payload",
                                         "final", "ms", "verb", "tidy", "name", "heard"})
#: The statuses a call may settle on (``yuki.one.runner.CLEAN``: done, already so, a memory write kept, a send they
#: stopped), with ``verified`` not false, nothing flagged and no ``then``.
ONE_CLEAN: frozenset[str] = frozenset({"done", "already", "written", "stopped", "verified"})
#: A result's fields read off a runner's result object (``yuki.one.runner.OneResult``).
ONE_FIELDS: tuple[str, ...] = ("name", "status", "verified", "result", "said", "owed", "evidence", "after", "flags",
                               "flagged", "then", "question", "line", "final", "settle", "silent", "countdown", "swap",
                               "fallback", "note", "error", "heard")
#: Review fix H1: the user-words mark of the turn whose call is running on this thread (the runner's worker), read by
#: the countdown's ``publish`` as it opens: words heard after that mark came after the request the send is for.
_ONE_CALL_MARK: contextvars.ContextVar[int | None] = contextvars.ContextVar("yuki_one_call_mark", default=None)
#: Thinking once a turn is structurally hard (design 2.6, lead decision 3): adaptive at this effort, with room for it
#: (the talker's 400-token output ceiling would cut a thought short). Its text is omitted (never spoken, never read).
ONE_EFFORT = "medium"
ONE_THINK_MAX_TOKENS = 4000
#: ``Settings.one_thinking``: ``structure`` (the default: off on round 1, adaptive medium from round 2 when round 1's
#: reply shows the turn is hard), ``fast`` (never adaptive), ``adaptive_low`` (adaptive at low effort from round 2).
ONE_THINKING: tuple[str, ...] = ("structure", "fast", "adaptive_low")
#: A question the gate asked last turn: the yes/no read of their reply is waited for at most this long before a call
#: of the new turn goes to the gate (it starts with the turn and runs while round 1 is written).
ONE_ANSWER_WAIT_S = 3.0
#: A call waits at most this long for the calls written before it in the same reply (they run in order).
ONE_ORDER_WAIT_S = 75.0
#: The countdown paused on the user's sound: with no words and no voiced audio this long, it was noise (s).
ONE_NOISE_S = 2.0
#: What the model reads on a result that came back after code closed its turn (the hard stop): no run follows it.
ONE_LATE_NOTE = "The turn ended before this came back: say nothing about it now."
#: The runner did not load (its lane not merged, an import error, a contract this driver cannot call): every act fails
#: closed with this.
ONE_NO_RUNNER: dict[str, Any] = {
    "status": "failed", "verified": False, "error": "no_runner",
    "note": "Not done: Yuki's hands are not ready, so nothing was done. Say so in a few words."}
#: A runner call that raised: something may or may not have happened.
ONE_RUN_ERROR: dict[str, Any] = {
    "status": "failed", "verified": False, "error": "runner_error",
    "note": "Not confirmed: Yuki's hands hit an error, so it may not have happened. Say so in a few words."}
#: Final-B (one-voice RC A): the marker channel's format, as the last field of every result the ONE driver returns
#: (``OneVoice.with_format``): the same bytes every time (append-only, cache-safe), so the format is the last thing every
#: ONE round reads, as the volatile block's last line is under ``all`` (``mouth.MARKER_REMINDER``).
ONE_FORMAT_KEY = "reply_format"
#: Final-B (one-voice RC 2b/O1a): the statuses of an act that took effect (the runner's ``CLEAN`` with a launch under
#: way): no failure is owed for them, a ``then`` or a flag is the agent's next round, not a failure.
ONE_TOOK: frozenset[str] = frozenset({"done", "already", "written", "stopped", "starting"})
#: Final-B (one-voice RC F/O3): a closing ``stay_quiet`` of a user turn whose only calls were reads and of which nothing
#: was heard is refused once with this (code facts: the calls' kinds, what left the speaker).
ONE_READ_UNANSWERED = ("Not quiet: they asked and have heard nothing yet. Answer them now from what you read, in a few "
                       "words; if it holds nothing for them, say so.")
#: voice20 (final-D S5, contract C9): a closing reply (a lone stay_quiet, or words with no call) of a ONE turn that
#: Lane ONE's ``closing_hold()`` says to hold once is answered with that method's own sentence (its code facts: one
#: miss with nothing else tried; before contract C12, also a legal tidy step untaken), as the stay_quiet's result or as
#: a note of its own after the closing words. This line is only the fallback, for a reason given as a bare code word.
ONE_MISS_HOLD = ("Not done yet: one way did not take and nothing else was tried. Try one other way now (an undoable "
                 "act), then say in a few words what is left.")
#: voice20 (final-D S22i): the rerun of a ONE result round whose closing words had no marker line (none of them was
#: spoken) answered with a lone stay_quiet: refused once with this (a stream fact, never the words).
ONE_UNHEARD_QUIET = ("Not quiet: your last reply had no say line, so nothing of it was spoken and they heard nothing. "
                     "Say it now, the say line first.")


def _code_word(why: str) -> dict[str, Any]:
    """voice20: a reason another lane's code gave, for the content-free log: the word itself when it is a code word
    (letters, digits and underscores, at most 40), else only its length."""
    text = str(why or "")
    if text and len(text) <= 40 and text.replace("_", "").isalnum() and text.isascii():
        return {"why": text}
    return {"why_chars": len(text)}


def _hold_text(why: str) -> str:
    """voice20 review M1 (contract C9): what a held closing reply is answered with: the sentence Lane ONE's
    ``closing_hold()`` gave (code's own words to the agent: its miss, contract C12), else :data:`ONE_MISS_HOLD`
    (nothing, or a bare code word). A shape test on code's string, never the user's words."""
    text = " ".join(str(why or "").split())
    return ONE_MISS_HOLD if not text or "why" in _code_word(text) else text


def one_thinking_mode(settings: Any) -> str:
    """``Settings.one_thinking`` (:data:`ONE_THINKING`); anything else: ``structure``."""
    value = str(getattr(settings, "one_thinking", "") or "structure").strip().lower()
    return value if value in ONE_THINKING else "structure"


def one_kind(name: str) -> str:
    """``act`` | ``read`` | ``write`` | ``control`` | ``unknown`` for a ONE tool name (``yuki.one.tools.SPECS``)."""
    if name in ONE_CONTROLS:
        return "control"
    from yuki.one import tools as one_tools

    spec = one_tools.SPECS.get(str(name or ""))
    if spec is None:
        return "unknown"
    if spec.acts:
        return "act"
    return spec.kind if spec.kind in ("read", "write", "control") else "unknown"


def one_result(raw: Any) -> dict[str, Any]:
    """A runner's result as a plain dict of its facts (the contract: ``run(...) -> dict``, or
    ``yuki.one.runner.OneResult``: its fields, its ``clean`` as ``_clean`` and its ``for_model()`` as ``_payload``, what
    the model reads). ``None`` or an unreadable object: a failure."""
    if raw is None:
        return {"status": "failed", "verified": False, "error": "no_result"}
    if isinstance(raw, dict):
        return dict(raw)
    out: dict[str, Any] = {key: getattr(raw, key) for key in ONE_FIELDS if hasattr(raw, key)}
    clean = getattr(raw, "clean", None)
    if isinstance(clean, bool):
        out["_clean"] = clean
    for_model = getattr(raw, "for_model", None)
    if callable(for_model):
        try:
            payload = for_model()
            if isinstance(payload, dict):
                out["_payload"] = dict(payload)
        except Exception:  # noqa: BLE001 - the plain fields then
            pass
    return out or {"status": "failed", "verified": False, "error": "unreadable_result"}


def one_payload(result: dict[str, Any]) -> dict[str, Any]:
    """What the model reads of a result: the runner's own ``for_model()`` when it gave one, else every field but the
    driver's own keys and empty values."""
    if isinstance(result.get("_payload"), dict):
        return dict(result["_payload"])
    return {k: v for k, v in result.items()
            if k not in ONE_PRIVATE and not str(k).startswith("_") and v is not None and v != "" and v != []
            and v != {}}


def _one_flagged(result: dict[str, Any]) -> bool:
    """Whether the act's diff came back flagged (it covered what they watched, took the keyboard, left something open):
    ``flagged`` true, or any truthy flag in ``flags`` (a list, a set or a mapping of flag -> bool)."""
    if result.get("flagged") is True:
        return True
    flags = result.get("flags")
    if isinstance(flags, dict):
        return any(bool(v) for v in flags.values())
    if isinstance(flags, (list, tuple, set, frozenset)):
        return any(bool(v) for v in flags)
    if isinstance(flags, str):
        return bool(flags.strip())
    return bool(flags)


def one_outcome(name: str, args: Any, result: dict[str, Any]) -> dict[str, Any]:
    """One call's outcome for the settle rule (code facts only): its kind, whether it verified, whether its diff was
    flagged, its ``then`` (acts only), and whether code settled or the conversation ends with it."""
    kind = one_kind(name)
    status = str(result.get("status") or "").strip().lower()
    if kind == "control":
        ok = True
    elif isinstance(result.get("_clean"), bool):
        ok = bool(result["_clean"])   # the runner's own reading (``OneResult.clean``)
    else:
        ok = result.get("verified") is True or (status in ONE_CLEAN and result.get("verified") is not False)
    then = ""
    if kind == "act":
        from yuki.one.tools import then_of

        then = " ".join(str(result.get("then") or "").split()) or then_of(args if isinstance(args, dict) else {})
    return {"name": str(name or ""), "kind": kind, "verified": bool(ok), "flagged": _one_flagged(result),
            "then": then, "settle": bool(result.get("settle")), "silent": bool(result.get("silent")),
            "ends": name in ONE_ENDS}


def one_settle(outcomes: list[dict[str, Any]]) -> str:
    """The settle rule over one reply's outcomes, in order (addendum must-fix 2, lead decision 2): ``end`` (code settled
    it, or the conversation ends: no model run), ``code`` (every act verified, nothing flagged, every ``then`` empty:
    code's done lines were the answer, no closing call), ``control`` (only controls: their own handlers decide), or
    ``model`` (anything else: a read to answer from, a step that did not verify, a flagged diff to tidy, parts left:
    the agent gets its next round)."""
    if not outcomes:
        return "model"
    if any(o.get("ends") or o.get("settle") for o in outcomes):
        return "end"
    work = [o for o in outcomes if o.get("kind") != "control"]
    if not work:
        return "control"
    for o in work:
        if o.get("kind") not in ("act", "write"):
            return "model"
        if not o.get("verified") or o.get("flagged") or o.get("then"):
            return "model"
    return "code"


def one_hard(calls: list[tuple[str, Any]]) -> bool:
    """Whether a turn is structurally hard, from round 1's reply (code facts, never words; design 2.6): two or more
    acting calls (two parts), acts with no skill among them (no skill fits: the verbs work it out), a look, or a send
    declared (``run_skill`` with ``consent``)."""
    acting = [(n, a) for n, a in calls if one_kind(n) == "act"]
    if len(acting) >= 2:
        return True
    if any(n == "look" for n, _a in calls):
        return True
    if any(n == "run_skill" and isinstance(a, dict) and a.get("consent") for n, a in acting):
        return True
    return bool(acting) and not any(n == "run_skill" for n, _a in acting)


def is_thought(message: Any) -> bool:
    """A thinking block Pipecat stored in the context (``LLMSpecificMessage`` with ``{"type": "thought"}``)."""
    if isinstance(message, dict):
        return False
    inner = getattr(message, "message", None)
    return isinstance(inner, dict) and inner.get("type") == "thought"


def strip_thoughts(messages: list[Any]) -> list[Any]:
    """``messages`` without the thinking blocks Pipecat stored (a turn's end, before any history edit)."""
    return [m for m in messages or [] if not is_thought(m)]


def with_volatile_at(messages: Any, block: dict[str, Any], anchor: str | None) -> Any:
    """:func:`with_volatile`, but onto the user's own message of this turn (``anchor``: its text), the same bytes every
    round of the turn (design 3.3.3: no harness text after tool results; the turn stays append-only for the cache and
    the thinking blocks). That message's cache breakpoint, when it has one, moves onto the block (it is frozen for the
    turn, so later rounds read it from the cache). No such message (the summary rewrote it): the newest user message,
    as :func:`with_volatile`. Only the copy sent is changed."""
    if not isinstance(messages, list) or not anchor:
        return with_volatile(messages, block)
    want = " ".join(str(anchor).split())
    for i in range(len(messages) - 1, -1, -1):
        message = messages[i]
        if not (isinstance(message, dict) and message.get("role") == "user"):
            continue
        content = message.get("content")
        parts = [{"type": "text", "text": content}] if isinstance(content, str) else list(content or [])
        if not any(isinstance(p, dict) and p.get("type") == "text"
                   and " ".join(str(p.get("text") or "").split()) == want for p in parts):
            continue
        added = dict(block)
        if parts and isinstance(parts[-1], dict) and parts[-1].get("cache_control"):
            last = dict(parts[-1])
            added["cache_control"] = last.pop("cache_control")
            parts[-1] = last
        out = list(messages)
        out[i] = {**message, "content": [*parts, added]}
        return out
    return with_volatile(messages, block)


def _one_bind(fn: Any, values: dict[str, Any]) -> tuple[list[Any], dict[str, Any]] | None:
    """``fn``'s arguments from ``values`` by parameter name (the runner's contract is read off its own signature);
    ``None`` when ``fn`` needs a parameter this driver cannot fill."""
    import inspect

    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return None
    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for p in sig.parameters.values():
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        if p.name in values:
            if p.kind is p.POSITIONAL_ONLY:
                args.append(values[p.name])
            else:
                kwargs[p.name] = values[p.name]
        elif p.default is p.empty:
            return None
    return args, kwargs


def _one_in_call(mark: int | None, fn: Callable[..., Any], args: list[Any], kwargs: dict[str, Any]) -> Any:
    """``fn(*args, **kwargs)`` on the runner's worker thread with the call's turn mark set (review fix H1: a send's
    countdown opened on this thread reads it)."""
    token = _ONE_CALL_MARK.set(mark)
    try:
        return fn(*args, **kwargs)
    finally:
        _ONE_CALL_MARK.reset(token)


def _one_runner_module() -> Any:
    """``yuki.one.runner`` (lane oRun), or ``None`` until it is merged or when it fails to load (fail closed)."""
    try:
        from yuki.one import runner

        return runner
    except Exception:  # noqa: BLE001 - every act then fails closed (ONE_NO_RUNNER)
        return None


@dataclass
class _OneReply:
    """One model reply's calls in the order written, the turn they belong to, and their outcomes as they come back
    (each call waits for the ones before it: a reply's calls run in order, hotTools' collector keeps them all)."""

    turn: Any
    ids: list[str]
    names: dict[str, str]
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    done: dict[str, asyncio.Event] = field(default_factory=dict)
    verdict: str = ""
    #: Review fixes M1/M2: each call's arguments and result (every non-success of the reply owes code's fallback, not
    #: only the last call's), the calls already owed, and whether the reply's own words (or a say_first) were spoken.
    args: dict[str, dict[str, Any]] = field(default_factory=dict)
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    owed: set[str] = field(default_factory=set)
    spoke: bool = False
    #: Final-B (one-voice RC B/O1b): the turn's owed tokens this reply took on (held while its calls run, dropped once
    #: their results are in), and the calls of it that were tried (an act or write that ran through the runner; never
    #: a read: review fix F4).
    took: list[int] = field(default_factory=list)
    tried: set[str] = field(default_factory=set)
    released: bool = False
    #: voice20 (final-D S22i): this reply answers the reply watch's rerun of an unheard close (marker loss).
    rerun: bool = False

    def before(self, call_id: str) -> list[str]:
        return self.ids[: self.ids.index(call_id)] if call_id in self.ids else []

    def last(self, call_id: str) -> bool:
        return bool(self.ids) and self.ids[-1] == call_id

    def in_order(self, skip: str | None = None) -> list[dict[str, Any]]:
        return [self.outcomes[i] for i in self.ids if i in self.outcomes and i != skip]


class OneVoice:
    """The ONE agent's driver for one voice conversation (``brain = "one"``; made by :class:`Conversation`).

    * **Turns.** A user turn starts at the first model call whose context ends on a new message of theirs: one
      ``yuki.one.policy.Turn`` (the gate's state, carried from the last turn: unanswered questions, recent sends) with
      its ``yuki.one.budget.TurnBudget``. A question the gate asked last turn gets its yes/no read now, on a worker.
    * **Rounds.** Before every model call of the turn, ``budget.round()``; spent: code says its line, no model call.
      The per-turn block (time, Situation, lookup, notes: ``mouth.volatile_block``) is made once, at round 1, frozen,
      and rides on their own message every round (:func:`with_volatile_at`).
    * **Thinking.** Round 1 runs as the lean talker does (``between_tools``); from round 1's reply code picks the
      turn's mode (:func:`one_hard`): adaptive medium from round 2 when it is hard. Thinking blocks of adaptive rounds
      stay in the context for the turn and are stripped at its end, before any history edit (the summary).
    * **Calls.** Every call of a reply runs in order (:class:`_OneReply`), through the runner (``run(call, turn) ->
      dict``: budget -> gate -> desk -> act -> desk -> diff -> result; code's verified done line through ``speak``).
      ``quick_memory`` stays the voice's own read (gated here) and the four controls the bridge's.
    * **Settle.** The last call of a reply settles it (:func:`one_settle`): by code (``run_llm`` false) when every act
      verified, nothing was flagged and every ``then`` is empty; else the agent's next round. A non-success owes code's
      fallback (``narrator.OwedLedger``), said only if the agent's round is not heard.
    * **Hard stop.** A timer of ``budget.hard_left()``: the generation is interrupted, rule #149's line said, the turn
      closed; late results settle silently.
    * **Countdown.** One ``yuki.one.countdown.Countdown`` for the conversation (the runner runs sends through it): the
      island shows it (``VoiceBridge.one_send_pending``), the user's speech pauses it and their words decide (the
      agent's next round), Stop/Esc cancels it (:meth:`stop_send`), a chip swaps (:meth:`send_choice`).
    * **Context.** Pipecat's automatic summary never runs inside a turn; at a turn's end it runs when due (8k).
    """

    def __init__(self, conversation: Any) -> None:
        self.c = conversation
        #: The current user turn's gate state (``yuki.one.policy.Turn``), and the last turn that counted (carried from).
        self.turn: Any = None
        self.base: Any = None
        #: The current turn's own message: its identity and its text (the per-turn block rides on it).
        self.anchor: tuple[int, str] | None = None
        self.anchor_text: str | None = None
        #: The per-turn block, frozen at round 1.
        self.frozen: dict[str, Any] | None = None
        self.rounds = 0
        #: ``off`` | ``adaptive`` | ``adaptive_low``, picked from round 1's reply (:meth:`reply`).
        self.mode = "off"
        self.decided = False
        self.over = True
        self._committed = False
        #: This turn's facts for the claim gate: an acting call was made; one verified.
        self.called = False
        self.acted = False
        self.replies: dict[str, _OneReply] = {}
        self._answer: Any = None
        self._timer: Any = None
        self._runner: Any = None
        self._runner_tried = False
        self._countdown: Any = None
        self._schemas: list[dict[str, Any]] | None = None
        self._tokens = itertools.count(1)
        self._owed: list[int] = []
        self._seq = 0
        self._spoke_at = 0.0
        self._paused_at = 0.0
        self._words_since = True
        self._noise_task: Any = None
        #: Review fix H1: how many final transcripts with words were heard (``user_words``), and each turn's mark: the
        #: count its own words make (a turn begun on Flux's eager end of turn, before its final words: one more).
        self._heard_n = 0
        self._marks: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()
        #: Final-B (one-voice RC G): the owed asks' tokens (any turn): a new user turn drops them (the gate carries its
        #: own questions into the turn and reads their answer; an owed ask after their answer re-asks it).
        self._owed_asks: list[int] = []
        #: Final-B (one-voice RC B/O1b): runner and read calls in flight (the ledger's busy check counts them).
        self._running = 0
        #: Final-B: when words of Yuki's last left the speaker, and when the current turn began (monotonic): "nothing
        #: heard this turn" is a code fact of the stream, never of the words.
        self._heard_at = 0.0
        self._turn_at = 0.0
        #: Final-B (one-voice RC F/O3): a closing stay_quiet of a read-only, unheard user turn was refused this turn.
        self._quiet_refused = False
        #: Final-B (one-voice RC A): a closing reply of a result round with words and no marker line was not heard:
        #: ``due`` (the reply watch answers again once it is quiet), ``asked`` (that rerun is on its way); once a turn.
        self._rerun = ""
        self._rerun_used = False
        self._rerun_at = 0.0
        #: voice20 (final-D S5, contract C9): a closing reply of words was held once by Lane ONE's ``closing_hold``:
        #: ``due`` (the reply watch gives the agent its note once it is quiet), ``asked`` (that round is on its way);
        #: once a turn (``_hold_used``); its why (ONE's sentence, the note's text: :func:`_hold_text`), whose reading
        #: gave it (``turn``: the gate's turn, e.g. a tidy step; ``budget``: the budget's miss), and whether the held
        #: words that were spoken end as a question (review L1: they get longer to answer it).
        self._hold = ""
        self._hold_used = False
        self._hold_at = 0.0
        self._hold_why = ""
        self._hold_src = ""
        self._hold_asks = False
        #: Integration F2: the turn budget the hold paused while it waits on speech and quiet (waits are not counted,
        #: as the countdown's: the retry window stays open for the held round), resumed when the round goes or the turn
        #: ends (:meth:`_hold_resume`); ``None``: none paused.
        self._hold_paused: Any = None
        #: voice20 (final-D S22d): the owed tokens of acts whose announcing words (a say_first, or the reply's words
        #: before the call) were heard and that did not take: kept owed past a later try, until words are heard.
        self._claimed: set[int] = set()

    # -- plumbing ---------------------------------------------------------------------------------------------------

    def with_format(self, result: Any) -> Any:
        """Final-B (one-voice RC A): a result the ONE driver returns, with the marker channel's format as its last
        field (:data:`ONE_FORMAT_KEY`, ``mouth.MARKER_REMINDER``, the same bytes every time), so every ONE round reads
        the format last. Marker channel only; a result that is not a mapping is left as it is."""
        if not isinstance(result, dict) or getattr(self.c, "channel", "") != "marker":
            return result
        from yuki.voice.mouth import MARKER_REMINDER

        out = {k: v for k, v in result.items() if k != ONE_FORMAT_KEY}
        out[ONE_FORMAT_KEY] = MARKER_REMINDER
        return out

    def heard_now(self, now: float) -> None:
        """Words of Yuki's left the speaker (``Conversation._on_audible``)."""
        self._heard_at = max(self._heard_at, float(now or 0.0))

    def heard_since(self, since: float) -> bool:
        """Whether any words of Yuki's left the speaker at or after ``since`` (monotonic)."""
        return self._heard_at > 0.0 and self._heard_at >= since

    def heard_this_turn(self) -> bool:
        return self.turn is not None and self.heard_since(self._turn_at)

    def busy(self) -> bool:
        """Final-B (one-voice RC B/O1b): for the owed ledger, a ONE turn is busy while it is open (a round being
        written or a runner call pending): an owed line falls due only after the turn is over with nothing heard."""
        return self._running > 0 or not self.over

    def _turn_groups(self, turn: Any = None) -> list[_OneReply]:
        """The replies of ``turn`` (the current one), each once, in the order they were made."""
        turn = self.turn if turn is None else turn
        seen: dict[int, _OneReply] = {}
        for group in list(self.replies.values()):
            if group.turn is turn and id(group) not in seen:
                seen[id(group)] = group
        return list(seen.values())

    def _log(self, op: str, **fields: Any) -> None:
        log = getattr(self.c, "log", None)
        if log is None:
            return
        try:
            log.event("one_voice", op=op, session=getattr(self.c, "session", None), **fields)
        except Exception:  # noqa: BLE001 - a log never breaks the voice
            pass

    def _gate_log(self, event: str, **fields: Any) -> None:
        """The gate's, the budget's and the countdown's content-free lines, into the voice log."""
        log = getattr(self.c, "log", None)
        if log is None:
            return
        try:
            log.event(event, session=getattr(self.c, "session", None), **fields)
        except Exception:  # noqa: BLE001
            pass

    def _agent(self) -> Any:
        """The worker lane's agent: its client and settings carry the commit check and the yes/no read (as the hands'
        own commit check reads them today)."""
        runtime = getattr(self.c.bridge, "runtime", None)
        return getattr(getattr(runtime, "worker", None), "agent", None)

    def preload(self) -> None:
        """The ONE modules imported now, at the conversation's build (never on a live turn's path)."""
        from yuki.brain import check, consent  # noqa: F401
        from yuki.memory import contacts  # noqa: F401 - the send's name lookup (row 4, the countdown's name)
        from yuki.one import budget, countdown, policy, prompt, tools  # noqa: F401

        if _one_runner_module() is None:
            self._log("runner", ready=False, why="import")

    def schemas(self) -> list[dict[str, Any]]:
        """This conversation's ONE voice tools (``yuki.one.tools.schemas("voice", keys)``), fixed once."""
        if self._schemas is None:
            from yuki.one import tools as one_tools

            keys = list(self.c._skill_keys())
            self._schemas = one_tools.schemas("voice", keys)
            self._log("tools", tools=len(self._schemas), chars=one_tools.block_chars(self._schemas), skills=len(keys))
        return list(self._schemas)

    def register(self, llm: Any) -> None:
        """Every ONE voice tool on the talker's service: the controls to the bridge (sync, as before), quick_memory to
        the voice's own read, everything else to the runner. Acts and slow reads are never cancelled by a barge-in
        (the act may have happened; its result must reach the context); acts have no Pipecat timer (the budget's hard
        stop and the runner's own backstop end them)."""
        from yuki.one import tools as one_tools

        for tool in self.schemas():
            name = str(tool["name"])
            if name in ONE_CONTROLS:
                llm.register_function(name, self._control_for(name))
            elif name == QUICK_MEMORY["name"]:
                llm.register_function(name, self.quick, cancel_on_interruption=False, timeout_secs=20)
            else:
                spec = one_tools.SPECS.get(name)
                acts = spec is not None and spec.acts
                llm.register_function(name, self.handle, cancel_on_interruption=False,
                                      timeout_secs=None if acts else 30)

    def _control_for(self, name: str) -> Callable[[Any], Any]:
        handler = getattr(self.c.bridge, name)

        async def run(params: Any) -> None:
            await self.control(params, handler)

        return run

    def hold_summaries(self, summarizer: Any) -> None:
        """Pipecat's automatic summary never runs inside a turn (it would edit the turn's history, thinking blocks
        and all): its trigger is held here and fired at a turn's end (:meth:`turn_over`), at the same 8k threshold."""
        if summarizer is None or getattr(summarizer, "_yuki_should", None) is not None:
            return
        should = getattr(summarizer, "_should_summarize", None)
        if not callable(should):
            return
        summarizer._yuki_should = should
        summarizer._should_summarize = lambda: False

    # -- the people index and the recent messages (row 4's facts, from memory) ---------------------------------------

    def _people_in(self, text: str) -> list[list[str]] | None:
        memory = getattr(self.c, "memory", None)
        call = getattr(memory, "call", None)
        if not callable(call):
            return None

        def go(client: Any) -> list[list[str]]:
            from yuki.memory.contacts import named_in

            return [list(c.names) for c, _matched in named_in(getattr(client, "store", client), text)]

        try:
            got = call("one_people", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001 - memory can't say
            return None
        return got if isinstance(got, list) else None

    def _recent_to(self, to: str) -> list[dict[str, Any]]:
        memory = getattr(self.c, "memory", None)
        call = getattr(memory, "call", None)
        if not callable(call):
            return []

        def go(client: Any) -> list[dict[str, Any]]:
            from yuki.memory.contacts import resolve, samples

            store = getattr(client, "store", client)
            rows: list[dict[str, Any]] = []
            for person in resolve(store, to)[:1]:
                for route in person.routes:
                    rows += samples(store, route, 3)
            rows.sort(key=lambda r: -float(r.get("at") or 0.0))
            return rows[:3]

        try:
            got = call("one_recent", go, timeout_s=0.8)
        except Exception:  # noqa: BLE001
            return []
        return [r for r in got if isinstance(r, dict)] if isinstance(got, list) else []

    # -- turns ------------------------------------------------------------------------------------------------------

    def _words(self) -> tuple[list[str], list[str]]:
        """Their words for this request (the current exchange, as the hands' commit check reads it) and up to 3
        earlier turns."""
        exchange = list(self.c._exchange_words())
        heard = [t for _at, t in list(getattr(self.c.observer, "recent_heard", ()) or ())]
        earlier = heard[: max(0, len(heard) - len(exchange))][-3:]
        return exchange, earlier

    def _checker(self) -> tuple[Any, Any]:
        """The commit check for a turn's gate (the worker agent's client, as the hands' own reads it) and its
        settings."""
        from yuki.brain.check import CommitCheck

        agent = self._agent()
        settings = getattr(agent, "settings", None) if agent is not None else None
        settings = settings if settings is not None else self.c.settings
        return CommitCheck((lambda: agent.client) if agent is not None else None, settings, self._gate_log), settings

    def _new_turn(self, anchor: tuple[int, str] | None, *, eager: bool = False) -> None:
        from yuki.one import budget as one_budget
        from yuki.one import policy

        if self.turn is not None:
            self.turn.moved_on = True   # safety timing 4: a late act of the last turn takes no tidy step of code's now
        if self.turn is not None and not self.over:
            self.turn_over("next")   # they spoke on: the last turn ends here (its thinking blocks go, its timer stops)
        else:
            self._strip()
        # review fix H1: their new words while a send counts down (begun before it opened, or an eager end of turn
        # whose final words are still to come): it pauses now, and their words decide (``user_words``)
        self._pause_countdown("turn")
        checker, settings = self._checker()
        words, earlier = self._words()
        if anchor is not None and anchor[1] and (not words or words[-1] != anchor[1]):
            words = [*words, anchor[1]][-4:]   # the newest words before their final transcript was kept
        self._seq += 1
        turn = policy.Turn("voice", words, earlier, request_id=f"{getattr(self.c, 'session', '')}-one-{self._seq}",
                           checker=checker, settings=settings, people_in=self._people_in, recent=self._recent_to,
                           budget=one_budget.TurnBudget("voice", one_budget.limits_for("voice", self.c.settings)),
                           log=self._gate_log).carry(self.base)
        self.turn = turn
        obs = getattr(self.c, "observer", None)
        begun = float(getattr(obs, "_user_started_at", 0.0) or 0.0) > float(getattr(obs, "last_user_at", 0.0) or 0.0)
        # its own final words not heard yet (Flux's eager end of turn): they will make the count one more
        self._marks[turn] = self._heard_n + (1 if eager and begun else 0)
        self.anchor, self.anchor_text = anchor, (anchor[1] if anchor is not None else None)
        self.frozen, self.rounds, self.mode, self.decided = None, 0, "off", False
        self.over, self._committed, self.called, self.acted = False, False, False, False
        self._owed = []
        # final-B (one-voice RC G): their new words answer (or move on from) every question owed before them
        self._drop_owed_asks()
        self._turn_at = time.monotonic()
        self._quiet_refused, self._rerun, self._rerun_used = False, "", False
        self._hold, self._hold_used, self._hold_why = "", False, ""
        self._hold_src, self._hold_asks = "", False
        self._hold_resume()   # integration F2: never a budget left paused (the last turn's own ended above)
        self._claimed = set()
        self._answer = None
        if turn.asks and self.anchor_text:
            self._read_answer(turn, self.anchor_text)
        self._log("turn", n=self._seq, words=len(words), earlier=len(earlier), asks=len(turn.asks),
                  sent_before=len(turn.sent_before))

    def _read_answer(self, turn: Any, reply: str) -> None:
        """The user answered the gate's question from last turn (or said something else): its yes/no read, on a
        worker, recorded on the turn as a code fact before any of the turn's calls goes to the gate."""
        try:
            question = list(turn.asks.values())[-1][0]
        except Exception:  # noqa: BLE001
            return
        agent = self._agent()
        client = getattr(agent, "client", None) if agent is not None else None
        settings = getattr(agent, "settings", None) if agent is not None else None
        settings = settings if settings is not None else self.c.settings

        def work() -> bool:
            from yuki.one.policy import read_answer

            yes = bool(read_answer(client, settings, question, reply, log=self._gate_log))
            turn.answer(yes)
            return yes

        try:
            self._answer = asyncio.get_running_loop().run_in_executor(None, work)
        except RuntimeError:
            self._answer = None

    async def _wait_answer(self) -> None:
        pending = self._answer
        if pending is None or pending.done():
            return
        try:
            await asyncio.wait_for(asyncio.shield(pending), ONE_ANSWER_WAIT_S)
        except Exception:  # noqa: BLE001 - not answered in time: the gate asks again (fail closed)
            self._log("answer", waited=True, done=False)

    async def begin(self, context: Any, call: Any) -> bool:
        """Before each model call (the talker service's ``_process_context``): the turn this call belongs to (a new one
        on their new words), then the budget's round (before every model call of the turn). ``False``: code settled
        the turn (rounds spent, the hard stop, a closed turn): no model call is made."""
        anchor: tuple[int, str] | None = None
        if getattr(call, "user_turn", False):
            try:
                messages = list(context.get_messages() or [])
            except Exception:  # noqa: BLE001
                messages = []
            last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role") == "user"), None)
            if last is not None:
                anchor = (id(last), _user_text(last))
        if self.turn is None or (anchor is not None and anchor != self.anchor):
            self._new_turn(anchor, eager=bool(getattr(call, "one_eager", False)))
        turn = self.turn
        spend = turn.budget.round()
        if not spend.ok:
            self._log("round", refused=spend.why, rounds=self.rounds)
            await self._settle_by_code(spend.line, silent=spend.silent, why=spend.why)
            return False
        self.rounds += 1
        call.one_round = self.rounds
        if self.rounds >= 2:
            # final-B (one-voice RC D/O6, design 2.6): hardness from this turn's code facts before every later round
            self._switch_if_hard()
        if self.over:
            # review fix L2: a round after code settled the turn (Pipecat's own run after a missing function's result):
            # the agent speaks in this turn again, so it is open again until that round ends
            self.over = False
            self._log("reopened", rounds=self.rounds)
        if self._timer is None:
            self._arm()   # review fix L2: every round of the turn runs under the hard stop, not only round 1
        self._log("round", n=self.rounds, user=bool(getattr(call, "user_turn", False)), thinking=self._thinking_now())
        return True

    def needs_reads(self, call: Any) -> bool:
        """Whether this call waits for the lookup and the desktop (only the call that makes the turn's block)."""
        return self.frozen is None or bool(getattr(call, "one_fresh", False))

    def _thinking_now(self) -> str:
        return self.mode if self.rounds >= 2 else "off"

    def keeps_thoughts(self, call: Any) -> bool:
        """An adaptive round's thinking blocks stay in the context for the turn (stripped at its end)."""
        return int(getattr(call, "one_round", 0) or 0) >= 2 and self.mode != "off"

    def state(self, state: dict[str, Any] | None) -> dict[str, Any]:
        """The measured states for the ONE turn's block: the conversation's (``_prompt_state``) without the talker's
        hand notes (they name hand_over; the agent reads each result itself)."""
        return {k: v for k, v in (state or {}).items() if k not in ("hand_trouble", "hand_game", "acted")}

    def volatile(self, call: Any, make: Callable[[], dict[str, Any]]) -> tuple[dict[str, Any], str | None]:
        """The per-turn block for this call and the text of their message it rides on (``None``: the newest user
        message, as before): made once per turn at round 1 (made again on a rerun), the same bytes every round."""
        if call is None or not getattr(call, "one_round", 0) or self.turn is None:
            return make(), None   # the warm-up, or a call outside a ONE round
        if self.frozen is None or getattr(call, "one_fresh", False):
            block = make()
            if not (self.anchor_text and getattr(call, "user_turn", False)):
                return block, None   # a turn no words of theirs started: a fresh block each round
            self.frozen = block
        return self.frozen, self.anchor_text

    def thinking(self, params: dict[str, Any], call: Any) -> None:
        """Round 2 on of a hard turn: adaptive thinking at medium effort (``adaptive_low``: low), its text omitted, with
        room for it. Round 1 and easy turns keep the talker's ``between_tools``."""
        if call is None or int(getattr(call, "one_round", 0) or 0) < 2 or self.mode == "off":
            return
        params["thinking"] = {"type": "adaptive", "display": "omitted"}
        params["output_config"] = {"effort": "low" if self.mode == "adaptive_low" else ONE_EFFORT}
        floor = int(getattr(self.c.settings, "voice_mouth_max_tokens", 400) or 400)
        params["max_tokens"] = max(floor, ONE_THINK_MAX_TOKENS)

    def reply(self, function_calls: Any) -> None:
        """A reply's calls, complete and about to run (hotTools' collector gave all of them): kept in order as one
        group, and from the turn's first reply with calls the turn's thinking mode (:func:`one_hard`)."""
        calls = [(str(getattr(fc, "tool_call_id", "") or ""), str(getattr(fc, "function_name", "") or ""),
                  getattr(fc, "arguments", None) if isinstance(getattr(fc, "arguments", None), dict) else {})
                 for fc in function_calls or []]
        # only calls this driver runs are in the reply's order (a name it has no handler for is answered by Pipecat's
        # own missing-function path, and nothing would ever wait for it)
        known = {str(t.get("name") or "") for t in self.schemas()}
        calls = [c for c in calls if c[0] and c[1] in known]
        if not calls or calls[0][0] in self.replies:
            return
        if self.turn is None:
            self._new_turn(None)
        group = _OneReply(turn=self.turn, ids=[c[0] for c in calls], names={c[0]: c[1] for c in calls},
                          args={c[0]: dict(c[2]) for c in calls}, spoke=self._reply_spoke(),
                          rerun=self._rerun == "asked")
        for cid, _name, _args in calls:
            group.done[cid] = asyncio.Event()
            self.replies[cid] = group
        if len(self.replies) > 96:
            for old in list(self.replies)[:48]:
                del self.replies[old]
        self._committed = True
        if any(one_kind(n) == "act" for _c, n, _a in calls):
            self.called = True
        self._decide_mode(one_hard([(n, a) for _c, n, a in calls]), "calls", calls=len(calls))
        if group.turn is self.turn and any(one_kind(n) not in ("control", "unknown") for _c, n, _a in calls):
            # final-B (one-voice RC B/O1b, the hands' _take_owed rule): the agent goes on with what it was given: the
            # turn's owed lines stay held while this reply's calls run, and go once their results are in (kept only
            # if nothing of it was tried: :meth:`_let_owed_go`)
            group.took, self._owed = list(self._owed), []
        self._log("reply", calls=len(calls), tools=[n for _c, n, _a in calls], round=self.rounds)

    def _decide_mode(self, hard: bool, why: str, **fields: Any) -> None:
        """The turn's thinking mode, switched at most once a turn (design 2.6): ``fast`` never thinks and
        ``adaptive_low`` always does from round 2 (both decided at once); ``structure`` stays off until the turn is
        hard (its first reply's calls, :func:`one_hard`, or later its results, :meth:`_switch_if_hard`), then adaptive
        medium for the rest of the turn."""
        if self.decided:
            return
        mode = one_thinking_mode(self.c.settings)
        if mode == "fast":
            self.mode = "off"
        elif mode == "adaptive_low":
            self.mode = "adaptive_low"
        elif hard:
            self.mode = "adaptive"
        else:
            return   # easy so far: decided again before the next round, from what this turn's results show
        self.decided = True
        self._log("thinking", mode=self.mode, hard=hard, why=why, round=self.rounds, **fields)

    def _switch_if_hard(self) -> None:
        """Final-B (one-voice RC D/O6): before a round >= 2, the turn is hard (design 2.6, code facts of this turn
        only, never words) when a result is not a verified success (an act or write that did not take, a skill that
        asked or handed over), a look was run, two act rounds were made, or a send counted down."""
        if self.decided or self.turn is None:
            return
        groups = self._turn_groups()
        act_rounds = 0
        why = ""
        for group in groups:
            if any(one_kind(n) == "act" for n in group.names.values()):
                act_rounds += 1
            for cid in group.ids:
                name = group.names.get(cid, "")
                result = group.results.get(cid) or {}
                args = group.args.get(cid) or {}
                kind = one_kind(name)
                status = str(result.get("status") or "").strip().lower()
                if name == "look":
                    why = why or "look"
                elif kind in ("act", "write") and cid in group.results and not (
                        status in ONE_CLEAN and result.get("verified") is not False):
                    why = why or "not_verified"
                if result.get("countdown") or (name == "run_skill" and isinstance(args, dict) and args.get("consent")):
                    why = why or "countdown"
        if not why and act_rounds >= 2:
            why = "two_act_rounds"
        if why:
            self._decide_mode(True, why)

    def _reply_spoke(self) -> bool:
        """Review fix M2: whether the reply now making its calls spoke (its words, or a say_first said early), from the
        talker service's own stream marks (never the words)."""
        call = getattr(getattr(self.c, "_llm", None), "_yuki_call", None)
        return bool(call is not None and (getattr(call, "first_text", None) is not None
                                          or getattr(call, "early_say", None) is not None))

    def reply_end(self, call: Any, speculating: bool = False) -> None:
        """A reply ended. One with no call of a ONE round ends the turn (the agent decided it is done)."""
        if speculating or call is None or not getattr(call, "one_round", 0) or self.turn is None or self.over:
            return
        rerun, self._rerun = self._rerun, ("" if self._rerun == "asked" else self._rerun)
        if self._hold == "asked":
            self._hold = ""   # voice20 (S5): the held round's own reply is judged as any other (the hold is once)
        if getattr(call, "tool", False):
            return   # (a rerun answered with calls: a later closing reply of the turn is judged as any other)
        self._committed = True
        if self._closing_unheard(call):
            # final-B (one-voice RC A): its closing words had no marker line, so nothing of them was spoken and
            # nothing else was heard since: the turn stays open and the reply watch answers it again once (the
            # same rerun rule the user's own turn has), never a silent end
            self._rerun, self._rerun_at = "due", time.monotonic()
            self._log("unheard_close", round=int(getattr(call, "one_round", 0) or 0))
            return
        spoke = getattr(call, "first_text", None) is not None and not getattr(call, "one_unmarked", False)
        why = self._closing_hold() if spoke else None
        if why is not None:
            # voice20 (final-D S5, contract C9): Lane ONE says this closing reply is held once (one miss with nothing
            # else tried and time left; or a legal tidy step untaken): the turn stays open, and once they have been
            # quiet a moment (time to answer an offer themselves; longer when the spoken words asked them something,
            # review L1) the reply watch gives the agent one more round with ONE's own sentence as the note
            self._hold, self._hold_at, self._hold_used, self._hold_why = "due", time.monotonic(), True, why
            self._hold_asks = bool(getattr(call, "one_asks", False))
            # integration F2: the wait on the words' end and their quiet is not counted (the budget's own rule for
            # waits): the retry window the hold offers is still open when the held round's act comes
            budget = getattr(self.turn, "budget", None)
            if budget is not None and self._hold_paused is None and callable(getattr(budget, "pause", None)):
                budget.pause()
                self._hold_paused = budget
            self._log("hold", closing="words", round=int(getattr(call, "one_round", 0) or 0), source=self._hold_src,
                      asks=self._hold_asks, **_code_word(why))
            return
        if rerun == "asked" and self._rerun_silent(call) and not self._owed_pending():
            # the rerun spoke nothing either and nothing is owed: the error tone says so (a non-speech notice)
            self.c._tone("error")
            self._log("unheard_close", round=int(getattr(call, "one_round", 0) or 0), gave_up=True)
        if self._opened_marker(call):
            # voice22 (A9, final-D S10 leftover): round 1 chose a quiet marker alone on the first words of a
            # conversation they opened: refused once like a stay_quiet; the turn ends here as the agent's, and the
            # conversation's reply watch answers it again (round 2, the turn's block made again with the note)
            self.turn_over("opened_quiet")
            return
        self.turn_over("model")

    def _opened_marker(self, call: Any) -> bool:
        """voice22 (A9, the S10 leftover of voice21): round 1's reply is a quiet marker alone (no call: a stay_quiet
        call is :meth:`control`'s, refused by :meth:`_quiet_unanswered`), nothing of it spoken and no say_first, on the
        first words of a conversation they opened themselves (``Conversation._opened_quiet``, ``via`` one_marker: once
        a conversation, with its own conditions: no call live, no game, no dictation, nothing heard or acted). Refused
        once like a stay_quiet: the conversation's turn has its marker waived and is marked ``opened_quiet``, so the
        reply watch answers it again once with ``mouth.OPENED_RERUN_NOTE`` (``Conversation._unanswered``; the rerun
        makes the turn's block again). The rerun's own quiet is final. Stream facts only; never raises (no refusal)."""
        try:
            if int(getattr(call, "one_round", 0) or 0) != 1 or self._quiet_refused or not self.anchor_text:
                return False
            if getattr(call, "one_marker", None) != "quiet" or getattr(call, "one_unmarked", False):
                return False
            if getattr(call, "first_text", None) is not None or getattr(call, "early_say", None) is not None:
                return False
            opened = getattr(self.c, "_opened_quiet", None)
            if not callable(opened) or not opened(spoke=False, via="one_marker"):
                return False
            self._quiet_refused = True
            turn = getattr(getattr(self.c, "observer", None), "turn", None)
            if turn is not None and getattr(turn, "trigger", None) == "user":
                turn.marker, turn.opened_quiet = None, True
            self._log("quiet", refused="opened_first", round=1, via="marker")
            return True
        except Exception as exc:  # noqa: BLE001 - the quiet stands
            self._log("quiet", error=type(exc).__name__)
            return False

    def _rerun_silent(self, call: Any) -> bool:
        """Review fix F1: the rerun's closing reply gave them nothing, judged from its own stream facts when it ends
        (its words reach the speaker only after this, so "heard since" is not known yet): no marker line, or no
        words of it and no say_first went to the voice, and nothing else of Yuki's was heard since it began. A
        ``quiet`` marker (silence chosen) or a line held back as a verbatim repeat (heard already) is no give-up,
        as under every other brain (``_unanswered``, ``_give_up``)."""
        if getattr(call, "one_marker", None) == "quiet" or getattr(call, "one_repeat", False):
            return False
        spoke = not getattr(call, "one_unmarked", False) and (
            getattr(call, "first_text", None) is not None or getattr(call, "early_say", None) is not None)
        return not spoke and not self.heard_since(float(getattr(call, "entered", 0.0) or 0.0))

    def _closing_unheard(self, call: Any) -> bool:
        """Final-B (one-voice RC A): a closing reply (no call) of a result round of a turn their words began, whose text
        came with no marker line (``ReplyChannel.unmarked``: none of it spoken), with nothing of Yuki's heard since it
        began, and no rerun used this turn yet. Code facts of the stream only."""
        if self._rerun_used or not self.anchor_text or int(getattr(call, "one_round", 0) or 0) < 2:
            return False
        if not getattr(call, "one_unmarked", False):
            return False
        if getattr(call, "first_text", None) is not None or getattr(call, "early_say", None) is not None:
            return False
        return not self.heard_since(float(getattr(call, "entered", 0.0) or 0.0))

    def rerun_due(self) -> bool:
        """The reply watch's ONE rerun is due (:meth:`reply_end`)."""
        return self._rerun == "due" and not self.over and self.turn is not None and not self.turn.budget.closed

    def rerun_go(self) -> None:
        """The reply watch is answering the turn again now (once a turn)."""
        self._rerun, self._rerun_used = "asked", True

    def rerun_stale(self, wait_s: float) -> bool:
        """The rerun has been due this long with no quiet moment to run it in (sound or speech went on)."""
        return self._rerun == "due" and time.monotonic() - self._rerun_at >= wait_s

    def rerun_expire(self) -> None:
        """No quiet moment came for the rerun: the turn ends as it would have (what it owes falls due), never held open
        until the hard stop's line."""
        self._rerun, self._rerun_used = "", True
        self._log("unheard_close", expired=True)
        self.turn_over("unheard")

    # -- voice20 (final-D S5, contract C9): a closing reply after one miss is held once -------------------------------

    #: After held closing words, the agent's other way goes once they have been quiet this long (s): long enough to
    #: answer an offer themselves (their words then start a turn of their own and the hold goes).
    HOLD_QUIET_S = 1.5
    #: Review L1: the same wait when the held words that were spoken end a sentence as a question (their own closing
    #: mark, ``ReplyChannel.spoken_asks``): an answer takes longer to begin than a reaction, and Flux reports its start
    #: of turn some 0.5-1 s into speech.
    HOLD_ASK_QUIET_S = 3.0

    def hold_quiet_s(self) -> float:
        """How long they must have been quiet before the held round goes (:data:`HOLD_ASK_QUIET_S` after a spoken
        question, else :data:`HOLD_QUIET_S`)."""
        return self.HOLD_ASK_QUIET_S if self._hold_asks else self.HOLD_QUIET_S

    def _closing_hold(self) -> str | None:
        """Why this turn's closing reply is held once (Lane ONE's sentence), else ``None``. Contract C9, read through
        ``getattr`` (a no-op until that lane is merged); review M1: the gate's turn (``policy.Turn.closing_hold``) is
        asked first, as it holds the budget's reading (one miss, no act after it, the retry window open) and, before
        the S1 decision put the tidy step in code, the S1 fallback's (a flagged diff's one legal undoable step
        untaken); the turn's budget (``TurnBudget.closing_hold``) only when the turn has no such reading. Contract C12
        (voice21): the turn's reading may now be the miss reason alone (a sentence, or a bare code word: then
        :data:`ONE_MISS_HOLD` is the note, :func:`_hold_text`). The voice's own facts first: once a turn, a turn their
        words began and no newer words of theirs moved on from, not closed by code, and never after a send or join
        that missed (it is never tried again in the turn). Sets ``_hold_src``. Never raises."""
        self._hold_src = ""
        turn = self.turn
        if turn is None or self.over or self._hold_used or not self.anchor_text:
            return None
        budget = getattr(turn, "budget", None)
        if budget is None or getattr(budget, "closed", False) is True \
                or getattr(budget, "lasting_missed", False) is True:
            return None
        mark = self._marks.get(turn)
        if mark is not None and self._heard_n > mark:
            return None   # their newer words make a turn of their own

        def ask(owner: Any) -> tuple[bool, str]:
            fn = getattr(owner, "closing_hold", None)
            if not callable(fn):
                return False, ""
            try:
                got = fn()
            except Exception as exc:  # noqa: BLE001 - no reading from it
                self._log("hold", error=type(exc).__name__)
                return False, ""
            return True, (" ".join(str(got).split()) if got else "")

        answered, why = ask(turn)
        source = "turn"
        if not answered:
            answered, why = ask(budget)
            source = "budget"
        if not why:
            return None
        if source == "turn":
            # content-free provenance for the log: the budget's own sentence (a miss), or the turn's own (a tidy step)
            own_answered, own = ask(budget)
            if own_answered and own == why:
                source = "budget"
        self._hold_src = source
        return why

    def _lone_quiet(self, group: _OneReply) -> bool:
        """The reply is a stay_quiet alone (the whole closing reply) of the current turn."""
        return group.turn is self.turn and bool(group.names) and all(n == "stay_quiet" for n in group.names.values())

    def _quiet_unheard_rerun(self, group: _OneReply) -> bool:
        """voice20 (final-D S22i): a lone stay_quiet answering the reply watch's rerun of an unheard close (closing
        words with no marker line, none of them spoken), that spoke nothing itself, with nothing of Yuki's heard since
        that close and no quiet refused this turn yet: the agent's words never reached them, so its quiet is refused
        once (live final-D p1_github: the rerun chose stay_quiet and its 121 characters were never heard). Stream facts
        only."""
        if not group.rerun or self._quiet_refused or group.spoke or not self._lone_quiet(group):
            return False
        if self.heard_since(self._rerun_at):
            return False
        self._quiet_refused = True
        return True

    def hold_due(self) -> bool:
        """The reply watch's held round is due (:meth:`reply_end`)."""
        return self._hold == "due" and not self.over and self.turn is not None and not self.turn.budget.closed

    def hold_note(self) -> str:
        """The held round's note: Lane ONE's own sentence (:func:`_hold_text`; :data:`ONE_MISS_HOLD` only as the
        fallback), after the agent's closing words in the bracketed form of the turn's own notes, as a developer-role
        message (Pipecat's Anthropic adapter sends it as the user's turn, as :meth:`Conversation._announce` does);
        marker channel: the format last, as every ONE round reads it."""
        from yuki.voice.mouth import MARKER_REMINDER

        note = f"[{_hold_text(self._hold_why)}]"
        return f"{note}\n{MARKER_REMINDER}" if getattr(self.c, "channel", "") == "marker" else note

    def _hold_resume(self) -> None:
        """Integration F2: the budget the hold paused counts again (the held round's own generation is counted)."""
        budget, self._hold_paused = self._hold_paused, None
        if budget is not None:
            try:
                budget.resume()
            except Exception as exc:  # noqa: BLE001 - never breaks the turn
                self._log("hold", resume_error=type(exc).__name__)

    def hold_go(self) -> str:
        """The reply watch gives the held turn its round now (once a turn): the note it runs with."""
        self._hold_resume()
        self._hold = "asked"
        self._log("hold", go=True, round=self.rounds, waited_ms=round((time.monotonic() - self._hold_at) * 1000),
                  source=self._hold_src, asks=self._hold_asks, **_code_word(self._hold_why))
        return self.hold_note()

    def hold_stale(self, wait_s: float) -> bool:
        """The held round has been due this long with no quiet moment to run it in."""
        return self._hold == "due" and time.monotonic() - self._hold_at >= wait_s

    def hold_expire(self) -> None:
        """No quiet moment came for the held round: the turn ends as it would have (what it owes falls due)."""
        self._hold = ""
        self._log("hold", expired=True)
        self.turn_over("model")

    def _owed_pending(self) -> bool:
        ledger = getattr(self.c, "ledger", None)
        try:
            pending = {item.token for item in ledger.pending} if ledger is not None else set()
        except Exception:  # noqa: BLE001
            return False
        return any(t in pending for t in self._owed)

    def claim_open(self) -> bool:
        """The claim gate (speech guard): something was called this turn and nothing of it verified."""
        return bool(self.called and not self.acted)

    def turn_over(self, why: str) -> None:
        """The turn ended (the agent's reply with no call, a settle by code, the hard stop, their next words): its
        timer stops, its thinking blocks leave the context, then the summary runs if it is due."""
        if self.over:
            return
        self.over = True
        self._rerun = ""
        self._hold = ""
        self._hold_resume()   # integration F2: a hold that expired, or their new words: the wait is over
        self._cancel_timer()
        # final-B (one-voice RC B/O1c): the turn is over: what it still owes falls due now (said by code at the next
        # quiet moment unless the agent's own words were heard for it)
        self._release_owed()
        if self._committed and self.turn is not None:
            self.base = self.turn
        self._strip()
        self._summarize()
        facts = self.turn.budget.facts() if self.turn is not None and self.turn.budget is not None else {}
        self._log("turn_over", why=why, mode=self.mode, called=self.called, acted=self.acted, **facts)

    def _strip(self) -> None:
        context = getattr(self.c, "_llm_context", None)
        if context is None:
            return
        try:
            messages = list(context.get_messages() or [])
            kept = strip_thoughts(messages)
            if len(kept) != len(messages):
                context.set_messages(kept)
                self._log("thoughts", stripped=len(messages) - len(kept))
        except Exception as exc:  # noqa: BLE001 - never breaks the turn
            self._log("thoughts", error=type(exc).__name__)

    def _summarize(self) -> None:
        summarizer = getattr(self.c, "_summarizer", None)
        should = getattr(summarizer, "_yuki_should", None)
        request = getattr(summarizer, "_request_summarization", None)
        if not callable(should) or not callable(request):
            return
        pending = getattr(self, "_summary_task", None)
        if pending is not None and not pending.done():
            return   # a turn reopened and ended again before the last request started: never two summaries
        try:
            if not should():
                return
            self._summary_task = asyncio.ensure_future(request())
            self._log("summary", requested=True)
        except Exception as exc:  # noqa: BLE001 - a summary is an optimisation
            self._log("summary", error=type(exc).__name__)

    # -- the hard stop ------------------------------------------------------------------------------------------------

    def _cancel_timer(self) -> None:
        timer, self._timer = self._timer, None
        if timer is not None:
            timer.cancel()

    def _arm(self) -> None:
        """The hard stop's timer, for the counted time left (a countdown's pause does not run it down)."""
        self._cancel_timer()
        loop, turn = getattr(self.c, "_loop", None), self.turn
        if loop is None or turn is None or self.over or turn.budget is None:
            return
        self._timer = loop.call_later(max(0.05, turn.budget.hard_left()) + 0.05, self._hard_tick, turn)

    def _hard_tick(self, turn: Any) -> None:
        self._timer = None
        if turn is not self.turn or self.over or turn.budget.closed:
            return
        if not turn.budget.hard_due():
            self._arm()   # paused time (a countdown) moved the stop
            return
        asyncio.ensure_future(self.hard_stop(turn))

    async def hard_stop(self, turn: Any) -> None:
        """The hard stop (addendum must-fix 6): the generation is interrupted (the pipeline's interruption, as a
        barge-in), rule #149's line is said, and the turn is closed: every later result settles silently."""
        from yuki.one.budget import LINE_REST_LEFT

        if turn.budget.closed and self.over:
            return
        turn.budget.close("hard")
        self._log("hard_stop", **turn.budget.facts())
        self._drop_owed()
        self.turn_over("hard")   # first: the cut reply's end is not read as the agent ending the turn
        await self._interrupt()
        await self.c._speak_owed(LINE_REST_LEFT)

    async def _interrupt(self) -> None:
        worker = getattr(self.c, "worker", None)
        if worker is None:
            return
        try:
            from pipecat.frames.frames import InterruptionWorkerFrame

            await worker.queue_frame(InterruptionWorkerFrame())
            await asyncio.sleep(0.3)   # through the pipeline before the line goes after it
        except Exception as exc:  # noqa: BLE001 - the line is said anyway
            self._log("hard_stop", interrupt_error=type(exc).__name__)

    async def _settle_by_code(self, line: str, *, silent: bool, why: str) -> None:
        self._drop_owed()
        if line and not silent:
            await self.c._speak_owed(line)
        self.turn_over(why)

    # -- the owed fallback ----------------------------------------------------------------------------------------

    def _owe(self, result: dict[str, Any], outcome: dict[str, Any], *, keep: bool = True) -> int | None:
        """A non-success act that code said nothing for owes code's fallback (``OwedLedger``): said only if the
        agent's next round is not heard (it chose silence, or never came within ``RUN_WAIT_S``). ``keep``: the token
        is the current turn's (dropped when it settles by code); an earlier turn's late result keeps its own (review
        fix L1). A held call still under way owes nothing (``_no_owe``: the agent asks or says it).
        Returns the owed token (voice20: :meth:`_owe_group` marks a claimed act's), else ``None``."""
        ledger = getattr(self.c, "ledger", None)
        status = str(result.get("status") or "").strip().lower()
        if ledger is None or outcome.get("kind") != "act" or result.get("said") \
                or result.get("_no_owe") or status == "changed":
            return None   # a send their words or a chip changed: nothing failed; their words (or the agent) decide
        if status in ONE_TOOK or result.get("verified") is True:
            # final-B (one-voice RC B/O1a): the act took effect: a ``then`` (parts left), a flag (a tidy-up) or an
            # "already" nobody heard is the agent's next round, never code's failure line
            return None
        from yuki.one.budget import LINE_REST_LEFT

        question = " ".join(str(result.get("question") or "").split())
        if status in ("ask", "asked"):
            if not question:
                return None   # no structured question to owe: the agent words it from the result
            kind, text = "ask", question
        else:
            # the runner's own fallback line (``OneResult.owed``), else rule #149's
            kind, text = "failed", str(result.get("owed") or result.get("fallback") or "").strip() or LINE_REST_LEFT
        token = -next(self._tokens)
        try:
            if ledger.owe(token, kind, text, int(getattr(self.c.observer, "user_turns", 0))) is None:
                return None
            if kind == "ask":
                self._owed_asks.append(token)
                del self._owed_asks[:-24]
            if keep:
                self._owed.append(token)
                if not self.over:
                    # final-B (one-voice RC B/O1c): held while the turn is open (the agent's next round is coming);
                    # due once it is over with nothing heard (:meth:`turn_over`)
                    ledger.hold(token, True)
        except Exception:  # noqa: BLE001 - the agent's round says it anyway
            return None
        return token

    def _owe_group(self, group: _OneReply) -> None:
        """Review fix M1: when the agent gets its next round, every act of the reply that did not verify and that code
        said nothing for owes code's fallback, not only the reply's last call (``[failed act, done act]``,
        ``[failed act, stay_quiet]``). Each call once. voice20 (final-D S22d): a failure of a call whose announcing
        words were heard (:meth:`_claimed_call`) is marked claimed (:meth:`_let_owed_go` keeps it owed)."""
        keep = self._current(group)
        for cid in group.ids:
            outcome, result = group.outcomes.get(cid), group.results.get(cid)
            if outcome is None or result is None or cid in group.owed:
                continue
            group.owed.add(cid)
            token = self._owe(result, outcome, keep=keep)
            if token is not None and keep and self._claimed_call(group, cid):
                self._claimed.add(token)
                self._log("owed", claimed=True)

    def _claimed_call(self, group: _OneReply, call_id: str) -> bool:
        """voice20 (final-D S22d): words announcing this act went to the voice before it ran (its say_first, said early
        or by :meth:`_say_first_if_silent`, or the reply's own words before its calls: the talker service's
        ``_yuki_spoke``, a stream fact; never the words). Live final-D k_video_another_one under one: "Putting on a
        MrBeast Gaming video." was heard, the skill handed over, a later open_url was tried (which dropped the owed
        line) and the turn ended with nothing more said."""
        if group.turn is not self.turn or call_id not in group.names:
            return False
        spoken = getattr(getattr(self.c, "_llm", None), "_yuki_spoke", None)
        try:
            return call_id in spoken if spoken is not None else False
        except TypeError:
            return False

    @staticmethod
    def _heard(group: _OneReply) -> bool:
        """Review fix M2: something of the reply reached them: its own words or a say_first, a line code said for one
        of its calls (``said``) or heard earlier this turn (``heard``), or an act the agent chose to do in silence
        (``say_when_done`` given as ``""``: they watch it happen)."""
        if group.spoke:
            return True
        for cid in group.ids:
            result = group.results.get(cid) or {}
            if str(result.get("said") or "").strip() or result.get("heard") is True:
                return True
            args = group.args.get(cid) or {}
            if "say_when_done" in args and not " ".join(str(args.get("say_when_done") or "").split()) \
                    and (group.outcomes.get(cid) or {}).get("kind") == "act":
                return True
        return False

    def _drop_owed(self) -> None:
        ledger = getattr(self.c, "ledger", None)
        for token in self._owed:
            try:
                ledger.drop(token)
            except Exception:  # noqa: BLE001
                pass
        self._owed = []

    def _release_owed(self) -> None:
        """The turn is over: its held owed lines are due again (the ledger's own rules: said by code at a quiet moment
        unless heard). Never raises."""
        ledger = getattr(self.c, "ledger", None)
        if ledger is None:
            return
        for token in self._owed:
            try:
                ledger.hold(token, False)
            except Exception:  # noqa: BLE001
                pass

    def _drop_owed_asks(self) -> None:
        """Final-B (one-voice RC G): a new user turn: every owed ask still pending goes (their words answer it, or
        move on; the gate's own questions are carried into the turn and read from their answer)."""
        ledger = getattr(self.c, "ledger", None)
        asks, self._owed_asks = self._owed_asks, []
        for token in asks:
            try:
                if ledger is not None:
                    ledger.drop(token)
            except Exception:  # noqa: BLE001
                pass
        if asks:
            self._owed = [t for t in self._owed if t not in asks]

    def _let_owed_go(self, group: _OneReply) -> None:
        """Final-B (one-voice RC B/O1b, the hands' ``_let_owed_go``): once a reply's calls are all in, the owed lines
        it took on (:meth:`reply`) are dropped: the agent went on with what it was given, and its new results owe
        their own lines. Nothing of it tried (every call held, late, or a read): they stay owed (held while the turn is
        open).
        Once per reply. Never raises.

        voice20 (final-D S22d): an owed failure of an act whose announcing words were heard (``_claimed``: "Putting on
        X." said, then nothing of it happened) is not let go by a later try alone: only by a later act of this reply
        that took effect and left no parts (no ``then``). Else it stays held, due once the turn is over unless words
        of Yuki's are heard by then (the ledger's own rule): a claim that never landed always gets a closing line."""
        if group.released or not group.took or any(not e.is_set() for e in group.done.values()):
            return
        group.released = True
        took, group.took = group.took, []
        ledger = getattr(self.c, "ledger", None)
        if ledger is None:
            return
        tried = bool(group.tried)
        finished = any(self._finished(group, cid) for cid in group.tried)
        kept = 0
        for token in took:
            try:
                if tried and token in self._claimed and not finished and group.turn is self.turn and not self.over:
                    self._owed.append(token)   # voice20 (S22d): still held, due once the turn is over
                    kept += 1
                elif tried:
                    ledger.drop(token)
                elif group.turn is self.turn and not self.over:
                    self._owed.append(token)   # still held: due once the turn is over
                else:
                    ledger.hold(token, False)
            except Exception:  # noqa: BLE001
                pass
        self._log("owed", took=len(took), dropped=tried, **({"claimed_kept": kept} if kept else {}))

    @staticmethod
    def _finished(group: _OneReply, call_id: str) -> bool:
        """voice20 (final-D S22d): a call of the reply that acted and took effect with nothing left (no ``then``): the
        statuses :func:`OneVoice._owe` counts as taken, or ``verified`` true. Code facts of the result only."""
        outcome = group.outcomes.get(call_id) or {}
        result = group.results.get(call_id) or {}
        if outcome.get("kind") != "act" or outcome.get("then"):
            return False
        status = str(result.get("status") or "").strip().lower()
        return status in ONE_TOOK or result.get("verified") is True

    @staticmethod
    def _tried(group: _OneReply, call_id: str, name: str) -> None:
        """Review fix F4 (the hands' rule: a fallback goes only after the talker's next act): a call of the reply that
        ran counts as tried for :meth:`_let_owed_go` only when it acts or writes (the tool's own spec,
        :func:`one_kind`). A read (look, quick_memory, the memory reads) keeps the turn's owed lines held until the
        turn is over, when the ledger marks them heard if the agent's words came."""
        if one_kind(name) in ("act", "write"):
            group.tried.add(call_id)

    # -- the calls ------------------------------------------------------------------------------------------------

    def _group(self, call_id: str, name: str, args: dict[str, Any] | None = None) -> _OneReply:
        group = self.replies.get(call_id)
        if group is None:
            if self.turn is None:
                self._new_turn(None)
            group = _OneReply(turn=self.turn, ids=[call_id], names={call_id: name}, args={call_id: dict(args or {})},
                              spoke=self._reply_spoke())
            group.done[call_id] = asyncio.Event()
            self.replies[call_id] = group
        return group

    async def _after_earlier(self, group: _OneReply, call_id: str) -> None:
        for prior in group.before(call_id):
            event = group.done.get(prior)
            if event is None or event.is_set():
                continue
            try:
                await asyncio.wait_for(event.wait(), ONE_ORDER_WAIT_S)
            except asyncio.TimeoutError:
                self._log("order", waited_out=True)

    def _decide(self, group: _OneReply, call_id: str) -> bool:
        """``run_llm`` for a runner call's result: only the reply's last call runs the agent again, and only when the
        settle rule says so (a closed turn never). Review fixes: a reply code would settle with nothing heard (a lone
        ``remember``, a verified act with no line, no words) gets one closing round instead (M2); when the agent gets
        its round every non-success of the reply owes code's fallback (M1); a result of a turn their next words already
        ended runs no round of its own (L1: the new turn's round reads it, its fallback stays owed)."""
        if not group.last(call_id) or group.turn.budget.closed:
            if group.last(call_id):
                group.verdict = "end"
            return False
        current = self._current(group)
        verdict = one_settle(group.in_order())
        if verdict == "code" and current and not self._heard(group):
            verdict = "model"
            self._log("closing_round", calls=len(group.ids))
        group.verdict = verdict
        if verdict != "model":
            return False
        self._owe_group(group)
        if not current:
            return self._moved_on_run(group)
        return True

    def _moved_on_run(self, group: _OneReply) -> bool:
        """``run_llm`` for a reply that needs the agent but whose turn their newer words moved on from (review fix L1).
        Their words' own round reads it: no round of its own, and the turn ends if it is still the current one (its
        timer stops). One exception (addendum must-fix 2: parts left always get the agent's round): their round already
        began before this came back, and the reply left parts of their request (a ``then``) or a flagged diff, with no
        send their words changed: one round after theirs carries it on. A failure alone is owed (the ledger), never an
        extra round."""
        if group.turn is not self.turn:
            changed = any(str((group.results.get(cid) or {}).get("status") or "").lower() == "changed"
                          for cid in group.ids)
            left = any(o.get("kind") == "act" and (o.get("then") or o.get("flagged"))
                       for o in group.outcomes.values())
            if left and not changed:
                self._log("moved_on", run=True, parts=True)
                return True
            return False
        self._words_moved_on(group)
        return False

    def _current(self, group: _OneReply) -> bool:
        """Review fix L1: the reply's turn is still the one their words are on: no newer turn began, and no newer
        final words of theirs came (their words make their own round, which reads this result: a send their words
        stopped, a slow act that came back after they spoke on)."""
        if group.turn is not self.turn:
            return False
        mark = self._marks.get(group.turn)
        return mark is None or self._heard_n <= mark

    def _words_moved_on(self, group: _OneReply) -> None:
        """A result of a turn their newer words moved on from runs no round of its own (review fix L1); if that turn is
        still the current one (their words' own round has not begun yet), it ends now (its timer stops: no hard stop
        line for a turn they moved on from)."""
        if group.turn is self.turn and not self.over:
            self.turn_over("words")

    def _decide_inner(self, group: _OneReply, call_id: str, explicit: Any) -> bool | None:
        """``run_llm`` for a control's or quick_memory's own result (``None``: the handler's own properties stand)."""
        if not group.last(call_id):
            return False
        if group.names.get(call_id) in ONE_ENDS or group.turn.budget.closed:
            group.verdict = "end"
            return False
        current = self._current(group)
        others = group.in_order(skip=call_id)
        if others and one_settle(others) == "model":
            group.verdict = "model"
            self._owe_group(group)   # review fix M1: the failure is owed even if the agent's round stays quiet
            if not current:
                return self._moved_on_run(group)   # review fix L1: their newer words' own round reads this result
            return True   # an act of this reply needs the agent (a failure, a flag, parts left): never silence
        group.verdict = one_settle(group.in_order())
        if not current:
            if group.verdict == "model":
                return self._moved_on_run(group)
            return False   # review fix L1: their newer words' own round reads this result
        if explicit is not None:
            return None
        return {"model": True, "code": False, "end": False}.get(group.verdict)

    def _settled(self, group: _OneReply, call_id: str, run_llm: Any) -> None:
        """After a reply's last call settled: code settled it, the conversation ends, or a control ended it with no
        run (stay_quiet): the turn is over. A read answered at once on its own copy (``run_llm`` false there) is not."""
        if not group.last(call_id) or group.turn is not self.turn:
            return
        if group.verdict in ("code", "end") or (group.verdict == "control" and run_llm is False):
            self.turn_over(group.verdict)

    def _refresh_words(self, turn: Any) -> None:
        """Their words as the gate reads them, again at the call (their final transcript may have come after round 1
        began on the eager one)."""
        if turn is not self.turn:
            return
        words, earlier = self._words()
        if self.anchor_text and (not words or words[-1] != self.anchor_text):
            words = [*words, self.anchor_text][-4:]
        turn.words = [" ".join(str(w).split()) for w in words if str(w or "").strip()]
        turn.earlier = [" ".join(str(w).split()) for w in earlier if str(w or "").strip()][-3:]

    def _runner_obj(self) -> Any:
        if self._runner_tried:
            return self._runner
        self._runner_tried = True
        module = _one_runner_module()
        if module is None:
            self._log("runner", ready=False, why="import")
            return None
        hands = getattr(self.c.bridge, "hands", None)
        values: dict[str, Any] = {
            "channel": "voice", "hands": hands, "speak": self.speak, "countdown": self.countdown(),
            "log": self._gate_log, "settings": self.c.settings, "memory": getattr(self.c, "memory", None),
            "session": getattr(self.c, "session", None), "bridge": self.c.bridge,
            "runtime": getattr(self.c.bridge, "runtime", None), "quick_memory": getattr(self.c, "quick_memory", None),
            "menu": str(getattr(getattr(self.c, "mouth_memory", None), "skills", "") or "")}
        verbs = self._verbs(hands)
        if verbs is not None:
            values["verbs"] = verbs   # oRun's verbs layer over this conversation's hands (``voice.hands.Verbs``)
        factory = getattr(module, "Runner", None)
        obj: Any = None
        if callable(factory):
            bound = _one_bind(factory, values)
            if bound is not None:
                try:
                    obj = factory(*bound[0], **bound[1])
                except Exception as exc:  # noqa: BLE001 - the module's own run, else fail closed
                    self._log("runner", ready=False, why=f"make:{type(exc).__name__}")
        if obj is None and callable(getattr(module, "run", None)):
            obj = module
        self._runner = obj
        self._log("runner", ready=obj is not None, kind="class" if obj is not module else "module")
        return obj

    @staticmethod
    def _verbs(hands: Any) -> Any:
        """The hand verbs the runner acts with (``voice.hands.Verbs(hands)``, lane oRun), or ``None``."""
        if hands is None:
            return None
        try:
            from yuki.voice import hands as hands_module

            make = getattr(hands_module, "Verbs", None)
            return make(hands) if callable(make) else None
        except Exception:  # noqa: BLE001 - no verbs: no runner (fail closed)
            return None

    async def _run(self, name: str, args: dict[str, Any], turn: Any, call_id: str) -> dict[str, Any]:
        """One call through the runner (``run(call, turn) -> dict``, its parameters read off its signature), on a
        worker thread (it takes the desk lease and may wait in the countdown there). Never raises."""
        runner = self._runner_obj()
        fn = getattr(runner, "run", None) if runner is not None else None
        if not callable(fn):
            return dict(ONE_NO_RUNNER)
        call = {"name": name, "args": dict(args), "id": call_id, "channel": "voice"}
        values = {"call": call, "name": name, "tool": name, "args": dict(args), "tool_input": dict(args),
                  "arguments": dict(args), "turn": turn, "speak": self.speak, "channel": "voice", "call_id": call_id,
                  "tool_call_id": call_id}
        bound = _one_bind(fn, values)
        if bound is None:
            self._log("runner", ready=False, why="signature")
            return dict(ONE_NO_RUNNER)
        mark = self._marks.get(turn) if turn is not None else None
        try:
            if asyncio.iscoroutinefunction(fn):
                token = _ONE_CALL_MARK.set(mark)
                try:
                    raw = await fn(*bound[0], **bound[1])
                finally:
                    _ONE_CALL_MARK.reset(token)
            else:
                import functools

                raw = await asyncio.get_running_loop().run_in_executor(None, functools.partial(
                    _one_in_call, mark, fn, bound[0], bound[1]))
                if asyncio.iscoroutine(raw):
                    raw = await raw
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - fail closed, said as not confirmed
            self._log("call_error", tool=name, error=type(exc).__name__)
            return dict(ONE_RUN_ERROR)
        return one_result(raw)

    def _held_call(self, name: str, call_id: str, args: dict[str, Any], turn: Any) -> bool:
        """Review fix M4: this act waits for their answer (``Conversation._held``, decided once as the reply ended, the
        same rule as under ``all``: recognition was unsure of their words and the reply spoke with the act; the sound
        hands, ``keep_usual`` and a sound-only skill never wait). Only an act of the current turn."""
        if turn is not self.turn or one_kind(name) != "act":
            return False
        held = getattr(self.c, "_held", None)
        if not callable(held):
            return False
        try:
            return bool(held(name, call_id, args))
        except Exception:  # noqa: BLE001 - unknown: it runs (the gate decides it, as every act)
            return False

    #: From a worker thread a line is waited for until it is queued in the voice (at most this long), so an act's
    #: after-read comes after it (``yuki.one.runner.speaker``).
    SPEAK_WAIT_S = 1.0

    def speak(self, text: str) -> None:
        """Code's line said now in the one voice (a verified done line, a final refusal's, "Sending to <name>"): any
        thread; from a worker it returns once the line is queued (bounded). Never raises."""
        text = " ".join(str(text or "").split())
        loop = getattr(self.c, "_loop", None)
        if not text or loop is None:
            return
        try:
            if not _not_on(loop):
                asyncio.ensure_future(self.c._speak_result([text]))
                return
            asyncio.run_coroutine_threadsafe(self.c._speak_result([text]), loop).result(timeout=self.SPEAK_WAIT_S)
        except Exception:  # noqa: BLE001 - not queued in time: it still goes
            pass

    async def handle(self, params: Any) -> None:
        """The Pipecat handler of every ONE tool but the controls and quick_memory: in the reply's order, through the
        runner; the reply's last call settles it (:func:`one_settle`)."""
        from pipecat.frames.frames import FunctionCallResultProperties

        name = str(getattr(params, "function_name", "") or "")
        call_id = str(getattr(params, "tool_call_id", "") or "")
        args = dict(params.arguments) if isinstance(getattr(params, "arguments", None), dict) else {}
        group = self._group(call_id, name, args)
        run_llm: Any = False
        self._running += 1
        try:
            await self._after_earlier(group, call_id)
            turn = group.turn
            started = time.monotonic()
            how = "runner"
            if turn.budget.late():
                result: dict[str, Any] = {"status": "closed", "note": ONE_LATE_NOTE, "settle": True, "silent": True}
                how = "late"
            else:
                if self._held_call(name, call_id, args, turn):
                    # review fix M4: words and this act together on a turn recognition was unsure of: nothing is done
                    # and nothing was said; the agent's next round asks about the unsure word or calls it again alone
                    # (no thinker: the hold resolves through the agent itself)
                    result, how = {"status": "held", "verified": False, "_no_owe": True,
                                   "note": held_note(getattr(self.c.observer, "heard", None))}, "held"
                else:
                    await self._wait_answer()
                    self._refresh_words(turn)
                    await self._say_first_if_silent(params, group, call_id, name, args, turn)
                    self._tried(group, call_id, name)
                    result = await self._run(name, args, turn, call_id)
            outcome = one_outcome(name, args, result)
            group.outcomes[call_id] = outcome
            group.results[call_id] = result
            if outcome["kind"] == "act" and outcome["verified"] and turn is self.turn:
                self.acted = True
            line = str(result.get("line") or "").strip()
            if result.get("settle") and not result.get("silent") and line \
                    and " ".join(line.split()) not in " ".join(str(result.get("said") or "").split()):
                await self.c._speak_owed(line)   # the turn settled by code (the second refusal): its line, once
            await self._tidy_debt(group, call_id, turn, result, outcome)
            run_llm = self._decide(group, call_id)   # owes every non-success of the reply when the agent runs
            if group.verdict == "end" and turn is self.turn:
                self._drop_owed()
            payload = one_payload(result)
            if turn.budget.closed and not result.get("settle"):
                payload["how_to_handle_it"] = ONE_LATE_NOTE   # came back after the hard stop: no run follows
            self._log("call", tool=name, kind=outcome["kind"], verified=outcome["verified"],
                      flagged=outcome["flagged"], then=bool(outcome["then"]), settle=outcome["settle"],
                      last=group.last(call_id), verdict=group.verdict or None, run_llm=bool(run_llm), how=how,
                      current=turn is self.turn, ms=round((time.monotonic() - started) * 1000))
            await params.result_callback(self.with_format(payload),
                                         properties=FunctionCallResultProperties(run_llm=bool(run_llm)))
        finally:
            self._running = max(0, self._running - 1)
            event = group.done.get(call_id)
            if event is not None:
                event.set()
            self._let_owed_go(group)
            self._settled(group, call_id, run_llm)

    async def _say_first_if_silent(self, params: Any, group: _OneReply, call_id: str, name: str,
                                   args: dict[str, Any], turn: Any) -> None:
        """Final-B (one-voice RC C): a slow act's say_first said by code right before it runs when the reply that made
        it said nothing aloud and it was not said while the call was written (the bridge's ``_ack_if_silent``: one
        acknowledgement per request, a stream fact, the agent's own words), as the hands do under ``all``: never a
        silent wait for a slow act's done line. Only the current turn's acts. Never raises."""
        if turn is not self.turn or one_kind(name) != "act" or not str(args.get("say_first") or "").strip():
            return
        ack = getattr(self.c.bridge, "_ack_if_silent", None)
        if not callable(ack):
            return
        try:
            await ack(params, args)
        except Exception as exc:  # noqa: BLE001 - the act's own done line still comes
            self._log("say_first", error=type(exc).__name__)
            return
        llm = getattr(self.c, "_llm", None)
        if llm is not None and call_id in getattr(llm, "_yuki_spoke", ()):
            group.spoke = True

    async def _tidy_debt(self, group: _OneReply, call_id: str, turn: Any, result: dict[str, Any],
                         outcome: dict[str, Any]) -> None:
        """Final-B (one-desk D3/F6): the reply's last call would settle the turn by code while an earlier act's
        flagged diff of the turn is still open with no tidy step taken: the runner measures the desk against that
        act's own before (``Runner.tidy_debt``, once a turn, a few ms on a worker); still flagged, this result carries
        the net After and its flags, so the agent gets one round to tidy (never a loop). Never raises.

        integ22 (one22 merge note 1, A23): measured too when every later call of the reply is a ``stay_quiet``
        (``[act, stay_quiet]``: the bridge's own handler answers the quiet, which never reaches the runner), not only
        at the reply's last call."""
        if call_id not in group.ids or turn is not self.turn or turn.budget.closed:
            return
        later = group.ids[group.ids.index(call_id) + 1:]
        if any(group.names.get(i) != "stay_quiet" for i in later):
            return
        tidy = getattr(turn, "tidy", None)
        if tidy is None or not getattr(tidy, "open", False) or getattr(tidy, "acts", 0) or getattr(tidy, "given", True):
            return
        if one_settle(group.in_order()) != "code":
            return
        runner = self._runner_obj()
        measure = getattr(runner, "tidy_debt", None) if runner is not None else None
        if not callable(measure):
            return
        try:
            debt = await asyncio.get_running_loop().run_in_executor(None, measure, turn)
        except Exception as exc:  # noqa: BLE001 - no measure: code settles as before
            self._log("tidy_debt", error=type(exc).__name__)
            return
        if not isinstance(debt, dict) or not debt:
            return
        target = result["_payload"] if isinstance(result.get("_payload"), dict) else result
        target["after"] = str(debt.get("after") or "")
        target["flags"] = list(debt.get("flags") or [])
        result["flagged"] = True
        outcome["flagged"] = True
        self._log("tidy_debt", flagged=True, flags=len(target["flags"]))

    async def quick(self, params: Any) -> None:
        """``quick_memory`` on the ONE agent: the gate first (a read: allowed, the turn's private read noted; the
        budget's reads), then the voice's own quick memory (its answer-at-once path kept), in the reply's order."""
        from pipecat.frames.frames import FunctionCallResultProperties

        name = str(getattr(params, "function_name", "") or "")
        call_id = str(getattr(params, "tool_call_id", "") or "")
        args = dict(params.arguments) if isinstance(getattr(params, "arguments", None), dict) else {}
        group = self._group(call_id, name, args)
        final: dict[str, Any] = {"run_llm": False}
        self._running += 1
        try:
            await self._after_earlier(group, call_id)
            turn = group.turn
            refusal: dict[str, Any] | None = None
            if turn.budget.late():
                refusal = {"status": "closed", "note": ONE_LATE_NOTE, "settle": True, "silent": True}
            elif getattr(self.c, "quick_memory", None) is None:
                refusal = {"status": "failed", "note": "Memory is not available right now."}
            else:
                from yuki.one import policy

                decision = policy.gate(name, args, {}, turn)
                if not decision.allowed:
                    refusal = {"status": "refused", "note": decision.line or decision.question,
                               "settle": decision.settle, "silent": decision.silent, "line": decision.line}
            if refusal is not None:
                group.outcomes[call_id] = one_outcome(name, args, refusal)
                group.results[call_id] = refusal
                if refusal.get("settle") and not refusal.get("silent") and refusal.get("line"):
                    await self.c._speak_owed(str(refusal["line"]))
                run_llm = self._decide(group, call_id)
                final["run_llm"] = run_llm
                await params.result_callback(self.with_format(one_payload(refusal)),
                                             properties=FunctionCallResultProperties(run_llm=bool(run_llm)))
                return
            original = params.result_callback
            # (a read: review fix F4, it never takes the turn's owed lines off them: :meth:`_tried`)

            async def settle(result: Any, *, properties: Any = None) -> None:
                group.outcomes[call_id] = one_outcome(name, args, {"status": "read"})
                explicit = getattr(properties, "run_llm", None) if properties is not None else None
                decided = self._decide_inner(group, call_id, explicit)
                final["run_llm"] = explicit if decided is None else decided
                props = properties if decided is None else FunctionCallResultProperties(run_llm=decided)
                await original(self.with_format(result), properties=props)

            params.result_callback = settle
            await self.c._quick_memory_call(params)
        finally:
            self._running = max(0, self._running - 1)
            event = group.done.get(call_id)
            if event is not None:
                event.set()
            self._let_owed_go(group)
            self._settled(group, call_id, final.get("run_llm"))

    async def control(self, params: Any, handler: Callable[[Any], Any]) -> None:
        """A voice control (stay_quiet, end_conversation, go_quiet, more_of_result): the bridge's own handler, in the
        reply's order; when it is the reply's last call and an act of the reply needs the agent, the agent runs."""
        from pipecat.frames.frames import FunctionCallResultProperties

        name = str(getattr(params, "function_name", "") or "")
        call_id = str(getattr(params, "tool_call_id", "") or "")
        group = self._group(call_id, name)
        final: dict[str, Any] = {"run_llm": None}
        try:
            await self._after_earlier(group, call_id)
            if group.turn.budget.late() and name not in ONE_ENDS:
                group.outcomes[call_id] = one_outcome(name, {}, {"settle": True, "silent": True})
                final["run_llm"] = False
                await params.result_callback({"ok": True, "how_to_handle_it": ONE_LATE_NOTE},
                                             properties=FunctionCallResultProperties(run_llm=False))
                return
            unanswered = self._quiet_unanswered(group) if name == "stay_quiet" else ""
            if unanswered:
                # final-B (one-voice RC F/O3): a read's answer is owed: the closing quiet of their turn whose only
                # calls were reads, with nothing heard, is refused once (code facts, like the bridge's result_owed);
                # voice21 (final-D S10): so is a round-1 quiet on the first words of a conversation they opened
                group.outcomes[call_id] = one_outcome(name, {}, {})
                group.verdict = "model"
                final["run_llm"] = True
                self._log("quiet", refused=unanswered, round=self.rounds)
                note = OPENED_QUIET if unanswered == "opened_first" else ONE_READ_UNANSWERED
                await params.result_callback(self.with_format({"not_quiet": note}),
                                             properties=FunctionCallResultProperties(run_llm=True))
                return
            if name == "stay_quiet" and self._lone_quiet(group):
                # voice20 (final-D S22i): the rerun of an unheard close chose quiet while nothing reached them; (S5,
                # contract C9) Lane ONE holds a closing quiet once (one miss with nothing else tried, or a legal tidy
                # step untaken), with its own sentence as the refusal (review M1). Each once.
                why, note = "", ""
                if self._quiet_unheard_rerun(group):
                    why, note = "unheard_rerun", ONE_UNHEARD_QUIET
                else:
                    held = self._closing_hold()
                    if held is not None:
                        self._hold_used, self._hold_why = True, held
                        why, note = "closing_hold", _hold_text(held)
                        self._log("hold", closing="quiet", round=self.rounds, source=self._hold_src,
                                  **_code_word(held))
                if note:
                    group.outcomes[call_id] = one_outcome(name, {}, {})
                    group.verdict = "model"
                    final["run_llm"] = True
                    self._log("quiet", refused=why, round=self.rounds)
                    await params.result_callback(self.with_format({"not_quiet": note}),
                                                 properties=FunctionCallResultProperties(run_llm=True))
                    return
            original = params.result_callback

            async def settle(result: Any, *, properties: Any = None) -> None:
                group.outcomes[call_id] = one_outcome(name, {}, {})
                explicit = getattr(properties, "run_llm", None) if properties is not None else None
                decided = self._decide_inner(group, call_id, explicit)
                final["run_llm"] = explicit if decided is None else decided
                props = properties if decided is None else FunctionCallResultProperties(run_llm=decided)
                await original(self.with_format(result), properties=props)

            params.result_callback = settle
            await handler(params)
        finally:
            event = group.done.get(call_id)
            if event is not None:
                event.set()
            self._let_owed_go(group)
            self._settled(group, call_id, final.get("run_llm"))

    def _quiet_unanswered(self, group: _OneReply) -> str:
        """Why this ``stay_quiet`` is refused once (``""``: it is not), code facts only (the calls' kinds, the stream,
        the opening), and no quiet refused this turn yet:

        * ``read_unanswered`` (final-B one-voice RC F/O3): it is the whole closing reply of a result round of a turn
          their words began, every earlier call of the turn was a read (look, quick_memory, memory), nothing of Yuki's
          was heard this turn, and they are not on a call (a quiet there is the judgement that the words were for it);
        * ``opened_first`` (voice21, final-D S10, the user's decision 2): it is the whole reply of round 1 on the first
          words of a conversation they opened themselves, with no call, no game in front, no dictation, nothing said
          by this reply and nothing heard this turn (``Conversation._opened_quiet``, once a conversation). Live final-D
          p24_hinglish_garbled: unmarked scratch and stay_quiet in round 1, nothing said. A second quiet is final."""
        if group.turn is not self.turn or self._quiet_refused or not self.anchor_text:
            return ""
        if any(n != "stay_quiet" for n in group.names.values()):
            return ""
        if self.rounds < 2:
            opened = getattr(self.c, "_opened_quiet", None)
            if group.spoke or not callable(opened) or not opened(spoke=group.spoke, via="one"):
                return ""
            self._quiet_refused = True
            return "opened_first"
        names = [n for g in self._turn_groups() if g is not group for n in g.names.values()]
        if not names or any(one_kind(n) != "read" for n in names):
            return ""
        if self.heard_this_turn() or getattr(self.c, "call_app", None):
            return ""
        self._quiet_refused = True
        return "read_unanswered"

    # -- the send countdown -----------------------------------------------------------------------------------------

    def countdown(self) -> Any:
        """The conversation's one countdown host (``yuki.one.countdown.Countdown``): the island through the bridge,
        "Sending to <name>" in the one voice, memory's own name for the person; the runner runs every send through it."""
        if self._countdown is None:
            from yuki.brain import consent
            from yuki.one.countdown import Countdown

            agent = self._agent()
            seconds = getattr(agent, "send_countdown_s", None) if agent is not None else None
            self._countdown = Countdown(publish=self._publish,
                                        seconds=float(seconds) if isinstance(seconds, (int, float)) else None,
                                        say=self.speak, said_name=lambda who: consent.spoken_name(who, self._people_in),
                                        log=self._gate_log)
        return self._countdown

    def _publish(self, event: Any) -> None:
        """The countdown's ``publish`` (its own thread: the runner's worker): to the island through the bridge
        (``VoiceBridge.one_send_pending``), then, each time it counts (as it opens, and again after a pause that was
        noise), review fix H1: their words that came after the request this send is for, before it opened, end it
        (nothing sent; their words decide), and their speech going on as it opens pauses it."""
        publish = getattr(self.c.bridge, "one_send_pending", None)
        if callable(publish):
            try:
                publish(event)
            except Exception:  # noqa: BLE001 - presentation only; the countdown runs on
                pass
        if str(getattr(event, "state", "") or "") == "counting":
            try:
                self._on_counting()
            except Exception as exc:  # noqa: BLE001 - its own pause limit, Stop and their words still stop it
                self._log("countdown", error=type(exc).__name__)

    def _on_counting(self) -> None:
        countdown = self._countdown
        if countdown is None:
            return
        mark = _ONE_CALL_MARK.get()
        if mark is not None and self._heard_n > mark:
            # words of theirs came after the request this send is for, before its countdown opened: they decide
            countdown.user_speaking()
            if countdown.user_done(words=True):
                self._log("countdown", spoke=True, before=True)
            return
        if getattr(getattr(self.c, "observer", None), "user_speaking", False):
            self._pause_countdown("speaking")   # they began speaking before it opened: their words decide

    def _pause_countdown(self, why: str) -> bool:
        """Review fix H1: an open countdown still counting pauses now (their speech began before it opened, or their
        new words started a turn while it counts); their final words then end it (:meth:`user_words`), and a pause that
        brings none counts on for the time it had left (:meth:`_noise_watch`). Any thread."""
        countdown = self._countdown
        if countdown is None or not countdown.counting or not countdown.user_speaking():
            return False
        self._paused_at = time.monotonic()
        self._words_since = False
        self._log("countdown", paused=True, why=why)
        self._watch_noise()
        return True

    def _watch_noise(self) -> None:
        loop = getattr(self.c, "_loop", None)
        if loop is None or (self._noise_task is not None and not self._noise_task.done()):
            return
        if _not_on(loop):
            self._noise_task = asyncio.run_coroutine_threadsafe(self._noise_watch(), loop)
        else:
            self._noise_task = asyncio.ensure_future(self._noise_watch())

    def stop_send(self) -> bool:
        """Stop / Esc on the island (``ui.app._on_send_stop`` -> ``voice.interrupt()``): an open countdown ends
        cancelled, nothing sent. Any thread."""
        countdown = self._countdown
        return bool(countdown is not None and countdown.cancel())

    def send_choice(self, kind: str, value: str) -> bool:
        countdown = self._countdown
        return bool(countdown is not None and countdown.swap(kind, value))

    def user_started(self) -> None:
        """Flux's start of turn: an open countdown pauses at once (their words then decide, through the agent). Safety
        timing 4: their next turn began while a runner call of the current one runs, so that late act takes no tidy
        step of code's."""
        self._spoke_at = time.monotonic()
        self._words_since = False
        if self.turn is not None and self._running > 0:
            self.turn.moved_on = True
        countdown = self._countdown
        if countdown is None or not countdown.counting:
            return
        if countdown.user_speaking():
            self._paused_at = self._spoke_at
            self._log("countdown", paused=True)
        self._watch_noise()

    def user_words(self, text: str) -> None:
        """Their final words (any): an open countdown ends, nothing sent; the agent decides. Review
        fix H1: a countdown still counting (their speech began before it opened, so nothing paused it) is paused and
        ended the same way; words with no text resume a paused one (noise)."""
        words = bool(" ".join(str(text or "").split()))
        self._words_since = True
        if words:
            self._heard_n += 1
        countdown = self._countdown
        if countdown is None:
            return
        if words and countdown.counting:
            unpaused = countdown.user_speaking()
            if countdown.user_done(words=True):
                self._log("countdown", spoke=True, unpaused=unpaused)
            return
        if countdown.user_done(words=words):
            self._log("countdown", spoke=words)

    async def _noise_watch(self) -> None:
        """A pause that brought no words, with nobody speaking and no voiced audio for :data:`ONE_NOISE_S`, was noise:
        the countdown runs on for the time it had left. While they may still be speaking it stays paused (its own
        limit, ``countdown.PAUSE_MAX_S``, ends it with nothing sent)."""
        try:
            while True:
                await asyncio.sleep(0.25)
                countdown = self._countdown
                if countdown is None or not countdown.counting or self._words_since:
                    return
                now = time.monotonic()
                if now - max(self._spoke_at, self._paused_at) < ONE_NOISE_S \
                        or getattr(self.c.observer, "user_speaking", False):
                    continue
                voice = getattr(self.c, "_voice_activity", None)
                if voice is not None:
                    _since, last = voice()
                    if last and now - last < ONE_NOISE_S:
                        continue
                if countdown.user_done(words=False):
                    self._log("countdown", noise=True)
                return
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001 - the countdown's own pause limit ends it then
            return

    def close(self) -> None:
        """The conversation ends: the timer stops, an open countdown is cancelled (nothing sent)."""
        self._cancel_timer()
        self.stop_send()
        if not self.over:
            self.over = True
            self._log("turn_over", why="closed")


def _not_on(loop: Any) -> bool:
    """Whether the calling thread is not running ``loop`` (a call from another thread)."""
    try:
        return asyncio.get_running_loop() is not loop
    except RuntimeError:
        return True


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
        # xGlue (lKeys review r1 L1): the near-name doubt reads the names only (near_terms)
        self.observer.keyterms = [t for t in near_terms(settings.user_names, self.keyterms, settings.voice_keyterms_max,
                                                        name_terms())
                                  if t.casefold() != WAKE_NAME.casefold()]
        # voice20 (final-D S12): the apps running on the desk, measured by the tail shape only (heard_note ``apps``)
        self.observer.desk_apps = self._desk_app_names
        # voice21 (final-D decision 3): the user's own apps and sites (contract C11, read with the keyterms), the same
        # measure after them (heard_note ``own``), read as each heard word is measured (the read runs beside the
        # keyterms and may land later); none until lane names21's reader is merged
        self.observer.own_names = lambda: [t for t in own_names() if t.casefold() != WAKE_NAME.casefold()]
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
        #: bTalk (design 10.1): spoken requests run on the new brain (``brain = "all"``), and the talker in force
        #: (``full`` / ``lean``); fixed for the conversation with its tools and prompt.
        self.brain_voice = voice_on_brain(settings)
        self.talker = talker_kind(settings)
        #: kMind: the talker's model: the lean talker's own (``Settings.voice_lean_model``), else the mouth's.
        self.talker_model = str((getattr(settings, "voice_lean_model", "") if self.talker == "lean" else "")
                                or settings.voice_mouth_model)
        #: oVoice (phase 4): the ONE agent owns the spoken turn (``brain = "one"``): its driver (:class:`OneVoice`),
        #: else ``None`` and nothing of it runs. Its model: ``Settings.one_voice_model`` when set, else the lean
        #: talker's (Sonnet 5.5).
        self.one = voice_one(settings)
        self.one_driver: OneVoice | None = OneVoice(self) if self.one else None
        if self.one_driver is not None:
            self.talker_model = str(getattr(settings, "one_voice_model", "") or self.talker_model)
            self.observer.on_user_started = self.one_driver.user_started
            self.observer.on_user_words = self.one_driver.user_words
        else:
            # final-B (contract C4, all-messaging RC3): their speech starting pauses an open send countdown of the
            # "all" brain (the bridge's own; their words then stop or resume it), as the ONE countdown does
            self.observer.on_user_started = self._user_speech_started
        #: yConsumers: the lean talker (voice on the new brain) gets memory's one "you right now" line in its volatile
        #: block, from :class:`TalkerYou`'s background cache; the full talker is byte-for-byte as before.
        self._you_on = self.talker == "lean"
        self._you_attached = False
        self._you_stats0: dict[str, int] = {}   # TalkerYou's counters when this conversation attached (deltas logged)
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
        # -- vfix4 -------------------------------------------------------------------------------------------
        #: The last hour read again on the wake (:meth:`_refresh_recent`), until the first call has waited for it.
        self._recent_job: Any = None
        #: Closes that read once the first call has stopped waiting (a later read is dropped).
        self._recent_close: Any = None
        #: The talker's quick memory tool (:class:`yuki.voice.recall.QuickMemory`), made with the services.
        self.quick_memory: Any = None
        #: vRound2: the calls of a reply the reply-level rule left out (:func:`reply_skips`), by tool call id: each is
        #: answered :data:`SIBLING_SKIPPED` when its handler starts, and runs nothing (bounded; taken once).
        self._left_out: dict[str, str] = {}
        # -- vGame (5 Oct) -----------------------------------------------------------------------------------
        #: The full-screen game in front (its name from memory's game voice session, or ``"fullscreen"``), else
        #: ``None``; with a call (fresh evidence) this is focus mode (:attr:`focus`). Set by the session.
        self.game_app: str | None = None
        #: When the wake word heard the name during this conversation (monotonic; the spotter keeps listening while a
        #: conversation runs): a turn the name was heard in is said to Yuki (measured, never words).
        self._names: deque[float] = deque(maxlen=16)
        #: Until when the next turn counts as said to Yuki: the user said the name alone and was answered "Yeah?".
        self._name_carry_until = 0.0
        #: When the acknowledgement played on the eager end of turn of the words being finished (monotonic), else None,
        #: and the start of speech of those words: one acknowledgement per utterance (second review R2).
        self._eager_ack_at: float | None = None
        self._eager_ack_start: float | None = None
        #: Final-B (one-voice RC F): quick_memory reads in flight (a read-only turn is not answered again while one runs).
        self._reads_running = 0
        self._read_done_at = 0.0
        #: The instant acknowledgement lines in this conversation's voice (:class:`yuki.voice.fastpath.AckVoice`).
        self.ack: Any = None
        #: ``(pcm, kind)``: play a short clip of Yuki's voice now (24 kHz int16 mono) on the conversation's output,
        #: outside the pipeline (the acknowledgement); set by the session.
        self.on_sound: Callable[[bytes, str], None] | None = None
        #: ``()``: the user ended the conversation and no goodbye is said (the router's end, D1, or the talker's
        #: end_conversation with an empty say_first): the end cue now (the session's); set by the session.
        self.on_dismissed: Callable[[], None] | None = None
        #: Review F2/F5: the first words decide whether they were said to Yuki: speech recognition must have written
        #: the name opening them (a wake in a game or a call, or right after a goodbye). Set by the session at the wake.
        self.name_check = False
        #: voice21 (final-D S10): the one refusal of a quiet on the first words of a conversation the user opened
        #: themselves was used (:meth:`_opened_quiet`): a second quiet is final.
        self._opened_quiet_used = False
        #: voice22 (A9): the observer's number of the user turn whose quiet the bridge refused that way (its answer is
        #: a later turn: :meth:`_opened_answer`), ``None`` before any; (review L3) and the refused stay_quiet's tool call
        #: id, when the bridge passed it: the answer is the run its result started.
        self._opened_refused_n: int | None = None
        self._opened_refused_id: str | None = None
        #: Review F4: ``(start of speech, named, how)`` of the words being finished now, set by the fast path on each
        #: eager and final transcript (:meth:`_utterance_named`).
        self._utterance: tuple[float, bool, str] | None = None
        #: Review F1: when this conversation decided to end (monotonic): a name heard in words from before it is not a
        #: new wake (the session reads it).
        self.stopped_at: float | None = None
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
            loop.call_soon_threadsafe(self._note_name, float(at))

    def _note_name(self, at: float) -> None:
        obs = self.observer
        if obs.bot_speaking and not obs.user_speaking:
            return   # Yuki's own voice saying her name (on speakers the mic hears it), not the user calling her
        self._names.append(at)

    def _named_turn(self, text: str = "", *, final: bool = True) -> tuple[bool, str]:
        """Whether the user's words now (``text``) were said to Yuki by measured evidence, and which: the first words
        after a wake by name, the turn right after the name alone, or the wake word heard the name in it. ``final``: the
        final transcript (its turn is open and counted); else the eager one (not counted yet, and nothing is used up).

        Review F2/F5 (5 Oct): in a game or a call, and right after a goodbye, the spotter's hit alone is not enough
        (accidental wakes on sound-alikes, the name said in a story): the first words are said to Yuki when speech
        recognition also wrote the name opening them (:attr:`name_check`, the name is kept in what it hears), and in
        focus mode a name the spotter heard mid-conversation counts the same way."""
        from yuki.voice.fastpath import name_opens

        obs = self.observer
        now = time.monotonic()
        turns = obs.user_turns if final else obs.user_turns + 1
        if turns <= 1:
            if self.woken_by_name:
                return True, "wake"
            if self.name_check and name_opens(text):
                return True, "wake_confirmed"
        if now < self._name_carry_until:
            if final:
                self._name_carry_until = 0.0
            return True, "after_name"
        start = float(getattr(obs, "_user_started_at", 0.0) or 0.0)
        if any(start - self.NAME_BEFORE_S <= t <= now for t in self._names) \
                and (not self.focus or name_opens(text)):
            return True, "spotter"
        return False, ""

    def _utterance_named(self) -> bool:
        """Review F4: whether the words being finished now were said to Yuki, for a talker call or the router that
        answers them (a speculative one on Flux's eager end of turn included, when the observer's open turn is still the
        last one). Set by the fast path on each transcript of the utterance; nothing set for this utterance: no."""
        mark = self._utterance
        start = float(getattr(self.observer, "_user_started_at", 0.0) or 0.0)
        return bool(mark is not None and mark[0] == start and mark[1])

    def _fast_turn(self, text: str, *, final: bool) -> bool:
        """vGame: the fast path on a transcript before the talker (``FastPath``). ``True``: the frame is taken.

        Only the wake word's measured evidence is read here, never what the words mean. The name alone, said to
        Yuki, is answered "Yeah?" at once and the next turn counts as said to her. A turn said to her by name gets the
        instant "Mm-hm" (on its eager end of turn when that comes first, once per utterance) and goes on to the talker
        as before.
        Keyword audit A1-A4 (20 Oct, decision D1): the fast "go away" word list, its grace, its echo check and its
        held "Mm-hm" are gone; the router's model and the talker end the conversation by their judgement of intent
        (:meth:`_route_end`, ``end_conversation``)."""
        from yuki.voice.fastpath import bare_name, with_name
        from yuki.voice.keyterms import name_forms

        if self._stop.is_set():
            return self.reason == "user_asked"   # once they ended it, nothing more reaches the talker
        if self.dormant:
            return False
        # report 88: how recognition wrote the name before (memory's learned forms, "UK" for Yuki) counts as the name
        # where it opens what was said; for these checks only (the talker gets the words as heard)
        text, form = with_name(text, name_forms())
        if form is not None and final:
            self.log.event("fast_path", op="name_form", session=self.session, words=len(form.split()))
        s = self.settings
        start = float(getattr(self.observer, "_user_started_at", 0.0) or 0.0)
        named, how = self._named_turn(text, final=final)
        prev = self._utterance
        if not named and prev is not None and prev[0] == start and prev[1]:
            # R8: an earlier (eager) transcript of these same words had the name (Flux often drops it from the final)
            named, how = True, prev[2]
        # review F4: the talker call and the router answering these words read this, not the observer's open turn
        self._utterance = (start, named, how)
        if not final:
            # the acknowledgement already on the eager end of turn (~0.15 s sooner); a premature one is a backchannel
            if not named or not getattr(s, "voice_fast_ack", True) or not text.strip():
                return False
            if bare_name(text):
                return True   # the name alone is answered on its final words: no speculative talker reply to it
            if self._eager_ack_at is not None and self._eager_ack_start == start:
                return False   # review F7 / R2: once per utterance ("open... uh... Spotify": eager, resumed, eager)
            self._ack("request", how, eager=True)
            return False
        # R2/R8: the eager acknowledgement belongs to this utterance only; cleared on every final transcript
        eager_ack, ack_start = self._eager_ack_at, self._eager_ack_start
        self._eager_ack_at = self._eager_ack_start = None
        turn = self.observer.turn
        if turn is not None and turn.trigger == "user" and named:
            turn.named = True
        if not named or not getattr(s, "voice_fast_ack", True):
            return False
        if eager_ack is not None and ack_start == start and not bare_name(text):
            if turn is not None and turn.trigger == "user":
                turn.ack_at = eager_ack   # acknowledged on its eager words already
            return False
        if bare_name(text):
            self.observer.fast_handled("name")
            self._name_carry_until = time.monotonic() + self.NAME_CARRY_S
            self._ack("name", how)
            return True
        self._ack("request", how)
        return False

    def _ack(self, kind: str, how: str, *, eager: bool = False) -> None:
        """Play the instant acknowledgement (``"request"``: "Mm-hm.", ``"name"``: "Yeah?") unless Yuki is speaking.
        ``eager``: on the eager end of turn (the turn is not open yet: its time is kept for the final words)."""
        obs = self.observer
        turn = None if eager else obs.turn
        if obs.bot_speaking or obs.speech_pending:
            return   # (right after a goodbye the first words are named only when recognition heard the name open them)
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
        if eager:
            self._eager_ack_at = now
            self._eager_ack_start = float(getattr(obs, "_user_started_at", 0.0) or 0.0)
        elif turn is not None and turn.trigger == "user":
            turn.ack_at = now
        end, _how = obs._end_of_speech(now) if eager else ((turn.speech_end if turn is not None else None), "")
        self.log.event("fast_path", op="ack", session=self.session, kind=kind, by=how, played=played, eager=eager,
                       focus=self.focus, eos_to_ack_ms=round((now - end) * 1000) if end else None)

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
        if getattr(turn, "named", False) and (self.observer.user_speaking or self._user_voiced(now)):
            # said to Yuki and they are still talking: a sentence split at a pause ("Yuki, open..." "...Spotify"), not
            # talk to the others; the call idle decides as before
            self.log.event("session", state="focus_quiet_kept", session=self.session, owed=0, still_talking=True)
            return
        self.log.event("session", state="focus_quiet_end", session=self.session, game=bool(self.game_app),
                       call_mode=self._call_focus, turn=getattr(turn, "n", None))
        self._set_stop("not_for_yuki")

    def _hand_words(self) -> list[str]:
        """The hands' ``words``: the user's final utterances (the hands' checks and a skill's resolver read them)."""
        return list(self.observer.recent_words)

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
        if self.stopped_at is None:
            self.stopped_at = time.monotonic()   # review F1
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
        lean = self.talker == "lean"

        def work() -> None:
            try:
                from yuki.voice.recall import fetch_recent

                failed = object()
                text = fetch_recent(memory, failed=failed)
                ok = text is not failed   # a failed read keeps the view read at the build (review F4)
                card = None
                if lean:
                    from yuki.voice.mouth import fetch_you_card

                    card = fetch_you_card(memory)   # kMind (C1): the card as it is now; None keeps the build's
                with lock:
                    before = mouth_memory.recent
                    used = ok and gate["open"]
                    if used:
                        mouth_memory.recent = text
                    if card and gate["open"]:
                        mouth_memory.you_card = card
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
        # voice21 (final-D S10): a stay_quiet on the first words of a conversation they opened is refused once
        bridge.opened_quiet = self._opened_quiet_result
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
        bridge.turn_key = self.observer.turn_id   # phase F: a handed-over request's id in the logs names its turn
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
            # v3.18: memory keeps the hands' run traces
            hands.memory = self.memory if getattr(self.settings, "voice_hand_traces", True) else None
            hands.words = self._hand_words
            # x3 (fix-plan 3a, IF-words): the turns of the current exchange, which the hands' commit check reads as
            # this request (probed by the hands: an older Hands has no such hook)
            hands.exchange = self._exchange_words
            hands.turn_key = self.observer.turn_id
            hands.user_turn = lambda: self.observer.user_turns
            # safety timing 4: when their latest speech began, so an act's look knows their next turn began meanwhile
            hands.user_began = lambda: float(getattr(self.observer, "_user_started_at", 0.0) or 0.0)
            # Phase S: the skill path (its keys fixed with this conversation's tools), the engine's memory and settings
            hands.skill_keys = tuple(self._skill_keys())
            # review r2 L3: the volume skill runs unheld on unsure words, so then it takes no focus (the same state
            # _held reads: a Speech line for the latest words)
            hands.unsure = lambda: bool(self.observer.heard and not getattr(self.observer, "heard_plain", False))
            hands.skill_memory = self.memory
            hands.skill_settings = self.settings
            hands.last_skill = None
            hands.pending_keep = None

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
        self._drop_old_reflexes()   # D2 (20 Oct): the retired reflex book's stored row, deleted once
        if s.voice_hands and hands is not None and callable(getattr(hands, "warm_check", None)):
            hands.warm_check()   # vRound2 review r1 M1: the commit check's model client, made off the loop (no call)
        tts = getattr(self, "_tts", None)
        for service in (getattr(self, "_stt", None), tts):
            load = getattr(service, "load_now", None)   # vLocal: the models on this PC load now (their own thread)
            if callable(load):
                load()
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
        if self._you_on and not self._you_attached:
            # yConsumers: the talker's you-line cache refreshes now, in the background (a SetEvent, no pipe read here)
            self._you_attached = True
            you_cache = talker_you()
            self._you_stats0 = dict(you_cache.stats)
            you_cache.attach(self, True)
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
        """Cut Yuki's current speech (the cancel hotkey). Thread-safe. oVoice: under ``brain = "one"`` an open send
        countdown is cancelled first (the island's Stop/Esc reaches here): nothing is sent."""
        one = getattr(self, "one_driver", None)
        if one is not None:
            one.stop_send()   # the countdown logs its own end (one_countdown state=cancelled)
        loop, worker = self._loop, self.worker
        if loop is None or worker is None:
            return
        from pipecat.frames.frames import InterruptionWorkerFrame

        asyncio.run_coroutine_threadsafe(worker.queue_frame(InterruptionWorkerFrame()), loop)

    def send_choice(self, kind: str, value: str) -> bool:
        """oVoice: a chip tapped on a ONE send countdown (``who`` / ``where``): it ends with the swap and the agent
        sends to the new choice. Thread-safe; ``False`` with no ONE countdown open."""
        one = getattr(self, "one_driver", None)
        return bool(one is not None and one.send_choice(kind, value))

    # -- building ----------------------------------------------------------------

    def _local_engine(self) -> Any:
        """vLocal: this process's speech models on this PC (configured by the session off every loop; a conversation
        built without a session configures it here)."""
        from yuki.voice import local_models

        eng = local_models.engine()
        if not eng.started:
            local_models.configure_from(self.settings, log=self.log)
        return eng

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
        lean = getattr(self, "talker", "full") == "lean"
        one = getattr(self, "one_driver", None)
        if one is not None:
            # oVoice: the ONE agent's async paragraph (the lean one names hand_over, which it has not) and core
            from yuki.one.prompt import ONE_ASYNC_TOOL_INSTRUCTIONS, ONE_CORE

            one.preload()
            llm_service_module.ASYNC_TOOL_INSTRUCTIONS = ONE_ASYNC_TOOL_INSTRUCTIONS
            base_prompt = ONE_CORE
        else:
            llm_service_module.ASYNC_TOOL_INSTRUCTIONS = async_tool_instructions(lean=lean)
            base_prompt = LEAN_MOUTH_PROMPT if lean else MOUTH_PROMPT
        # Pipecat's final-result note says a result must be spoken whatever the
        # conversation moved on to. Here the narrator gate decides that, and a
        # stale result arrives marked held_back; the note defers to the payload.
        async_tool_messages._FINAL_DESCRIPTION = FINAL_DESCRIPTION
        # One list for every recogniser (yuki.voice.keyterms.vocabulary): Yuki's name, the user's names, memory's names
        # (a correction's right name first), never a heard form; Flux's keyterms and Parakeet's hotwords alike.
        # voice20 review L4 (S22j): the start-of-conversation list keeps the same length cap as the mid-call update
        # (one over-long term makes Flux reject the whole list)
        terms = keyterm_list(vocabulary(s.user_names, self.keyterms, s.voice_keyterms_max), [], s.voice_keyterms_max)
        if stt_provider(s) == "local":
            # vLocal: Parakeet on this PC, with its own end of turn (Silero VAD + Smart Turn), shaped like Flux
            from yuki.voice.local_stt import ParakeetSTTService

            stt = ParakeetSTTService(engine=self._local_engine(), log=self.log, session=self.session, hotwords=terms)
        else:
            hints = [Language(code) for code in s.voice_stt_languages] if s.voice_stt_model.endswith("multi") else None
            stt = DeepgramFluxSTTService(
                api_key=get_key(DEEPGRAM) or "",
                enable_eager_end_of_turn=s.voice_eager_eot_threshold is not None,
                settings=DeepgramFluxSTTService.Settings(
                    model=s.voice_stt_model, language_hints=hints, eot_threshold=s.voice_eot_threshold,
                    eager_eot_threshold=s.voice_eager_eot_threshold, eot_timeout_ms=s.voice_eot_timeout_ms,
                    keyterm=terms,
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
        if stt_provider(s) != "local":
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
                model=self.talker_model, max_tokens=s.voice_mouth_max_tokens, enable_prompt_caching=True,
                thinking=AnthropicLLMService.ThinkingConfig(**thinking_off(self.talker_model)),
                # The core layer: Pipecat composes its async-tool paragraphs onto it; the
                # service subclass sends the result as the first of the prompt's layers.
                system_instruction=base_prompt,
            ),
        )
        if one is not None:
            one.register(llm)   # oVoice: every ONE voice tool (the controls to the bridge, the rest to the runner)
        elif lean:
            # bTalk (design 3): the lean talker hands over with hand_over (the exact words go by code); no
            # check_progress (its model call is gone: "how's it going?" is answered from the work line)
            llm.register_function("hand_over", self._tell_or_hold, cancel_on_interruption=False,
                                  cancellable_by_llm=True, timeout_secs=None)
        else:
            llm.register_function("tell_yuki", self._tell_or_hold, cancel_on_interruption=False,
                                  cancellable_by_llm=True, timeout_secs=None)
            llm.register_function("check_progress", self.bridge.check_progress)
        if one is None:
            llm.register_function("more_of_result", self.bridge.more_of_result)
            llm.register_function("stay_quiet", self.bridge.stay_quiet)
            llm.register_function("end_conversation", self.bridge.end_conversation)
            llm.register_function("go_quiet", self.bridge.go_quiet)
        hands = getattr(self.bridge, "hands", None)
        if hands is not None:
            self.observer.hands = hands   # the hands' own callbacks are bound with the bridge (_bind_bridge)
        if one is None and s.voice_hands and hands is not None:
            # The mouth's own one-step hands (§5.6). Not cancelled by a barge-in: the action
            # may already have happened, and its report must reach the context either way.
            # x3 (fix-plan 3d): the skill hand (run_skill with a Skills menu) has no pipecat timer (as hand_over): live
            # 10 Oct 21:08:14 a 31.9 s skill run was cancelled at 30 s and its result never reached the talker; the
            # hand's own backstop (Hands.SKILL_HAND_MAX_S) ends a run that never returns, and it then falls back to the
            # thinker. The old step-list run_skill (no menu) keeps its 30 s, as before (it has no backstop of its own)
            skill_hand = bool(self._skill_keys())
            for tool in self._talker_hand_tools():
                untimed = skill_hand and tool["name"] == "run_skill"
                llm.register_function(tool["name"], self._hand_or_hold, cancel_on_interruption=False,
                                      timeout_secs=None if untimed else 30)
        if self.memory is not None:
            # vfix4: the talker's quick look into memory (its to-do list, today's talk, a search) and its to-do adds;
            # not cancelled by a barge-in (an add may already be saved, and its report must reach the context).
            from yuki.voice.recall import QuickMemory

            self.quick_memory = QuickMemory(self.memory, log=self.log, session=self.session,
                                            alone=lambda call_id: call_id in getattr(self._llm, "_yuki_solo", ()),
                                            mark_spoken=lambda call_id: getattr(self._llm, "_yuki_spoke", set()).add(
                                                str(call_id)))
            if one is None:   # oVoice: under one it is registered with the ONE tools (gated, in the reply's order)
                llm.register_function(QUICK_MEMORY["name"], self._quick_memory_call, cancel_on_interruption=False,
                                      timeout_secs=20)
        from yuki.voice.local_models import DEFAULT_VOICE as LOCAL_DEFAULT_VOICE, is_local_voice

        provider = self.tts_provider
        voice = (s.voice_tts_voice if provider == s.voice_tts_provider else "") or DEFAULT_VOICES.get(provider, "")
        if (provider == "local") != is_local_voice(voice):   # vLocal: a voice on this PC is never a cloud voice id
            voice = LOCAL_DEFAULT_VOICE if provider == "local" else DEFAULT_VOICES.get(provider, "")
        if provider == "local":
            # vLocal: Supertonic or Kokoro on this PC (Piper if it cannot load); marks stripped, clause-first
            from yuki.voice.local_tts import SherpaTTSService

            tts = _clause_tts(SherpaTTSService, strip_marks=True)(engine=self._local_engine(), voice=voice,
                                                                 log=self.log, session=self.session)
        elif provider == "gemini":
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
                                log=self.log, render=getattr(tts, "render_pcm", None))   # vLocal: rendered here
            _ack_workers().submit(self.ack.load)
        if getattr(s, "voice_speech_guard", True) and callable(getattr(tts, "set_guard", None)):
            # Every line passes one cheap check before it is heard (v3.10): only what a person says to the user.
            from yuki.voice.speechguard import SpeechGuard

            # bTalk (design 3): under brain = "all" the check makes no model call; its two code rules stay
            # oVoice: the same under brain = "one" (the claim gate stays, as built)
            code_only = ({"code_only": True} if getattr(self, "brain_voice", False) or getattr(self, "one", False)
                         else {})
            guard = SpeechGuard(self._client, s.voice_mouth_model, log=self.log, session=self.session,
                                timeout_s=float(getattr(s, "voice_speech_guard_timeout_s", 2.5)),
                                context=lambda: {"user": self.observer.last_user_text},
                                shadow=self.channel == "marker", **code_only)   # v3.16: marker: it only measures
            guard.on_drop = self.observer.guarded
            if self.talker == "lean":
                # kMind (the claim gate), phase 2: no "done" after a non-success act of this turn
                guard.claim_open = self._claim_open
                guard.on_claim = self._claim_dropped
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
        # Phase S: the hands for this conversation, fixed (the tools head the cached prefix): with a Skills menu the
        # skill hands (run_skill from its keys, keep_usual) and, with the volume skill, no mixer hand or volume keys;
        # else as before phase S (the step-list run_skill only where the old learning path can offer ways)
        one = getattr(self, "one_driver", None)
        if one is not None:
            # oVoice: the ONE agent's voice tools (yuki.one.tools.schemas("voice", keys); its ceiling at import)
            listed: Any = one.schemas()
        else:
            hand_tools = self._talker_hand_tools()
            # bTalk (design 3): the lean talker's hand_over in tell_yuki's place, and no check_progress
            head = ((HAND_OVER,) if getattr(self, "talker", "full") == "lean" else (TELL_YUKI, CHECK_PROGRESS))
            listed = (*head, MORE_OF_RESULT, STAY_QUIET, END_CONVERSATION, GO_QUIET,
                      *([QUICK_MEMORY] if self.memory is not None else []), *hand_tools)
        tools = ToolsSchema(standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["properties"],
                           required=t["required"])
            for t in listed
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
        if one is not None:
            one.hold_summaries(self._summarizer)   # oVoice: never inside a turn; at a turn's end when due (8k)
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
        if self._you_on:
            talker_you().start()   # yConsumers: its thread exists before anything is live (once per process)
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
            you_cache = None
            if self._you_attached:   # first: a raise below must not leave the refresh thread asking every 3 s
                self._you_attached = False
                you_cache = talker_you()
                you_cache.attach(self, False)
            self._scroll_hold(False)
            self._cancel_route()
            self._cancel_stops()
            if getattr(self, "one_driver", None) is not None:
                self.one_driver.close()   # oVoice: the hard stop's timer, an open countdown (nothing sent)
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
            self.log.event("narrator", op="summary", **self.gate.stats)
            if you_cache is not None:   # this conversation's counts only (the cache is the process's)
                base = self._you_stats0
                self.log.event("you_line", op="summary", session=self.session,
                               **{k: v - base.get(k, 0) for k, v in you_cache.stats.items()})
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
    #: Speech recognition's keyterms are at most this many in all (the memory names, then the screen's). xGlue (lKeys
    #: what's left 1): 80, with memory's list at ``voice_keyterms_max`` 65, so at least 15 screen names fit. Deepgram
    #: takes at most 100 terms and 500 tokens: memory's list stays under ``keyterms.MAX_TOTAL_CHARS`` (520) and each
    #: screen name under ``screennames.MAX_CHARS`` (32) of at most ``voice_screen_terms`` (30), as before.
    KEYTERMS_TOTAL_MAX = 80

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

    def _desk_app_names(self) -> list[str]:
        """voice20 (final-D S12): the apps running on the desk (:func:`desk_app_names`) from the hands' last desktop
        read (read while they talk: ``prefetch_desk``), never Yuki's own; no new read. ``[]`` with no hands or no
        read."""
        hands = getattr(getattr(self, "bridge", None), "hands", None)
        snap = getattr(hands, "last", None) if hands is not None else None
        return [n for n in desk_app_names(getattr(snap, "overview", None)) if n.casefold() != WAKE_NAME.casefold()]

    async def _apply_screen_terms(self, names: list[str]) -> None:
        """New names on the screen: the Speech line's near names and recognition's keyterms (memory names first)."""
        self.observer.screen_terms = list(names)
        s = self.settings
        memory_terms = vocabulary(s.user_names, self.keyterms, s.voice_keyterms_max)
        terms = keyterm_list(memory_terms, list(names), self.KEYTERMS_TOTAL_MAX)
        stt = getattr(self, "_stt", None)
        if stt is None or self.worker is None or terms == getattr(self, "_terms_sent", None):
            return
        if callable(getattr(stt, "set_hotwords", None)):   # vLocal: Parakeet takes them per decoded segment
            stt.set_hotwords(terms)
            self._terms_sent = terms
            self.log.event("keyterms", op="screen", terms=len(terms), screen=len(names))
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
            llm._yuki_warming = True   # oVoice: a warm-up is no ONE round (the turn's block is not touched)
            try:
                params = llm._get_llm_invocation_params(LLMContext(messages=[{"role": "user", "content": "."}],
                                                                   tools=self._tools))
            finally:
                llm._yuki_warming = False
            request = {"system": params["system"], "tools": params["tools"], "messages": params["messages"]}
        except Exception as exc:  # noqa: BLE001 - then only the connection is warmed
            self.log.event("warm", note=f"plain warm-up ({type(exc).__name__})")
        try:
            response = await self._client.messages.create(model=self.talker_model, max_tokens=1,
                                                          thinking=thinking_off(self.talker_model), **request)
            usage = getattr(response, "usage", None)
            self.log.event("warm", ms=round((time.monotonic() - started) * 1000),
                           cache_write_tokens=getattr(usage, "cache_creation_input_tokens", None),
                           cache_read_tokens=getattr(usage, "cache_read_input_tokens", None))
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            self.log.event("warm", error=f"{type(exc).__name__}: {str(exc)[:200]}")

    def _skill_keys(self) -> list[str]:
        """Phase S: this conversation's skill keys, from the Skills menu memory gave it (``MouthMemory.skills``), when
        the skill path is on (``Settings.skills``) and the talker has hands; else none, and the menu is dropped so the
        hands block is as before phase S. Fixed for the conversation (read once, with its tools)."""
        cached = getattr(self, "_skill_keys_cache", None)
        if cached is not None:
            return list(cached)
        from yuki.voice.mouth import menu_keys

        memory = getattr(self, "mouth_memory", None)
        hands = getattr(self.bridge, "hands", None)
        keys: list[str] = []
        if skills_on(self.settings) and self.settings.voice_hands and hands is not None and memory is not None:
            keys = menu_keys(getattr(memory, "skills", None))
        if not keys and memory is not None and getattr(memory, "skills", None):
            memory.skills = None
        self._skill_keys_cache = tuple(keys)
        return keys

    def _talker_hand_tools(self) -> list[dict[str, Any]]:
        """The talker's hands for this conversation (:func:`yuki.voice.hands.talker_hand_tools`), fixed once."""
        cached = getattr(self, "_hand_tools_cache", None)
        if cached is not None:
            return cached
        s = self.settings
        tools: list[dict[str, Any]] = []
        if s.voice_hands and getattr(self.bridge, "hands", None) is not None:
            tools = talker_hand_tools(self._skill_keys())
        self._hand_tools_cache = tools
        if self.log is not None:
            mouth_memory = getattr(self, "mouth_memory", None)
            # source: snapshot / memory / stale / fallback / none / off (review r2 M1: a fallback menu is logged)
            self.log.event("skill_menu", session=self.session, skills=len(self._skill_keys()),
                           chars=len(getattr(mouth_memory, "skills", None) or ""),
                           source=str(getattr(mouth_memory, "skills_source", "") or "") or None, hands=len(tools))
        return tools

    def _held(self, name: str, call_id: str | None = None, args: Any = None) -> bool:
        """Whether this call is held: recognition was unsure of the latest words and the mouth spoke with it. The sound
        hands, ``keep_usual`` and a ``run_skill`` of a sound-only skill (:meth:`_sound_skill`) are never held. oVoice
        (review fix M4): under ``brain = "one"`` the same rule, for the ONE agent's acts only (a read, a write, a
        control never waits); a held act is answered by the ONE driver itself (``OneVoice.handle``: nothing done, the
        agent's next round asks or calls it again alone), never handed to the old thinker."""
        if getattr(self, "one", False) and one_kind(str(name or "")) != "act":
            return False
        from yuki.voice.hands import AUDIO_HANDS

        decided = getattr(getattr(self, "_llm", None), "_yuki_hold", None) or {}
        if call_id is not None and str(call_id) in decided:
            return decided[str(call_id)]  # decided once, when the reply ended (its words went with that decision)
        call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
        spoke = call is not None and call.first_text is not None and call.user_turn
        return bool(spoke and self.observer.heard and not getattr(self.observer, "heard_plain", False)
                    and name not in AUDIO_HANDS and name != "keep_usual" and not self._sound_skill(name, args))

    def _sound_skill(self, name: str, args: Any) -> bool:
        """Review r1 L3: a ``run_skill`` that is a sound hand in effect, so it runs at once on unsure words as the old
        ``app_volume`` and volume keys did: the volume skill (it replaced them), or a menu skill whose every rung
        takes no focus (its data). Never on the old learned-way path (no Skills menu)."""
        if name != "run_skill" or not isinstance(args, dict):
            return False
        key = str(args.get("skill") or "")
        try:
            keys = self._skill_keys()
        except Exception:  # noqa: BLE001 - no menu known: held like any window hand
            return False
        if not key or key not in keys:
            return False
        from yuki.voice.hands import VOLUME_SKILL

        if key == VOLUME_SKILL:
            return True
        try:
            from yuki.skills.catalog import load

            skill = load().get(key)
        except Exception:  # noqa: BLE001 - unknown: held like any window hand
            return False
        routes = [r for r in (getattr(skill, "routes", ()) or ()) if r.via != "agent"]
        return bool(routes) and not any(r.needs_focus for r in routes)

    #: x3 (IF-words; the same numbers as fix-plan Item 2a's): the current exchange is at most this many of the user's
    #: turns, each heard within this long (s); the newest always counts.
    EXCHANGE_TURNS = 4
    EXCHANGE_S = 120.0

    def _exchange_words(self) -> list[str]:
        """x3 (fix-plan 3a, IF-words): the user's turns of the current exchange, oldest first, each exactly as heard: back
        to the bridge's floor (the last spoken request that finished with its outcome, or one still open: Item 2's
        ``exchange_floor``, probed), at most :data:`EXCHANGE_TURNS`, heard within :data:`EXCHANGE_S`; the newest always
        counts. The hands' commit check reads these as "this request" (live 10 Oct "Close it, dude." ... "is not
        closed" was held twice when the check read the newest turn alone), and a held call keeps them (3e). The
        heard turns' times and the job table only, never the words' meaning."""
        floor = None
        probe = getattr(self.bridge, "exchange_floor", None)
        if callable(probe):
            try:
                got = probe()
                floor = float(got) if got is not None else None
            except Exception:  # noqa: BLE001 - no floor known: the times alone cut it
                floor = None
        now = time.monotonic()
        out: list[str] = []
        for at, text in reversed(list(getattr(self.observer, "recent_heard", ()) or ())):
            if out and (len(out) >= self.EXCHANGE_TURNS or now - at > self.EXCHANGE_S
                        or (floor is not None and at <= floor)):
                break
            out.append(text)
        return list(reversed(out))

    #: x3 (3e): a call held on unsure words and called again (by the talker, or by its second stay_quiet) within this
    #: long (s) carries the user's words as they were when it was held.
    HELD_WORDS_S = 120.0

    def _resent(self, params: Any) -> Any:
        """x3 (fix-plan 3e): a held call called again carries the words snapshot taken when it was held: its context
        gets back any of the user's turns of that moment it lacks (:class:`HeldWords`), so the turn that triggered it
        is never lost (r16 20:47: "Can you WhatsApp him?" was held, and the call made again carried only the two turns
        before it). The same hand, or the talker's hand-off, takes the snapshot once; any other hand keeps its own
        words. Returns the params to act on (the same object when nothing was held)."""
        held = getattr(self, "_held_words", None)
        if not held:
            return params
        at, name, turns, held_id = held
        if time.monotonic() - at > self.HELD_WORDS_S:
            self._held_words = None
            return params
        if params.function_name != name and params.function_name not in HANDOFF_TOOLS:
            return params
        self._held_words = None
        try:
            import dataclasses

            # placed by the held call's own id (review r1 M1), so a short turn said long ago is never taken for it
            out = dataclasses.replace(params, context=HeldWords(params.context, turns, held_id))
        except Exception:  # noqa: BLE001 - a stand-in params object: its own context, as before
            return params
        self.log.event("hands" if params.function_name not in HANDOFF_TOOLS else "bridge", op=params.function_name,
                       held_words=len(turns))
        return out

    def _turn_reached(self, text: str) -> None:
        """A final turn of the user's goes on to the talker (``FastPath``: not the name alone): kept, with when it was
        heard, for the current exchange (:meth:`_exchange_words`; x3, IF-words), then offered to the voice job waiting
        for their answer (:meth:`_answer_turn`)."""
        words = " ".join(str(text or "").split())
        if not words:
            return
        self.observer.recent_heard.append((time.monotonic(), words))
        self._answer_turn(words)

    def _answer_turn(self, text: str) -> None:
        """x3 (fix-plan 3g, IF-answer): the user's turn that reaches the talker goes to the voice job waiting for their
        answer (``VoiceBridge.answer_waiting``, Item 2; probed): it claims the turn and routes it unless the talker's own
        call for that turn says it is a new task or replaces the job. Live 10 Oct "Yeah." and "Yes." after "Did you mean
        Hemanth?" never reached the job. The fast path's own turn (the name alone) never comes here."""
        answer = getattr(self.bridge, "answer_waiting", None)
        if not callable(answer) or self.dormant or not " ".join(str(text or "").split()):
            return
        try:
            used = bool(answer(str(text)))
        except Exception as exc:  # noqa: BLE001 - the talker still has the turn, as before
            self.log.event("bridge", op="answer_turn", error=type(exc).__name__)
            return
        if used:
            self.log.event("bridge", op="answer_turn", claimed=True)

    async def _hold(self, params: Any) -> None:
        self.log.event("hands" if params.function_name not in HANDOFF_TOOLS else "bridge", op=params.function_name,
                       held="asked_first")
        self.bridge.held_at = time.monotonic()
        # x3 (3e): the words snapshot of this moment, the turn that triggered it included, goes with the held call
        turns = self._exchange_words()
        held_id = str(params.tool_call_id or "") or None
        self._held_words = (time.monotonic(), params.function_name, turns, held_id) if turns else None
        context = HeldWords(params.context, turns, held_id) if turns else params.context
        self.bridge.held_call = (params.function_name, dict(params.arguments or {}), context)
        self.bridge._held_refused = False
        # The reply's words were held with it and never said (v3.6): the user heard nothing yet, so nothing
        # was promised. The mouth now asks plainly, or acts if the words were clear after all.
        await params.result_callback({"held": held_note(self.observer.heard)})

    async def _tell_or_hold(self, params: Any) -> None:
        """tell_yuki (or the lean talker's hand_over), held like a hand when the mouth asked and handed over in one reply
        on doubtful words."""
        name = params.function_name if params.function_name in HANDOFF_TOOLS else "tell_yuki"
        if await self._skip_left_out(params):
            return
        if self._held(name, params.tool_call_id):
            self.bridge.drop_early(str(params.tool_call_id))
            await self._hold(params)
            return
        await self.bridge.tell_yuki(self._resent(params))

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
        from yuki.voice.bridge import _takes, exact_words

        # Phase S (D10): the request is the user's own words since Yuki last replied
        words = ({"words": exact_words(context, skip=self.bridge.not_heard, heard=heard)}
                 if _takes(self.bridge.begin_early, "words") else {})
        if getattr(self, "brain_voice", False) and _takes(self.bridge.begin_early, "turns"):
            # bTalk: the new brain's user words are the turns since Yuki last replied, each as heard (design 3), and
            # the conversation goes along for the labelled context built from it (never quotable as their words)
            words["turns"] = heard_words(context, skip=self.bridge.not_heard, since_reply=True)
            words["convo"] = context
        try:
            started = self.bridge.begin_early(call_id, heard, screen=screen, said=spoken_words(context), **words)
        except TypeError:  # a stand-in bridge without the v3.9 arguments
            started = self.bridge.begin_early(call_id, heard)
        if started:
            self.observer.handoff_started(time.monotonic())

    # -- E3a (v3.17): the hand-off router; D1 (20 Oct): its end of the conversation by intent ----------------------

    def _route_turn(self, context: Any, call: Any) -> None:
        """The talker's call for the user's turn begins: the router decides alongside it whether the turn needs the
        thinker (``voice_router``; yuki.voice.router) and, D1 (20 Oct, keyword audit A1), whether the user is ending
        the conversation or calling off a send that counts down (``voice_router_end``, under every brain:
        :meth:`_route_end`). Not while dormant; in a game or a call only on words said to Yuki by name (review F4),
        but for the end any words while a send counts down (a wrong stop never sends). The hand-off: not on words
        recognition was unsure of (a call may be held until they answer), not while a voice task is open (a follow-up
        routes by that task's state). The end: not while anything else waits for their words or runs for them
        (:meth:`_words_awaited`: then the words are the conversation's), but a send counting down comes first, so the
        router judges a spoken stop of it even then (review L1; the end itself is still refused,
        :meth:`_route_end_block`)."""
        from yuki.voice.router import ends_on

        handoff, ends = router_on(self.settings), ends_on(self.settings)
        if not (handoff or ends) or self.dormant or not getattr(call, "user_turn", False):
            return
        named = self._utterance_named()
        awaited = self._words_awaited() if ends else ""
        if self.focus and not named:
            # vGame: in a game or a call only words said to Yuki by name go to the router (review F4); while a send of
            # theirs counts down, any words may call it off, so the router judges them for the end only
            if awaited != "countdown":
                return
            handoff = False
        self._cancel_route(keep_stop=True)   # integ23 S2: newer words never void an earlier decision's stop of a send
        pending = getattr(self.bridge, "routed_job", None)
        if callable(pending) and pending() is not None:
            # an earlier start the talker never took (this is a new run for the turn): withdrawn before a new one
            self.log.event("router", op="withdrawn", why="new_run")
            self.bridge.drop_early(getattr(self.bridge, "ROUTE_KEY", "router"))
        if (self.observer.heard and not getattr(self.observer, "heard_plain", False)) or self.bridge.has_open_voice_job():
            handoff = False
        if awaited not in ("", "countdown"):
            ends = False
        if not (handoff or ends):
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
        # sVoice r2 L2 (sWire): the router knows the talker's skills, so "play my focus playlist" stays the talker's
        menu = getattr(getattr(self, "mouth_memory", None), "skills", None) if self._skill_keys() else None
        sending = awaited == "countdown"
        request = router_request(heard[-1], history=history, desk=getattr(snap, "text", None) or None,
                                 call=self.call_app,
                                 model=str(getattr(s, "voice_router_model", "") or s.voice_mouth_model),
                                 skills=menu, named=named, sending=sending)
        turn = self.observer.user_turns
        # what this decision may still do: the talker's reply ending first voids its hand-off (:meth:`_reply_done`);
        # integ23 S2: asked while an "all" send counts down, it has a stop part newer words never void (``stop``), for
        # the sends open now only (``sends``: never one opened since); under one their final words end its countdown
        state = {"handoff": handoff, "ends": ends, "stop": False, "to": None, "sends": None,
                 "start": float(getattr(self.observer, "_user_started_at", 0.0) or 0.0)}
        counting = getattr(self.bridge, "counting_sends", None)
        if sending and callable(counting) and getattr(self, "one_driver", None) is None:
            state["sends"] = counting()
            state["stop"] = bool(state["sends"])
        self._route_state = state
        task = asyncio.ensure_future(self._route(request, heard, context, call, turn, state))
        self._router_task = task
        out = getattr(self.bridge, "router_sends", None)
        if state["stop"] and isinstance(out, dict):
            # integ23 S1: a held countdown's watch never counts on while this verdict is out (VoiceBridge.
            # _held_send_watch), bounded by decide's timeout and the end's wait; cleared when the decision is done,
            # cancelled or failed (a done callback runs for a task cancelled before it started too)
            from yuki.voice.router import DECIDE_TIMEOUT_S

            key = id(state)
            out[key] = time.monotonic() + DECIDE_TIMEOUT_S + self.ROUTE_END_WAIT_S
            task.add_done_callback(lambda _t, k=key, d=out: d.pop(k, None))

    async def _route(self, request: dict[str, Any], heard: list[str], context: Any, call: Any, turn: int,
                     state: dict[str, Any] | None = None) -> None:
        from yuki.voice.bridge import spoken_words
        from yuki.voice.router import decide

        state = state if state is not None else {"handoff": True, "ends": False, "start": 0.0}
        route = await decide(self._client, request)
        state["to"] = route.to
        call.route, call.route_ms = route.to, route.ms
        self.log.event("router", op="decided", to=route.to, screen=route.screen, ms=route.ms, error=route.error,
                       **({} if state["handoff"] else {"handoff": False}))
        if route.to == "end":
            if state["ends"] or state.get("stop"):
                await self._route_end(float(state["start"]), state)
            return
        if route.to != "thinker" or not state["handoff"]:
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
        from yuki.voice.bridge import _takes, exact_words

        words = ({"words": exact_words(context, skip=getattr(self.bridge, "not_heard", None), heard=heard)}
                 if _takes(begin, "words") else {})   # Phase S (D10): the user's own words since Yuki last replied
        try:
            started = bool(callable(begin) and begin(key, heard, screen=route.screen, said=spoken_words(context),
                                                     routed=True, **words))
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
        """The talker's reply ended: a router start it did not take (it answered, used a hand, stayed quiet) is void.
        D1: a router decision still out that may end the conversation runs on (its end reads the reply itself: words
        said or a call made keep the conversation, :meth:`_route_end_block`; a silent stay_quiet does not), but a late
        "thinker" from it starts nothing."""
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
                state = getattr(self, "_route_state", None)
                if isinstance(state, dict) and state.get("ends"):
                    state["handoff"] = False   # the talker answered first: a late "thinker" must start nothing
                    return
                task.cancel()   # the talker answered first: a late "thinker" must start nothing
                self.log.event("router", op="cancelled", why="reply_first")

    #: D1: the router's end waits at most this long for Flux's eager end of turn to be confirmed by the final words (s);
    #: a speculation withdrawn (they went on talking) cancels the decision with the talker's call (``words_withdrawn``).
    ROUTE_END_WAIT_S = 3.0

    async def _route_end(self, start: float, state: dict[str, Any] | None = None) -> None:
        """D1 (20 Oct, keyword audit A1): the router's model judged that the user's words end the conversation (they
        send Yuki away or sign off) or, while a send of theirs counts down, call it off. Once a speculative talker call
        on the same words was confirmed (bounded): a send counting down is stopped at once, nothing sent, even when the
        talker answered first (:meth:`_stop_sends`: a wrong stop never sends); then the conversation ends with no
        goodbye, the end cue only, unless :meth:`_route_end_block` says why not (then the talker's own judgement
        stands: its end_conversation ends it). ``start``: the start of speech of the words judged. ``state``: the
        decision's own (:meth:`_route_turn`). integ23 S2: a decision asked while an "all" send counted down (``stop``)
        stops that send even when they spoke again since (logged ``late``; only the sends open when it was asked,
        ``sends``; under one their final words end the countdown themselves), and when newer words voided its end
        part (:meth:`_cancel_route`), the stop is all it does. Measured states only, never the words; logged
        content-free."""
        sends = state.get("sends") if state else None
        stop_part = bool(state and state.get("stop") and sends)
        llm = getattr(self, "_llm", None)
        waited = 0.0
        while llm is not None and getattr(getattr(llm, "_speculation_gate", None), "is_speculating", False):
            if state is not None and not state.get("ends"):
                break   # newer words voided its end: these were final (a withdrawn eager turn cancels it whole)
            if waited >= self.ROUTE_END_WAIT_S:
                self.log.event("router", op="end_skipped", session=self.session, why="unconfirmed")
                return
            await asyncio.sleep(0.02)
            waited += 0.02
        obs = self.observer
        sending = self._words_awaited() == "countdown"
        same = float(getattr(obs, "_user_started_at", 0.0) or 0.0) == float(start)
        if sending and not self._stop.is_set() and (same or stop_part):
            self._stop_sends(late=not same, only=None if same else sends)
        if state is not None and not state.get("ends"):
            return   # integ23 S2: its end part was voided by newer words; the stop was its part
        why = self._route_end_block(start, sending=sending)
        if why:
            self.log.event("router", op="end_skipped", session=self.session, why=why)
            return
        turn = obs.turn
        obs.fast_handled("end")
        now = time.monotonic()
        self.log.event("router", op="end", session=self.session, call_mode=bool(self.call_app),
                       game=bool(self.game_app), sending=sending or None,
                       eos_to_end_ms=round((now - turn.speech_end) * 1000) if turn is not None and turn.speech_end
                       else None)
        self._end_cue_now()
        self.log.event("session", state="end_asked", session=self.session, waited_ms=0, by="router")
        self._set_stop("user_asked")

    def _route_end_block(self, start: float, *, sending: bool = False) -> str:
        """Why the router's end may not act now (a code word), else ``""``: the conversation is ending or dormant;
        they spoke again or are speaking (a new start of speech, their voice just now); something now waits for their
        words or runs for them (:meth:`_words_awaited`; a send counting down is ended with the conversation, never
        sent, and with ``sending``, the send just stopped, so is the ONE runner call that held it); there is no user
        turn open for the words; or the talker already answered them: Yuki is speaking or about to, its reply has
        words, or it made a call other than stay_quiet. A send still counting down hides nothing else (review L1:
        :meth:`_words_awaited` puts it first only for the router's judgement of a spoken stop): a hand at work, a
        scroll, a call held for their answer or another task still refuses the end. Measured states only; never
        raises."""
        try:
            if self._stop.is_set() or self.dormant:
                return "stopping"
            obs = self.observer
            if float(getattr(obs, "_user_started_at", 0.0) or 0.0) != float(start):
                return "spoke_again"
            if obs.user_speaking or self._user_voiced(time.monotonic()):
                return "speaking"
            awaited = self._words_awaited()
            if awaited == "countdown":
                awaited = self._words_awaited(besides_send=True)
            if awaited not in ("", "countdown") and not (sending and awaited == "running"):
                return "awaited"
            turn = obs.turn
            if turn is None or turn.trigger != "user":
                return "no_turn"
            if obs.bot_speaking or obs.speech_pending or strip_tags("".join(turn.reply)).strip():
                return "replied"
            if [t for t in turn.tools if t != "stay_quiet"]:
                return "acted"
            return ""
        except Exception:  # noqa: BLE001 - unsure: the talker's own judgement
            return "unknown"

    def _words_awaited(self, *, besides_send: bool = False) -> str:
        """D1 / keyword audit A1: what waits for the user's next words or runs for them now (a code word), else ``""``.
        ``"countdown"``: a send of theirs counting down (an "all" voice job's, ``VoiceBridge.awaiting_words``; under one
        the countdown): their words decide it (the talker cancels it, the router may stop it), and an end never lets it
        go. It comes first (review L1): the router judges a spoken stop of a send even while a hand works, a scroll
        runs or a call is held. ``besides_send``: what else waits, the countdown left out (the router's end itself is
        refused while anything does, :meth:`_route_end_block`). Anything else means their words are the
        conversation's, for the talker (or the ONE agent) to judge, never the router's end: a voice task queued,
        running or waiting for their answer ("cancel" may be that answer), a hand at work, a continuous scroll, a call
        held until they answer (their first turn after it only, :meth:`_held_unanswered`), and under one a question of
        the gate's or a runner call still running. Measured states only; unsure: ``"unknown"``. Never raises."""
        try:
            bridge = self.bridge
            one = getattr(self, "one_driver", None)
            if one is not None:
                countdown = getattr(one, "_countdown", None)
                if not besides_send and countdown is not None and countdown.counting:
                    return "countdown"
                if getattr(getattr(one, "turn", None), "asks", None):
                    return "gate_asked"
                if int(getattr(one, "_running", 0) or 0) > 0:
                    return "running"
            awaiting = getattr(bridge, "awaiting_words", None)
            jobs = str(awaiting() or "") if callable(awaiting) else ("task" if bridge.has_open_voice_job() else "")
            if jobs == "countdown" and not besides_send:
                return jobs
            if jobs and jobs != "countdown":
                return jobs
            if int(getattr(bridge, "busy_tools", 0) or 0) > 0:
                return "hand"
            if self._scrolling_line() is not None:
                return "scroll"
            if getattr(bridge, "held_call", None) is not None and self._held_unanswered(bridge):
                return "held"
            return ""
        except Exception:  # noqa: BLE001
            return "unknown"

    def _held_unanswered(self, bridge: Any) -> bool:
        """Review M1: whether the call the talker held until the user answers (``_hold``: doubtful words) still waits
        for that answer, for the words judged now. The held call is cleared only when the bridge unbinds or on a second
        stay_quiet, never when they answer, so its age alone (``voice_hold_max_s``, 600 s) kept every later turn of the
        conversation "held". It waits through their first turn after it only: no final turn of theirs reached the
        talker (``recent_heard``, :meth:`_turn_reached`) between the hold and the start of the speech judged now. That
        start, not a count of turns, so an eager (speculative) call on their next words, whose own final is not in
        yet, reads the same as its confirmed call. No time of the hold: held (unsure). Measured times only."""
        at = getattr(bridge, "held_at", None)
        if at is None:
            return True
        at = float(at)
        if time.monotonic() - at > float(getattr(self.settings, "voice_hold_max_s", 600.0)):
            return False
        start = float(getattr(self.observer, "_user_started_at", 0.0) or 0.0)
        heard = list(getattr(self.observer, "recent_heard", ()) or ())
        return not any(at < float(when) < start for when, _words in heard)

    def _stop_sends(self, late: bool = False, only: frozenset[int] | None = None) -> None:
        """D1: the router judged that the user's words call off a send counting down (or send Yuki away while one
        does): it is stopped at once, nothing sent. Under one the driver's countdown is cancelled (``OneVoice.
        stop_send``); under all the bridge stops the job whose countdown is open (``VoiceBridge.stop_sends``).
        ``late`` (integ23 S2): they spoke again before the verdict came (a stop never sends, so it still applies), and
        ``only`` the sends open when the words were judged are stopped. Logged content-free. Never raises."""
        one = getattr(self, "one_driver", None)
        try:
            if one is not None:
                stopped = int(bool(one.stop_send()))
            else:
                stop = getattr(self.bridge, "stop_sends", None)
                stopped = (int(stop(only=only) or 0) if only is not None else int(stop() or 0)) if callable(stop) else 0
        except Exception as exc:  # noqa: BLE001 - the held countdown never counts on past the end either way
            self.log.event("router", op="send_stop_error", session=self.session, error=type(exc).__name__)
            return
        self.log.event("router", op="send_stopped", session=self.session, n=stopped, **({"late": True} if late else {}))

    def _end_cue_now(self) -> None:
        """The end cue for an end with no goodbye said (``on_dismissed``: the session's soft end tone, the user's End
        tone choice). Never raises."""
        if self.on_dismissed is not None:
            try:
                self.on_dismissed()
            except Exception:  # noqa: BLE001 - the cue is a courtesy; the end is not
                pass

    def _drop_old_reflexes(self) -> None:
        """D2 (20 Oct, keyword audit A5): the reflex book is gone; its stored row is deleted once per process, off the
        loop (the ordered worker the voice's memory writes go through), idempotently (:func:`drop_old_reflexes`; a
        failure is tried again by a later conversation). Logged content-free: whether a row was there. Never raises."""
        memory = self.memory
        call = getattr(memory, "call", None) if memory is not None else None
        if _OLD_REFLEXES["done"] or not callable(call):
            return
        _OLD_REFLEXES["done"] = True
        log = self.log

        def work() -> None:
            try:
                gone = bool(call("voice reflex drop", lambda c: drop_old_reflexes(c.store), timeout_s=3.0))
            except Exception as exc:  # noqa: BLE001 - memory slow or away: a later conversation tries again
                _OLD_REFLEXES["done"] = False
                log.event("reflex", op="drop_error", error=type(exc).__name__)
                return
            log.event("reflex", op="dropped_store", removed=gone)

        try:
            from yuki.voice.workers import ordered

            ordered().submit(work)
        except Exception as exc:  # noqa: BLE001
            _OLD_REFLEXES["done"] = False
            log.event("reflex", op="drop_error", error=type(exc).__name__)

    def _cancel_route(self, keep_stop: bool = False) -> None:
        """The router's decision still out is void, and its hand-off timer. integ23 S2: ``keep_stop`` (newer words: a
        new talker call on a user turn, a new turn's interruption): a decision asked while a send counted down
        (``stop``) that has not decided otherwise keeps its stop part: its end and hand-off are void, and it runs on
        alone (kept in ``_stop_tasks``) so a late "end" still stops the send but never ends the conversation (a stop
        never sends). State only."""
        state, self._route_state = getattr(self, "_route_state", None), None
        task, self._router_task = getattr(self, "_router_task", None), None
        if task is not None and not task.done():
            if keep_stop and isinstance(state, dict) and state.get("stop") and state.get("to") in (None, "end"):
                state["ends"] = state["handoff"] = False
                kept = getattr(self, "_stop_tasks", None)
                if kept is None:
                    kept = self._stop_tasks = set()
                kept.add(task)
                task.add_done_callback(kept.discard)
                self.log.event("router", op="stop_kept", session=self.session)
            else:
                task.cancel()
        timer, self._route_timer = getattr(self, "_route_timer", None), None
        if timer is not None:
            timer.cancel()

    def _cancel_stops(self) -> None:
        """integ23 S2: the conversation is over: the decisions kept for their stop part only are cancelled too (past
        its end :meth:`_route_end` stops nothing, and a held countdown never counts on past it). State only."""
        for task in list(getattr(self, "_stop_tasks", None) or ()):
            if not task.done():
                task.cancel()

    def _note_reply(self, calls: list[tuple[str, str, Any]]) -> None:
        """vRound2: the reply-level rule (:func:`reply_skips`) over a reply of several calls, before Pipecat runs them
        in parallel: the calls it leaves out are kept here until their handlers start. Logged content-free. oVoice: not
        under ``brain = "one"`` (nothing is handed to a thinker; the reply's calls run in order)."""
        if getattr(self, "one", False):
            return
        skips = reply_skips(calls, getattr(self.bridge, "hands", None))
        if not skips:
            return
        if len(self._left_out) > 32:
            self._left_out.clear()   # ids of long-settled replies; this reply's are added just below
        self._left_out.update(skips)
        self.log.event("hands", op="reply_rule", calls=len(calls), left_out=len(skips),
                       why=next(iter(skips.values())))

    async def _skip_left_out(self, params: Any) -> bool:
        """vRound2: a call the reply-level rule left out is answered :data:`SIBLING_SKIPPED` and runs nothing (a hand-off
        among them also drops its early start); ``False`` for any other call."""
        why = self._left_out.pop(str(params.tool_call_id), None)
        if why is None:
            return False
        from pipecat.frames.frames import FunctionCallResultProperties

        handoff = params.function_name in HANDOFF_TOOLS
        if handoff:
            self.bridge.drop_early(str(params.tool_call_id))
        self.log.event("bridge" if handoff else "hands", op=params.function_name, left_out=why,
                       call=params.tool_call_id)
        await params.result_callback(dict(SIBLING_SKIPPED), properties=FunctionCallResultProperties(run_llm=False))
        return True

    async def _hand_or_hold(self, params: Any) -> None:
        """A hand, unless recognition was unsure of the user's words and the mouth spoke with the call.

        Measured 2026-09-27 (clarify replay, 4 of 6 runs): "close the cloud tabs" got "The Claude tabs, three
        and four?" and the close in the same reply, before the answer. A window hand called with words while
        the latest words were doubtful (the Speech line) is held, not run (hand-offs too): its report tells
        the mouth to wait for the answer if it asked (stay_quiet), else to call it again, so nothing is
        lost. Sound hands touch no window and run. State only (the note, the stream, which hand), never what was said.
        """
        if await self._skip_left_out(params):
            return
        if thinker_holds(params.function_name, self.bridge):
            # pVoice: a run no user turn started, while the thinker holds this turn's request: never a second hand on it
            from pipecat.frames.frames import FunctionCallResultProperties

            self.log.event("hands", op=params.function_name, thinker_holds=True, call=params.tool_call_id)
            await params.result_callback(dict(THINKER_HOLDS), properties=FunctionCallResultProperties(run_llm=False))
            return
        if self._held(params.function_name, params.tool_call_id, params.arguments):
            await self._hold(params)
            return
        params = self._resent(params)
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
        if not spoke:
            self._memory_lead(call_id)
        self._reads_running += 1
        try:
            await self.quick_memory.handle(params)
        finally:
            self._reads_running = max(0, self._reads_running - 1)
            self._read_done_at = time.monotonic()

    def _user_speech_started(self) -> None:
        """Contract C4: the VAD's start of their speech, to the bridge (``VoiceBridge.user_speech_started``: an open
        send countdown of the "all" brain pauses; with none open it does nothing). Guarded: a bridge without it (before
        hands19's change) is left as it is. Never raises."""
        started = getattr(self.bridge, "user_speech_started", None)
        if not callable(started):
            return
        try:
            started()
        except Exception as exc:  # noqa: BLE001 - the countdown's own rules still stop it
            self.log.event("bridge_error", where="user_speech_started", error=type(exc).__name__)

    def _memory_lead(self, call_id: str) -> None:
        """Final-B (all-questions RC6): a reply that calls quick_memory with nothing said first gets a short lead by
        code at once, from the cached acknowledgement voice ("Mm-hm.", :meth:`_ack`), so the lookup and the answer's
        round are not silent. Only when nothing was said or acknowledged for these words yet (one acknowledgement per
        request: the bridge's ``may_ack``) and Yuki is not speaking. Stream facts only. Never raises."""
        try:
            obs = self.observer
            turn = obs.turn
            if turn is None or turn.ack_at is not None or turn.heard or obs.bot_speaking or obs.speech_pending:
                return
            if self.one_driver is not None and self.one_driver.heard_this_turn():
                return
            may = getattr(self.bridge, "may_ack", None)
            if callable(may) and not may():
                return
            self._ack("request", "memory")
            acked = getattr(self.bridge, "acked", None)   # one acknowledgement per request: no second lead
            if callable(acked):
                acked()
            self.log.event("quick_memory", session=self.session, op="lead", call=call_id)
        except Exception as exc:  # noqa: BLE001 - the answer still comes
            self.log.event("quick_memory", session=self.session, op="lead_error", error=type(exc).__name__)

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
        if self.one_driver is not None:
            result = self.one_driver.with_format(result)   # final-B: the same last field the ONE result carries
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

        voice21 (final-D S10): except the first quiet on the first words of a conversation the user opened themselves,
        when it was a quiet marker with no stay_quiet call (a call is the bridge's to refuse): that turn is unanswered
        once more, and its rerun is told why (``mouth.OPENED_RERUN_NOTE``); a second quiet is final.
        """
        obs = self.observer
        turn = obs.turn
        if turn is None:
            return None
        if (turn.trigger != "user" or not (turn.user_text or "").strip()) and not self._opened_answer(turn):
            return None
        # final-B (one-voice RC F, compare O3a): only an acting call makes a turn answered; reads (quick_memory, and
        # under one its look and memory reads) still owe their answer aloud
        acting = self._acting_tools(turn.tools)
        if (turn.heard and not turn.claim_dropped) or acting or turn.gave_up:
            return None  # heard something, or acted, or handed over, or already given up on
        if turn.quiet and not self._opened_marker_quiet(turn):
            return None  # chose silence
        if turn.tools and (self._reads_running or (self.one_driver is not None and self.one_driver.busy())
                           or float(obs.last_response_end or 0.0) <= self._read_done_at):
            return None  # a read of it is still running, or the reply that answers it has not ended yet
        if not (turn.cut or turn.responses):
            return None  # its reply may still be on its way
        return turn

    def _opened_answer(self, turn: Any) -> bool:
        """voice22 (A9, the S10 leftover of voice21): ``turn`` is the talker's run that answers the bridge's refusal of
        the first quiet on their opening words (``mouth.OPENED_QUIET``, :meth:`_opened_quiet_result`): the result turn
        the observer opened right after that user turn, when its call answers that refusal's result (review L3: the
        refused call's id in its ``LlmCall.after_tools``; a late job's line that opened first ends its context with a
        developer message and is not it; no id or no call to read: the turn number alone, as before). Under "all" only
        (ONE's refusal is ``OneVoice.control``'s and its round 2 is its own). Such a run that gave them nothing
        audible, acted on nothing and did not choose quiet again (a second quiet is final) is unanswered once more
        (:meth:`_unanswered`): the reply watch answers it again once with ``mouth.RERUN_NOTE``, and a silent rerun gets
        the give-up tone (live final-D q_discord_today: a reply of unmarked text, none of it spoken). Turn numbers and
        call ids only, never the words."""
        n = getattr(self, "_opened_refused_n", None)
        if (n is None or getattr(self, "one_driver", None) is not None or getattr(turn, "trigger", None) != "result"
                or getattr(turn, "n", None) != n + 1):
            return False
        refused = getattr(self, "_opened_refused_id", None)
        call = getattr(turn, "llm", None)
        return not refused or call is None or refused in tuple(getattr(call, "after_tools", ()) or ())

    def _opened_marker_quiet(self, turn: Any) -> bool:
        """voice21 (final-D S10): the user turn's silence was a quiet marker with no stay_quiet call, on the first words
        of a conversation they opened themselves (:meth:`_opened_quiet`), its reply ended a moment ago
        (:data:`REPLY_WATCH_QUIET_S`: the stay_quiet call a quiet marker is written with comes just after the reply, and
        is the bridge's to refuse), and it is not a rerun already: the marker is waived (``turn.marker`` back to
        ``None``) and the turn marked ``opened_quiet``, so the reply watch answers it again once with
        ``mouth.OPENED_RERUN_NOTE``; the rerun's own quiet is final. Not under one (ONE's round 1 is
        ``OneVoice._quiet_unanswered``'s). Stream facts only."""
        if (turn.marker != "quiet" or "stay_quiet" in turn.tools or turn.rerun or not turn.responses
                or getattr(self, "one_driver", None) is not None):
            return False
        if time.monotonic() - float(self.observer.last_response_end or 0.0) < self.REPLY_WATCH_QUIET_S:
            return False
        if not self._opened_quiet(via="marker"):
            return False
        turn.marker, turn.opened_quiet = None, True
        return True

    def _acting_tools(self, tools: Any) -> list[str]:
        """The calls of a turn that act or hand over (everything but a read: ``quick_memory``, and under one the reads
        of the ONE tool table, :func:`one_kind`). Tool kinds from the tools' own specs, never words."""
        out: list[str] = []
        for name in tools or ():
            if name == QUICK_MEMORY["name"]:
                continue
            if self.one_driver is not None and one_kind(str(name)) == "read":
                continue
            out.append(name)
        return out

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
                one = self.one_driver
                if one is not None and self.worker is not None and one.rerun_due():
                    # final-B (one-voice RC A): a ONE result round's closing words had no marker line and nothing
                    # was heard: the same rerun the user's own turn gets, once, when it is quiet
                    obs = self.observer
                    if not (obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending
                            or one._running) and self._quiet_for() >= self.REPLY_WATCH_QUIET_S:
                        one.rerun_go()
                        self.log.event("reply_watch", op="rerun", one=True, round=one.rounds)
                        await self.worker.queue_frame(LLMRunFrame())
                    elif one.rerun_stale(self.RERUN_WAIT_S):
                        one.rerun_expire()
                    continue
                if one is not None and self.worker is not None and callable(getattr(one, "hold_due", None)) \
                        and one.hold_due():
                    # voice20 (final-D S5, contract C9): a ONE turn's closing words were held by Lane ONE: once they
                    # have been quiet a moment (their own answer to an offer starts a turn of its own; longer after a
                    # spoken question, review L1), the agent gets one more round with the hold's note after its words
                    obs = self.observer
                    if not (obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending
                            or one._running) and self._quiet_for() >= one.hold_quiet_s():
                        note = one.hold_go()
                        self.log.event("reply_watch", op="hold", one=True, round=one.rounds)
                        if not await self._one_note_run(note):
                            one.hold_expire()   # its words are not the context's last message: it ends as before
                    elif one.hold_stale(self.RERUN_WAIT_S):
                        one.hold_expire()
                    continue
                turn = self._unanswered()
                obs = self.observer
                if turn is None or self.worker is None:
                    continue
                if obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending:
                    continue
                now = time.monotonic()
                said_at = turn.transcript_at
                if said_at is None and self._opened_answer(turn):
                    said_at = turn.opened   # voice22 (A9): the refusal's answer run has no words of its own
                if said_at is None or now - said_at < self.REPLY_WATCH_AFTER_S:
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
                if turn.claim_dropped:
                    # kMind: answered again because a line claimed something done with nothing done (told so)
                    turn.claim_dropped, turn.claim_rerun = False, True
                obs.rerun_turn = turn
                self.log.event("reply_watch", op="rerun", turn=turn.n, cut=bool(turn.cut),
                               responses=turn.responses, after_ms=round((now - said_at) * 1000),
                               marker=turn.marker, unmarked=turn.unmarked, tts_silent=turn.tts_silent,
                               guarded=turn.guarded, opened_quiet=bool(getattr(turn, "opened_quiet", False)),
                               opened_answer=turn.trigger == "result")
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
        if self.one_driver is not None:
            self.one_driver.heard_now(now)   # final-B: "nothing heard this turn" is the stream's fact

    async def _owed_tick(self) -> None:
        """Say by code (or show with a sound) an outcome whose talker run was not heard, at a quiet moment."""
        ledger = self.ledger
        if ledger is None or not ledger.pending:
            return
        obs = self.observer
        ledger.expire(obs.user_turns)
        now = time.monotonic()
        busy = bool(obs.bot_speaking or obs.generating or obs.user_speaking or obs.speech_pending or obs.owes_reply
                    or getattr(self.bridge, "busy_tools", 0)
                    # final-B (one-voice RC B/O1b): a ONE turn still open or a runner call of it pending
                    or (self.one_driver is not None and self.one_driver.busy()))
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
        """An owed outcome said by code in the one voice: the thinker's own words, never held by the line check (but
        never with Yuki's internal words in them, report 76: the sentences that carry one are left out)."""
        from yuki.core.ownwords import strip_internal

        text = strip_internal(text)
        if not text.strip():
            return
        tts = getattr(self, "_tts", None)
        unguard = getattr(tts, "unguard", None)
        if callable(unguard):
            unguard(text)
        await self._speak(text)

    async def _speak_result(self, lines: list[str]) -> None:
        """C1 (v3.17): a result's sentences said by code in the one voice, each its own piece (so each starts the moment
        its audio is ready, and a sentence fetched ahead, C2, is found by its words); the thinker's own words, never
        held by a blocking line check (as an owed outcome's), but never with Yuki's internal words in them (report 76:
        the sentences that carry one are left out)."""
        from yuki.core.ownwords import strip_internal

        lines = [kept for kept in (strip_internal(line) for line in lines) if kept.strip()]
        tts = getattr(self, "_tts", None)
        unguard = getattr(tts, "unguard", None)
        if lines and not self.observer.bot_speaking:
            self.observer.speech_pending_since = time.monotonic()   # words on their way: Yuki is about to speak
        for line in lines:
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
    #: tTaste: the fresh uploads a run named stay in the talker's calls this long (s; ``youtube_feed.OFFER_KEEP_S``:
    #: what "the B one" is matched against).
    OFFER_KEEP_S = 900.0

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
            if state.get("hand_trouble") and isinstance(getattr(hands, "trouble_game", None), bool):
                # x4TalkerPrompt merge note (review r2 M1): the hands' own game signal keys the talker's game note
                state["hand_game"] = hands.trouble_game
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
        if turn is not None and getattr(turn, "claim_rerun", False):
            state["claim"] = True    # kMind: the rerun is told its last reply claimed something not done
        if turn is not None and getattr(turn, "opened_quiet", False):
            state["opened_quiet"] = True   # voice21 (S10): a quiet on their first words answered again (the note)
        scrolling = self._scrolling_line()
        if scrolling:
            state["scrolling"] = scrolling   # V7: a page scrolling on by itself; the talker stops it
        dictating = self._dictating_now()
        if dictating is not None:
            # N7 (report 47): another app's text field changed while they spoke this turn (typing or dictation)
            state["dictating"] = (f"{dictating.get('app')} \"{dictating.get('title')}\" (the field "
                                  f"{dictating.get('field')})")
        if self.focus:
            # vGame: a full-screen game in front or a call on: Yuki stays out of the way (mouth.FOCUS_GUIDE)
            state["focus"] = {"game": self.game_app or None, "call": self._call_focus}
        if hands is not None and getattr(hands, "skill_keys", ()):
            # Phase S: the menu's keys (the guides' volume wording) and the last skill run, its options and a question
            # waiting (mouth.volatile_block; at most 300 characters, after every cache breakpoint)
            state["skill_keys"] = tuple(hands.skill_keys)
            now_line = hands.skills_now() if callable(getattr(hands, "skills_now", None)) else None
            if now_line:
                state["skills_now"] = now_line
            # tTaste merge note 3: the options the last run offered aloud (read by the hands after its line was said,
            # never on this loop), while the offer can still be picked
            last = getattr(hands, "last_skill", None) or {}
            offers = last.get("offers") if isinstance(last, dict) else None
            if offers and time.time() - float(last.get("at") or 0.0) <= self.OFFER_KEEP_S:
                state["skills_options"] = list(offers)
        if self._you_on:
            # yConsumers: memory's one "you right now" line, rendered in RAM from the background cache (no pipe read)
            you_line = talker_you().line()
            if you_line:
                state["you"] = you_line
        if self.talker == "lean":
            # kMind (claim gate): whether a hand or skill of the talker's did something verified in this user turn
            acted = self._acted_this_turn()
            if acted is not None:
                state["acted"] = acted
        if self._opened_by_hand():
            state["opened"] = True   # fTalk: the first words of a conversation they opened themselves (mouth.OPENED_NOTE)
        return state

    def _dictating_now(self) -> dict[str, Any] | None:
        """N7 (report 47): the hands' reading of another app's text field that changed while they spoke this turn
        (typing or dictation into it), else ``None``. Measured, never the words. Never raises."""
        try:
            hands = getattr(self.bridge, "hands", None)
            dictating = getattr(hands, "dictating", None) if hands is not None else None
            started = getattr(self.observer, "_user_started_at", None)
            if isinstance(dictating, dict) and started is not None \
                    and float(dictating.get("since") or 0) >= started - 1.0:
                return dictating
        except Exception:  # noqa: BLE001
            pass
        return None

    def _opened_how(self) -> str:
        """voice21 (final-D S10): how the user opened this conversation themselves, when the call now answers its first
        words: ``"hand"`` (the hotkey, the tray, a card: :meth:`_opened_by_hand`) or ``"wake"`` (the wake word heard
        the name: ``woken_by_name``; or, where the name had to open the words, ``name_check`` with the name written
        there); ``""`` for anything else (a later turn, a call for a result, a wake right after a goodbye). The
        opening is measured, never the words. Never raises."""
        try:
            if self.dormant or self.after_goodbye:
                return ""
            call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
            if call is not None and not call.user_turn:
                return ""
            if self._call_turns() != 1:
                return ""
            if self._opened_by_hand():
                return "hand"
            if (self.woken_by_name and not self.name_check) or (self.name_check and self._utterance_named()):
                return "wake"
        except Exception:  # noqa: BLE001
            pass
        return ""

    def _opened_quiet(self, *, spoke: bool = False, via: str = "") -> bool:
        """voice21 (final-D S10, the user's decision 2 of 19 Oct): whether this quiet is refused once. It is the first
        quiet on the first words of a conversation the user opened themselves (:meth:`_opened_how`: the wake word, the
        hotkey, the tray or a card), with no call live (``call_app``), no full-screen game in front (vGame's focus mode
        ends the conversation on a quiet there), no dictation measured while they spoke (:meth:`_dictating_now`) and
        nothing of Yuki's heard this turn (its words left the speaker, or ``spoke``: the reply making the quiet put
        words out). Taken once a conversation (``True`` uses it): a second quiet is final, and "go away" still ends the
        conversation through end_conversation. ``via`` (``bridge``, ``marker``, ``one``, voice22's ``one_marker``)
        goes to the content-free log.
        Live final-D: q_discord_today (all) and p24_hinglish_garbled (one) went silent on such words. Never a turn that
        already acted or has an answer on its way (:meth:`_opened_acted`). Never raises."""
        try:
            if getattr(self, "_opened_quiet_used", False) or self.call_app or self.focus or spoke:
                return False
            how = self._opened_how()
            if not how or self._dictating_now() is not None:
                return False
            turn = self.observer.turn
            if turn is None or turn.trigger != "user" or turn.heard:
                return False
            if self._opened_acted(turn):
                return False
            one = getattr(self, "one_driver", None)
            if one is not None and one.heard_this_turn():
                return False
            self._opened_quiet_used = True
            turn.quiet_refused = True   # integration F4: their words were for Yuki (memory never files them as others')
            self.log.event("session", state="opened_quiet_refused", session=self.session, opened=how, via=via)
            return True
        except Exception:  # noqa: BLE001
            return False

    def _opened_acted(self, turn: Any) -> bool:
        """voice21 review M1: the turn's words already got an act, or one is on its way, so a quiet with it is the
        talker's "say nothing more" and never refused by :meth:`_opened_quiet` (its note says nothing answered them,
        which would ask for a second act: a playpause pressed again toggles back). Any of: an acting call of the turn
        (a hand, run_skill, a hand-off; the calls that never act, :data:`NOT_ACTING`, do not count, nor ONE's reads);
        a call that closes it (end_conversation, go_quiet); a hand acting now (``busy_tools``) or a task for these
        words whose outcome will be
        said (``VoiceBridge._answer_for_words``). Stream and job facts only, never the words. Never raises (unsure:
        acted, so the quiet stands)."""
        try:
            tools = list(getattr(turn, "tools", None) or ())
            if [t for t in self._acting_tools(tools) if t not in NOT_ACTING]:
                return True
            if "end_conversation" in tools or "go_quiet" in tools:
                return True
            bridge = getattr(self, "bridge", None)
            if bridge is not None:
                if int(getattr(bridge, "busy_tools", 0) or 0) > 0:
                    return True
                # integration F3: only a task for these words (this conversation's current turn); a task of an
                # earlier conversation still running never holds the refusal back
                on_way = getattr(bridge, "_answer_for_words", None) or getattr(bridge, "_answer_on_its_way", None)
                if callable(on_way) and on_way():
                    return True
            return False
        except Exception:  # noqa: BLE001
            return True

    def _opened_quiet_result(self, spoke: bool = False, call_id: Any = None) -> dict[str, Any] | None:
        """voice21 (final-D S10): the bridge's stay_quiet refusal (:meth:`_opened_quiet`, ``via`` bridge) as its tool
        result: ``mouth.OPENED_QUIET``, and in the marker channel the reply format last (as every ONE result carries
        it: the q_discord_today reply that went quiet had no marker line); ``None``: the quiet stands. ``call_id``: the
        refused stay_quiet's tool call id (voice22 review L3)."""
        if not self._opened_quiet(spoke=bool(spoke), via="bridge"):
            return None
        from yuki.voice.mouth import MARKER_REMINDER

        # voice22 (A9): the talker's run that answers this refusal is the result run its result starts; a silent one
        # gets the reply watch's rerun (:meth:`_opened_answer`)
        self._opened_refused_n = getattr(self.observer.turn, "n", None)
        self._opened_refused_id = str(call_id) if call_id else None
        out: dict[str, Any] = {"not_quiet": OPENED_QUIET}
        if getattr(self, "channel", "") == "marker":
            out[ONE_FORMAT_KEY] = MARKER_REMINDER
        return out

    def _opened_by_hand(self) -> bool:
        """fTalk (live test A, k_eating_video): the call answers the first words of a conversation the user opened
        themselves without the wake word (the hotkey, the tray, a card): those words are said to Yuki, as the first
        words after a wake by name are (``named``). A wake (``woken_by_name`` or ``name_check``) and a wake after a
        goodbye keep their own rules. Measured state, never the words. Never raises."""
        try:
            if self.dormant or self.woken_by_name or self.name_check or self.after_goodbye:
                return False
            call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
            if call is not None and not call.user_turn:
                return False
            return self._call_turns() <= 1
        except Exception:  # noqa: BLE001
            return False

    def _call_turns(self) -> int:
        """The user turns a talker call made now answers, its own included. Review M1 (fTalk): a speculative call on
        Flux's eager end of turn is made before the final words count their turn (``user_turns``), so an utterance
        begun after the last counted words (``_user_started_at`` past ``last_user_at``, both set by the observer) adds
        one, as ``_named_turn`` does with ``final``. Measured from the stream's own marks, never the words."""
        obs = self.observer
        begun = float(getattr(obs, "_user_started_at", 0.0) or 0.0) > float(getattr(obs, "last_user_at", 0.0) or 0.0)
        return int(obs.user_turns) + (1 if begun else 0)

    def _claim_open(self) -> bool:
        """kMind (the claim gate), phase 2 (2.4): the talker's words now can claim something done that is not: this
        reply makes no call (its own result speaks for a call), the talker called something in this user turn
        (something was asked: its own tool use says so, :meth:`Hands.called_this_turn`) and nothing it did verified
        (``Hands.acted_this_turn``). A plain reply (no call in the turn) and a turn with a verified act
        get no model call. A stream and state fact, never the words. Never raises."""
        try:
            call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
            if call is None or call.tool:
                return False
            one = getattr(self, "one_driver", None)
            if one is not None:
                return one.claim_open()   # oVoice: the ONE turn's own facts (its runner calls, never the hands')
            hands = getattr(self.bridge, "hands", None)
            called = getattr(hands, "called_this_turn", None)
            if not callable(called) or not called():
                return False
            return not hands.acted_this_turn()
        except Exception:  # noqa: BLE001
            return False

    def _claim_dropped(self) -> None:
        """kMind: a line of the reply claimed something done with nothing done; it was dropped (the turn is answered
        again once, told so: ``_reply_watch``, ``mouth.CLAIM_NOTE``)."""
        turn = self.observer.turn
        if turn is not None and turn.trigger == "user":
            turn.claim_dropped = True
            self.log.event("claim_gate", op="dropped", turn=turn.n)

    def _acted_this_turn(self) -> bool | None:
        """kMind (the claim gate, ``Hands.acted_this_turn``): ``True`` / ``False`` for a user turn, ``None`` when
        there are no hands or this call is not a user turn's. Never raises. oVoice: ``None`` under ``brain = "one"``
        (the agent reads its own results)."""
        if getattr(self, "one", False):
            return None
        call = getattr(getattr(self, "_llm", None), "_yuki_call", None)
        if call is not None and not getattr(call, "user_turn", True):
            return None
        hands = getattr(self.bridge, "hands", None)
        fn = getattr(hands, "acted_this_turn", None)
        if not callable(fn):
            return None
        try:
            return bool(fn())
        except Exception:  # noqa: BLE001
            return None

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
        if call is not None:
            try:
                # final-B (one-voice RC A): this reply's own reading, for the ONE driver's closing-reply checks
                call.one_unmarked = bool(facts.get("unmarked"))
                call.one_marker = facts.get("marker")
                call.one_repeat = bool(facts.get("repeat_dropped"))
                call.one_asks = bool(getattr(channel, "spoken_asks", False))   # voice20 review L1 (punctuation)
            except Exception:  # noqa: BLE001 - a stand-in call that takes no attribute
                pass
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
        if quick and not heard:
            self._end_cue_now()   # D1: told to go, with no goodbye: the end cue, as the router's end gives
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

    async def _one_note_run(self, note: str) -> bool:
        """voice20 (final-D S5): a ONE turn's next round, with code's note as a developer-role message (Pipecat's
        Anthropic adapter sends it as the user's turn, as :meth:`_announce`) right after the agent's own closing words,
        in the bracketed form of the turn's own notes. Only when the context's newest
        message is those words (an assistant message): never after a tool result (design 3.3.3: harness text there
        reads as injection). Append-only; the turn's frozen block still rides on their own message. ``False``: not
        queued (the caller ends the turn as it would have ended)."""
        from pipecat.frames.frames import LLMMessagesAppendFrame

        context = getattr(self, "_llm_context", None)
        try:
            messages = list(context.get_messages() or []) if context is not None else []
        except Exception:  # noqa: BLE001
            messages = []
        last = next((m for m in reversed(messages) if isinstance(m, dict) and m.get("role")), None)
        if self.worker is None or not str(note or "").strip() or last is None or last.get("role") != "assistant" \
                or last.get("tool_calls"):
            self.log.event("reply_watch", op="hold", one=True, queued=False,
                           last=str(last.get("role")) if isinstance(last, dict) else None)
            return False
        await self.worker.queue_frame(LLMMessagesAppendFrame(
            messages=[{"role": "developer", "content": str(note)}], run_llm=True))
        return True

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


def async_tool_instructions(lean: bool = False) -> str:
    """The standing async-tool paragraph composed into the mouth's prompt (replaces Pipecat's). ``lean`` (bTalk): the
    lean talker's, which names hand_over."""
    if lean:
        from yuki.voice.mouth import LEAN_ASYNC_TOOL_INSTRUCTIONS

        return LEAN_ASYNC_TOOL_INSTRUCTIONS
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
