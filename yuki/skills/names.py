"""Names: a measured match of a heard name against the user's OWN apps and sites (final-D S18; the user's decision 3,
19 Oct: "not flag, but it should automatically understand it and work").

Speech recognition garbles the names of the user's own things: "Bring up the scored." for Discord, "Open Porbex
Kaladra." for Excalidraw. Left to a model's guess, a garbled name went to the wrong app (final-D p24_misheard: Process
Lasso, 0.40 by spelling against Excalidraw's 0.61) or to an album (switch_discord under ONE: SCRAPYARD). Here code
measures the heard words against the user's own names; when one is a clear winner the act takes it and its done line
names what it did ("Opening Discord."), never a question and never a warning. With no clear winner nothing changes.

**The names** (:func:`own_names`, :func:`own_entries`): the apps (the programs with a window now, the programs the
user's timeline in memory shows them using, most used first, then the Start menu's entries) and the sites (memory's
links seen: the pages the user had in front in a browser, never a page Yuki opened; one per registrable domain, named
by its most visited host, at least :data:`SITE_MIN_VISITS` visits), each once, at most :data:`OWN_MAX`. An app is
named as the user sees it ("Process Lasso"), a site by its host ("excalidraw.com": :func:`is_site`, :func:`site_url`).
skill22 (A6, contract C14): with ``channels``, also their own channels on the sites' named pages (:data:`CHANNEL`:
taste's kinds of a site file's page with a speakable value, YouTube's ``/@handle``; their picked ones, most picked
first, then the ones their own app showed), each by its own display name when the site gave one, at most
:data:`OWN_CHANNELS`, their room kept inside :data:`OWN_MAX`; :func:`own_names` lists them. :func:`own_url` puts a raw
address of such a page through the same measure.

**The measure** (:func:`best_match`, :func:`score`). Runs of one to :data:`MAX_WORDS` heard words are compared with
each name's spoken forms (an app's name, with and without a bracketed part; a site's registrable name, "excalidraw" for
excalidraw.com) by two distances, averaged:

* the spelling: edit distance over the longer one, doubled letters read once, letters as
  :func:`yuki.memory.contacts.romanize` reads them (a Devanagari word is compared in Latin letters);
* the sound: the same distance on a sound code computed from the letters (:func:`sound_code`: the people index's
  consonant classes and three vowel classes). A fixed format rule over letters, never a word list.

A form that stands in the heard words as written scores 1.0. Otherwise a run and a form need :data:`NEAR_MIN_LETTERS`
letters, the same first sound and about the same length (:data:`LENGTH_RATIO`). The tail shape (final-B / final-D): a
name's first sound can go into the word heard before it ("the|scored" for "dis|cord", "Porb|ex Kaladra" for
"Ex|calidraw"): a word of at least :data:`TAIL_MIN_LETTERS` letters is measured with the name's first one or two
letters put back when the word before ends in them as written, or is them heard as another short word ("the" for
"di").

One mode (kw3, 20 Oct, contract K2; the user's direction: intent, never words, drives Yuki): a name given to an act
(``as_name``: a launch's app name, a site value, an app slot's value: a name the model already chose), measured whole
or by the runs that end at its last word (never a part before them: "microsoft edge" is no teams.microsoft.com), bar
:data:`BAR`. The whole-turn mode (every run of a user's turn, bar :data:`TURN_BAR`) is gone: no caller fills an app or a
site from any one-to-three-word run of what the user said ("Turn the game down so I can hear the score" measured "the
score" as Discord); a misheard name is understood by the model, and only the name it chose is resolved here.
:data:`TURN_BAR` stays as a strength bar for a reader that keeps only the clearest winners (the voice's near reading of
a heard stretch, ``yuki.voice.pipeline.measured_near``: a hint to the model, never an act).

skill22 (A14: "this card", "chat BP", "InstaCol" and "Ark" scored under the bar). A possessive's "'s" is no word
("Deezy's" is "Deezy": a format rule), and the sound code says "j" as the voiced "ch" (:func:`sound_code`). A name
given to an act is also measured three more ways, each a fixed rule over letters and sounds: a short name (three or
four letters) by the very same sounds (:data:`SHORT_BAR`: "Ark" for Arc, "Deezy" for Deji); a vowel heard as another
class costing half an edit (:data:`NAME_VOWEL_COST`: "this card" for Discord); a one-word name of theirs said by its
first letters as written, when the given name is one written word (:data:`CLIP_SHARE`: "insta" for instagram.com; a
word the recognizer wrote as two joined ones, "InstaCol", by its first part). The short rule and the clip score under
:data:`TURN_BAR` (:data:`NAME_RULE_MAX`), so a reader at that bar never takes a name by them. "chat BP" stays under
the bar (0.69: one letter of "GPT" dropped and one changed is two edits of seven; no fixed rule raises it without
raising plain words).

**The winner** must reach the bar and beat the best name of every other group by :data:`MARGIN`. Names one of which
holds the other ("Chrome" in "Google Chrome") or with the same spoken form (the Claude app and claude.ai) are one
group: within it the higher score wins, then the longer run of heard words, then the name listed first (an app listed
before a site of the same name wins a tie). Otherwise ``None``: today's behaviour, unchanged.

Calibrated on 19 Oct on the DB copy (200 own names: 123 apps, 77 sites), the clips' transcripts of final-B/final-D and
the user's 1008 turns; the numbers are at each constant and in docs/research/final-test-2026-10-19/builds/names21.md.

Contract C11 (voice21): :func:`own_names` and :func:`best_match` (name mode only, contract K2), both pure apart from
the store read, never raising.
Nothing here is logged: the callers log content-free facts (the score, the margin, the kind and source of the name),
never the name or the words.
"""

from __future__ import annotations

import sys
import threading
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable, Sequence

