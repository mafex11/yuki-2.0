"""Knowing the user as a person: what each journal item is, and the portrait's Personality section.

Contract: ``docs/MEMORY.md`` -> "Personality: what is true and what is a joke".

Every journal item carries a *nature* (migration 14, ``journal.nature``), chosen by
the model that writes it (the journal, conversation memory):

* ``fact`` - plainly told or shown as true about the user's life or work;
* ``trait`` - how they think and behave, their humour, values, tastes, habits,
  drawn from how they talk and what they say about themselves;
* ``story`` - an anecdote from their life that they told;
* ``humor`` - a joke, bit, tease, boast or banter: kept as humour context, never
  treated as a fact;
* ``uncertain`` - possibly misheard or ambiguous: never used as a fact anywhere.

Commitments are not journal items; they keep their own to-do path.

Readers label items by nature (:func:`nature_mark`) and leave ``uncertain`` ones out
wherever they would be taken as facts (:func:`usable`). The portrait builds a
``personality`` kind of fact from traits, stories and humour, each with an
*aspect* (:data:`ASPECTS`); its Personality section is written by the render call
into its own tool field and placed under a heading code writes
(:data:`PERSONALITY_HEADING`), so :func:`split_personality` can hand the section to
the voice mouth separately without parsing model output.

Public API::

    NATURES, ASPECTS, ASPECT_TITLES, PERSONALITY_HEADING
    normalize_nature(value) -> str;  nature_mark(nature) -> str;  usable(entry) -> bool
    NATURE_GUIDANCE          # the journal prompts' paragraph (behavioural)
    split_personality(portrait) -> (rest, section | None)
"""

from __future__ import annotations

from typing import Any

from yuki.memory.store import JOURNAL_NATURES, PERSONALITY_ASPECTS

NATURES: tuple[str, ...] = JOURNAL_NATURES
ASPECTS: tuple[str, ...] = PERSONALITY_ASPECTS

#: How a reader marks an item after its text (a fact needs no mark).
_MARKS: dict[str, str] = {
    "fact": "",
    "trait": " [trait]",
    "story": " [a story they told]",
    "humor": " [joke/banter - humour, not a fact]",
    "uncertain": " [uncertain - possibly misheard or ambiguous, not a fact]",
}

#: The Personality section's parts, in the order the render writes them.
ASPECT_TITLES: dict[str, str] = {
    "talk": "how they talk",
    "cares": "what they care about",
    "joke": "running jokes and bits (humour - never facts)",
    "story": "stories they tell about themselves",
    "people": "how they are with different people",
}

#: The heading code puts above the render's Personality text (so it can be found again).
PERSONALITY_HEADING = (
    "Personality (how they talk, what they care about, their running jokes and stories; "
    "a joke is a joke, never a fact):"
)


def normalize_nature(value: Any) -> str:
    """One of :data:`NATURES`; anything else is ``fact`` (items recorded before natures existed)."""
    text = str(value or "").strip().lower()
    return text if text in NATURES else "fact"


def nature_mark(nature: Any) -> str:
    """The label a reader appends to an item of this nature (``""`` for a fact)."""
    return _MARKS[normalize_nature(nature)]


def usable(entry: Any) -> bool:
    """Whether a journal item may be read as evidence at all (``uncertain`` ones may not)."""
    return normalize_nature(getattr(entry, "nature", "fact")) != "uncertain"


NATURE_GUIDANCE = """\
WHAT EACH ITEM IS. Give every fact its nature, so that Yuki knows what is true, what is the user \
being themselves and what is a joke:
- fact: something plainly told or shown as true about the user's life, work, plans, people or \
what happened.
- trait: how the user thinks and behaves - their humour, values, tastes, habits, how they talk \
(register, languages, swearing, banter) and how they are with people - drawn from how they talk \
and what they say about themselves ("The user swears casually and switches between Hindi and \
English mid-sentence with friends"; "The user says solving maths problems gives them a rush").
- story: an anecdote from their own life that they told ("The user said that in college their \
alarm clock was so loud the neighbours complained, so they switched to a sunrise lamp").
- humor: a joke, a bit, a tease, a boast, banter, a hypothetical said for effect, playing along. \
Keep it - it shows who they are and what they laugh at - but write it as the joke it was, never \
as a fact: "The user joked that they need a pilot partner who earns a million a year so they \
never have to work." Teasing someone else is humour too.
- uncertain: it may be misheard, or you cannot tell whether it was meant seriously or what it \
refers to. Write it with the doubt in the sentence ("The user said what sounded like ...", "The \
user said, possibly joking, that ...").
The nature is a label, not a filter: record every fact, plan, story and detail you would record \
anyway, and label each. The sentence itself must carry the nature for humour and uncertain items, \
so that a reader who sees only the sentence is not misled. A trait you see in how they talk is \
written as what they do ("The user swears casually ..."), not as something they said; record a \
trait once, and again only when the sources show something new about it. A joke about something \
real can sit next to a fact about it: record the fact only where the user's own words state it \
plainly. A claim that would be surprising if true - unusually big, free, rich, famous or \
extreme - is a fact only when the words around it make it plainly serious and clearly heard; \
said to impress or wind up friends it is humor, and when you cannot tell it is uncertain."""


def split_personality(portrait: str | None) -> tuple[str, str | None]:
    """``(the portrait without its Personality section, the section)``; the section is ``None`` when absent.

    The section sits under :data:`PERSONALITY_HEADING`, which the portrait worker writes
    itself (never the model), and runs to the next blank-line block that starts with the
    "Open loops" heading the worker also writes, or to the end.
    """
    text = portrait or ""
    start = text.find(PERSONALITY_HEADING)
    if start < 0:
        return text, None
    end = text.find("\n\nOpen loops", start)
    section = text[start:end if end >= 0 else len(text)].strip()
    rest = (text[:start].rstrip() + ("\n\n" + text[end:].lstrip("\n") if end >= 0 else "")).strip()
    return rest, section


__all__ = [
    "NATURES", "ASPECTS", "ASPECT_TITLES", "PERSONALITY_HEADING", "NATURE_GUIDANCE",
    "normalize_nature", "nature_mark", "usable", "split_personality",
]
