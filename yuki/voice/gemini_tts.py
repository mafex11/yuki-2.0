"""Yuki's voice: Gemini TTS (``gemini-3.8-flash-lite-tts``, voice Leda) as a Pipecat service (docs/VOICE.md §10).

Pipecat's own ``GeminiTTSService`` needs the google-genai and google-cloud-texttospeech packages (heavy,
and neither is installed); this is the small HTTP version Yuki needs: one ``aiohttp`` session per
conversation (its connections kept open and opened before the first reply), the Gemini API's
``streamGenerateContent`` over SSE, and 24 kHz 16-bit mono PCM straight into ``TTSAudioRawFrame``.

**Pieces in parallel, played in order.** Pipecat hands a reply to the voice sentence by sentence (the
first clause early, v3.7). Gemini takes ~1.0-1.3 s to its first audio whatever the length (measured
2026-09-27, 5+ runs: cold and warm connections alike), so each piece's request starts the moment the
piece is written, alongside the ones before it, and a player per reply plays them strictly in order;
the words of a piece (``TTSTextFrame``) are released after its audio, so the conversation's record
keeps only what was heard. Every piece of a reply is sent with the same voice and the same style.

**Expression** (:mod:`yuki.voice.expressive`): the mouth may start a reply with a style mark
(``<style: calm and soft>``), sent as the part's ``speechMetadata.style`` for every piece of that
reply (found by probing 2026-09-27: accepted on ``generateContent`` parts, and effective: a whispered
line measured half the level of a plain one, a shouted one ~1.8x), and may put Gemini's own sound tags
where a sound belongs (``<sigh>``); tags Gemini does not document are cut out, never read as words.

**Silence at the seams:** Gemini pads every request with 60-250 ms before and up to 430 ms after the
words; :class:`~yuki.voice.expressive.EdgeTrimmer` keeps 30 ms and 150 ms, with 4 ms fades, so sentence
gaps sound like a person's and the first word is heard sooner.

**Fallback, per piece (v3.16).** A piece whose Gemini request fails (an HTTP error, the quota, no audio within
``first_audio_timeout_s``, a stream with no sound in it) is said by Deepgram Aura instead, with every mark
stripped; the next piece tries Gemini again. A piece with no first byte from Gemini after ``hedge_s`` is also
started on Aura: whichever plays first wins and the other is cancelled (live 2 Oct 21:24:30 a 3 s timeout made
"open chrome" heard 6.1 s after the user stopped). A small circuit breaker keeps a failing Gemini out of the way:
``breaker`` failures within its window send the pieces straight to Aura for its open time, then Gemini is tried
again (before, one timeout switched the rest of the conversation to Aura: two voices, and no second chance). A
quota or key refusal also tells :func:`gemini_status` so the next conversations start on Aura
(``pipeline.preflight``). Aura gets one retry on a fresh connection when its pooled one was dropped before any
byte came back (live 2 Oct 21:25:04 ``ServerDisconnectedError``, the reply lost), and its pool keeps idle
connections only briefly. Keys come from the environment only (``GOOGLE_API_KEY``).

**A reply that plays nothing** (every piece dropped, marks alone, or a voice that failed) ends with a breath of
silence like any other, so Pipecat raises no "completed with no audio" error (and never marks the service
unusable for it); the conversation is told how many of its pieces had words (``_yuki_on_silent``), so the pill
says something only when words were lost (v3.16).
"""

from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

from yuki.voice.expressive import EdgeTrimmer, keep_known_tags, speakable, split_style, strip_tags

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
AURA_URL = "https://api.deepgram.com/v1/speak"
SAMPLE_RATE = 24_000
#: How long after a refusal (quota, key) new conversations start on the fallback voice (s).
DOWN_FOR_S = 600.0
#: The connection is opened again before a reply when it has been unused this long (s).
WARM_IDLE_S = 45.0
#: At a wake the connections are opened unless they were used this recently (s, v3.16).
WARM_RECENT_S = 5.0
#: A player waits at most this long for the next piece of an unfinished reply before closing it (s).
PIECE_WAIT_MAX_S = 30.0
#: How many piece requests of one conversation run at once.
PARALLEL = 4
#: Aura's pooled connections are kept idle at most this long (s): below the server's own idle limit (v3.16).
AURA_KEEPALIVE_S = 15.0

#: Process-wide state of the Gemini voice: ``{"down_until": monotonic, "why": str, "check": (ok, why, at)}``.
_STATE: dict[str, Any] = {}
_STATE_LOCK = threading.Lock()
#: A can-speak check is trusted this long (s).
CHECK_TRUST_S = 600.0


class GeminiError(Exception):
    """A Gemini request that did not give audio: ``status`` (HTTP, 0 for none) and a short reason."""

    def __init__(self, status: int, reason: str) -> None:
        super().__init__(f"{status} {reason}".strip())
        self.status = status
        self.reason = reason


def mark_down(why: str, for_s: float = DOWN_FOR_S) -> None:
    """Gemini refused (quota, key): new conversations use the fallback voice for ``for_s``."""
    with _STATE_LOCK:
        _STATE["down_until"] = time.monotonic() + for_s
        _STATE["why"] = why


def forget_status() -> None:
    with _STATE_LOCK:
        _STATE.clear()


def request_body(text: str, voice: str, style: str | None) -> dict[str, Any]:
    """The ``streamGenerateContent`` body for one piece (the style only when there is one)."""
    part: dict[str, Any] = {"text": text}
    if style:
        part["speechMetadata"] = {"style": style}
    return {"contents": [{"parts": [part]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice}}}}}


