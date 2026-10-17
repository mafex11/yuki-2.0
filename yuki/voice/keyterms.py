"""Names and terms speech recognition should expect, from memory (docs/VOICE.md §9, v3.5; lKeys, live test 1).

Live 2026-09-26 Deepgram heard two of the user's contacts' names as other words (one as two
English words), the thinker then messaged the wrong name and memory kept the misheard one as a
person (the real cases are in docs/, not in code). Deepgram
Flux takes keyterms (prompting: the words are boosted in recognition). They come from what
memory already holds as structured facts, never from a list in code. Read once per conversation, bounded,
through :class:`~yuki.agent.memory.MemoryAccess` (``yuki.memory.api.MemoryClient`` underneath).

Report 88 (learn from one correction): a name the user corrected once is an alias in memory at once, so its right
name leads the next conversation's terms; :func:`vocabulary` / :func:`speech_hotwords` is the one list both
recognisers use (Deepgram's keyterms, the local model's hotwords); the heard forms of Yuki's own name memory
learned are read in the same call (:func:`name_forms`) for the fast path.

**What the user uses lately** (lKeys, live test 1 of 17 Oct, problem 5; the real cases are in
docs/research/audit-2026-10-09/builds/lKeys.md, not in code). The terms came only from the portrait's facts, so they
were mostly old project names, while the words the user says to Yuki every day (his playlists, the channels he
watches, the sites and apps he opens, a friend's nickname) were missing, and each of those misheard broke a task.
:func:`usage_terms` builds the list from the user's own recent use, each source ranked by its own recency x count,
the sources taken in turn (best of each first):

* the right names of the user's corrections of the last :data:`CORRECTION_LEAD_DAYS`, newest first (they lead:
  report 88; a corrected name is one recognition is known to get wrong); older ones come back with the person;
* the people the user deals with (:mod:`yuki.memory.contacts`: their routes' recency-weighted own messages plus a
  share of theirs), each by their name, and by another name memory holds for them as a person's own (a heard form
  that is also a portrait person fact's subject: a nickname the user says, not a mishearing);
* the names of their tastes (:mod:`yuki.skills.taste`: playlists, YouTube channels; the decayed pick count), picked
  ones first, then names their own app showed;
* their learned skills' sites, services and apps (a decayed count of the times they did it, and of Yuki's runs);
* the apps and sites they had in front lately (the timeline: times in front, halved every :data:`HALF_LIFE_DAYS`). A
  site is named as its own pages write it: the run of title words that are the labels of its host, capitalised
  ("chatgpt.com" -> "ChatGPT", "docs.google.com" -> "Google Docs"), an identity of the words, never a list of sites;
* the user's own sites and accounts memory confirmed (``MemoryClient.user_resources``);
* then, while there is room, older corrections, the portrait's people and the names of their work and interests,
  newest first.

**Never a heard form** (it would teach recognition the mistake): every heard form of ``name_aliases`` is left out
("Barun" for Varun), unless memory holds that very word as a person's own name (above), and so are a resource's words
that were only heard, and a taste's "said" words (what recognition wrote). A term must be something recognition can
write and a person can say: Latin or Devanagari letters (the recognition languages; a name in another script is said
some other way, a resource of its own when memory confirmed it), not a typed handle (a digit right after a letter
in a word longer than :data:`HANDLE_MIN_CHARS`), short (:data:`MAX_WORDS`, :data:`MAX_CHARS`). Deepgram takes at
most 100 keyterms and 500 tokens across them (its docs: "the most important 20-50 terms"); the list stays within
``limit`` terms and :data:`MAX_TOTAL_CHARS` characters, and the pipeline's screen names fill up to its own total. A
name the user writes in lower case is given with a capital first letter (:func:`as_keyterm`: how Deepgram is
prompted with a proper noun; measured).
"""

from __future__ import annotations

import re
import time
import unicodedata
from typing import Any, Callable, Iterable

