"""Instant local answers before any model (vGame, 5 Oct 2026): "go away" ends at once, a request is acknowledged at once.

The user's words (5 Oct): "when I say you can go away, it should go away immediately, not take 10, 20 seconds", and
"whenever I say Yuki, do this or do that, it should immediately respond: yeah, I'm on it". Measured on seven days of
real use (report 76, logs of 28 Sep - 5 Oct): a dismissal went through the talker (its first token ~1.5 s), a spoken
goodbye and a wait for it to play (~2 s), so the conversation ended ~4 s after the words (p50 4.0 s, max 5.1 s), and in
a call 13 of 37 were judged as said to the friends: nothing ended, the call idle ran out 6-9 s later, he said it again.

Two things here, both deliberately small and logged:

* **The dismiss vocabulary** (:data:`DISMISS_PHRASES`): the user's own words for "go away" are a *command vocabulary*,
  like a hotkey, not a judgement. A turn whose words are only those phrases (with the few filler words in
  :data:`FILLER_WORDS` around them: the name, "okay", "dude", "please") ends the conversation on its final transcript,
  with no model call. Anything longer or different goes to the talker as before (the model judges "turn yourself off for
  two hours", "tell him to go away"). Closing when the words were for someone else costs little (say the name to talk
  again); staying open is what hurt. A match is provisional (second review R1/G1): it ends the conversation only after
  a short grace with no resumed speech, since "Stop... the music" and "Nothing... actually open Spotify" begin with
  these words and must reach the talker whole. A few phrases mean "stop *that*" when something is running (a task, a scroll):
  :data:`WHEN_IDLE_ONLY` are a dismissal only when nothing is.
* **The instant acknowledgement** (:class:`AckVoice`): a short line in Yuki's own voice ("Mm-hm.", or "Yeah?" to the
  name alone), rendered once by the voice's own service and kept on disk, played the moment a turn said to Yuki by name
  is transcribed. Whether a turn was said to Yuki is measured (the wake word's own hit, never words); what it says is
  the same neutral line for every request, so nothing is decided about the request itself.
"""

from __future__ import annotations

import threading
from typing import Any

import numpy as np

#: Phrases that, alone, end the conversation (normalised: lower case, apostrophes kept). The user's own words from the
#: week's dismissals (counts only, report 76) plus their plain variants. Keep this small: it is a command vocabulary.
DISMISS_PHRASES = frozenset({
    "go away", "go away now", "you can go", "you can go away", "you can go now", "get lost",
    "shut up", "be quiet", "quiet", "stop listening", "turn off", "go to sleep", "go back to sleep", "sleep",
    "you can go back to sleep", "you can sleep",
    "that's all", "thats all", "that's it", "thats it",
    "not you", "not now", "not talking to you", "i wasn't talking to you", "wasn't talking to you",
    "nothing", "bye", "bye bye", "goodbye", "good bye",
    "stop", "stop it", "cancel", "never mind", "nevermind", "leave it",
})
#: Of those, the ones that mean "stop that" while something runs (a task, a scroll, a hand): a dismissal only when
#: nothing does; otherwise the talker decides (it stops the scroll or cancels the task).
WHEN_IDLE_ONLY = frozenset({"stop", "stop it", "cancel", "never mind", "nevermind", "leave it"})
#: Of those, the ones that also confirm an answer ("The report from yesterday?" "That's it."): a dismissal only when
#: Yuki's last line was not a question and nothing runs (review F3, 5 Oct); otherwise the talker decides.
WHEN_NOT_ASKED = frozenset({"that's it", "thats it", "that's all", "thats all"})
#: Of those, the ones that mean something else in a call (second review R9, 5 Oct): "stop listening" there is go_quiet
#: for the rest of the call (the wake word off, CALL_LAYER), which only the talker does; a plain end left the wake on.
IN_CALL_TALKERS = frozenset({"stop listening"})
#: Words that may stand around a dismissal without changing it ("okay okay go away", "dude, shut up", "Yuki, bye").
#: Not "yes"/"yeah": "Yeah, that's it." confirms (review F3); "no" stays ("No, that's all." answers "anything else").
FILLER_WORDS = frozenset({
    "yuki", "hey", "ok", "okay", "no", "nah", "dude", "bro", "man", "please", "just", "now",
    "for now", "you", "so", "oh", "uh", "um", "and", "then", "thanks", "thank you", "alright", "all right", "sorry",
    "i said", "i just said", "already", "again", "seriously",
})
#: Words that may stand with the name alone ("hey Yuki", "Yuki?"): the user called her and is about to ask.
NAME_COMPANIONS = frozenset({"hey", "hi", "yo", "ok", "okay", "oh", "um", "uh"})
#: The wake word as speech recognition writes it (it is a keyterm of every conversation).
NAME = "yuki"
#: Longest phrase in the vocabularies, in words.
_LONGEST = max(len(p.split()) for p in DISMISS_PHRASES | FILLER_WORDS)
#: A dismissal is a short turn; anything longer is the talker's to judge.
MAX_WORDS = 12