def sse_audio(event: dict[str, Any]) -> list[bytes]:
    """The audio chunks in one parsed SSE ``data:`` event (base64 ``inlineData``). Raises on an error event."""
    if isinstance(event.get("error"), dict):
        err = event["error"]
        raise GeminiError(int(err.get("code") or 0), str(err.get("status") or err.get("message") or "error")[:120])
    out: list[bytes] = []
    for candidate in event.get("candidates") or []:
        for part in ((candidate.get("content") or {}).get("parts") or []):
            data = (part.get("inlineData") or {}).get("data")
            if data:
                out.append(base64.b64decode(data))
    return out


def _check_request(api_key: str, model: str, timeout_s: float) -> tuple[bool, str]:
    """One GET of the model (no audio, free): does this key reach this model?"""
    import urllib.error
    import urllib.request

    req = urllib.request.Request(f"{API_BASE}/models/{model}", headers={"x-goog-api-key": api_key})
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as response:  # noqa: S310 - fixed https URL
            return response.status == 200, "ok"
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 401, 403):
            return False, "the Google key is refused"
        if exc.code == 404:
            return False, f"the model {model} is not available to this key"
        return True, f"answered {exc.code}"  # not the key's fault: let the conversation try
    except Exception as exc:  # noqa: BLE001 - network: not cached, the conversation tries
        return True, f"unreachable ({type(exc).__name__})"


def gemini_status(api_key: str | None, model: str, *, timeout_s: float = 4.0,
                  refresh: bool = False) -> tuple[bool, str]:
    """Whether a conversation should start on the Gemini voice: ``(ok, why)``. Never raises.

    No key, a recent refusal (quota / key, :func:`mark_down`) or a refused model check says no. The
    check (one free GET) is done once per :data:`CHECK_TRUST_S`; a stale one answers from its last
    result and refreshes in the background, so a conversation's start never waits for it.
    """
    if not api_key:
        return False, "GOOGLE_API_KEY is not set"
    now = time.monotonic()
    with _STATE_LOCK:
        down, why = _STATE.get("down_until"), _STATE.get("why", "")
        check = _STATE.get("check")
    if down and now < down:
        return False, why or "refused a moment ago"
    if check and not refresh:
        ok, reason, at, key_model = check
        if key_model == (hash(api_key), model):
            if now - at > CHECK_TRUST_S:
                threading.Thread(target=lambda: gemini_status(api_key, model, refresh=True),
                                 name="yuki-voice-gemini-check", daemon=True).start()
            return ok, reason
    ok, reason = _check_request(api_key, model, timeout_s)
    if reason == "ok" or not ok:
        with _STATE_LOCK:
            _STATE["check"] = (ok, reason, time.monotonic(), (hash(api_key), model))
    return ok, reason


@dataclass
class _Piece:
    text: str
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)   # bytes chunks, then None
    pre: list[Any] = field(default_factory=list)                   # frames said before its audio
    post: list[Any] = field(default_factory=list)                  # its words, released after its audio
    task: asyncio.Task | None = None
    sent_at: float = 0.0
    first_audio_ms: float | None = None
    via: str = "gemini"
    #: The spoken-line check's verdict (yuki.voice.speechguard, v3.10), started with the piece; None: no check.
    verdict: Any = None
    created: float = field(default_factory=time.monotonic)
    #: Its key in the voice cache (yuki.voice.ttscache, v3.14) when it is short enough to keep; ``""``: not cached.
    cache_key: str = ""
    #: It starts its reply (the spoken-line check's ``first``).
    first: bool = False
    #: Dropped on purpose (the line check, or a stale "still on it"): never counted as lost words (v3.16).
    dropped: bool = False
    #: Its audio came whole from Gemini with no error on the way (only then is it kept in the voice cache).
    clean: bool = False
    #: Played from the voice fetched ahead for its words (C2, v3.17).
    prefetched: bool = False


#: C2 (v3.17): a piece fetched ahead is kept this long for its words to be said, then let go (s) ...
PREFETCH_KEEP_S = 30.0
#: ... and at most this many are kept at once (the oldest let go first).
PREFETCH_MAX = 6


@dataclass
class _Prefetch:
    """The voice of exact words fetched ahead (C2): what came so far, and whether it is complete."""

    key: tuple[str | None, str]
    chunks: list[bytes] = field(default_factory=list)
    done: bool = False
    via: str = "gemini"
    clean: bool = False
    first_audio_ms: float | None = None
    at: float = field(default_factory=time.monotonic)
    grew: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task | None = None
    used: bool = False


@dataclass
class _Reply:
    context_id: str
    style: str | None = None
    styled: bool = False                 # the reply's first piece has been seen (its style decided)
    pieces: list[_Piece] = field(default_factory=list)
    pending_pre: list[Any] = field(default_factory=list)
    closed: bool = False
    wake: asyncio.Event = field(default_factory=asyncio.Event)
    player: asyncio.Task | None = None
    tags: int = 0
    dropped: int = 0
    lead_ms: float = 0.0
    tail_ms: float = 0.0
    guarded: int = 0                     # pieces the spoken-line check dropped (v3.10)
    #: A piece of this reply was caught (not as a repeat): its later pieces are dropped unheard too (v3.13), so
    #: no reply is heard with a hole in it, and no reasoning goes on after its first sentence was stopped.
    tainted: bool = False
    heard: bool = False                  # some piece's audio went out