#: At most this many of the user's own names (contract C11).
OWN_MAX = 200
#: A site is one of the user's own once memory saw them in front of its pages this many times (visits, Yuki's own
#: opens never counted): one stray visit is no site of theirs.
SITE_MIN_VISITS = 2
#: How many of memory's links seen are read for the sites (newest first).
LINKS_READ = 5000
#: A program shows the user's web pages (a browser) when its pages in memory's links span at least this many
#: registrable domains (web_route's own browser measure counts hosts in the timeline, ``BROWSER_MIN_HOSTS`` 3).
BROWSER_MIN_SITES = 3
#: The names read from memory are kept this long (per database; a request reads them once).
OWN_TTL_S = 300.0
#: The programs with a window now are read again after this long.
RUNNING_TTL_S = 5.0
#: How long a caller with no store of its own waits for the process's memory to answer (a bounded call).
STORE_WAIT_S = 0.8
#: A run of at most this many heard words is compared with a name (a name speech recognition split: "eleven labs").
MAX_WORDS = 3
#: Letters a name's form needs to be matched as written (a shorter name matches nothing; "x" for x.com is no measure).
MIN_LETTERS = 3
#: Letters a run and a form both need to be matched by the measure, not as written: measured on the user's own turns,
#: one letter off a four-letter name is a plain word ("like" / "live", "game" / "gama"); "Arc" heard as "Ark" stays the
#: people index's sound rule's (``contacts.sounds_in`` with ``short``, the resolver's app check).
NEAR_MIN_LETTERS = 5
#: The shorter of a run and a form must be at least this share of the longer one's letters: a mishearing keeps about
#: the name's length ("discored" / "discord" 7 of 8, "exkaladra" / "excalidraw" 9 of 10, "chatbp" / "chatgpt" 6 of 7);
#: a whole word that is only the start of a longer name is another word ("open" / "openai", 4 of 6).
LENGTH_RATIO = 0.75
#: The tail shape: at most this many of a name's first letters can go into the word heard before it...
TAIL_HEAD = 2
#: ...and the word that keeps the rest has at least this many letters (a short word fits too many tails).
TAIL_MIN_LETTERS = 5
#: A name given to an act (:func:`best_match` ``as_name``): the winner's score must reach this (the mean of the
#: spelling and sound similarities, 1.0 = the name as written). Calibrated on the DB copy of 19 Oct: the clips'
#: misheard names score 0.80-0.89 ("Porbex Kaladra" 0.80, "GIF hub" 0.82, "the scored" 0.88, "Justaname" 0.89); of
#: 120 names a model might give that no Start entry has ("browser", "microsoft edge", "game"), 5 took one, each the
#: right one ("aniwave" aniwaves.ru 0.88, "settings app" Settings 0.78, "paint 3d" Paint 0.92)...
BAR = 0.75
#: ...a reader that keeps only the clearest winners (the voice's near reading of a heard stretch, a hint to the model:
#: ``yuki.voice.pipeline.measured_near``): this. Calibrated when whole turns were measured (kw3: that mode is gone): on
#: the user's 1008 turns the non-literal winners between 0.75 and 0.85 were plain words ("click" ~ Clock 0.78, "not in"
#: ~ notion.com 0.83, "open a" ~ openai.com 0.83, "What's up" ~ WhatsApp 0.85-, 13 turns); at 0.85, 6 winners, each a
#: real mishearing ("spot if" Spotify 0.93, "dev mefex" devmafex.com 0.88, "discard call" Discord 0.86).
TURN_BAR = 0.85
#: ...and beat the best name of every other group by at least this much (else two names are close: none is taken). The
#: clips' true winners led by 0.26-0.60; of the user's 200 names, 2 score 0.75 or more against another group's.
MARGIN = 0.1
#: skill22 (A14), a name given to an act only. A name of :data:`MIN_LETTERS` to ``NEAR_MIN_LETTERS - 1`` letters and a
#: run of as few say the very same sounds (one sound code, one onset): they score the mean of their spelling and 1.0,
#: at most :data:`NAME_RULE_MAX` ("Ark" / "arc" 0.83; "Deezy" / "deji" 0.75: "z" and "j" in one class, "ee" and "i"
#: one vowel).
SHORT_BAR = 1.0
#: ...a vowel heard as one of another class (:data:`_VOWEL_CLASS`) costs this much of an edit in the sound distance:
#: "this card" / Discord (a / o) 0.74 -> 0.78.
NAME_VOWEL_COST = 0.5
#: ...a name said by its first letters (a clip: "insta" for instagram.com): the given name's first word, when the name
#: was written as one word (its parts split where the recognizer joined two: "InstaCol" -> "Insta"), is a form's first
#: letters as written, at least :data:`NEAR_MIN_LETTERS` of them and at least this share of the form's letters; it
#: scores ``(1 + share) / 2`` ("insta" / "instagram" 5 of 9: 0.78). A longer name never: "Photoshop" is no "Photos".
CLIP_SHARE = 0.5
#: ...and a word the recognizer joined to the clip ("InstaCol": "insta khol", the Hindi "open" after the name) has at
#: most this many letters after the joint (measured: "PowerPoint" read as "Power" + "Point" took PowerToys, 0.78).
FUSED_MAX_LETTERS = 3
#: ...the short rule and the clip score at most this, under :data:`TURN_BAR`: they serve a name given to an act (bar
#: :data:`BAR`) and never a reader that keeps only the clearest winners (skill22 fix M3: voice22's near reading,
#: ``as_name`` at :data:`TURN_BAR`, took the plain word "life" for teams.live.com at 0.875 by the short rule; "photo"
#: for Photos scored 0.917 by the clip).
NAME_RULE_MAX = round(TURN_BAR - 0.01, 2)
#: skill22 (A6, contract C14): at most this many of the user's own channels (:data:`CHANNEL`), their room kept inside
#: :data:`OWN_MAX` when they are listed with the apps and sites (skill22 fix L2).
OWN_CHANNELS = 40
#: integ22 fix names-1: a raw address's value swapped for one of the user's channels (:func:`own_url`) and given again
#: within this long opens as written: the second ask of the very same value is a correction ("no, the main MKBHD
#: channel"), so a wrong swap costs one "not that". RAM only (the value's identity, never logged).
CHANNEL_SWAP_KEEP_S = 120.0

#: The kinds of names (code words, logged).
APP, SITE, CHANNEL = "app", "site", "channel"
#: Where a name came from (code words, logged): a program with a window now, the user's timeline, memory's links seen,
#: the Start menu, the user's taste (their channels).
RUNNING, TIMELINE, LINKS, START, TASTE = "running", "timeline", "links", "start", "taste"


@dataclass(frozen=True)
class Own:
    """One of the user's own names: ``name`` as said or opened (an app's display name, a site's host), ``kind``
    (:data:`APP` / :data:`SITE`), ``source`` (:data:`RUNNING`, :data:`TIMELINE`, :data:`LINKS`, :data:`START`),
    ``uses`` (a site's visits, an app's timeline stretches; 0 unknown); for a site, ``app``: the program its pages
    were most often in front in (its display name as memory keeps it), ``""`` when unknown; for an app of the
    timeline, ``key``: its process key ("chrome"), how a running program is named as the timeline names it. skill22:
    for a channel (:data:`CHANNEL`), ``key`` is its page's own value (a handle), ``site`` and ``page`` the site file
    and the page kind it is a page of (``youtube``, ``handle``), ``uses`` how often they picked it."""

    name: str
    kind: str
    source: str
    uses: int = 0
    app: str = ""
    key: str = ""
    site: str = ""
    page: str = ""


@dataclass(frozen=True)
class _Form:
    """How a run of words or a name's form reads for the measure: its letters (doubled once), its sound code and its
    onset (the code's first sound)."""

    letters: str
    code: str
    onset: str


# ---------------------------------------------------------------------------------------------------------------------
# The measure
# ---------------------------------------------------------------------------------------------------------------------


def _latin(word: str) -> str:
    """A word's Latin letters, lower case (:func:`yuki.memory.contacts.romanize`: accents dropped, a Brahmic script
    read letter by letter, digits left out)."""
    from yuki.memory.contacts import romanize

    return romanize(word)