#: Portrait fact kinds whose subject is a name: people first (the names that were misheard).
NAME_KINDS = ("person", "work", "interest")
#: A subject is used as a term only when it is short (a name, not a description): at most this many
#: words and characters. A measure of the value, not a list of words.
MAX_WORDS = 3
MAX_CHARS = 32
#: Characters across the whole memory list at most (Deepgram: 500 tokens across all keyterms, the screen names
#: included; ~2-4 characters a token for names, so this keeps the list and up to 10 screen names well under it).
MAX_TOTAL_CHARS = 520
#: The timeline read for the apps and sites the user had in front lately, and how fast a use fades (half after
#: this many days): the user's present, not last month's (an app left two weeks ago weighs a quarter).
USAGE_DAYS = 14.0
HALF_LIFE_DAYS = 7.0
#: Sites named per conversation at most (each needs its recent titles decrypted, in RAM only), and titles read per site.
MAX_SITES = 30
TITLES_PER_SITE = 12
#: Terms each source offers at most (the turns stop at ``limit`` long before).
SOURCE_DEPTH = 40
#: A word longer than this with a digit right after a letter is a handle typed ("name2nd", "name99"), not a name said
#: (a name with a number says it apart, or is this short: "T3"). Measured: two such handles in the list made heard
#: words near-names of them ("Bring" ~ a handle, the talker would have doubted a plain command).
HANDLE_MIN_CHARS = 3
#: A correction's right name leads the terms for this many days (report 88: the next conversations after it); later
#: it is ranked as the person is (the people the user deals with), else it fills what room is left.
CORRECTION_LEAD_DAYS = 7.0


def _usable(subject: str) -> str:
    text = " ".join(str(subject or "").split())
    if not text or len(text) > MAX_CHARS or len(text.split()) > MAX_WORDS:
        return ""
    return text


def _is_name(text: str) -> bool:
    """A work or interest subject that is a name (every word starts with a capital or a digit: "Deepgram",
    "ElevenLabs", "Corvana"), not a description ("Speech transcription setup"). A form test, no word list."""
    return bool(text) and all(word[:1].isupper() or word[:1].isdigit() for word in text.split())


def terms_from_facts(facts: list[Any], loops: list[Any] = (), limit: int = 50) -> list[str]:
    """Keyterms from portrait facts (``kind``, ``subject``), people first.

    Open loops are not used (``loops`` is ignored): their subjects are extracted per loop, and live one
    was a misheard contact's name, which as a keyterm would teach recognition the mistake."""
    order = {kind: i for i, kind in enumerate(NAME_KINDS)}
    ranked = sorted((f for f in facts if getattr(f, "kind", None) in order),
                    key=lambda f: order[f.kind])
    out: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        term = _usable(value)
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)

    del loops
    for fact in ranked:
        if fact.kind == "person":
            add(fact.subject)
    for fact in ranked:
        if fact.kind != "person" and _is_name(_usable(fact.subject)):
            add(fact.subject)
    return out[:limit]


# ---------------------------------------------------------------------------------------------------------------
# What the user uses lately (lKeys)
# ---------------------------------------------------------------------------------------------------------------

def speakable(term: str) -> bool:
    """Whether recognition can write ``term`` and a person can say it: its letters Latin or Devanagari (the
    recognition languages), at least two of them, and no word longer than :data:`HANDLE_MIN_CHARS` with a digit right
    after a letter (a typed handle). A test of the characters, never of the meaning."""
    letters = [ch for ch in term if ch.isalpha()]
    if len(letters) < 2:
        return False
    for ch in letters:
        name = unicodedata.name(ch, "")
        if not (name.startswith("LATIN") or name.startswith("DEVANAGARI")):
            return False
    for word in term.split():
        chars = [ch for ch in word if ch.isalnum()]
        if len(chars) > HANDLE_MIN_CHARS and any(a.isalpha() and b.isdigit() for a, b in zip(chars, chars[1:])):
            return False
    return True


def as_keyterm(term: str) -> str:
    """``term`` written as recognition is prompted best: a name written all in lower case gets a capital first letter
    (Deepgram's keyterm guidance: proper nouns capitalised; measured on the live test's clips with flux-general-en, the
    lower-case forms of two of the user's names were not recognised and the capitalised ones were). Only the case of
    the first letter changes; every reader matches names with case aside."""
    return term[:1].upper() + term[1:] if term and term == term.lower() and term[:1].isalpha() else term


def _fade(age_s: float) -> float:
    """A use's weight by its age: half after :data:`HALF_LIFE_DAYS`."""
    return 0.5 ** (max(0.0, float(age_s)) / 86400.0 / HALF_LIFE_DAYS)


_WORDS = re.compile(r"[^\W_]+")
_BRACKETED = re.compile(r"\s*[\(\[][^\)\]]*[\)\]]")


def _host_labels(site: str) -> set[str]:
    """The labels of a host or site key that may be its name: every label but the last (the top-level domain:
    "amazon.in" -> {"amazon"}); a site key without dots is its own label ("youtube")."""
    parts = [p for p in str(site or "").casefold().strip(".").split(".") if p]
    return set(parts[:-1] if len(parts) > 1 else parts)