def service_class() -> type:
    """:class:`GeminiTTSService`, built on Pipecat's ``TTSService`` (imported late: Pipecat is heavy)."""
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE
    import aiohttp
    from pipecat.frames.frames import (
        LLMFullResponseStartFrame, TTSAudioRawFrame, TTSStoppedFrame, TTSTextFrame, UserStartedSpeakingFrame,
        UserStoppedSpeakingFrame,
    )
    from pipecat.services.settings import TTSSettings
    from pipecat.services.tts_service import TTSService

    #: A dropped pooled connection, before any byte came back: worth one retry on a fresh one (v3.16).
    retry_errors = tuple(x for x in (getattr(aiohttp, "ServerDisconnectedError", None),
                                     getattr(aiohttp, "ClientOSError", None),
                                     getattr(aiohttp, "ClientConnectionResetError", None)) if x is not None)

    class GeminiTTSService(TTSService):
        """Gemini TTS over HTTP streaming, pieces fetched in parallel and played in order (module docstring)."""

        def __init__(self, *, api_key: str, model: str, voice: str, style: str = "",
                     base_url: str = API_BASE, first_audio_timeout_s: float = 3.0,
                     fallback_key: str | None = None, fallback_voice: str = "aura-2-thalia-en",
                     fallback_url: str = AURA_URL, log: Callable[..., None] | None = None,
                     ssl: Any = None, trim: bool = True, pause_frame_processing: bool = True,
                     cache: Any = None, hedge_s: float = 0.0,
                     breaker: tuple[int, float, float] = (2, 60.0, 60.0), **kwargs: Any) -> None:
            super().__init__(push_start_frame=True, push_stop_frames=True,
                             pause_frame_processing=pause_frame_processing,
                             sample_rate=SAMPLE_RATE, append_trailing_space=False,
                             settings=TTSSettings(model=model, voice=voice, language=None), **kwargs)
            self._api_key = api_key
            self._base = base_url.rstrip("/")
            self._style = (style or "").strip() or None
            self._first_audio_s = first_audio_timeout_s
            self._fallback_key = fallback_key
            self._fallback_voice = fallback_voice
            self._fallback_url = fallback_url
            self._log_fn = log
            self._ssl = ssl
            self._trim = trim
            self._http: aiohttp.ClientSession | None = None
            self._aura_http: aiohttp.ClientSession | None = None
            self._replies: dict[str, _Reply] = {}
            #: v3.16: Aura starts on a piece whose Gemini first byte has not come this long after its request (0: off).
            self._hedge_s = max(0.0, float(hedge_s or 0.0))
            #: v3.16: the circuit breaker (failures, window s, open s): Gemini skipped until ``_skip_until``.
            self._breaker_failures, self._breaker_window_s, self._breaker_open_s = (
                max(1, int(breaker[0])), float(breaker[1]), float(breaker[2]))
            self._failures: list[float] = []
            self._skip_until = 0.0
            self._used_at = 0.0              # when a request last went out (monotonic)
            self._limit = asyncio.Semaphore(PARALLEL)
            self.stats = {"pieces": 0, "fallback_pieces": 0, "errors": 0, "guarded": 0, "hedged": 0,
                          "hedge_won_by_aura": 0, "aura_retries": 0, "breaker_opened": 0, "silent_replies": 0,
                          "prefetched": 0, "prefetch_used": 0, "prefetch_wasted": 0}
            #: C2 (v3.17): the voice of exact words fetched ahead (a result's sentences, as the thinker writes them),
            #: by (style, words): a piece with those words plays from here at once.
            self._prefetched: dict[tuple[str | None, str], _Prefetch] = {}
            #: The spoken-line check (yuki.voice.speechguard): a piece's audio is held until its verdict, which runs
            #: alongside the speech request, and a dropped piece is never played nor its words kept (v3.10). In
            #: shadow mode (v3.16) the verdict is only logged: nothing waits for it, nothing is dropped.
            self._yuki_guard: Any = None
            self._yuki_audio_gate = True
            #: ``() -> bool``: the reply about to be heard was asked for by a "still on it" line whose job has
            #: finished since (``VoiceBridge.waiting_reply_stale``, v3.14); such a reply is dropped unheard.
            self._yuki_stale: Any = None
            #: ``(speakable_pieces)``: a reply finished with nothing audible (v3.16; set by the conversation).
            self._yuki_on_silent: Callable[[int], None] | None = None
            #: Short lines she says often, replayed from disk (yuki.voice.ttscache, v3.14), or ``None``.
            self._cache = cache

        def can_generate_metrics(self) -> bool:
            return True

        # -- the connection -----------------------------------------------------------------------

        def _log(self, event: str, **facts: Any) -> None:
            if self._log_fn is not None:
                try:
                    self._log_fn(event, **facts)
                except Exception:  # noqa: BLE001 - logging never breaks the voice
                    pass

        def _connector(self, *, keepalive_s: float, limit: int, fresh: bool = False) -> aiohttp.TCPConnector:
            ssl = self._ssl if self._ssl is not None else True
            if fresh:
                return aiohttp.TCPConnector(limit=1, force_close=True, ssl=ssl)
            return aiohttp.TCPConnector(limit=limit, keepalive_timeout=keepalive_s, ttl_dns_cache=600, ssl=ssl)

        def _session(self) -> aiohttp.ClientSession:
            if self._http is None or self._http.closed:
                self._http = aiohttp.ClientSession(connector=self._connector(keepalive_s=90, limit=PARALLEL + 1),
                                                   timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10))
            return self._http

        def _aura_session(self) -> aiohttp.ClientSession:
            """Aura's own pool (v3.16): idle connections kept only briefly, never shared with Gemini's."""
            if self._aura_http is None or self._aura_http.closed:
                self._aura_http = aiohttp.ClientSession(
                    connector=self._connector(keepalive_s=AURA_KEEPALIVE_S, limit=PARALLEL),
                    timeout=aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=10))
            return self._aura_http

        def _gemini_usable(self) -> bool:
            """Whether the next piece goes to Gemini: a key, and the circuit breaker closed (or its time over)."""
            return bool(self._api_key) and time.monotonic() >= self._skip_until

        def _gemini_failed(self, why: str, *, hard: bool = False) -> None:
            """A Gemini failure for one piece: the breaker opens on a refusal, or on enough failures in its window."""
            now = time.monotonic()
            if hard:
                self._skip_until = now + DOWN_FOR_S
                self._failures.clear()
                self.stats["breaker_opened"] += 1
                self._log("tts_breaker", state="open", hard=True, for_s=round(DOWN_FOR_S), why=str(why)[:60])
                return
            self._failures = [t for t in self._failures if now - t <= self._breaker_window_s] + [now]
            if len(self._failures) >= self._breaker_failures:
                self._skip_until = now + self._breaker_open_s
                self.stats["breaker_opened"] += 1
                self._log("tts_breaker", state="open", hard=False, for_s=round(self._breaker_open_s),
                          failures=len(self._failures), why=str(why)[:60])
                self._failures.clear()

        async def _warm(self, n: int = 2) -> None:
            """Open ``n`` connections before they are needed: one free GET of the model each."""
            if not self._gemini_usable():
                return
            self._used_at = time.monotonic()

            async def one() -> None:
                try:
                    async with self._session().get(f"{self._base}/models/{self._settings.model}",
                                                   headers={"x-goog-api-key": self._api_key}) as response:
                        await response.read()
                except Exception:  # noqa: BLE001 - only an optimisation
                    pass

            await asyncio.gather(*(one() for _ in range(n)))

        def _maybe_warm(self) -> None:
            if self._gemini_usable() and time.monotonic() - self._used_at > WARM_IDLE_S:
                self._used_at = time.monotonic()
                self.create_task(self._warm(), name="gemini-warm")

        def warm_now(self) -> None:
            """At a wake (v3.16): open the connections now, unless they were used a moment ago."""
            if self._gemini_usable() and time.monotonic() - self._used_at > WARM_RECENT_S:
                self._used_at = time.monotonic()
                self.create_task(self._warm(), name="gemini-warm")

        async def start(self, frame: Any) -> None:
            await super().start(frame)
            self._maybe_warm()

        async def _close_http(self) -> None:
            for name in ("_http", "_aura_http"):
                http = getattr(self, name)
                setattr(self, name, None)
                if http is not None and not http.closed:
                    try:
                        await http.close()
                    except Exception:  # noqa: BLE001
                        pass

        async def stop(self, frame: Any) -> None:
            for reply in self._replies.values():
                reply.closed = True  # the end: every reply finishes after its last piece
                reply.wake.set()
            await super().stop(frame)
            await self._cancel_all()
            self._drop_prefetches()
            await self._close_http()

        async def cancel(self, frame: Any) -> None:
            await self._cancel_all()
            self._drop_prefetches()
            await super().cancel(frame)
            await self._close_http()

        async def cleanup(self) -> None:
            await self._cancel_all()
            self._drop_prefetches()
            await super().cleanup()
            await self._close_http()

        # -- C2: the voice of a result fetched ahead (v3.17) ---------------------------------------------------

        def piece_key(self, text: str) -> tuple[str | None, str]:
            """``(style, words)`` a frame's text is fetched as: the same steps as Pipecat's preparation and run_tts."""
            prepared = self._prepare_text_for_tts(text)
            style, rest = split_style(prepared)
            rest, _dropped = keep_known_tags(rest)
            return (style or self._style), rest.strip()

        def prefetch(self, text: str) -> bool:
            """Fetch the voice of these exact words now, to be played the moment they are said (C2, 07 §6 C).

            For a result's sentence the thinker has just written: nothing is played here, only fetched (Gemini, hedged
            with Aura as a piece is); a ``TTSSpeakFrame`` with the same words later plays from it with no request.
            Unused after :data:`PREFETCH_KEEP_S` it is let go. Returns whether a fetch started.
            """
            try:
                key = self.piece_key(text)
            except Exception:  # noqa: BLE001 - only a head start
                return False
            if not speakable(key[1]):
                return False
            now = time.monotonic()
            self._prune_prefetch(now)
            if key in self._prefetched:
                return False
            entry = _Prefetch(key=key, at=now)
            self._prefetched[key] = entry
            while len(self._prefetched) > PREFETCH_MAX:
                oldest = min(self._prefetched.values(), key=lambda e: e.at)
                self._let_go(oldest)
            try:
                entry.task = self.create_task(self._prefetch_run(entry), name="gemini-prefetch")
            except Exception:  # noqa: BLE001 - the service is not running: nothing ahead
                self._prefetched.pop(key, None)
                return False
            self.stats["prefetched"] += 1
            self._log("tts_prefetch", op="started", chars=len(key[1]))
            return True

        async def _prefetch_run(self, entry: _Prefetch) -> None:
            piece = _Piece(text=entry.key[1])
            reply = _Reply(context_id="prefetch", style=entry.key[0], styled=True)
            piece.sent_at = time.monotonic()

            async def pump() -> None:
                while True:
                    chunk = await piece.queue.get()
                    if chunk is None:
                        return
                    entry.chunks.append(chunk)
                    entry.grew.set()

            pumping = asyncio.ensure_future(pump())
            try:
                async with self._limit:
                    await self._race(reply, piece, None)
            except asyncio.CancelledError:
                pumping.cancel()
                entry.done = True
                entry.grew.set()
                raise
            except Exception as exc:  # noqa: BLE001 - then the piece is fetched again when said
                self._log("tts_error", provider=piece.via, error=type(exc).__name__, prefetch=True)
            piece.queue.put_nowait(None)
            await pumping
            entry.via, entry.clean, entry.first_audio_ms = piece.via, piece.clean, piece.first_audio_ms
            entry.done = True
            entry.grew.set()

        def _take_prefetch(self, style: str | None, text: str) -> _Prefetch | None:
            now = time.monotonic()
            self._prune_prefetch(now)
            entry = self._prefetched.pop((style, text), None)
            if entry is None:
                return None
            entry.used = True
            self.stats["prefetch_used"] += 1
            return entry

        async def _from_prefetch(self, reply: _Reply, piece: _Piece, entry: _Prefetch) -> None:
            """A piece played from the voice fetched ahead for its words (as it comes, if it is still coming)."""
            piece.sent_at = time.monotonic()
            piece.prefetched = True
            self.stats["pieces"] += 1
            index = 0
            try:
                while True:
                    entry.grew.clear()
                    while index < len(entry.chunks):
                        if piece.first_audio_ms is None:
                            piece.first_audio_ms = round((time.monotonic() - piece.sent_at) * 1000)
                        piece.queue.put_nowait(entry.chunks[index])
                        index += 1
                    if entry.done:
                        break
                    await entry.grew.wait()
                piece.via, piece.clean = entry.via, entry.clean
                self._log("tts_prefetch", op="used", chunks=index, via=entry.via,
                          ahead_ms=round((piece.sent_at - entry.at) * 1000), first_audio_ms=piece.first_audio_ms)
            except asyncio.CancelledError:
                piece.queue.put_nowait(None)
                raise
            if index == 0:
                # Fetched ahead and nothing came (it failed): the piece is fetched now, as any other.
                piece.prefetched = False
                await self._fetch(reply, piece)
                return
            piece.queue.put_nowait(None)

        def _prune_prefetch(self, now: float) -> None:
            for entry in [e for e in self._prefetched.values() if now - e.at > PREFETCH_KEEP_S]:
                self._let_go(entry)

        def _let_go(self, entry: _Prefetch) -> None:
            self._prefetched.pop(entry.key, None)
            if not entry.used:
                self.stats["prefetch_wasted"] += 1
            if entry.task is not None and not entry.task.done():
                entry.task.cancel()

        def _drop_prefetches(self) -> None:
            for entry in list(self._prefetched.values()):
                self._let_go(entry)

        async def process_frame(self, frame: Any, direction: Any) -> None:
            if isinstance(frame, (UserStartedSpeakingFrame, UserStoppedSpeakingFrame, LLMFullResponseStartFrame)):
                self._maybe_warm()  # a reply is coming: its connection is open by the time its words are
            await super().process_frame(frame, direction)

        # -- pieces ---------------------------------------------------------------------------------

        async def run_tts(self, text: str, context_id: str) -> Any:
            reply = self._replies.get(context_id)
            if reply is None:
                reply = self._replies[context_id] = _Reply(context_id=context_id)
                reply.player = self.create_task(self._play(reply), name=f"gemini-play-{context_id[:8]}")
            style, rest = split_style(text)
            if not reply.styled:
                reply.styled = True
                reply.style = style or self._style
            rest, dropped = keep_known_tags(rest)
            reply.dropped += dropped
            reply.tags += rest.count("<")
            piece = _Piece(text=rest.strip())
            piece.pre, reply.pending_pre = reply.pending_pre, []
            if speakable(piece.text):
                await self.start_tts_usage_metrics(strip_tags(piece.text))
                piece.first = not any(speakable(p.text) for p in reply.pieces)
                if self._cache is not None and self._cache.cacheable(piece.text):
                    piece.cache_key = self._cache.key("gemini", str(self._settings.model), str(self._settings.voice),
                                                      reply.style, piece.text)
                take = getattr(self, "_take_unguarded", None)
                unguarded = bool(callable(take) and take(piece.text))   # an owed outcome said by code (v3.16)
                if self._yuki_guard is not None and not unguarded:
                    # The reply's first words are checked even when short: a fragment is (v3.13). A short line the
                    # check already said SAY to as a first piece is known (v3.14, the voice cache).
                    known = bool(piece.cache_key and piece.first and self._cache.approved(piece.cache_key))
                    try:
                        try:
                            piece.verdict = self._yuki_guard.start(strip_tags(piece.text), first=piece.first,
                                                                   known_ok=known)
                        except TypeError:   # a stand-in check without the v3.14 argument
                            piece.verdict = self._yuki_guard.start(strip_tags(piece.text), first=piece.first)
                    except Exception:  # noqa: BLE001 - the check fails open
                        piece.verdict = None
                ahead = self._take_prefetch(reply.style, piece.text) if self._prefetched else None
                if ahead is not None:
                    # C2 (v3.17): these exact words were fetched while the thinker was still writing them.
                    piece.task = self.create_task(self._from_prefetch(reply, piece, ahead), name="gemini-piece")
                else:
                    piece.task = self.create_task(self._fetch(reply, piece), name="gemini-piece")
            else:
                piece.queue.put_nowait(None)  # a style mark alone: nothing to say
            reply.pieces.append(piece)
            reply.wake.set()
            yield None  # the audio comes through the reply's player, in order

        async def append_to_audio_context(self, context_id: Any, frame: Any) -> None:
            """Frames the base class adds around a piece go with that piece, so they keep its order."""
            reply = self._replies.get(context_id) if context_id else None
            if reply is not None and frame is not None and not reply.closed:
                if isinstance(frame, TTSTextFrame) and reply.pieces:
                    reply.pieces[-1].post.append(frame)
                    reply.wake.set()
                    return
                if not isinstance(frame, (TTSAudioRawFrame, TTSStoppedFrame)):
                    reply.pending_pre.append(frame)
                    return
            await super().append_to_audio_context(context_id, frame)

        async def flush_audio(self, context_id: str | None = None) -> None:
            """The reply is complete (Pipecat's end of the turn): its player closes it after the last piece."""
            reply = self._replies.get(context_id or self._turn_context_id or "")
            if reply is not None:
                reply.closed = True
                reply.wake.set()

        async def _emit(self, context_id: str, frame: Any) -> None:
            await TTSService.append_to_audio_context(self, context_id, frame)

        async def _play(self, reply: _Reply) -> None:
            """Play the reply's pieces strictly in order, each as soon as its audio arrives."""
            ctx, index = reply.context_id, 0
            try:
                while True:
                    waited = 0.0
                    while index >= len(reply.pieces):
                        if reply.closed:
                            return await self._finish(reply)
                        reply.wake.clear()
                        try:
                            await asyncio.wait_for(reply.wake.wait(), timeout=1.0)
                        except TimeoutError:
                            waited += 1.0
                            self._refresh_audio_context(ctx)  # still in use: don't let the context time out
                            if waited >= PIECE_WAIT_MAX_S:
                                reply.closed = True
                    piece = reply.pieces[index]
                    index += 1
                    for frame in piece.pre:
                        await self._emit(ctx, frame)
                    checked = piece.verdict is None or self._yuki_guard is None
                    dropped = False
                    while True:
                        try:
                            chunk = await asyncio.wait_for(piece.queue.get(), timeout=1.0)
                        except TimeoutError:
                            self._refresh_audio_context(ctx)
                            continue
                        if chunk is None:
                            break
                        if not reply.heard and not dropped and callable(self._yuki_stale):
                            try:
                                try:
                                    stale = bool(self._yuki_stale(strip_tags(piece.text)))
                                except TypeError:   # a check that takes no words
                                    stale = bool(self._yuki_stale())
                            except Exception:  # noqa: BLE001 - plumbing: never in the way of a line
                                stale = False
                            if stale:
                                # Its outcome is known now: "still on it" would be stale, and the outcome follows.
                                checked, dropped, reply.tainted = True, True, True
                                piece.dropped = True
                                self.stats["stale"] = self.stats.get("stale", 0) + 1
                                self._log("tts_stale_dropped", chars=len(piece.text))
                                if piece.task is not None and not piece.task.done():
                                    piece.task.cancel()
                                continue
                        if not checked:
                            # Its audio is ready: the line is heard only if the check keeps it (it ran alongside).
                            checked = True
                            guard = self._yuki_guard
                            blocks = getattr(guard, "blocks", None)   # Yuki's internal words: dropped in shadow too
                            if getattr(guard, "shadow", False) and not (callable(blocks) and blocks(piece.verdict)):
                                # v3.16, the marker channel: the verdict is logged when it comes; nothing waits.
                                guard.observe(piece.verdict, strip_tags(piece.text))
                            else:
                                if reply.tainted and callable(getattr(guard, "drop_rest", None)):
                                    verdict = guard.drop_rest(strip_tags(piece.text))
                                else:
                                    wait = max(0.2, guard.timeout_s - (time.monotonic() - piece.created))
                                    verdict = await guard.decide(piece.verdict, strip_tags(piece.text), wait_s=wait)
                                self._refresh_audio_context(ctx)
                                if verdict.ok and verdict.by == "model" and piece.first and piece.cache_key:
                                    self._cache.approve(piece.cache_key)   # its own SAY, remembered with its audio
                                if not verdict.ok:
                                    reply.tainted = reply.tainted or verdict.by != "repeat"
                                    dropped = True
                                    piece.dropped = True
                                    reply.guarded += 1
                                    self.stats["guarded"] += 1
                                    if piece.task is not None and not piece.task.done():
                                        piece.task.cancel()
                        if not dropped:
                            reply.heard = True
                            await self._emit(ctx, TTSAudioRawFrame(chunk, SAMPLE_RATE, 1, context_id=ctx))
                    if dropped:
                        continue  # never heard: its words stay out of the conversation too
                    # Its words may be added just after its request went out: give them a moment.
                    if not piece.post and not reply.closed and index == len(reply.pieces):
                        reply.wake.clear()
                        try:
                            await asyncio.wait_for(reply.wake.wait(), timeout=0.05)
                        except TimeoutError:
                            pass
                    for frame in piece.post:
                        await self._emit(ctx, frame)
            except asyncio.CancelledError:
                raise

        async def _finish(self, reply: _Reply) -> None:
            ctx = reply.context_id
            lost = 0
            if not reply.heard:
                # Nothing of it played (every piece dropped, marks alone, or the voice failed): a breath of silence
                # closes the reply like any other, so there is no "completed with no audio" error (v3.16; before, a
                # marks-only reply raised it, and the pill showed Pipecat's text in red). The conversation is told
                # how many pieces had words that were lost (not dropped on purpose).
                await self._emit(ctx, TTSAudioRawFrame(bytes(SAMPLE_RATE // 50 * 2), SAMPLE_RATE, 1, context_id=ctx))
                lost = sum(1 for p in reply.pieces if speakable(p.text) and not p.dropped)
                self.stats["silent_replies"] += 1
            for frame in reply.pending_pre:
                await self._emit(ctx, frame)
            await self._emit(ctx, TTSStoppedFrame(context_id=ctx))
            await self.remove_audio_context(ctx)
            self._replies.pop(ctx, None)
            pieces = reply.pieces
            self._log("tts_reply", provider="gemini", pieces=len(pieces), style=bool(reply.style),
                      tags=reply.tags, dropped_tags=reply.dropped,
                      fallback_pieces=sum(1 for p in pieces if p.via not in ("gemini", "cache")),
                      cached_pieces=sum(1 for p in pieces if p.via == "cache"),
                      prefetched_pieces=sum(1 for p in pieces if p.prefetched),
                      first_audio_ms=[p.first_audio_ms for p in pieces if p.first_audio_ms is not None],
                      trimmed_lead_ms=round(reply.lead_ms), trimmed_tail_ms=round(reply.tail_ms),
                      heard=reply.heard, **({"lost_pieces": lost} if not reply.heard else {}))
            if not reply.heard and callable(self._yuki_on_silent):
                try:
                    self._yuki_on_silent(lost)
                except Exception:  # noqa: BLE001 - bookkeeping never breaks the voice
                    pass

        async def _fetch(self, reply: _Reply, piece: _Piece) -> None:
            """One piece's audio into its queue: Gemini, Aura, or both raced (v3.16). Always ends with ``None``."""
            piece.sent_at = time.monotonic()
            self.stats["pieces"] += 1
            kept: list[bytes] | None = [] if piece.cache_key else None
            if piece.cache_key:
                cached = await asyncio.to_thread(self._cache.get, piece.cache_key)
                if cached:
                    # Her own voice for these exact words, made before (v3.14): no request, first audio at once.
                    piece.via = "cache"
                    piece.first_audio_ms = round((time.monotonic() - piece.sent_at) * 1000)
                    self.stats["cached_pieces"] = self.stats.get("cached_pieces", 0) + 1
                    for at in range(0, len(cached), 4800):
                        piece.queue.put_nowait(cached[at:at + 4800])
                    piece.queue.put_nowait(None)
                    return
            try:
                async with self._limit:
                    await self._race(reply, piece, kept)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - nothing more to try for this piece
                self._log("tts_error", provider=piece.via, error=type(exc).__name__)
            finally:
                piece.queue.put_nowait(None)
            if piece.clean and piece.via == "gemini" and kept and piece.cache_key:
                # Fetched whole, from the voice itself: kept for the next time these exact words are said.
                await asyncio.to_thread(self._cache.put, piece.cache_key, b"".join(kept))

        async def _race(self, reply: _Reply, piece: _Piece, kept: list[bytes] | None) -> None:
            """Fetch one piece from Gemini (hedged with Aura after ``hedge_s``) or Aura alone; play the first to sound.

            Each source has its own edge trimmer; the first whose audio is audible after trimming wins, the other
            is cancelled, and only the winner's audio reaches the piece. A source that ends with no sound, or fails
            before any, lets the other say it (Aura is started then if it was not yet). A winner that breaks after
            its first audio ends the piece: part of it was said, so it is never said twice.
            """
            plain = strip_tags(piece.text)
            use_gemini = self._gemini_usable()
            can_aura = bool(self._fallback_key) and bool(plain.strip())
            if not use_gemini and not can_aura:
                return
            queue: asyncio.Queue = asyncio.Queue()
            tasks: dict[str, asyncio.Task] = {}
            trimmers = {"gemini": EdgeTrimmer(SAMPLE_RATE) if self._trim else None,
                        "aura": EdgeTrimmer(SAMPLE_RATE) if self._trim else None}
            done: set[str] = set()
            winner: str | None = None
            errors = 0

            async def pump(name: str) -> None:
                try:
                    source = self._gemini(piece.text, reply.style) if name == "gemini" else self._aura(plain)
                    async for chunk in source:
                        queue.put_nowait((name, chunk))
                    queue.put_nowait((name, None))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - handed to the race
                    queue.put_nowait((name, exc))

            def start(name: str) -> None:
                if name not in tasks:
                    tasks[name] = asyncio.ensure_future(pump(name))

            def emit(name: str, data: bytes) -> None:
                trimmer = trimmers[name]
                if trimmer is not None:
                    data = trimmer.feed(data)
                if data:
                    if piece.first_audio_ms is None:
                        piece.first_audio_ms = round((time.monotonic() - piece.sent_at) * 1000)
                    piece.queue.put_nowait(data)
                    if kept is not None:
                        kept.append(data)

            start("gemini" if use_gemini else "aura")
            hedge_at = piece.sent_at + self._hedge_s if (use_gemini and can_aura and self._hedge_s > 0) else None
            try:
                while True:
                    timeout = None
                    if winner is None and hedge_at is not None and "aura" not in tasks:
                        timeout = max(0.0, hedge_at - time.monotonic())
                    try:
                        name, item = await asyncio.wait_for(queue.get(), timeout=timeout)
                    except TimeoutError:
                        # No first byte from Gemini in time: Aura starts on the same piece; first to sound wins.
                        self.stats["hedged"] += 1
                        self._log("tts_hedge", op="started", after_ms=round((time.monotonic() - piece.sent_at) * 1000))
                        start("aura")
                        continue
                    if winner is not None and name != winner:
                        continue   # the loser's leftovers
                    if isinstance(item, BaseException) or item is None:
                        done.add(name)
                        if isinstance(item, BaseException):
                            errors += 1
                        if name == "gemini" and winner != "gemini":
                            if isinstance(item, BaseException):
                                self.stats["errors"] += 1
                                why = item.reason if isinstance(item, GeminiError) else type(item).__name__
                                status = item.status if isinstance(item, GeminiError) else 0
                                self._log("tts_fallback", provider="gemini", status=status, reason=str(why)[:80],
                                          had_audio=False)
                                hard = status in (401, 403, 429) or "RESOURCE_EXHAUSTED" in str(why)
                                if hard:
                                    mark_down(f"Gemini refused ({status} {why})")
                                self._gemini_failed(str(why), hard=hard)
                            else:
                                # The stream ended without a sound in it (live 30 Sept 21:32:52: "completed with no
                                # audio", the reply lost): Aura says this piece; the next one tries Gemini again.
                                self._log("tts_fallback", provider="gemini", status=200, reason="silent",
                                          had_audio=False)
                                self._gemini_failed("silent")
                        elif name == "aura" and isinstance(item, BaseException):
                            self._log("tts_error", provider="aura", error=type(item).__name__)
                        if winner == name:
                            piece.clean = item is None and errors == 0
                            return   # the winner ended (or broke after its first audio: never said twice)
                        if winner is None and can_aura and "aura" not in tasks:
                            start("aura")   # nothing from Gemini: Aura says it now
                            continue
                        if all(n in done for n in tasks):
                            return
                        continue
                    if winner is None:
                        trimmer = trimmers[name]
                        audible = trimmer.feed(item) if trimmer is not None else item
                        if not audible:
                            continue   # silence so far (Gemini's lead padding, or a silent stream)
                        winner = name
                        piece.via = name
                        raced = len(tasks) > 1 and not (set(tasks) - {name}) <= done   # the other was still running
                        for other, task in tasks.items():
                            if other != name and not task.done():
                                task.cancel()
                        if raced:
                            self._log("tts_hedge", op="winner", winner=name)
                            if name == "aura":
                                self.stats["hedge_won_by_aura"] += 1
                                self._gemini_failed("slow")   # a Gemini slower than the hedge counts against it
                        if name == "aura":
                            self.stats["fallback_pieces"] += 1
                        else:
                            self._failures.clear()   # it answered in time
                        if piece.first_audio_ms is None:
                            piece.first_audio_ms = round((time.monotonic() - piece.sent_at) * 1000)
                        piece.queue.put_nowait(audible)
                        if kept is not None:
                            kept.append(audible)
                        continue
                    emit(name, item)
            finally:
                for task in tasks.values():
                    if not task.done():
                        task.cancel()
                trimmer = trimmers.get(winner or "")
                if trimmer is not None:
                    tail = trimmer.finish()
                    if tail:
                        piece.queue.put_nowait(tail)
                        if kept is not None:
                            kept.append(tail)
                    reply.lead_ms += trimmer.trimmed_lead / SAMPLE_RATE * 1000
                    reply.tail_ms += trimmer.trimmed_tail / SAMPLE_RATE * 1000

        async def _gemini(self, text: str, style: str | None) -> AsyncIterator[bytes]:
            """Stream one piece from Gemini: its PCM chunks, as they come."""
            self._used_at = time.monotonic()
            url = f"{self._base}/models/{self._settings.model}:streamGenerateContent?alt=sse"
            body = request_body(text, str(self._settings.voice), style)
            async with asyncio.timeout(self._first_audio_s) as deadline:
                async with self._session().post(url, json=body, headers={"x-goog-api-key": self._api_key}) as resp:
                    if resp.status != 200:
                        detail = ""
                        try:
                            err = json.loads((await resp.read())[:2000] or b"{}").get("error") or {}
                            detail = str(err.get("status") or "")
                        except Exception:  # noqa: BLE001
                            pass
                        raise GeminiError(resp.status, detail or resp.reason or "")
                    first = True
                    async for raw in resp.content:
                        line = raw.strip()
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            event = json.loads(line[5:])
                        except ValueError:
                            continue
                        for chunk in sse_audio(event):
                            if first:
                                first = False
                                deadline.reschedule(None)  # audio flows: no deadline for the rest
                            yield chunk
                    if first:
                        raise GeminiError(200, "no audio")

        async def _aura(self, text: str) -> AsyncIterator[bytes]:
            """Stream one piece from Deepgram Aura (REST, the same 24 kHz PCM).

            v3.16: a pooled connection the server had already closed fails before any byte; that one try is
            repeated once on a fresh connection (closed after it), so the piece is still said.
            """
            params = {"model": self._fallback_voice, "encoding": "linear16", "sample_rate": str(SAMPLE_RATE),
                      "container": "none"}
            for attempt in (1, 2):
                fresh = None
                if attempt == 2:
                    fresh = aiohttp.ClientSession(connector=self._connector(keepalive_s=0, limit=1, fresh=True),
                                                  timeout=aiohttp.ClientTimeout(total=None, sock_connect=5,
                                                                                sock_read=10))
                session = fresh or self._aura_session()
                yielded = False
                try:
                    async with asyncio.timeout(10.0) as deadline:
                        async with session.post(self._fallback_url, params=params, json={"text": text},
                                                headers={"Authorization": f"Token {self._fallback_key}"}) as resp:
                            if resp.status != 200:
                                raise GeminiError(resp.status, "aura")
                            deadline.reschedule(None)
                            async for chunk in resp.content.iter_chunked(4800):
                                yielded = True
                                yield chunk
                    return
                except retry_errors as exc:
                    if yielded or attempt == 2:
                        raise
                    self.stats["aura_retries"] += 1
                    self._log("tts_retry", provider="aura", error=type(exc).__name__)
                finally:
                    if fresh is not None:
                        try:
                            await fresh.close()
                        except Exception:  # noqa: BLE001
                            pass

        # -- interruptions ----------------------------------------------------------------------------

        async def _cancel_reply(self, reply: _Reply) -> None:
            tasks = [p.task for p in reply.pieces if p.task is not None and not p.task.done()]
            if reply.player is not None and not reply.player.done():
                tasks.append(reply.player)
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=0.5)
                except BaseException:  # noqa: BLE001 - cancelled: that is the point
                    pass

        async def _cancel_all(self) -> None:
            replies, self._replies = list(self._replies.values()), {}
            for reply in replies:
                await self._cancel_reply(reply)

        async def _handle_interruption(self, frame: Any, direction: Any) -> None:
            await self._cancel_all()  # stop the requests and the players before the queues are reset
            await super()._handle_interruption(frame, direction)

        async def on_audio_context_interrupted(self, context_id: str) -> None:
            reply = self._replies.pop(context_id, None)
            if reply is not None:
                await self._cancel_reply(reply)
            await super().on_audio_context_interrupted(context_id)

    _SERVICE = GeminiTTSService
    return _SERVICE


_SERVICE: type | None = None
