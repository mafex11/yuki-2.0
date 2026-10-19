"""Instant local answers before any model (vGame, 5 Oct 2026): a request said to Yuki by name is acknowledged at once.

The user's words (5 Oct): "whenever I say Yuki, do this or do that, it should immediately respond: yeah, I'm on it".

**The instant acknowledgement** (:class:`AckVoice`): a short line in Yuki's own voice ("Mm-hm.", or "Yeah?" to the
name alone), rendered once by the voice's own service and kept on disk, played the moment a turn said to Yuki by name
is transcribed. Whether a turn was said to Yuki is measured (the wake word's own hit, never words); what it says is
the same neutral line for every request, so nothing is decided about the request itself.

Nothing here decides what the words mean. The fast "go away" vocabulary of 5 Oct (a turn made only of the user's
dismiss phrases ended the conversation with no model call) was removed on 20 Oct (the user: "it should be intelligence
and intent that should drive the assistant"; keyword audit A1-A4, decision D1): "stop" while music plays, "not now"
answering "send it now?" and "cancel" answering a task's question all ended the conversation. The router's small model
and the talker now end it by their judgement of intent (``yuki.voice.router``, ``end_conversation``).
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

#: Words that may stand with the name alone ("hey Yuki", "Yuki?"): the user called her and is about to ask.
NAME_COMPANIONS = frozenset({"hey", "hi", "yo", "ok", "okay", "oh", "um", "uh"})
#: The wake word as speech recognition writes it (it is a keyterm of every conversation).
NAME = "yuki"


def words(text: str) -> list[str]:
    """``text`` as lower-case words: letters, digits and apostrophes kept, everything else a space (a fixed format)."""
    text = (text or "").replace("’", "'").replace("‘", "'").lower()
    return "".join(ch if (ch.isalnum() or ch == "'") else " " for ch in text).split()


def bare_name(text: str) -> bool:
    """Whether ``text`` is the name alone ("Yuki.", "Hey Yuki?"): the user called her and has not asked yet."""
    tokens = words(text)
    return bool(tokens) and NAME in tokens and all(t == NAME or t in NAME_COMPANIONS for t in tokens)


def name_opens(text: str) -> bool:
    """Whether speech recognition wrote the name opening one of the sentences of ``text`` ("Yuki, pause the music.",
    "Push B. Hey Yuki, turn it up", "Yuki."), not inside one ("I had a long talk with, uh, Yuki").

    The second, independent hearing of the wake word (review F2/F5, 5 Oct): in a game, a call or right after a goodbye
    the spotter's hit alone is not enough to treat the words as said to Yuki (accidental wakes on sound-alikes; the name
    said in a story), so the name is kept in what recognition hears and must open what was said. Sentences are split
    on recognition's own end punctuation (a fixed format); the companions ("hey", "okay") may come before the name."""
    sentence: list[str] = []
    for ch in (text or "") + ".":
        if ch in ".?!":
            tokens = words("".join(sentence))
            while tokens and tokens[0] in NAME_COMPANIONS:
                tokens.pop(0)
            if tokens and tokens[0] == NAME:
                return True
            sentence = []
        else:
            sentence.append(ch)
    return False


def with_name(text: str, forms: Any = ()) -> tuple[str, str | None]:
    """``text`` with a learned heard form of the name (``forms``: memory's data, how speech recognition wrote "Yuki"
    before, :func:`yuki.voice.keyterms.name_forms`) written as the name where it opens a sentence, after the
    companions ("UK, pause the music." -> "Yuki pause the music."; "hey yoki" -> "hey Yuki"), and the form used (or
    ``None``). Inside a sentence nothing changes ("I lived in the UK"). For the fast path's checks only (the name
    alone, the name opening the words); the words go on to the talker as heard."""
    variants = sorted({tuple(words(f)) for f in forms or () if words(f)} - {(NAME,)}, key=len, reverse=True)
    if not variants or not text:
        return text, None
    out: list[str] = []
    used: str | None = None
    sentence: list[str] = []
    for ch in text + "\0":
        if ch not in ".?!\0":
            sentence.append(ch)
            continue
        seg = "".join(sentence)
        tokens = words(seg)
        lead = 0
        while lead < len(tokens) and tokens[lead] in NAME_COMPANIONS:
            lead += 1
        for v in variants:
            if tuple(tokens[lead:lead + len(v)]) == v:
                pad = " " if seg[:1].isspace() else ""
                seg = pad + " ".join([*tokens[:lead], "Yuki", *tokens[lead + len(v):]])
                used = used or " ".join(v)
                break
        out.append(seg + ("" if ch == "\0" else ch))
        sentence = []
    return "".join(out), used


#: The acknowledgement lines (Yuki's own voice, rendered once per voice): a request, and the name alone.
ACK_LINES = {"request": "Mm-hm.", "name": "Yeah?"}
#: Edges quieter than this (dBFS) are trimmed from a rendered line, so it plays at once and ends crisply.
TRIM_DBFS = -45.0
#: A rendered line is played at most this long (s): an acknowledgement is a breath, never a sentence.
MAX_ACK_S = 1.2