#: The vowels' three classes in the sound code (open, front, back: "a"; "e", "i", "y"; "o", "u"): measured on the
#: user's own turns, one class for every vowel made plain words their names ("click" ~ Clock, "leave" ~ live.com,
#: "open you" ~ openai.com); the three keep "discored" ~ Discord and "excaladra" ~ Excalidraw.
_VOWEL_CLASS = {"a": "A", "e": "I", "i": "I", "y": "I", "o": "U", "u": "U"}
_VOWEL_CODES = frozenset(_VOWEL_CLASS.values())
#: skill22 (A14): "j" is the voiced "ch" (the people index keeps it apart), so it joins ch's class as voicing merges
#: everywhere else in the code (b/p, d/t, g/k, z/s): "Deezy" / "Deji" say the same sounds (final-D deji row).
_CODE_MERGE = {"J": "S"}


def sound_code(letters: str) -> str:
    """The sound code of Latin letters: the people index's consonant classes (:data:`yuki.memory.contacts._SOUND_PAIRS`
    and ``_SOUND``: voicing, aspiration and the s/sh/ch, v/w/f merges; "chr"/"chl" said k; "c" before e/i/y said s;
    skill22: "j" with ch, :data:`_CODE_MERGE`) and the vowels in three classes (:data:`_VOWEL_CLASS`); a lone "h" says
    nothing, nor a "w" that no vowel follows ("draw"); the same class twice in a row counts once. "discord" ->
    ``TISKURT``, "the|scored" with "di" put back -> ``TISKURIT``. A fixed format rule over letters, never a word
    list."""
    from yuki.memory.contacts import _SOUND, _SOUND_PAIRS

    s = "".join(ch for ch in str(letters or "").casefold() if "a" <= ch <= "z")
    out: list[str] = []
    i = 0
    while i < len(s):
        pair = s[i:i + 2]
        if pair == "ch" and s[i + 2:i + 3] in ("r", "l"):
            code = "K"
            i += 2
        elif pair in _SOUND_PAIRS:
            code = _SOUND_PAIRS[pair]
            i += 2
        else:
            ch = s[i]
            i += 1
            if ch in _VOWEL_CLASS:
                code = _VOWEL_CLASS[ch]
            elif ch == "w" and s[i:i + 1] not in _VOWEL_CLASS:
                code = ""          # "w" is a consonant only before a vowel ("draw", "flow": a vowel's end)
            elif ch == "c":
                code = "S" if s[i:i + 1] in ("e", "i", "y") else "K"
            else:
                code = _SOUND.get(ch, "")
        for k in code:
            k = _CODE_MERGE.get(k, k)
            if not out or out[-1] != k:
                out.append(k)
    return "".join(out)


def _once(letters: str) -> str:
    out: list[str] = []
    for ch in letters:
        if not out or out[-1] != ch:
            out.append(ch)
    return "".join(out)


@lru_cache(maxsize=4096)
def _form(letters: str) -> _Form:
    once = _once(letters)
    code = sound_code(once)
    # the onset: the first consonant's class, or V for any vowel ("y" and a lone "h" open as a vowel does)
    return _Form(once, code, "V" if code[:1] in ("A", "I", "U") else code[:1])


def _distance(a: str, b: str) -> float:
    from yuki.memory.contacts import _distance as distance

    return distance(a, b)


def _code_distance(a: str, b: str, vowel_cost: float) -> float:
    """:func:`_distance` of two sound codes where a vowel class put for another costs ``vowel_cost`` of an edit
    (skill22: :data:`NAME_VOWEL_COST`); with 1.0 it is :func:`_distance`. Pure."""
    if vowel_cost >= 1.0 or a == b:
        return _distance(a, b)
    if not a or not b:
        return 1.0
    prev = [float(j) for j in range(len(b) + 1)]
    for i, ca in enumerate(a, 1):
        cur = [float(i)]
        for j, cb in enumerate(b, 1):
            swap = 0.0 if ca == cb else (vowel_cost if ca in _VOWEL_CODES and cb in _VOWEL_CODES else 1.0)
            cur.append(min(prev[j] + 1.0, cur[j - 1] + 1.0, prev[j - 1] + swap))
        prev = cur
    return prev[-1] / max(len(a), len(b))


def _pair(run: _Form, form: _Form) -> float:
    """How alike a run of a name given to an act and a name's form are (0..1): the mean of the spelling and the sound
    similarities, 0 when they open with another sound, are too short or too unlike in length. skill22: a short pair
    (:data:`SHORT_BAR`) is measured by the very same sounds, and a vowel of another class costs
    :data:`NAME_VOWEL_COST` of an edit in the sound distance."""
    la, lb = len(run.letters), len(form.letters)
    if run.onset != form.onset:
        return 0.0
    if MIN_LETTERS <= min(la, lb) and max(la, lb) < NEAR_MIN_LETTERS:
        if not run.code or run.code != form.code:
            return 0.0
        return min((1.0 - _distance(run.letters, form.letters) + SHORT_BAR) / 2.0, NAME_RULE_MAX)
    if min(la, lb) < NEAR_MIN_LETTERS or min(la, lb) < LENGTH_RATIO * max(la, lb):
        return 0.0
    spell = 1.0 - _distance(run.letters, form.letters)
    if run.code and form.code:
        sound = 1.0 - _code_distance(run.code, form.code, NAME_VOWEL_COST)
    else:
        sound = spell
    return (spell + sound) / 2.0


def _clip(head: _Form | None, form: _Form) -> float:
    """skill22 (A14): a name given to an act said by the first letters of one of the user's names, as written
    (:data:`CLIP_SHARE`: "insta" for instagram.com); its score ``(1 + share) / 2``, else 0. ``head``: the given name's
    first word when it was written as one word (:attr:`_Heard.head`)."""
    if head is None:
        return 0.0
    n, m = len(head.letters), len(form.letters)
    if n < NEAR_MIN_LETTERS or n >= m or n < CLIP_SHARE * m or not form.letters.startswith(head.letters):
        return 0.0
    return min((1.0 + n / m) / 2.0, NAME_RULE_MAX)


def is_site(name: Any) -> bool:
    """Whether one of the user's own names is a site (a host: written as a domain name), not an app."""
    try:
        from yuki.skills.connection import domain_like

        text = " ".join(str(name or "").split())
        return bool(text) and " " not in text and domain_like(text)
    except Exception:  # noqa: BLE001
        return False


def site_url(name: Any) -> str:
    """A site of the user's (its host) as the address of its front page (``https://<host>/``); ``""`` for no site."""
    text = " ".join(str(name or "").split()).casefold()
    return f"https://{text}/" if is_site(text) else ""


def _registrable(host: str) -> str:
    try:
        from yuki.memory.moments import registrable_domain

        return str(registrable_domain(host) or host)
    except Exception:  # noqa: BLE001
        return host


