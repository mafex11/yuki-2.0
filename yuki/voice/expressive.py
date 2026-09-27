"""Yuki's expressive voice marks: the Gemini voice's sound tags and a per-turn style (docs/VOICE.md §10).

The mouth may write two kinds of mark in what it says, and the model alone decides when (its prompt,
never code):

* a **sound tag**, one of Gemini TTS's own vocal tags in square brackets (``[sigh]``, ``[laugh]``,
  ``[short pause]``), placed exactly where the sound belongs: it goes to Gemini in Gemini's own
  spelling (``<sigh>``) and the voice makes that sound there;
* a **style mark** at the very start of a reply, ``[style: calm and soft]``: how the whole reply is
  delivered. It becomes the request's ``speechMetadata.style`` for every piece of that reply.

Why square brackets for the mouth when Gemini's tags are angle brackets: measured 2026-09-27 (offline
scenario runs), telling Haiku that angle-bracket marks are allowed made it write XML-like markup as
speech (``<thinking>...``, ``<stay_quiet>`` instead of calling the tool) in 5 of ~20 result turns.
Angle-bracket marks are still recognised here, so anything of the kind is cut out, never said.

Everything here is plumbing over that fixed syntax of our own: cutting the marks out of what the
chat, memory and logs show (:func:`strip_tags`), taking the style mark off the front of a reply
(:func:`split_style`), and sending Gemini only the tags it documents (:func:`keep_known_tags`), so a
tag the model made up is never read out as words. Nothing here decides how Yuki behaves.

:class:`EdgeTrimmer` trims the long silence Gemini puts at the start and end of every request
(measured 60-250 ms before and 60-430 ms after the words), so a reply split into several requests
has natural gaps, not half-second holes, and the first word is heard sooner. A measured audio level,
not a rule about words.
"""

from __future__ import annotations

from array import array

#: The vocal tags Gemini 3.8 TTS documents (https://ai.google.dev/gemini-api/docs/speech-generation,
#: read 2026-09-27). A vendor's fixed list: anything else in brackets is not sent to it.
GEMINI_TAGS: tuple[str, ...] = (
    "argh", "breath", "heavy breath", "exhales", "cackle", "cheer", "chuckle", "chuckles", "cough", "cry",
    "gasp", "giggle", "groan", "growl", "grunt", "grr", "hiss", "laugh", "laughter", "moan", "pant", "pff",
    "phew", "scream", "shout", "shriek", "sigh", "sighs", "sneeze", "snicker", "snort", "sob",
    "throat-clearing", "tsk", "whimper", "whispers", "whispering", "yawn", "short pause", "long pause",
)
_KNOWN = frozenset(GEMINI_TAGS)

#: The style mark's name inside the brackets (``[style: ...]``).
STYLE_KEY = "style"
#: A mark is at most this long inside its brackets; a longer bracketed stretch is not ours and stays.
MARK_MAX_CHARS = 80
#: The brackets a mark may use: the mouth's ``[...]`` and Gemini's own ``<...>``.
BRACKETS = (("[", "]"), ("<", ">"))


def _marks_in(text: str, left: str, right: str) -> list[tuple[int, int, str]]:
    found: list[tuple[int, int, str]] = []
    i = 0
    while True:
        start = text.find(left, i)
        if start < 0:
            return found
        end = text.find(right, start + 1)
        if end < 0:
            return found
        inside = text[start + 1:end]
        if left in inside:  # "a < b <sigh>": the mark begins at the later bracket
            i = start + 1 + inside.rfind(left)
            continue
        # Ours: a letter right after the bracket ("[sigh]", "[style: calm]"), short, one line; a closing
        # "</...>" too (never anything to say: markup a model wrote as text). "x < y and y > z" is not a mark.
        name = inside[1:] if inside[:1] == "/" else inside
        if name[:1].isalpha() and len(inside) <= MARK_MAX_CHARS and "\n" not in inside:
            found.append((start, end + 1, inside))
        i = end + 1


def _marks(text: str) -> list[tuple[int, int, str]]:
    """``(start, end, inside)`` of every mark in ``text``, in order (our syntax: no nesting, short)."""
    found = sorted(m for left, right in BRACKETS for m in _marks_in(text, left, right))
    kept: list[tuple[int, int, str]] = []
    for mark in found:  # one inside another ("[a <b> c]"): the outer one
        if kept and mark[0] < kept[-1][1]:
            continue
        kept.append(mark)
    return kept


def _style_value(inside: str) -> str | None:
    """The style of a mark's inside when it is a style mark (``style: warm`` / ``style warm``), else ``None``."""
    head = inside.lstrip()
    if head[:len(STYLE_KEY)].casefold() != STYLE_KEY:
        return None
    rest = head[len(STYLE_KEY):]
    if rest and not (rest[0] in ":= " or rest[0].isspace()):
        return None  # "styles", "stylish": not our mark
    return rest.lstrip(":= ").strip() or None


def _tidy(text: str) -> str:
    """One space between words, none before a mark of punctuation (what cutting marks out leaves behind)."""
    words = " ".join(text.split())
    out: list[str] = []
    for char in words:
        if char in ",.;:!?।" and out and out[-1] == " ":
            out.pop()
        out.append(char)
    return "".join(out).strip()


def strip_tags(text: str | None) -> str:
    """``text`` with every mark (sound tags and style marks) cut out: what the chat, memory and logs show."""
    if not text:
        return text or ""
    marks = _marks(text)
    if not marks:
        return text
    parts, last = [], 0
    for start, end, _inside in marks:
        parts.append(text[last:start])
        parts.append(" ")
        last = end
    parts.append(text[last:])
    return _tidy("".join(parts))


