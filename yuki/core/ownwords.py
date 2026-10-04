"""Yuki's own internal vocabulary, kept out of what the user hears and reads (report 76, honest wording).

Live 28 Sep-5 Oct: replies said "the thinker's still messaging him" and "the thinker's clicking around", two replies
ended in the speech channel's marker word "act", and cards once spoke of "the coach's nudge cards". These are Yuki's
own fixed words for its own parts and format, not anything the user said, so a deterministic check on that closed
vocabulary is the right tool here (the speech guard's model judgement stays for everything else):

* :data:`INTERNAL_WORDS` - names of Yuki's parts ("thinker", "talker");
* :data:`TOOL_WORDS` - Yuki's own tool and field names with an underscore (``tell_yuki``, ``say_first``,
  ``check_progress``): a file or site name of the user's with an underscore is theirs and stays;
* :data:`MARKER_WORDS` - the speech channel's marker words, only when one stands alone as a whole piece or as the
  last word after a sentence has ended ("Opening Chrome. act"); "act" inside a sentence is English.

:func:`internal_word` finds one; :func:`strip_internal` drops the sentences that carry one (a card keeps the rest).
"""

from __future__ import annotations

#: Yuki's names for its own parts. Whole words, any case, with or without "'s". ("coach" is not here: it is an
#: everyday word for a person in the user's life, and One Yuki retired it as a name of Yuki's.)
INTERNAL_WORDS: frozenset[str] = frozenset({"thinker", "thinkers", "talker", "talkers"})
#: Yuki's tool and field names written with an underscore (the agent's and the voice's; a test keeps this in step with
#: both tool lists).
TOOL_WORDS: frozenset[str] = frozenset({
    "answer_question", "app_volume", "ask_user", "check_progress", "correct_memory", "end_conversation", "fetch_url",
    "focus_window", "forget_rule", "go_quiet", "hand_over", "launch_app", "look_at_desktop", "look_at_focus",
    "look_at_window", "media_key", "more_of_result", "note_to_self", "open_app", "open_url", "open_yuki",
    "quick_memory", "read_clipboard", "read_file", "read_page", "read_tabs", "remember_how", "remember_rule",
    "run_powershell", "run_skill", "set_slider", "spawn_helpers", "stay_quiet", "system_facts", "take_screenshot",
    "tell_yuki", "type_text", "watch_for", "window_action", "write_clipboard", "say_first", "say_when_done",
    "tool_use", "tool_result", "check_situation",
})
#: The speech channel's marker words (yuki.voice.mouth.MARKERS; its "more" divider is an everyday answer, "More.").
MARKER_WORDS: frozenset[str] = frozenset({"say", "act", "quiet"})
#: Marks that end a sentence (Latin, Devanagari danda, CJK).
_SENTENCE_MARKS = ".!?…।。！？"


def _words(text: str) -> list[str]:
    """The line's words, casefolded, punctuation around them dropped, a possessive "'s" taken off."""
    out = []
    for raw in (text or "").split():
        word = raw.strip("\"'“”‘’()[]{}<>,.;:!?…।。！？*").casefold()
        for tail in ("'s", "’s"):
            if word.endswith(tail):
                word = word[: -len(tail)]
        if word:
            out.append(word)
    return out


def internal_word(text: str) -> str | None:
    """The first internal word in ``text`` (see the module docstring), or ``None``."""
    words = _words(text)
    for word in words:
        if word in INTERNAL_WORDS or word in TOOL_WORDS:
            return word
    if len(words) == 1 and words[0] in MARKER_WORDS:
        return words[0]
    stripped = (text or "").rstrip()
    if len(words) >= 2 and words[-1] in MARKER_WORDS:
        head = stripped[: len(stripped) - len(stripped.split()[-1])].rstrip()
        if head and head[-1] in _SENTENCE_MARKS:
            return words[-1]
    return None


def _sentences(text: str) -> list[str]:
    """``text`` cut after each sentence mark that a space or the end follows (the marks kept)."""
    out, cur = [], ""
    for i, ch in enumerate(text or ""):
        cur += ch
        nxt = text[i + 1] if i + 1 < len(text) else " "
        if ch in _SENTENCE_MARKS and nxt.isspace():
            out.append(cur)
            cur = ""
    if cur.strip():
        out.append(cur)
    return out


def strip_internal(text: str) -> str:
    """``text`` without the sentences that carry an internal word (a lone trailing marker word dropped too); ``""``
    when nothing is left. A text without one comes back unchanged."""
    if internal_word(text) is None:
        return text
    kept = []
    for sentence in _sentences(text):
        if internal_word(sentence) is None:
            kept.append(sentence)
            continue
        words = sentence.split()
        if len(words) >= 2 and _words(words[-1]) and _words(words[-1])[0] in MARKER_WORDS \
                and internal_word(" ".join(words[:-1])) is None and internal_word(sentence) in MARKER_WORDS:
            kept.append(" ".join(words[:-1]))   # "Done act": the sentence without its stray marker
    return " ".join(s.strip() for s in kept if s.strip())


__all__ = ["INTERNAL_WORDS", "MARKER_WORDS", "TOOL_WORDS", "internal_word", "strip_internal"]