@lru_cache(maxsize=4096)
def _name_forms(name: str) -> tuple[_Form, ...]:
    """A name's spoken forms: an app's whole name and the same without a bracketed part ("T3 Code (Beta)" -> "T3
    Code"); a site's registrable name ("excalidraw" for excalidraw.com and app.excalidraw.com, "github" for
    x.github.io). Neither a subdomain nor the suffix is a form (measured: "app", "auth" and "login" made "not in"
    app.notion.com and "open you" auth.openai.com; "claudecom" made "claude code" claude.com). Forms of fewer than
    :data:`MIN_LETTERS` letters are left out. Structure only."""
    from yuki.memory.contacts import spoken_words

    text = " ".join(str(name or "").split())
    out: list[str] = []
    if is_site(text):
        host = text.casefold()
        if host.startswith("www."):
            host = host[4:]
        out.append(_registrable(host).split(".")[0])
    else:
        out.append(text)
        if "(" in text:
            out.append(text.split("(")[0])
    forms: list[_Form] = []
    for raw in out:
        letters = "".join(_latin(w) for _s, w in spoken_words(raw))
        if len(letters) >= MIN_LETTERS:
            f = _form(letters)
            if f not in forms:
                forms.append(f)
    return tuple(forms)


@lru_cache(maxsize=4096)
def _clip_forms(name: str) -> tuple[_Form, ...]:
    """skill22 (A14): the forms of a name that a clip may stand for (:func:`_clip`): those written as one word
    ("instagram", "audiomack", "WhatsApp"), never a name of several words, whose first word said alone is a partial
    name, no clip ("About Java", "Control Panel": the Start menu's own partial match has those)."""
    from yuki.memory.contacts import spoken_words

    text = " ".join(str(name or "").split())
    if is_site(text):
        host = text.casefold()
        raw = _registrable(host[4:] if host.startswith("www.") else host).split(".")[0]
    else:
        raw = text
    if len(spoken_words(raw)) != 1:
        return ()
    return tuple(f for f in _name_forms(name)[:1])


@dataclass(frozen=True)
class _Heard:
    """The heard words as the measure reads them: every run of 1 to :data:`MAX_WORDS` words (its form, its number of
    letters, whether it ends at the last word, whether it is every word) and every (word before, word) pair the tail
    shape may apply to (with whether the word is the last one). skill22: ``head``, the first word's form when the
    words were written as one word (a clip's place: :func:`_clip`), else ``None``."""

    runs: tuple[tuple[_Form, int, bool, bool], ...]
    tails: tuple[tuple[str, str, _Form, bool], ...]
    head: _Form | None = None


def _split_joined(word: str) -> list[str]:
    """skill22 (A14): a word the recognizer wrote as two joined words, split where a small letter meets a capital
    ("InstaCol" -> "Insta", "Col"; "ChatGPT" -> "Chat", "GPT"); a word in one case stays whole ("DISCRD"). A format of
    the text, never a guess at meaning."""
    out: list[str] = []
    start = 0
    for i in range(1, len(word)):
        if word[i].isupper() and word[i - 1].islower():
            out.append(word[start:i])
            start = i
    out.append(word[start:])
    return [w for w in out if w]


def _written_words(text: str) -> list[str]:
    """The heard text's words as written (:func:`yuki.memory.contacts.spoken_words`), a possessive's "'s" left out
    ("Deezy's" -> "Deezy": English writes it with an apostrophe). A format rule only."""
    from yuki.memory.contacts import spoken_words

    spans = spoken_words(text)
    kept: list[str] = []
    for i, (start, word) in enumerate(spans):
        if i and word.casefold() == "s" and start >= 1 and text[start - 1] in "'’" and \
                spans[i - 1][0] + len(spans[i - 1][1]) == start - 1:
            continue        # the possessive's "s" of the word before it
        kept.append(word)
    return kept


@lru_cache(maxsize=256)
def _heard(text: str) -> _Heard:
    written = _written_words(text)
    words = [_latin(w) for w in written]
    last = len(words)
    runs: list[tuple[_Form, int, bool, bool]] = []
    for i in range(len(words)):
        for n in range(1, MAX_WORDS + 1):
            part = words[i:i + n]
            if len(part) < n or any(not w for w in part):
                break        # a word with no letters (digits only) breaks a run
            letters = "".join(part)
            runs.append((_form(letters), len(letters), i + n == last, i == 0 and i + n == last))
    tails = []
    for i in range(1, len(words)):
        before, word = words[i - 1], words[i]
        if before and len(word) >= TAIL_MIN_LETTERS:
            tails.append((before, word, _form(before), i + 1 == last))
    # the clip's place: a name written as one word, or the first part of one the recognizer joined to a short word
    # ("InstaCol" -> "Insta"; measured: "PowerPoint" -> "Power" made PowerToys, so a longer second part is no fused
    # word); the parts never make runs of their own ("PowerPoint" is no "Point")
    parts = _split_joined(written[0]) if len(written) == 1 else []
    if len(parts) > 1 and len(_latin("".join(parts[1:]))) > FUSED_MAX_LETTERS:
        parts = []
    first = _latin(parts[0]) if parts else ""
    head = _form(first) if first else None
    return _Heard(tuple(runs), tuple(tails), head)


def _heard_of(heard: Any) -> _Heard | None:
    """The heard words as the measure reads them (:class:`_Heard`), or ``None`` for none. ``heard``: one text (a name
    given to an act, or a stretch a reader checks as written). kw3: no longer a list of a turn's parts (that served the
    whole-turn mode, gone)."""
    if not isinstance(heard, str):
        return None
    text = " ".join(heard.split())
    return _heard(text) if text else None


def _head_taken(before: str, before_form: _Form, head: str) -> bool:
    """Whether the word heard before a name's tail took the name's head: it ends in the head as written ("porb|ex"),
    or it is the head heard as another short word ("the" for "di": the same consonant sounds, at most one letter
    longer). Measured: a word that merely ends in the head's consonant ("youtube" for "ob") made "youtube studio" OBS
    Studio."""
    if before.endswith(head):
        return True
    consonants = _consonants(sound_code(head))
    return bool(consonants) and len(before) <= len(head) + 1 and _consonants(before_form.code) == consonants


def _consonants(code: str) -> str:
    return "".join(k for k in code if k not in ("A", "I", "U"))


def _no_head(heard: _Heard) -> _Heard:
    """The heard words with no clip's place (:func:`best_match` ``clip=False``)."""
    return _Heard(heard.runs, heard.tails, None) if heard.head is not None else heard


