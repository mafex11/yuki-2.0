"""Yuki's own internal vocabulary, kept out of what the user hears and reads (report 76, honest wording).

Live 28 Sep-5 Oct: replies said "the thinker's still messaging him" and "the thinker's clicking around", two replies
ended in the speech channel's marker word "act", and cards once spoke of "the coach's nudge cards". These are Yuki's
own fixed words for its own parts and format, not anything the user said, so a deterministic check on that closed
vocabulary is the right tool here (the speech guard's model judgement stays for everything else):

* :data:`INTERNAL_WORDS` - names of Yuki's parts ("thinker", "talker");
* :data:`TOOL_WORDS` - Yuki's own tool and field names with an underscore (``tell_yuki``, ``hand_over``,
  ``say_first``, ``check_progress``): a file or site name of the user's with an underscore is theirs and stays;
* :data:`MARKER_WORDS` - the speech channel's marker words, only when one stands alone as a whole piece or as the
  last word after a sentence has ended ("Opening Chrome. act"); "act" inside a sentence is English.

:func:`internal_word` finds one; :func:`strip_internal` drops the sentences that carry one (a card keeps the rest).

r32 (final-J2 kw_dismiss_after_answer, both brains): :func:`text_call` reads the talker's own unspoken text (a reply with
no marker line, or what follows its act line) for one of Yuki's own calls (:data:`CALL_WORDS`) written out as text in a
call's shape, so the stream can tell "a call written as text, nothing ran" from "no marker". Yuki's own output and
its closed vocabulary only, never the user's words.
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
    # bTalk (brain = "all"): the lean talker's cancel tool, beside hand_over; the full talker's own cancel tool
    "cancel_hand_over", "cancel_tell_yuki",
})
#: Yuki's field and format words among :data:`TOOL_WORDS`: never the name of a call.
FIELD_WORDS: frozenset[str] = frozenset({"say_first", "say_when_done", "tool_use", "tool_result"})
#: The names of Yuki's own calls: :data:`TOOL_WORDS` without its field and format words.
CALL_WORDS: frozenset[str] = TOOL_WORDS - FIELD_WORDS
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


def _runs(line: str) -> list[tuple[int, int, str]]:
    """The line's runs of letters, digits and underscores, as ``(start, end, run casefolded)``."""
    out: list[tuple[int, int, str]] = []
    start: int | None = None
    for i, ch in enumerate(line + " "):
        if ch.isalnum() or ch == "_":
            if start is None:
                start = i
        elif start is not None:
            out.append((start, i, line[start:i].casefold()))
            start = None
    return out


#: Brackets a call's arguments are written in, opening and closing.
_OPENS, _CLOSES = "({[", ")}]"
#: How a written argument's value starts: a string, an object or a list.
_VALUE_STARTS = "\"'“‘{["
#: Bare values an argument may take (casefolded).
_LITERALS = frozenset({"true", "false", "null", "none"})


def _value_follows(text: str) -> bool:
    """Whether ``text`` starts with a written value: a string, an object, a list, a number or a bare literal."""
    text = text.lstrip()
    if not text:
        return False
    if text[0] in _VALUE_STARTS or text[0].isdigit() or (text[0] == "-" and text[1:2].isdigit()):
        return True
    runs = _runs(text)
    return bool(runs) and runs[0][0] == 0 and runs[0][2] in _LITERALS


def _argument_follows(text: str) -> bool:
    """Whether ``text`` starts with one of a call's arguments: a run, then ``=``; or a run, then ``:`` and a value
    (:func:`_value_follows`). A label in prose ("reason: they asked") has no value after it."""
    runs = _runs(text)
    if not runs or runs[0][0] != 0:
        return False
    rest = text[runs[0][1]:].lstrip()
    return rest[:1] == "=" or (rest[:1] == ":" and _value_follows(rest[1:]))