def words(text: str) -> list[str]:
    """``text`` as lower-case words: letters, digits and apostrophes kept, everything else a space (a fixed format)."""
    text = (text or "").replace("’", "'").replace("‘", "'").lower()
    return "".join(ch if (ch.isalnum() or ch == "'") else " " for ch in text).split()


def dismissal(text: str, *, busy: bool = False, asked: bool = False, call: bool = False) -> str | None:
    """The dismiss phrase ``text`` consists of (with fillers around it), or ``None`` when it is anything else.

    ``busy``: something is running (a voice task, a scroll, a hand): :data:`WHEN_IDLE_ONLY` and :data:`WHEN_NOT_ASKED`
    phrases are then not a dismissal. ``asked``: Yuki's last line was a question: :data:`WHEN_NOT_ASKED` phrases are
    then an answer, not a dismissal. ``call``: a call is on: :data:`IN_CALL_TALKERS` phrases are the talker's (the
    whole turn goes to it). Every word must belong to the vocabulary; at least one must be a dismiss phrase.
    A transcript written as a question ("So that's it?") is never a command.
    """
    if (text or "").rstrip().endswith("?"):
        return None   # speech recognition's own punctuation: asked, not told (a fixed format)
    tokens = words(text)
    if not tokens or len(tokens) > MAX_WORDS:
        return None
    found: str | None = None
    i = 0
    while i < len(tokens):
        for k in range(min(_LONGEST, len(tokens) - i), 0, -1):
            phrase = " ".join(tokens[i:i + k])
            if phrase in DISMISS_PHRASES:
                if call and phrase in IN_CALL_TALKERS:
                    return None   # R9: in a call "stop listening" is go_quiet for the call, the talker's
                # a phrase that is not a dismissal now ("that's all" answering her question) is neutral: another
                # phrase in the same words may still be one ("That's all for now. Bye.")
                if not (busy and phrase in WHEN_IDLE_ONLY) and not ((busy or asked) and phrase in WHEN_NOT_ASKED):
                    found = found or phrase
                i += k
                break
            if phrase in FILLER_WORDS:
                i += k
                break
        else:
            return None
    return found


def vocabulary_only(text: str) -> bool:
    """Whether every word of ``text`` belongs to the dismiss vocabulary or its fillers ("Yuki, no", "Okay, turn off"):
    words that may still become a dismissal, so the eager acknowledgement waits for more (second review R4)."""
    tokens = words(text)
    i = 0
    while i < len(tokens):
        for k in range(min(_LONGEST, len(tokens) - i), 0, -1):
            if " ".join(tokens[i:i + k]) in DISMISS_PHRASES | FILLER_WORDS:
                i += k
                break
        else:
            return False
    return bool(tokens)


def said_in(phrase: str, line: str) -> bool:
    """Whether the words of ``phrase`` stand together in ``line`` (both as :func:`words`): Yuki's own line holding the
    dismiss words, so a transcript of them may be her echo (second review G2)."""
    need, have = words(phrase), words(line)
    return bool(need) and any(have[i:i + len(need)] == need for i in range(len(have) - len(need) + 1))


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
    ``None``). Inside a sentence nothing changes ("I lived in the UK"). For the fast path's checks only (the
    dismissal, the name alone, the name opening the words); the words go on to the talker as heard."""
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


__all__ = ["DISMISS_PHRASES", "WHEN_IDLE_ONLY", "WHEN_NOT_ASKED", "IN_CALL_TALKERS", "FILLER_WORDS", "ACK_LINES", "AckVoice", "dismissal",
           "bare_name", "name_opens", "vocabulary_only", "said_in", "words", "trim", "scale"]