def _measure(heard: _Heard, name: str) -> tuple[float, int]:
    """``(score, letters of the heard run it came from)`` of one name against a name given to an act ("Porbex
    Kaladra", "the scored"; 0 when nothing compares): a form counts as written only for all of its words (a part of
    "microsoft edge" is no name of theirs), and only runs that end at the last word are measured (a word put before
    the name, "the", "my", stays out of it)."""
    best = (0.0, 0)
    for form in _name_forms(name):
        for run, size, at_end, every in heard.runs:
            if not at_end:
                continue
            if run.letters == form.letters:
                got = (1.0 if every else 0.0, size)
            else:
                got = (_pair(run, form), size)
            if got > best:
                best = got
        if best[0] >= 1.0:
            continue
        if heard.head is not None and form in _clip_forms(name):
            got = (_clip(heard.head, form), len(heard.head.letters))
            if got > best:
                best = got
        for before, word, before_form, at_end in heard.tails:
            if not at_end:
                continue
            for j in range(1, TAIL_HEAD + 1):
                head = form.letters[:j]
                if len(form.letters) - j < TAIL_MIN_LETTERS - 1 or len(before) < j or \
                        not _head_taken(before, before_form, head):
                    continue
                got = (_pair(_form(head + word), form), len(word))
                if got > best:
                    best = got
    return best


def score(heard: Any, name: Any, *, as_name: bool = False) -> float:
    """How closely a name given to an act holds one name (0..1; 1.0 when a form of it stands there as written): the
    measure :func:`best_match` ranks by, for a caller comparing a name it was given (a model's pick) with the winner.
    ``heard`` and ``as_name`` as for :func:`best_match` (kw3, contract K2: without ``as_name``, no whole-turn measure:
    0.0). Never raises (0.0)."""
    try:
        if not as_name:
            return 0.0
        got = _heard_of(heard)
        name = " ".join(str(name or "").split())
        if got is None or not name:
            return 0.0
        return round(_measure(got, name)[0], 3)
    except Exception:  # noqa: BLE001
        return 0.0


def same_group(a: Any, b: Any) -> bool:
    """Whether two names are one for the margin: a spoken form of one equals one of the other's, or holds it (both at
    least four letters: "chrome" in "googlechrome", "discord" in "discordcom"). Never raises."""
    try:
        fa = [f.letters for f in _name_forms(" ".join(str(a or "").split()))]
        fb = [f.letters for f in _name_forms(" ".join(str(b or "").split()))]
        return any(x == y or (min(len(x), len(y)) >= 4 and (x in y or y in x)) for x in fa for y in fb)
    except Exception:  # noqa: BLE001
        return False


def literal(heard: Any, name: Any) -> bool:
    """Whether a spoken form of ``name`` stands in the heard words (one text) as written (a run of them, spaces aside):
    the name was heard right, nothing was misheard. Never raises."""
    try:
        got = _heard_of(heard)
        if got is None:
            return False
        runs = {r.letters for r, _n, _e, _w in got.runs}
        return any(f.letters in runs for f in _name_forms(" ".join(str(name or "").split())))
    except Exception:  # noqa: BLE001
        return False


def best_match(heard: Any, names: Iterable[Any], *, literal_ok: bool = True, as_name: bool = False,
               bar: float | None = None, clip: bool = True) -> tuple[str, float, float] | None:
    """The one of ``names`` (the user's own: :func:`own_names`, or a caller's subset) a name given to an act names,
    measured (see the module docstring): ``(name, score, margin)`` when it scores at least the bar and beats the best
    name of every other group by :data:`MARGIN`; else ``None``. ``heard``, with ``as_name``: the name the caller was
    given ("Porbex Kaladra", "the scored": a model's own pick, measured whole, or by its last words, never by a part
    before them; bar :data:`BAR`). kw3 (C5, contract K2, 20 Oct): the whole-turn mode is gone; a call without
    ``as_name`` measures nothing (``None``), so no caller can fill an app or a site from a run of a user's turn.
    ``literal_ok=False``: a name whose form stands in the words as written is left out (a caller that wants only
    mishearings: a name heard right needs no help). ``bar``: another bar for a caller that has one (the voice's near
    reading keeps only winners at :data:`TURN_BAR`). skill22: ``clip=False`` leaves the clip out (:func:`_clip`;
    :func:`own_url`: a channel's own short name is often another channel's whole one, "Sidemen" and
    "SidemenReacts"). Pure; never raises (``None``)."""
    try:
        if not as_name:
            return None
        got = _heard_of(heard)
        if got is None or not got.runs:
            return None
        pool = list(dict.fromkeys(" ".join(str(n or "").split()) for n in names or () if str(n or "").strip()))
        scored: list[tuple[float, int, int, str]] = []
        runs = {r.letters for r, _n, _e, _w in got.runs}
        for i, name in enumerate(pool):
            if not literal_ok and any(f.letters in runs for f in _name_forms(name)):
                continue
            value, size = _measure(got if clip else _no_head(got), name)
            if value > 0.0:
                scored.append((value, size, i, name))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        top = scored[0]
        if top[0] < (bar if bar is not None else BAR):
            return None
        rival = next((t for t in scored[1:] if not same_group(top[3], t[3])), None)
        margin = top[0] - (rival[0] if rival is not None else 0.0)
        if margin < MARGIN:
            return None
        return top[3], round(top[0], 3), round(margin, 3)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------------
# The user's own names
# ---------------------------------------------------------------------------------------------------------------------

_lock = threading.Lock()
_memory_cache: dict[str, tuple[float, list[Own]]] = {}
_running_cache: list[tuple[float, list[str]]] = []
#: integ22 fix names-1: every channel of the user's as written, uncapped (``(site, page, squashed value)``), per
#: database for :data:`OWN_TTL_S`; and the squashed values :func:`own_url` swapped, with when (RAM only).
_written_cache: dict[str, tuple[float, frozenset[tuple[str, str, str]]]] = {}
_swapped: dict[str, float] = {}


def _squash(text: str) -> str:
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _memory_entries(store: Any) -> list[Own]:
    """The apps the user's timeline shows them using (``resolve.known_apps``: their own rows, never Yuki's; most used
    first) and the sites they had in front (memory's links seen, never Yuki's opens; most visited first). Cached per
    database for :data:`OWN_TTL_S`. Never raises (``[]``)."""
    if store is None:
        return []
    cache_key = str(getattr(store, "path", id(store)))
    now = time.monotonic()
    with _lock:
        hit = _memory_cache.get(cache_key)
        if hit is not None and now - hit[0] < OWN_TTL_S:
            return list(hit[1])
    out: list[Own] = []
    try:
        from yuki.skills.resolve import known_apps

        apps = known_apps(store)
        for key, a in sorted(apps.items(), key=lambda kv: -int(kv[1].get("uses") or 0)):
            names = [str(n) for n in a.get("names") or [] if str(n).strip()]
            if names:
                out.append(Own(names[0], APP, TIMELINE, int(a.get("uses") or 0), key=str(key)))
    except Exception:  # noqa: BLE001 - no timeline: the sites alone
        pass
    try:
        out += _site_entries(store.links(how="viewed", limit=LINKS_READ))
    except Exception:  # noqa: BLE001 - no links: the apps alone
        pass
    out += _channel_entries(store)
    with _lock:
        _memory_cache[cache_key] = (now, list(out))
    return out