def has_marks(text: str | None) -> bool:
    return bool(text) and bool(_marks(text or ""))


def split_style(text: str) -> tuple[str | None, str]:
    """``(style, rest)``: a style mark at the start of ``text`` taken off; later style marks are dropped too.

    The mouth writes the style mark first; only the first one counts. ``rest`` keeps the sound tags.
    """
    style: str | None = None
    parts, last = [], 0
    for start, end, inside in _marks(text):
        value = _style_value(inside)
        if value is None:
            continue
        if style is None and not text[last:start].strip() and not parts:
            style = value
        parts.append(text[last:start])
        last = end
    if not parts and style is None:
        return None, text
    parts.append(text[last:])
    return style, " ".join("".join(parts).split())


def keep_known_tags(text: str) -> tuple[str, int]:
    """``(text, dropped)``: what goes to Gemini: its documented tags in its own spelling, other marks cut out.

    A tag is matched on its inside, case and spacing aside (``[Short  pause]`` is ``<short pause>``); a
    mark that is not one (a made-up ``[smiles]``, stray markup) is never read out as words.
    """
    marks = _marks(text)
    if not marks:
        return text, 0
    parts, last, dropped = [], 0, 0
    for start, end, inside in marks:
        parts.append(text[last:start])
        name = " ".join(inside.split()).casefold()
        if name in _KNOWN:
            parts.append(f"<{name}>")
        else:
            dropped += 1
            parts.append(" ")
        last = end
    parts.append(text[last:])
    return _tidy("".join(parts)) if dropped else "".join(parts), dropped


def speakable(text: str) -> bool:
    """Whether ``text`` has anything for a voice to say or sound (words, or one of Gemini's sound tags)."""
    return bool(strip_tags(text).strip()) or any(" ".join(inside.split()).casefold() in _KNOWN
                                                 for _s, _e, inside in _marks(text))


class EdgeTrimmer:
    """Trims the silence around one request's 16-bit mono PCM, as the chunks stream in.

    Leading audio below ``level`` is dropped until the first loud 10 ms window, keeping ``lead_s``
    before it; quiet audio is held back (never loud audio: no delay once words flow) and released when
    more loud audio follows, so pauses inside the request stay whole; at the end, at most ``trail_s``
    of the quiet tail is kept. Short fades at both cut edges keep a cut from clicking.
    """

    WINDOW_S = 0.01
    FADE_S = 0.004

    def __init__(self, sample_rate: int = 24_000, *, level: int = 250, lead_s: float = 0.03,
                 trail_s: float = 0.15) -> None:
        self.rate = sample_rate
        self.level = level
        self.window = max(1, int(sample_rate * self.WINDOW_S))
        self.lead = int(sample_rate * lead_s)
        self.trail = int(sample_rate * trail_s)
        self.fade = max(1, int(sample_rate * self.FADE_S))
        self._started = False
        self._pending = array("h")   # quiet samples held back (before the first word: the lead-in)
        self._odd = b""              # a byte left over from a chunk of odd length
        self.trimmed_lead = 0        # samples dropped before the words (for the log)
        self.trimmed_tail = 0

    def _loud(self, seg: array) -> bool:
        return bool(len(seg)) and (max(seg) > self.level or min(seg) < -self.level)

    def _last_loud(self, samples: array) -> int:
        """Index just past the last loud 10 ms window in ``samples`` (0: none)."""
        n, w = len(samples), self.window
        start = (n - 1) // w * w if n else 0
        while start >= 0 and n:
            if self._loud(samples[start:start + w]):
                return min(n, start + w)
            start -= w
        return 0

    def _first_loud(self, samples: array) -> int:
        """Index of the first loud 10 ms window in ``samples`` (``-1``: none)."""
        w = self.window
        for start in range(0, len(samples), w):
            if self._loud(samples[start:start + w]):
                return start
        return -1

    def _fade(self, samples: array, *, at_start: bool) -> None:
        n = min(self.fade, len(samples))
        for i in range(n):
            k = (i + 1) / (n + 1)
            j = i if at_start else len(samples) - 1 - i
            samples[j] = int(samples[j] * k)

    def feed(self, chunk: bytes) -> bytes:
        """The part of ``chunk`` (plus anything held back) that can be played now."""
        data = self._odd + chunk
        self._odd = data[len(data) - (len(data) % 2):] if len(data) % 2 else b""
        samples = array("h")
        samples.frombytes(data[:len(data) - len(self._odd)])
        buf = self._pending + samples
        if not self._started:
            first = self._first_loud(buf)
            if first < 0:
                keep = buf[-self.lead:] if self.lead else array("h")
                self.trimmed_lead += len(buf) - len(keep)
                self._pending = keep
                return b""
            cut = max(0, first - self.lead)
            self.trimmed_lead += cut
            buf = buf[cut:]
            self._fade(buf, at_start=True)
            self._started = True
        loud_end = self._last_loud(buf)
        out, self._pending = buf[:loud_end], buf[loud_end:]
        return out.tobytes()

    def finish(self) -> bytes:
        """What is left at the end of the request: at most ``trail_s`` of the quiet tail, faded out."""
        if not self._started:
            self.trimmed_lead += len(self._pending)
            self._pending = array("h")
            return b""
        tail = self._pending[:self.trail]
        self.trimmed_tail += len(self._pending) - len(tail)
        self._pending = array("h")
        if len(tail):
            self._fade(tail, at_start=False)
        return tail.tobytes()