def site_name(site: str, texts: Iterable[str]) -> str:
    """How ``site``'s own pages (or a skill named after it) write its name: the run of consecutive words of ``texts``
    that are labels of the host, case aside, each capitalised as a name (:func:`_is_name`), the run found most often
    (then the longest). "chatgpt.com" + "ChatGPT" -> "ChatGPT"; "meet.google.com" + "Google Meet" -> "Google Meet";
    "amazon.in" + "Amazon.in: Online Shopping" -> "Amazon". ``""`` when no text writes it. Identity of words only."""
    labels = _host_labels(site)
    if not labels:
        return ""
    counts: dict[str, int] = {}

    def close(run: list[str]) -> None:
        if run:
            found = " ".join(run)
            counts[found] = counts.get(found, 0) + 1
        run.clear()

    for text in texts:
        text = str(text or "")
        run: list[str] = []
        end = 0
        for m in _WORDS.finditer(text):
            if run and text[end:m.start()].strip():   # a run is words apart only by spaces ("Mail - Google" is two)
                close(run)
            end = m.end()
            if m.group().casefold() in labels and _is_name(m.group()):   # a label written as a name
                run.append(m.group())
            else:
                close(run)
        close(run)
    if not counts:
        return ""
    return max(counts.items(), key=lambda kv: (kv[1], len(kv[0])))[0]


def _app_name(app: str) -> str:
    """An app's display name as said: without a bracketed part ("T3 Code (Alpha)" -> "T3 Code")."""
    return " ".join(_BRACKETED.sub("", str(app or "")).split())


def _dec(store: Any, ciphertext: Any) -> str:
    try:
        return " ".join(str(store._dec(ciphertext) or "").split()) if ciphertext else ""
    except Exception:  # noqa: BLE001 - a row the key can't read is skipped
        return ""


class _Places:
    """The apps and sites the user had in front in the last :data:`USAGE_DAYS` (the timeline, the user's own time:
    rows Yuki made are left out), each weighed by how many times they were in front (timeline rows: a count, so a
    game left running for hours is not a hundred uses), each time halved every :data:`HALF_LIFE_DAYS`; the sites'
    names from their own recent titles (decrypted in RAM, never kept or logged)."""

    def __init__(self, store: Any, now: float) -> None:
        self.store, self.now = store, now
        self.app_of: dict[str, str] = {}          # process key -> display name
        self.apps: dict[str, float] = {}
        self.hosts: dict[str, float] = {}
        self.names: dict[str, str] = {}           # host -> its name as its pages write it
        since = now - USAGE_DAYS * 86400.0
        try:
            rows = store._query(
                "SELECT app, process, host, CAST((? - ended_at) / 86400 AS INTEGER), COUNT(*) "
                "FROM timeline WHERE started_at > ? AND state = 'present' AND coalesce(by_yuki, 0) = 0 "
                "AND coalesce(app, '') != '' GROUP BY app, process, host, 4", (now, since))
        except Exception:  # noqa: BLE001
            rows = []
        for app, process, host, age_d, times in rows:
            w = float(times or 0) * 0.5 ** (max(0, int(age_d or 0)) / HALF_LIFE_DAYS)
            name = _app_name(app)
            if process and name:
                self.app_of.setdefault(str(process).casefold(), name)
            if name:
                self.apps[name] = self.apps.get(name, 0.0) + w
            if host:
                self.hosts[str(host)] = self.hosts.get(str(host), 0.0) + w
        top = [h for h, _w in sorted(self.hosts.items(), key=lambda kv: -kv[1])][:MAX_SITES]
        titles: dict[str, list[str]] = {h: [] for h in top}
        if top:
            try:
                marks = ",".join("?" * len(top))
                found = store._query(f"SELECT host, title_ciphertext FROM timeline WHERE started_at > ? AND host IN "
                                     f"({marks}) AND title_ciphertext IS NOT NULL ORDER BY started_at DESC LIMIT 6000",
                                     (since, *top))
            except Exception:  # noqa: BLE001
                found = []
            for host, ct in found:
                got = titles.get(host)
                if got is not None and len(got) < TITLES_PER_SITE:
                    title = _dec(store, ct)
                    if title:
                        got.append(title)
        for host, texts in titles.items():
            name = site_name(host, texts)
            if name:
                self.names[host] = name

    def name_of_site(self, site: str, texts: Iterable[str] = ()) -> str:
        """A skill's bound site ("chatgpt.com", "youtube") named: by its own words (``texts``: the skill's name), else
        as the pages of a host it is (or is a label of) write it."""
        name = site_name(site, texts)
        if name:
            return name
        site = str(site or "").casefold().strip(".")
        if not site:
            return ""
        for host in sorted(self.names, key=lambda h: -self.hosts.get(h, 0.0)):
            h = host.casefold()
            if h == site or h.endswith("." + site) or ("." not in site and site in h.split(".")):
                return self.names[host]
        return ""

    def ranked(self) -> list[str]:
        """Apps and named sites together, most used lately first."""
        both = [(w, name) for name, w in self.apps.items()]
        both += [(w, self.names[h]) for h, w in self.hosts.items() if h in self.names]
        return [name for _w, name in sorted(both, key=lambda x: -x[0])]