def trim(pcm: bytes, rate: int = 24_000) -> bytes:
    """``pcm`` (int16 mono) without its quiet edges (measured levels; a 10 ms window), capped at :data:`MAX_ACK_S`."""
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768.0
    win = max(1, rate // 100)
    n = samples.size // win
    if n == 0:
        return pcm
    rms = np.sqrt(np.mean(samples[: n * win].reshape(n, win) ** 2, axis=1))
    loud = np.nonzero(rms > 10 ** (TRIM_DBFS / 20))[0]
    if loud.size == 0:
        return b""
    start = max(0, int(loud[0]) - 1) * win
    end = min(samples.size, (int(loud[-1]) + 2) * win, start + int(MAX_ACK_S * rate))
    return pcm[start * 2:end * 2]


def scale(pcm: bytes, gain_db: float) -> bytes:
    """``pcm`` (int16) at ``gain_db`` (0: unchanged)."""
    if not pcm or not gain_db:
        return pcm
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32) * (10 ** (gain_db / 20))
    return np.clip(samples, -32768, 32767).astype("<i2").tobytes()


class AckVoice:
    """The acknowledgement lines in this conversation's voice: on disk once, in memory while it lives.

    :meth:`load` runs off the voice loop (a worker thread, when the conversation is built): each line comes from the
    TTS cache (:class:`yuki.voice.ttscache.TTSCache`, keyed by provider, model, voice and the exact words), else is
    rendered once by the voice's own service over HTTP and stored. :meth:`pcm` only reads memory. A voice it cannot
    render here (Gemini's streaming voice) has no lines: the caller plays a soft tick instead.
    """

    def __init__(self, *, provider: str, voice: str, model: str = "", cache: Any = None, log: Any = None,
                 render: Any = None) -> None:
        self.provider = (provider or "").strip().lower()
        self.voice = voice or ""
        self.model = model or ""
        self.cache = cache
        self.log = log
        self._render = render or self._render_http
        self._pcm: dict[str, bytes] = {}
        self._lock = threading.Lock()
        self.loaded = threading.Event()

    def key(self, kind: str) -> str | None:
        if self.cache is None:
            return None
        return self.cache.key(self.provider, self.model, self.voice, "ack", ACK_LINES[kind])

    def load(self) -> None:
        """Fill the lines (worker thread; never raises)."""
        facts: dict[str, Any] = {}
        try:
            for kind, text in ACK_LINES.items():
                key = self.key(kind)
                pcm = self.cache.get(key) if (self.cache is not None and key) else None
                how = "cache"
                for attempt in range(2 if not pcm else 0):
                    # rendered once by the voice itself; offline 5 Oct one of four renders came back with no sound in
                    # it, so an empty one is asked again once (never cached)
                    how = "rendered" if attempt == 0 else "rendered_again"
                    try:
                        pcm = trim(self._render(text) or b"")
                    except Exception as exc:  # noqa: BLE001 - no line then: a tick
                        pcm, how = b"", f"error: {type(exc).__name__}"
                    if pcm:
                        if self.cache is not None and key:
                            self.cache.put(key, pcm)
                        break
                if pcm:
                    with self._lock:
                        self._pcm[kind] = pcm
                facts[kind] = {"how": how, "ms": round(len(pcm or b"") / 48)}
        finally:
            self.loaded.set()
            if self.log is not None:
                self.log.event("fast_path", op="ack_voice", provider=self.provider, lines=facts)

    def pcm(self, kind: str) -> bytes | None:
        with self._lock:
            return self._pcm.get(kind)

    def _render_http(self, text: str) -> bytes:
        """One line from the voice's own REST endpoint, as 24 kHz int16 mono (Deepgram Aura or ElevenLabs)."""
        import json
        import urllib.request

        from yuki.voice.keys import DEEPGRAM, ELEVENLABS, get_key

        if self.provider == "deepgram":
            url = (f"https://api.deepgram.com/v1/speak?model={self.voice}&encoding=linear16&sample_rate=24000"
                   "&container=none")
            headers = {"Authorization": f"Token {get_key(DEEPGRAM) or ''}", "Content-Type": "application/json"}
            body = json.dumps({"text": text}).encode("utf-8")
        elif self.provider == "elevenlabs":
            url = f"https://api.elevenlabs.io/v1/text-to-speech/{self.voice}?output_format=pcm_24000"
            headers = {"xi-api-key": get_key(ELEVENLABS) or "", "Content-Type": "application/json"}
            body = json.dumps({"text": text, **({"model_id": self.model} if self.model else {})}).encode("utf-8")
        else:
            return b""
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=10) as response:  # noqa: S310 - a fixed vendor endpoint
            return response.read()


__all__ = ["ACK_LINES", "AckVoice", "NAME", "NAME_COMPANIONS", "bare_name", "name_opens", "scale", "trim",
           "with_name", "words"]
