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

Two modes. A whole turn (the default, contract C11's use): every run, bar :data:`TURN_BAR`. A name given to an act
(``as_name``: a launch's app name, a site value, an app slot's value): the name whole, or the runs that end at its last
word (never a part before them: "microsoft edge" is no teams.microsoft.com), bar :data:`BAR`.

**The winner** must reach the bar and beat the best name of every other group by :data:`MARGIN`. Names one of which
holds the other ("Chrome" in "Google Chrome") or with the same spoken form (the Claude app and claude.ai) are one
group: within it the higher score wins, then the longer run of heard words, then the name listed first (an app listed
before a site of the same name wins a tie). Otherwise ``None``: today's behaviour, unchanged.

Calibrated on 19 Oct on the DB copy (200 own names: 123 apps, 77 sites), the clips' transcripts of final-B/final-D and
the user's 1008 turns; the numbers are at each constant and in docs/research/final-test-2026-10-19/builds/names21.md.

Contract C11 (voice21): :func:`own_names` and :func:`best_match`, both pure apart from the store read, never raising.
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
#: ...a whole turn (the default): this. On the user's 1008 turns the non-literal winners between 0.75 and 0.85 were
#: plain words ("click" ~ Clock 0.78, "not in" ~ notion.com 0.83, "open a" ~ openai.com 0.83, "What's up" ~ WhatsApp
#: 0.85-, 13 turns); at 0.85, 6 winners (8 with names heard right left out), each a real mishearing ("spot if"
#: Spotify 0.93, "dev mefex" devmafex.com 0.88, "discard call" Discord 0.86). "Open Porbex Kaladra." (0.80) and "GIF
#: hub cold bro." (0.82) fall under it as whole turns; their names given to an act take the name bar.
TURN_BAR = 0.85
#: ...and beat the best name of every other group by at least this much (else two names are close: none is taken). The
#: clips' true winners led by 0.26-0.60; of the user's 200 names, 2 score 0.75 or more against another group's.
MARGIN = 0.1

#: The kinds of names (code words, logged).
APP, SITE = "app", "site"
#: Where a name came from (code words, logged): a program with a window now, the user's timeline, memory's links seen,
#: the Start menu.
RUNNING, TIMELINE, LINKS, START = "running", "timeline", "links", "start"


@dataclass(frozen=True)
class Own:
    """One of the user's own names: ``name`` as said or opened (an app's display name, a site's host), ``kind``
    (:data:`APP` / :data:`SITE`), ``source`` (:data:`RUNNING`, :data:`TIMELINE`, :data:`LINKS`, :data:`START`),
    ``uses`` (a site's visits, an app's timeline stretches; 0 unknown); for a site, ``app``: the program its pages
    were most often in front in (its display name as memory keeps it), ``""`` when unknown; for an app of the
    timeline, ``key``: its process key ("chrome"), how a running program is named as the timeline names it."""

    name: str
    kind: str
    source: str
    uses: int = 0
    app: str = ""
    key: str = ""


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


def sound_code(letters: str) -> str:
    """The sound code of Latin letters: the people index's consonant classes (:data:`yuki.memory.contacts._SOUND_PAIRS`
    and ``_SOUND``: voicing, aspiration and the s/sh/ch, v/w/f merges; "chr"/"chl" said k; "c" before e/i/y said s)
    and the vowels in three classes (:data:`_VOWEL_CLASS`); a lone "h" says nothing, nor a "w" that no vowel follows
    ("draw"); the same class twice in a row counts once. "discord" -> ``TISKURT``, "the|scored" with "di" put back ->
    ``TISKURIT``. A fixed format rule over letters, never a word list."""
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


def _pair(run: _Form, form: _Form) -> float:
    """How alike a run of heard words and a name's form are (0..1): the mean of the spelling and the sound
    similarities, 0 when they open with another sound, are too short or too unlike in length."""
    la, lb = len(run.letters), len(form.letters)
    if min(la, lb) < NEAR_MIN_LETTERS or min(la, lb) < LENGTH_RATIO * max(la, lb) or run.onset != form.onset:
        return 0.0
    spell = 1.0 - _distance(run.letters, form.letters)
    sound = 1.0 - _distance(run.code, form.code) if run.code and form.code else spell
    return (spell + sound) / 2.0


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


@dataclass(frozen=True)
class _Heard:
    """The heard words as the measure reads them: every run of 1 to :data:`MAX_WORDS` words (its form, its number of
    letters, whether it ends at the last word, whether it is every word) and every (word before, word) pair the tail
    shape may apply to (with whether the word is the last one)."""

    runs: tuple[tuple[_Form, int, bool, bool], ...]
    tails: tuple[tuple[str, str, _Form, bool], ...]


@lru_cache(maxsize=256)
def _heard(text: str) -> _Heard:
    from yuki.memory.contacts import spoken_words

    words = [_latin(w) for _s, w in spoken_words(text)]
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
    return _Heard(tuple(runs), tuple(tails))


def _heard_of(heard: Any) -> _Heard | None:
    """The heard words as the measure reads them (:class:`_Heard`), or ``None`` for none. ``heard``: a text, or a list
    or tuple of texts, the parts of one turn (a caller's cut: resolve's ``_outside``, the turn outside the message's
    own text): each part read alone, so a run never spans two parts. A part's runs end at that part's own last word."""
    if isinstance(heard, (list, tuple)):
        parts = [_heard(t) for t in (" ".join(str(p or "").split()) for p in heard if isinstance(p, str)) if t]
        if not parts:
            return None
        return _Heard(tuple(r for h in parts for r in h.runs), tuple(t for h in parts for t in h.tails))
    text = " ".join(str(heard or "").split())
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


def _measure(heard: _Heard, name: str, as_name: bool = False) -> tuple[float, int]:
    """``(score, letters of the heard run it came from)`` of one name against the heard words (0 when nothing
    compares). ``as_name``: the heard words are a name given to an act ("Porbex Kaladra", "the scored"): a form counts
    as written only for all of them (a part of "microsoft edge" is no name of theirs), and only runs that end at the
    last word are measured (a word put before the name, "the", "my", stays out of it)."""
    best = (0.0, 0)
    for form in _name_forms(name):
        for run, size, at_end, every in heard.runs:
            if as_name and not at_end:
                continue
            if run.letters == form.letters:
                got = (1.0 if (every or not as_name) else 0.0, size)
            else:
                got = (_pair(run, form), size)
            if got > best:
                best = got
        if best[0] >= 1.0:
            continue
        for before, word, before_form, at_end in heard.tails:
            if as_name and not at_end:
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
    """How closely the heard words hold one name (0..1; 1.0 when a form of it stands there as written): the measure
    :func:`best_match` ranks by, for a caller comparing a name it was given (a model's pick) with the winner.
    ``heard`` and ``as_name`` as for :func:`best_match`. Never raises (0.0)."""
    try:
        got = _heard_of(heard)
        name = " ".join(str(name or "").split())
        if got is None or not name:
            return 0.0
        return round(_measure(got, name, as_name)[0], 3)
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
    """Whether a spoken form of ``name`` stands in the heard words as written (a run of them, spaces aside): the name
    was heard right, nothing was misheard. ``heard`` as for :func:`best_match`. Never raises."""
    try:
        got = _heard_of(heard)
        if got is None:
            return False
        runs = {r.letters for r, _n, _e, _w in got.runs}
        return any(f.letters in runs for f in _name_forms(" ".join(str(name or "").split())))
    except Exception:  # noqa: BLE001
        return False


def best_match(heard: Any, names: Iterable[Any], *, literal_ok: bool = True, as_name: bool = False,
               bar: float | None = None) -> tuple[str, float, float] | None:
    """The one of ``names`` (the user's own: :func:`own_names`, or a caller's subset) the heard words name, measured
    (see the module docstring): ``(name, score, margin)`` when it scores at least the bar and beats the best name of
    every other group by :data:`MARGIN`; else ``None``. ``heard``: a whole turn ("Bring up the scored.", bar
    :data:`TURN_BAR`) or, with ``as_name``, a name given to an act ("Porbex Kaladra", "the scored": measured whole, or
    by its last words, never by a part before them; bar :data:`BAR`). ``heard`` may also be a list or tuple of texts,
    the parts of one turn a caller cut (resolve: the turn outside the message's own text): a run never spans two
    parts. ``literal_ok=False``: a name whose form stands in the words as written is left out (a caller that wants
    only mishearings: a name heard right needs no help). ``bar``: another bar (a caller with a second signal: a
    model's own pick that the turn's winner agrees with is confirmed at :data:`BAR`). Pure; never raises
    (``None``)."""
    try:
        got = _heard_of(heard)
        if got is None or not got.runs:
            return None
        pool = list(dict.fromkeys(" ".join(str(n or "").split()) for n in names or () if str(n or "").strip()))
        scored: list[tuple[float, int, int, str]] = []
        runs = {r.letters for r, _n, _e, _w in got.runs}
        for i, name in enumerate(pool):
            if not literal_ok and any(f.letters in runs for f in _name_forms(name)):
                continue
            value, size = _measure(got, name, as_name)
            if value > 0.0:
                scored.append((value, size, i, name))
        if not scored:
            return None
        scored.sort(key=lambda t: (-t[0], -t[1], t[2]))
        top = scored[0]
        if top[0] < (bar if bar is not None else BAR if as_name else TURN_BAR):
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
    with _lock:
        _memory_cache[cache_key] = (now, list(out))
    return out


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
                start: Sequence[Any] | None = None) -> list[Own]:
    """The user's own apps and sites (:class:`Own`), in use first: the programs with a window now (``running``, else a
    read of the window list), the apps their timeline shows (most used first), the sites they visit (most visited
    first), then the Start menu's entries (``start``: ``[{"name", "appid"}]`` or names, else the list this process last
    read); each name once (letters and digits compared), at most :data:`OWN_MAX`. Each kind keeps at least half of
    that when both have more (in use first; a kind with fewer leaves its room to the other), so a growing timeline
    never cuts the sites. ``store``: the memory to read (else this process's own memory when it is open; else no
    timeline and no sites). A running program is named as the timeline names it when it knows it. Never raises."""
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
        # names21 review F5: each kind capped on its own within OWN_MAX, in the listed order (in use first)
        sites = sum(1 for e in listed if e.kind == SITE)
        keep_sites = min(sites, max(OWN_MAX // 2, OWN_MAX - (len(listed) - sites)))
        keep_apps = OWN_MAX - keep_sites
        out: list[Own] = []
        for e in listed:
            if e.kind == SITE and keep_sites > 0:
                keep_sites -= 1
                out.append(e)
            elif e.kind != SITE and keep_apps > 0:
                keep_apps -= 1
                out.append(e)
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


def own_names(store: Any = None) -> list[str]:
    """Contract C11: the user's own apps and sites as names (:func:`own_entries`: an app's display name, a site's
    host), deduped, at most :data:`OWN_MAX`, in use first. Pure apart from the reads; never raises (``[]``)."""
    return [e.name for e in own_entries(store)]


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


__all__ = ["APP", "BAR", "TURN_BAR", "BROWSER_MIN_SITES", "LENGTH_RATIO", "LINKS", "MARGIN", "MAX_WORDS", "MIN_LETTERS",
           "NEAR_MIN_LETTERS", "OWN_MAX", "Own", "RUNNING",
           "SITE", "SITE_MIN_VISITS", "START", "TAIL_HEAD", "TAIL_MIN_LETTERS", "TIMELINE", "best_match", "is_site",
           "literal", "own_entries", "own_names", "own_site_entries", "same_group", "score", "site_browser", "site_url",
           "sites_of",
           "sound_code",
           "start_names"]