def _corrected(store: Any, now: float) -> tuple[list[str], list[str]]:
    """The right names of the user's corrections, newest first (Yuki's own name forms are not corrections here):
    those of the last :data:`CORRECTION_LEAD_DAYS` (they lead), and the older ones (they come back through the
    people the user deals with, else fill what room is left)."""
    try:
        rows = list(reversed(store.aliases()))
    except Exception:  # noqa: BLE001
        return [], []
    fresh = [a["name"] for a in rows if now - float(a.get("at") or 0) <= CORRECTION_LEAD_DAYS * 86400.0]
    return fresh, [a["name"] for a in rows if a["name"] not in fresh]


def _people(store: Any, now: float, own_names: set[str]) -> list[str]:
    """The people the user deals with lately (:func:`yuki.memory.contacts.directory`'s order: their routes' own-message
    weight plus a share of theirs), each by their name and by another name memory holds as a person's own."""
    try:
        from yuki.memory import contacts as C

        ranked = []
        for c in C.people(store):
            try:
                rk = C.rank_routes(store, c, now=now)
                w = sum(r.score + C.THEIRS_WEIGHT * r.weight_theirs for r in rk.routes)
            except Exception:  # noqa: BLE001
                continue
            if w > 0:
                ranked.append((w, c))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for _w, c in sorted(ranked, key=lambda x: (-x[0], str(x[1].name).casefold())):
        out.append(c.name)
        out += [a["name"] for a in c.aliases if str(a.get("name") or "").casefold() in own_names]
    return out


def _tastes(store: Any) -> list[str]:
    """The names of the user's tastes: picked ones by their decayed pick count, then the names their own app showed,
    newest first. What the user *said* for one (``@said`` rows) is what recognition wrote, never a term."""
    try:
        from yuki.skills.taste import SAID_SUFFIX, SEEN_SOURCE

        rows = store._query("SELECT kind, source, name_ciphertext, n_obs, last_at FROM tastes")
    except Exception:  # noqa: BLE001
        return []
    picked, shown = [], []
    for kind, source, name_ct, n_obs, last_at in rows:
        if str(kind or "").endswith(SAID_SUFFIX):
            continue
        name = _dec(store, name_ct)
        if not name:
            continue
        if source == SEEN_SOURCE or not n_obs:
            shown.append((float(last_at or 0.0), name))
        else:
            picked.append((float(n_obs), name))
    return [n for _w, n in sorted(picked, key=lambda x: -x[0])] + [n for _t, n in sorted(shown, key=lambda x: -x[0])]