def _channel_entries(store: Any) -> list[Own]:
    """skill22 (A6, contract C14): the user's own channels (:data:`CHANNEL`): for each taste kind of a site file's page
    with one speakable value (``taste.site_kinds``: YouTube's ``/@handle`` pages), the ones they picked (most picked
    first), then the ones their own app showed (``taste.seen_names``), each named by the display name the site gave it
    (``youtube_feed.display_names``, for that module's own kind) or else by its page's value, at most
    :data:`OWN_CHANNELS`. Never raises (``[]``)."""
    out: list[Own] = []
    try:
        from yuki.skills import taste, youtube_feed

        kinds = taste.site_kinds()
    except Exception:  # noqa: BLE001
        return []
    for kind, site, page, _var in kinds:
        try:
            picked = sorted(taste.top(store, kind, n=10_000), key=lambda t: (-t.n_obs, t.entity_key))
            rows = [(" ".join(str(t.name or "").split()), int(t.n_obs or 0)) for t in picked]
            have = {_squash(n) for n, _u in rows}
            rows += [(n, 0) for n in taste.seen_names(store, kind) if _squash(n) not in have]
            rows = [(n.lstrip("@"), u) for n, u in rows if n.lstrip("@")][:OWN_CHANNELS]
            shown = youtube_feed.display_names(store, [n for n, _u in rows]) \
                if kind == youtube_feed.CHANNEL_KIND else {}
        except Exception:  # noqa: BLE001 - this kind unread: the others
            continue
        for handle, uses in rows:
            out.append(Own(" ".join(str(shown.get(handle) or handle).split()), CHANNEL, TASTE, uses, key=handle,
                           site=site, page=page))
    return out[:OWN_CHANNELS]


def _site_entries(links: Iterable[Any]) -> list[Own]:
    """The user's sites from memory's links seen (pages they had in front; never Yuki's opens): one per registrable
    domain, named by its most visited host ("app.slack.com"), most visited first, at least :data:`SITE_MIN_VISITS`
    visits. Only pages shown in a browser count, measured as memory's links show it: a program whose pages span at
    least :data:`BROWSER_MIN_SITES` registrable domains (an app that draws its own pages, Spotify's or a launcher's,
    shows one or two: those are its insides, no site to open). Each with the program its pages were in front in
    most."""
    visits: dict[str, int] = {}
    via: dict[str, dict[str, int]] = {}
    domains_of: dict[str, set[str]] = {}
    for link in links or ():
        if getattr(link, "by_yuki", False):
            continue
        host = str(getattr(link, "host", "") or "").strip().strip(".").casefold()
        if host.startswith("www."):
            host = host[4:]
        if not is_site(host):
            continue
        n = max(1, int(getattr(link, "visits", 1) or 1))
        visits[host] = visits.get(host, 0) + n
        app = " ".join(str(getattr(link, "app", "") or "").split())
        if app:
            via.setdefault(host, {})[app] = via.get(host, {}).get(app, 0) + n
            domains_of.setdefault(app, set()).add(_registrable(host))
    browsers = {app for app, domains in domains_of.items() if len(domains) >= BROWSER_MIN_SITES}
    by_domain: dict[str, list[tuple[int, str, str]]] = {}
    for host, n in visits.items():
        apps_of = via.get(host) or {}
        app = max(apps_of.items(), key=lambda kv: (kv[1], kv[0]))[0] if apps_of else ""
        if app and app not in browsers:
            continue
        by_domain.setdefault(_registrable(host), []).append((n, host, app))
    out: list[Own] = []
    for _domain, hosts in by_domain.items():
        total = sum(n for n, _h, _a in hosts)
        if total < SITE_MIN_VISITS:
            continue
        _n, host, app = max(hosts, key=lambda t: (t[0], -len(t[1])))
        out.append(Own(host, SITE, LINKS, total, app))
    out.sort(key=lambda e: (-e.uses, e.name))
    return out


def _default_entries() -> list[Own]:
    """:func:`_memory_entries` of this process's own memory when it is already open (the tray's ``MemoryAccess``,
    a bounded call of :data:`STORE_WAIT_S`); ``[]`` when there is none (never opened here: a test, the memory
    service, a tool), it is slow or it fails."""
    module = sys.modules.get("yuki.agent.memory")
    access = getattr(module, "_default", None) if module is not None else None
    if access is None or getattr(access, "_client", None) is None:
        return []
    try:
        got = access.call("own_names", lambda c: _memory_entries(getattr(c, "store", c)), timeout_s=STORE_WAIT_S)
        return list(got or [])
    except Exception:  # noqa: BLE001
        return []


def _running_now() -> list[str]:
    """The programs with a user window now (their image names without ".exe"), each once, top window first; read again
    after :data:`RUNNING_TTL_S`. A read of the window list only. Never raises (``[]``)."""
    now = time.monotonic()
    with _lock:
        if _running_cache and now - _running_cache[0][0] < RUNNING_TTL_S:
            return list(_running_cache[0][1])
    out: list[str] = []
    try:
        from yuki.perception.windows import list_windows

        for w in list_windows():
            image = str(getattr(w, "process_name", "") or "").strip()
            if image.casefold().endswith(".exe"):
                image = image[:-4]
            if image and image.casefold() not in {o.casefold() for o in out}:
                out.append(image)
    except Exception:  # noqa: BLE001
        return []
    with _lock:
        _running_cache[:] = [(now, list(out))]
    return out


def _start_now() -> list[str]:
    """The Start menu's entries as this process last read them (``launch.start_apps_cached``: never a new read here),
    an entry name several entries share left out (no one app's name: "Manual", "Uninstall"). ``[]`` when not read."""
    module = sys.modules.get("yuki.actions.launch")
    if module is None:
        return []
    try:
        apps = module.start_apps_cached() or []
    except Exception:  # noqa: BLE001
        return []
    return start_names(apps)


def start_names(apps: Sequence[Any]) -> list[str]:
    """The Start menu's names that are one entry's own (``[{"name", "appid"}]`` as ``launch.get_start_apps`` gives
    them): a name several entries share is left out ("Manual", "Uninstall": no one app's name). Never raises."""
    try:
        names = [" ".join(str((a.get("name") if hasattr(a, "get") else a) or "").split()) for a in apps or ()]
        counts: dict[str, int] = {}
        for n in names:
            counts[n.casefold()] = counts.get(n.casefold(), 0) + 1
        return [n for n in dict.fromkeys(names) if n and counts.get(n.casefold()) == 1]
    except Exception:  # noqa: BLE001
        return []