def _holds_arguments(bracketed: str) -> bool:
    """Whether the bracket ``bracketed`` opens holds a call's arguments: nothing yet (its line ends there, the call goes
    on below), none (it closes at once), a value or an argument. A bracket of prose words holds none."""
    inner = bracketed[1:].lstrip()
    return not inner or inner[0] in _CLOSES or inner[0] in _VALUE_STARTS or _argument_follows(inner)


def _closes_at_end(bracketed: str) -> bool:
    """Whether the bracket ``bracketed`` opens closes with nothing but marks after it on its line, or is still open at
    the line's end (a call written over several lines). Strings in double quotes are skipped."""
    depth, quoted, escaped = 0, False, False
    for i, ch in enumerate(bracketed):
        if quoted:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                quoted = False
        elif ch == '"':
            quoted = True
        elif ch in _OPENS:
            depth += 1
        elif ch in _CLOSES:
            depth -= 1
            if depth <= 0:
                return not any(c.isalnum() for c in bracketed[i + 1:])
    return True


def _call_shaped(line: str, runs: list[tuple[int, int, str]], k: int) -> bool:
    """Whether ``runs[k]`` (one of :data:`CALL_WORDS`) stands in ``line`` as a call written out. The structured
    shapes, anywhere in the line: an element named for it (``<name``); the value of a call's ``name`` key
    (``name="..."``, ``"name": "..."``). Otherwise the call is its line: nothing before its word but marks (a namespace
    glued to it, ``x.name``, or one of :data:`MARKER_WORDS` alone, allowed), and then either nothing but marks after it
    (not a question), or its arguments: a bracket glued to it, or after a space, ``:`` or ``=`` a bracket that holds
    arguments (:func:`_holds_arguments`), closing at the line's end or going on below (:func:`_closes_at_end`); or,
    after a space, an argument (:func:`_argument_follows`).

    Format only. A tool's name inside a sentence of prose is not a call, and neither is a line that only weighs it: "Not
    end_conversation (they asked a follow-up).", "end_conversation?", "end_conversation() is wrong here"
    (integ32 review F1 / F3)."""
    start, end, _name = runs[k]
    if line[:start].rstrip().endswith("<"):
        return True
    if k > 0 and runs[k - 1][2] == "name":
        between = line[runs[k - 1][1]:start]
        if not between.strip(" \t\"'=:") and ("=" in between or ":" in between):
            return True
    token = start
    while token > 0 and not line[token - 1].isspace():
        token -= 1
    before = [w for w in _words(line[:token]) if any(ch.isalnum() for ch in w)]
    if before and not (len(before) == 1 and before[0] in MARKER_WORDS):
        return False   # words before it on its line (a marker word alone aside: "act open_app(...)"): prose
    tail = line[end:]
    after = tail.lstrip()
    glued = len(after) == len(tail)
    if after[:1] in (":", "="):
        after = after[1:].lstrip()
        glued = False
    if after[:1] in ("(", "{"):
        return (glued or _holds_arguments(after)) and _closes_at_end(after)
    if k + 1 >= len(runs):
        return not any(ch in "?？" for ch in tail)   # alone on its line, never asked
    if line[end:runs[k + 1][0]].strip() not in ("", ":"):
        return False   # a mark between it and the next word: "stay_quiet? No: ..."
    return _argument_follows(line[runs[k + 1][0]:])


def text_call(text: str) -> str | None:
    """r32: the first of Yuki's own calls (:data:`CALL_WORDS`) that ``text`` writes out as a call (:func:`_call_shaped`,
    line by line), or ``None``. For the talker's unspoken text only (a reply with no marker line, what follows its act
    line): no tool block came for such a call, so nothing ran. Returns a name of the closed set, never the text."""
    for line in (text or "").splitlines():
        runs = _runs(line)
        for k, (_start, _end, run) in enumerate(runs):
            if run in CALL_WORDS and _call_shaped(line, runs, k):
                return run
    return None


__all__ = ["CALL_WORDS", "FIELD_WORDS", "INTERNAL_WORDS", "MARKER_WORDS", "TOOL_WORDS", "internal_word",
           "strip_internal", "text_call"]