def _skills(store: Any, now: float, places: _Places) -> list[str]:
    """The user's live learned skills' names for things (a bound site, a bound value that is a name: "Google Meet",
    and the app they did it in), most done lately first: the times the learner saw them do it and Yuki's runs,
    each faded by its last time."""
    import json

    try:
        from yuki.skills.catalog import LIVE_STATES

        marks = ",".join("?" * len(LIVE_STATES))
        rows = store._query(f"SELECT app, uses, last_used_at, name_ciphertext, spec_ciphertext, provenance FROM skills "
                            f"WHERE origin = 'learned' AND deleted_at IS NULL AND retired_at IS NULL AND state IN "
                            f"({marks})", tuple(LIVE_STATES))
    except Exception:  # noqa: BLE001
        return []
    site_keys = {h.casefold().split(".")[-2] if "." in h else h.casefold() for h in places.hosts}
    ranked: list[tuple[float, list[str]]] = []
    for app, uses, last_used, name_ct, spec_ct, prov in rows:
        try:
            p = json.loads(prov or "{}")
            spec = json.loads(_dec(store, spec_ct) or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(p, dict) or not isinstance(spec, dict):
            continue
        w = (float(p.get("n") or 0) * _fade(now - float(p.get("last_at") or 0))
             + float(uses or 0) * _fade(now - float(last_used or 0)))
        label = _dec(store, name_ct)
        names: list[str] = []
        bound = spec.get("bound") if isinstance(spec.get("bound"), dict) else {}
        for value in bound.values():
            value = " ".join(str(value or "").split())
            if "." in value or value.casefold() in site_keys:   # a host or a site's key: named as its pages write it
                names.append(places.name_of_site(value, [label]))
            elif _is_name(value):                               # a value that is a name ("Google Meet")
                names.append(value)
        # the program the learner saw them use (the provenance's app: "chrome"; the row's app is its connection)
        for key in (p.get("app"), app):
            got = places.app_of.get(str(key or "").casefold(), "")
            if got:
                names.append(got)
                break
        names = [n for n in names if n]
        if names and w > 0:
            ranked.append((w, names))
    return [n for _w, names in sorted(ranked, key=lambda x: -x[0]) for n in names]


def _resources(client: Any) -> list[str]:
    """The words of the user's own sites and accounts whose spelling memory confirmed (never one only heard)."""
    get = getattr(client, "user_resources", None)
    try:
        return [t for r in (get() if callable(get) else []) if r.get("confirmed") for t in r.get("keyterms") or ()]
    except Exception:  # noqa: BLE001
        return []


def _portrait(store: Any) -> tuple[list[str], set[str]]:
    """The portrait's people, then the names of their work and interests, each newest first; and the people's names
    (casefolded): the names memory holds as a person's own."""
    try:
        facts = list(store.portrait_facts(NAME_KINDS, include_corrections=False))
    except Exception:  # noqa: BLE001
        return [], set()
    facts.sort(key=lambda f: -float(getattr(f, "valid_from", 0) or 0))
    people = [f.subject for f in facts if f.kind == "person" and _usable(f.subject)]
    names = [f.subject for f in facts if f.kind != "person" and _is_name(_usable(f.subject))]
    return people + names, {" ".join(str(p).split()).casefold() for p in people}


def usage_terms(client: Any, limit: int = 50, *, now: float | None = None,
                counts: dict[str, int] | None = None) -> list[str]:
    """The keyterms for one conversation from what the user uses lately (the module docstring): the corrections'
    right names first, then people, tastes, skills, apps and sites, and confirmed resources taken in turn (each
    source's best first), then the portrait while there is room; never a heard form, at most ``limit`` terms and
    :data:`MAX_TOTAL_CHARS` characters. ``client``: a ``MemoryClient`` (its ``store``) or a store. ``counts`` (when
    given) gets the terms each source gave (content-free numbers for the log). Never raises."""
    try:
        return _usage_terms(client, limit, time.time() if now is None else float(now), counts)
    except Exception:  # noqa: BLE001 - recognition works without them
        return []


def _usage_terms(client: Any, limit: int, now: float, counts: dict[str, int] | None) -> list[str]:
    store = getattr(client, "store", client)
    limit = max(0, int(limit))
    try:
        heard = {" ".join(str(a["heard"]).split()).casefold() for a in store.aliases(name_forms=True)}
    except Exception:  # noqa: BLE001
        heard = set()
    portrait, own_names = _portrait(store)
    places = _Places(store, now)
    sources: list[tuple[str, list[str]]] = [
        ("people", _people(store, now, own_names)),
        ("tastes", _tastes(store)),
        ("skills", _skills(store, now, places)),
        ("places", places.ranked()),
        ("resources", _resources(client)),
    ]
    out: list[str] = []
    seen: set[str] = {WAKE_NAME.casefold()}
    chars = 0
    tally: dict[str, int] = {}

    def add(value: Any, source: str) -> bool:
        nonlocal chars
        term = _usable(value)
        key = term.casefold()
        if (not term or key in seen or (key in heard and key not in own_names) or not speakable(term)
                or chars + len(term) > MAX_TOTAL_CHARS):
            return False
        seen.add(key)
        out.append(as_keyterm(term))
        chars += len(term)
        tally[source] = tally.get(source, 0) + 1
        return True

    fresh, older = _corrected(store, now)
    for name in fresh:
        if len(out) >= limit:
            break
        add(name, "corrected")
    queues = [(label, iter(terms[:SOURCE_DEPTH])) for label, terms in sources]
    while queues and len(out) < limit:
        still: list[tuple[str, Any]] = []
        for label, it in queues:
            if len(out) >= limit:
                break
            for value in it:          # the next term this source can add (one per turn)
                if add(value, label):
                    still.append((label, it))
                    break
        queues = still
    for label, rest in (("corrected", older), ("portrait", portrait)):
        for value in rest:
            if len(out) >= limit:
                break
            add(value, label)
    if counts is not None:
        counts.update(tally)
    return out[:limit]


#: Yuki's own name: the first term of every conversation (the wake word).
WAKE_NAME = "Yuki"
#: The heard forms of Yuki's own name memory learned (report 88: "UK", "yoki"), as last read with the keyterms. One
#: memory per process; the fast path reads it to take them as the name (:func:`yuki.voice.fastpath.with_name`).
_NAME_FORMS: list[str] = []
#: Most learned forms of the name used (kept small: a few sound-alikes, each learned once).
MAX_NAME_FORMS = 8


def name_forms() -> list[str]:
    """The heard forms of Yuki's name memory learned, as last read with :func:`memory_keyterms` (newest first)."""
    return list(_NAME_FORMS)


def vocabulary(user_names: Any = (), memory_terms: Any = (), cap: int = 50) -> list[str]:
    """The words speech recognition should expect, for every recogniser (Deepgram's keyterms and the local speech
    model's hotwords use this one list): Yuki's name, the user's own names, then memory's names (the right names of the
    user's corrections first, then what the user uses lately: :func:`usage_terms`), each once (case aside), at most
    ``cap``. A heard form is never in it (it would teach recognition the mistake)."""
    out: list[str] = []
    seen: set[str] = set()
    for term in [WAKE_NAME, *(user_names or ()), *(memory_terms or ())]:
        term = " ".join(str(term or "").split())
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            out.append(term)
    return out[: max(1, int(cap))]


def speech_hotwords(memory: Any, user_names: Any = (), *, limit: int = 50,
                    timeout_s: float = 1.5) -> tuple[list[str], dict[str, Any]]:
    """One call for any recogniser: :func:`memory_keyterms` read and put in :func:`vocabulary` order. The local
    speech model's hotwords and Deepgram's keyterms come from here, so a name learned from one correction reaches both
    in the next conversation. Never raises."""
    terms, facts = memory_keyterms(memory, limit=limit, timeout_s=timeout_s)
    return vocabulary(user_names, terms, limit), facts


def memory_keyterms(memory: Any, *, limit: int = 50, timeout_s: float = 1.5) -> tuple[list[str], dict[str, Any]]:
    """The keyterms for one conversation, and content-free facts for the log (``{"terms", "chars", "ms", "error",
    "name_forms", "sources"}``: ``sources`` is how many terms each source gave). The heard forms of Yuki's name are
    read in the same call and kept for :func:`name_forms`.

    Never raises; with memory unavailable the list is empty (the user's names are added by the caller).
    """
    started = time.perf_counter()
    if memory is None:
        return [], {"terms": 0, "error": "no memory"}
    counts: dict[str, int] = {}

    def read(client: Any) -> tuple[list[str], list[str] | None]:
        # lKeys: what the user uses lately (usage_terms), read on the memory service's own store; a client without a
        # store (older ones) falls back to MemoryClient.speech_names, then to the portrait's facts.
        forms = getattr(client, "name_forms", None)
        heard = list(forms(WAKE_NAME))[:MAX_NAME_FORMS] if callable(forms) else None
        if getattr(client, "store", None) is not None:
            return usage_terms(client, limit, counts=counts), heard
        names: Callable[[int], Any] | None = getattr(client, "speech_names", None)
        if callable(names):
            return list(names(limit)), heard
        return terms_from_facts(client.store.portrait_facts(NAME_KINDS, include_corrections=False), (), limit), heard

    try:
        got = memory.call("speech keyterms", read, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - recognition works without them
        return [], {"terms": 0, "ms": round((time.perf_counter() - started) * 1000), "error": type(exc).__name__}
    terms, heard = got if isinstance(got, tuple) and len(got) == 2 else (got, None)
    if heard is not None:
        _NAME_FORMS[:] = heard
    return list(terms or []), {"terms": len(terms or []), "chars": sum(len(t) for t in terms or []),
                               "name_forms": len(_NAME_FORMS), "sources": dict(counts),
                               "ms": round((time.perf_counter() - started) * 1000)}