def own_entries(store: Any = None, *, running: Sequence[str] | None = None,
                start: Sequence[Any] | None = None, channels: bool = False) -> list[Own]:
    """The user's own apps and sites (:class:`Own`), in use first: the programs with a window now (``running``, else a
    read of the window list), the apps their timeline shows (most used first), the sites they visit (most visited
    first), then the Start menu's entries (``start``: ``[{"name", "appid"}]`` or names, else the list this process last
    read); each name once (letters and digits compared), at most :data:`OWN_MAX`. Each kind keeps at least half of
    that when both have more (in use first; a kind with fewer leaves its room to the other), so a growing timeline
    never cuts the sites. ``store``: the memory to read (else this process's own memory when it is open; else no
    timeline and no sites). A running program is named as the timeline names it when it knows it. skill22
    (``channels``, contract C14): then the user's own channels (:func:`_channel_entries`), at most
    :data:`OWN_CHANNELS`, a name an app or a site already has left out; their room is kept inside :data:`OWN_MAX`
    (the apps and sites share what is left), so the whole list is at most :data:`OWN_MAX`. Never raises."""
    try:
        memory = _memory_entries(store) if store is not None else _default_entries()
        shown: dict[str, str] = {}
        for e in memory:
            if e.kind == APP:
                shown.setdefault(_squash(e.name), e.name)
        keyed = {e.key: e.name for e in memory if e.kind == APP and e.key}
        try:
            from yuki.skills.resolve import app_name
        except Exception:  # noqa: BLE001
            app_name = str.casefold
        listed: list[Own] = []
        seen: set[str] = set()

        def add(entry: Own) -> None:
            key = _squash(entry.name)
            if len(key) >= MIN_LETTERS and key not in seen:
                seen.add(key)
                listed.append(entry)

        for image in (running if running is not None else _running_now()):
            text = " ".join(str(image or "").split())
            add(Own(keyed.get(app_name(text)) or shown.get(_squash(text)) or text, APP, RUNNING))
        for e in memory:
            if e.kind == APP:
                add(e)
        for e in memory:
            if e.kind == SITE:
                add(e)
        for n in (start_names(start) if start is not None else _start_now()):
            add(Own(n, APP, START))
        # skill22 fix L2: the channels' room is kept inside OWN_MAX (a reader that keeps OWN_MAX names, the voice's,
        # cuts from the end: the channels came last and lost 6 of 39 on the DB copy)
        chans: list[Own] = []
        if channels:
            have: set[str] = set()
            for e in [e for e in memory if e.kind == CHANNEL][:OWN_CHANNELS]:
                key = _squash(e.name)
                if len(key) >= MIN_LETTERS and key not in have:
                    have.add(key)
                    chans.append(e)
        room = OWN_MAX - len(chans)
        # names21 review F5: each kind capped on its own within that room, in the listed order (in use first)
        sites = sum(1 for e in listed if e.kind == SITE)
        keep_sites = min(sites, max(room // 2, room - (len(listed) - sites)))
        keep_apps = room - keep_sites
        out: list[Own] = []
        for e in listed:
            if e.kind == SITE and keep_sites > 0:
                keep_sites -= 1
                out.append(e)
            elif e.kind != SITE and keep_apps > 0:
                keep_apps -= 1
                out.append(e)
        taken = {_squash(e.name) for e in out}
        out += [e for e in chans if _squash(e.name) not in taken]
        return out
    except Exception:  # noqa: BLE001
        return []


def own_site_entries(store: Any = None) -> list[Own]:
    """The user's own sites (:class:`Own`, kind :data:`SITE`), most visited first, at most :data:`OWN_MAX`: the site
    measures' own list (resolve's site slots, web_route's open), never cut to make room for apps. ``store`` as for
    :func:`own_entries`; no desk read. Never raises (``[]``)."""
    try:
        memory = _memory_entries(store) if store is not None else _default_entries()
        return [e for e in memory if e.kind == SITE][:OWN_MAX]
    except Exception:  # noqa: BLE001
        return []


def own_names(store: Any = None, *, running: Sequence[str] | None = (), channels: bool = True) -> list[str]:
    """Contracts C11 and C14: the user's own apps and sites as names (:func:`own_entries`: an app's display name, a
    site's host), deduped, in use first; skill22 (C14, A6): then their own channels (a channel's display name:
    "Deji"), at most :data:`OWN_CHANNELS` (``channels=False``: none), at most :data:`OWN_MAX` names in all (the
    channels' room kept inside it). A caller that acts on a name by its kind reads :func:`own_entries` (``.kind``):
    a channel is never an app (skill22 fix M2). skill22 (E5): the
    window list is never read here (``running``: the programs with a window now, when a caller has read them; ``None``
    reads the window list as :func:`own_entries` does): the voice's own-names read takes the apps from memory's
    timeline and the Start list only. Pure apart from the reads; never raises (``[]``)."""
    return [e.name for e in own_entries(store, running=running, channels=channels)]


def own_channel_entries(store: Any = None) -> list[Own]:
    """skill22 (A6): the user's own channels (:class:`Own`, kind :data:`CHANNEL`), most picked first, at most
    :data:`OWN_CHANNELS`, never cut for apps or sites. ``store`` as for :func:`own_entries`; no desk read. Never
    raises (``[]``)."""
    try:
        memory = _memory_entries(store) if store is not None else _default_entries()
        return [e for e in memory if e.kind == CHANNEL][:OWN_CHANNELS]
    except Exception:  # noqa: BLE001
        return []


def _channels_written(store: Any) -> frozenset[tuple[str, str, str]]:
    """integ22 fix names-1: every channel of the user's as written, ``(site, page, squashed value)``: for each taste
    kind of a site page with one speakable value (:func:`_channel_entries`' kinds), all the values they picked and all
    their own app showed, never capped (:data:`OWN_CHANNELS` caps only the match pool). Cached per database for
    :data:`OWN_TTL_S`. Never raises (empty)."""
    if store is None:
        return frozenset()
    cache_key = str(getattr(store, "path", id(store)))
    now = time.monotonic()
    with _lock:
        hit = _written_cache.get(cache_key)
        if hit is not None and now - hit[0] < OWN_TTL_S:
            return hit[1]
    out: set[tuple[str, str, str]] = set()
    try:
        from yuki.skills import taste

        for kind, site, page, _var in taste.site_kinds():
            try:
                values = [t.name for t in taste.top(store, kind, n=10_000)] + list(taste.seen_names(store, kind))
            except Exception:  # noqa: BLE001 - this kind unread: the others
                continue
            out |= {(site, page, _squash(str(v or "").lstrip("@"))) for v in values if _squash(str(v or ""))}
    except Exception:  # noqa: BLE001
        return frozenset()
    got = frozenset(out)
    with _lock:
        _written_cache[cache_key] = (now, got)
    return got


def own_channels_written(store: Any = None) -> frozenset[tuple[str, str, str]]:
    """:func:`_channels_written` on ``store``, else on this process's own memory when it is open (a bounded call of
    :data:`STORE_WAIT_S`, as :func:`_default_entries`). Never raises (empty)."""
    if store is not None:
        return _channels_written(store)
    module = sys.modules.get("yuki.agent.memory")
    access = getattr(module, "_default", None) if module is not None else None
    if access is None or getattr(access, "_client", None) is None:
        return frozenset()
    try:
        got = access.call("own_channels", lambda c: _channels_written(getattr(c, "store", c)), timeout_s=STORE_WAIT_S)
        return got if isinstance(got, frozenset) else frozenset()
    except Exception:  # noqa: BLE001
        return frozenset()


def _digits(text: Any) -> str:
    """The digits of ``text`` in order (a format rule: "NikoOmilana2" -> "2")."""
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def channel_url(entry: Own) -> str:
    """The address of one of the user's channels (its site file's template for its page kind, filled with its value);
    ``""`` when it can't be built. Data only (``yuki/connect/sites``)."""
    try:
        from yuki.connect.templates import fill

        if getattr(entry, "kind", "") != CHANNEL or not entry.site or not entry.page or not entry.key:
            return ""
        return str(fill(entry.site, entry.page, **{_page_var(entry.site, entry.page): entry.key}) or "")
    except Exception:  # noqa: BLE001
        return ""


def _page_var(site: str, page: str) -> str:
    """The one speakable value's name in a site page kind's template (``handle`` for YouTube's ``/@{handle}``)."""
    from yuki.skills import taste

    return next((var for _k, s, p, var in taste.site_kinds() if s == site and p == page), "")


def own_url(url: Any, *, store: Any = None) -> dict[str, Any] | None:
    """skill22 (A6; final-D youtube_channel_deji_arc: "Pull up Deezy's YouTube channel in Ark." became a raw
    ``open_url`` of ``https://www.youtube.com/@Deezy``, a stranger's page, since skill19's heard match runs only inside
    the web skill): a raw address of a site page whose value is a name (a page kind with one speakable value:
    ``taste.site_kinds``, YouTube's ``/@handle``) that is none of the user's own channels of that page as written, but
    is one of them by the same measure as every name given to an act (:func:`best_match` in name mode against their
    channels' display names and values: "Deezy" -> Deji 0.75; no clip: a channel's short name is often another
    channel's whole one, "Sidemen" for "SidemenReacts"). ``{"url": that channel's own address, "name": its name (RAM
    only), "kind": "channel", "how": "heard", "score", "margin", "source": "taste"}``; ``None`` when the address is no
    such page, its value is one of theirs as written, or none measures clearly (the address stays as given). The
    callers log only the code words and numbers. ``store`` as for :func:`own_entries`. Never raises.

    integ22 fixes (S5, names-1): "as written" is checked against every channel of theirs of that page kind, uncapped
    (:func:`own_channels_written`; only the match pool keeps :data:`OWN_CHANNELS`); the address is model-built, so only
    :func:`best_match` measures it (taste's loose sound tier stays with the user's own words); a value whose digits
    are not the channel's ("NikoOmilana2" for NikoOmilana) is another channel, never swapped; and the same value given
    again within :data:`CHANNEL_SWAP_KEEP_S` of its swap opens as written (a correction: a wrong swap costs one "not
    that")."""
    try:
        from urllib.parse import unquote

        from yuki.connect.templates import page_of

        m = page_of(str(url or "").strip())
        if m is None or not m.entity:
            return None
        value = " ".join(unquote(str(m.entity)).split()).lstrip("@")
        entries = [e for e in own_channel_entries(store) if e.site == m.site and e.page == m.kind]
        if not value or not entries:
            return None
        heard = _squash(value)
        if any(heard in (_squash(e.name), _squash(e.key)) for e in entries) \
                or (m.site, m.kind, heard) in own_channels_written(store):
            return None          # one of theirs as written (any of them, past the match pool's cap): nothing to change
        now = time.monotonic()
        with _lock:
            for old in [k for k, t in _swapped.items() if now - t > CHANNEL_SWAP_KEEP_S]:
                _swapped.pop(old, None)
            if heard in _swapped:
                return None      # given again right after its swap: a correction, opened as written
        names: dict[str, Own] = {}
        for e in entries:
            for n in (e.name, e.key):
                names.setdefault(" ".join(str(n or "").split()), e)
        got = best_match(value, list(names), as_name=True, clip=False)
        entry = names.get(got[0]) if got is not None else None
        if got is None or entry is None:
            return None
        if _digits(value) and _digits(value) not in (_digits(entry.key), _digits(entry.name)):
            return None          # the same letters with another number: another channel
        new = channel_url(entry)
        if not new or new.casefold() == str(url or "").strip().casefold():
            return None
        with _lock:
            _swapped[heard] = now
        return {"url": new, "name": entry.name, "kind": CHANNEL, "how": "heard", "source": TASTE, "score": got[1],
                "margin": got[2]}
    except Exception:  # noqa: BLE001 - no measure: the address as given
        return None


def sites_of(entries: Iterable[Own]) -> list[str]:
    """The sites among the user's own names."""
    return [e.name for e in entries or () if getattr(e, "kind", "") == SITE]


def _browser_of(store: Any, host: str) -> str:
    """:func:`site_browser` on one store: web_route's own rule (skill19 R6, ``site_browser`` over
    ``measured_browsers`` read with the site's hosts): the browser its visits are concentrated in, as its display
    name. ``""`` when none is."""
    from yuki.skills.web_route import measured_browsers, site_browser as concentrated

    measured = measured_browsers(store, site_hosts=[host, f"www.{host}"])
    key = concentrated(measured)
    if not key:
        return ""
    return next((str(m.get("name") or key) for m in measured if m.get("key") == key), key)


def site_browser(host: Any, store: Any = None) -> str:
    """The browser the user opens one of their sites in (names21, a launch that took a heard name for a site): the
    one the site's own visits are concentrated in (web_route's rule for the web skill, :func:`_browser_of`), read on
    ``store`` or else this process's open memory (a bounded call); ``""`` when none is (the caller's default). Never
    raises."""
    text = " ".join(str(host or "").split()).casefold()
    if text.startswith("www."):
        text = text[4:]
    if not is_site(text):
        return ""
    try:
        if store is not None:
            return _browser_of(store, text)
        module = sys.modules.get("yuki.agent.memory")
        access = getattr(module, "_default", None) if module is not None else None
        if access is None or getattr(access, "_client", None) is None:
            return ""
        got = access.call("site_browser", lambda c: _browser_of(getattr(c, "store", c), text), timeout_s=STORE_WAIT_S)
        return str(got or "")
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["APP", "BAR", "TURN_BAR", "BROWSER_MIN_SITES", "CHANNEL", "CHANNEL_SWAP_KEEP_S", "CLIP_SHARE",
           "LENGTH_RATIO", "LINKS", "MARGIN", "MAX_WORDS", "MIN_LETTERS", "NAME_RULE_MAX", "NAME_VOWEL_COST",
           "NEAR_MIN_LETTERS", "OWN_CHANNELS", "OWN_MAX", "Own",
           "RUNNING", "SHORT_BAR", "SITE", "SITE_MIN_VISITS", "START", "TAIL_HEAD", "TAIL_MIN_LETTERS", "TASTE",
           "TIMELINE", "best_match", "channel_url", "is_site", "literal", "own_channel_entries", "own_channels_written",
           "own_entries",
           "own_names", "own_site_entries", "own_url", "same_group", "score", "site_browser", "site_url", "sites_of",
           "sound_code", "start_names"]
