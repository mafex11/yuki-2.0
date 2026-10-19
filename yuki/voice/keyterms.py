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

* the right names of all the user's live corrections lead (report 88: a corrected name is one recognition is known
  to get wrong, and it stays so however old the correction), those of the last :data:`CORRECTION_LEAD_DAYS` first,
  then by count x recency, up to :data:`CORRECTION_LEAD_SHARE` of the list (the rest take their turn as a source);
* the people the user deals with (:mod:`yuki.memory.contacts`: their routes' recency-weighted own messages plus a
  share of theirs), each by their name, and by another name the user himself gave memory as a person's (a heard
  form that is also the subject of a person fact of origin ``user``: a nickname the user says, not a mishearing);
* the names of their tastes (:mod:`yuki.skills.taste`: playlists, YouTube channels; the decayed pick count), picked
  ones first, then names their own app showed;
* their learned skills' sites, services and apps (a decayed count of the times they did it, and of Yuki's runs);
* the apps and sites they had in front lately (the timeline: times in front for at least :data:`PLACE_MIN_DWELL_S`,
  halved every :data:`HALF_LIFE_DAYS`). A site is named as its own pages write it: the run of title words that are
  the labels of its host, capitalised ("chatgpt.com" -> "ChatGPT", "docs.google.com" -> "Google Docs"), an identity
  of the words, never a list of sites;
* the user's own sites and accounts memory confirmed (``MemoryClient.user_resources``);
* then, while there is room, the portrait's people and the names of their work and interests, newest first.

**Never a heard form** (it would teach recognition the mistake): every heard form of ``name_aliases`` is left out
("Barun" for Varun), unless the user himself gave that very word as a person's name (above), and so are a resource's
words that were only heard, and a taste's "said" words (what recognition wrote). A term must be something recognition
can write and a person can say: Latin or Devanagari letters (the recognition languages; a name in another script is said
some other way, a resource of its own when memory confirmed it), not a typed handle (a digit right after a letter
in a word longer than :data:`HANDLE_MIN_CHARS`), short (:data:`MAX_WORDS`, :data:`MAX_CHARS`). Deepgram takes at
most 100 keyterms and 500 tokens across them (its docs: "the most important 20-50 terms"); the list stays within
``limit`` terms and :data:`MAX_TOTAL_CHARS` characters, and the pipeline's screen names fill up to its own total. A
name the user writes in lower case is given with a capital first letter (:func:`as_keyterm`: how Deepgram is
prompted with a proper noun; measured).

**The user's own apps and sites for the Speech line** (voice21, final-D decision 3): read beside the keyterms, in a
thread and a memory call of their own (:func:`refresh_own_names`), through contract C11 (lane names21's
``yuki.skills.names.own_names``, imported inside ``try/except ImportError``), each said as a name (a site by its
registrable name), kept for :func:`own_names`. They never become keyterms here; ``pipeline.heard_note`` measures a heard
word against them by the tail shape only.
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
#: included; ~2-4 characters a token for names). This bounds memory's part of the total of 80 terms: up to 65 memory
#: terms within these characters, beside 15 or more screen names of at most 32 characters each (the worst case is
#: unchanged by the move from 50 + 10).
MAX_TOTAL_CHARS = 520
#: The timeline read for the apps and sites the user had in front lately, and how fast a use fades (half after
#: this many days): the user's present, not last month's (an app left two weeks ago weighs a quarter).
USAGE_DAYS = 14.0
HALF_LIFE_DAYS = 7.0
#: A time in front counts as a use of the app or site only when it stayed in front at least this long (active or
#: passive seconds): the shell's passing surfaces (a start menu, a snipping overlay, a launcher's helper window) are in
#: front for a moment each time, the apps the user works in for longer. Measured on the user's timeline (review r1).
PLACE_MIN_DWELL_S = 10.0
#: Sites named per conversation at most (each needs its recent titles decrypted, in RAM only), and titles read per site.
MAX_SITES = 30
TITLES_PER_SITE = 12
#: Terms each source offers at most (the turns stop at ``limit`` long before).
SOURCE_DEPTH = 40
#: A word longer than this with a digit right after a letter is a handle typed ("name2nd", "name99"), not a name said
#: (a name with a number says it apart, or is this short: "T3"). Measured: two such handles in the list made heard
#: words near-names of them ("Bring" ~ a handle, the talker would have doubted a plain command).
HANDLE_MIN_CHARS = 3
#: Every live correction's right name leads the terms (report 88: a name the user corrected once is one recognition is
#: known to get wrong, so it leads every next conversation's terms, however old the correction), those of the last
#: this many days first, then the others by count x recency (:func:`_corrected`).
CORRECTION_LEAD_DAYS = 7.0
#: The corrections lead with at most this share of ``limit`` (a budget, so years of corrections never crowd out what
#: the user uses lately); the rest take their turn with the other sources, best first.
CORRECTION_LEAD_SHARE = 0.5


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
    rows Yuki made are left out), each weighed by how many times they stayed in front for a while (timeline rows of at
    least :data:`PLACE_MIN_DWELL_S`: a count, so a game left running for hours is not a hundred uses, and a surface
    passed through on the way is none), each time halved every :data:`HALF_LIFE_DAYS`; the sites' names from their own
    recent titles (decrypted in RAM, never kept or logged)."""

    def __init__(self, store: Any, now: float) -> None:
        self.store, self.now = store, now
        self.app_of: dict[str, str] = {}          # process key -> display name
        self.apps: dict[str, float] = {}          # display name -> weight of its times in front for a while
        self.used: dict[str, float] = {}          # host -> the same
        self.hosts: dict[str, float] = {}         # host -> weight of every time in front (naming, a skill's site)
        self.names: dict[str, str] = {}           # host -> its name as its pages write it
        since = now - USAGE_DAYS * 86400.0
        try:
            rows = store._query(
                "SELECT app, process, host, CAST((? - ended_at) / 86400 AS INTEGER), COUNT(*), "
                "SUM(CASE WHEN active_s + passive_s >= ? THEN 1 ELSE 0 END) "
                "FROM timeline WHERE started_at > ? AND state = 'present' AND coalesce(by_yuki, 0) = 0 "
                "AND coalesce(app, '') != '' GROUP BY app, process, host, 4", (now, PLACE_MIN_DWELL_S, since))
        except Exception:  # noqa: BLE001
            rows = []
        for app, process, host, age_d, times, stayed in rows:
            fade = 0.5 ** (max(0, int(age_d or 0)) / HALF_LIFE_DAYS)
            w, used = float(times or 0) * fade, float(stayed or 0) * fade
            name = _app_name(app)
            if process and name:   # every program seen in front is named (a skill's app), used for a while or not
                self.app_of.setdefault(str(process).casefold(), name)
            if name and used > 0:
                self.apps[name] = self.apps.get(name, 0.0) + used
            if host:
                self.hosts[str(host)] = self.hosts.get(str(host), 0.0) + w
                if used > 0:
                    self.used[str(host)] = self.used.get(str(host), 0.0) + used
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
        """Apps and named sites together, most used lately first (times in front for a while)."""
        both = [(w, name) for name, w in self.apps.items()]
        both += [(w, self.names[h]) for h, w in self.used.items() if h in self.names]
        return [name for _w, name in sorted(both, key=lambda x: -x[0])]


def _corrected(store: Any, now: float) -> list[str]:
    """The right names of every live correction of the user's (Yuki's own name forms are not corrections here; an
    undone or replaced one has ended), each once: those corrected in the last :data:`CORRECTION_LEAD_DAYS` first,
    then the rest, each group by count x recency (every live correction to that name, each halved every
    :data:`HALF_LIFE_DAYS`: a name the user corrected from three mishearings outranks one corrected once), then
    newest first."""
    try:
        rows = store.aliases()
    except Exception:  # noqa: BLE001
        return []
    shown: dict[str, str] = {}
    score: dict[str, float] = {}
    newest: dict[str, float] = {}
    for a in rows:
        name = " ".join(str(a.get("name") or "").split())
        if not name:
            continue
        key, at = name.casefold(), float(a.get("at") or 0)
        if at >= newest.get(key, float("-inf")):
            shown[key], newest[key] = name, at   # spelled as the newest correction spells it
        score[key] = score.get(key, 0.0) + _fade(now - at)
    lead_s = CORRECTION_LEAD_DAYS * 86400.0
    order = sorted(score, key=lambda k: (now - newest[k] > lead_s, -score[k], -newest[k]))
    return [shown[k] for k in order]


def _people(store: Any, now: float, own_names: set[str]) -> list[str]:
    """The people the user deals with lately (their routes' own-message weight plus a share of theirs: the directory's
    weight before lMsg, copied here, not its order), each by their name and by another name the user gave memory as a
    person's (``own_names``: :func:`_portrait`)."""
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
    """The names of the user's things the learner knows (contract C5, :func:`yuki.skills.learn.values.known`: every
    capability's values, marked not-mine left out): each value's label (a site by the name its pages write, a value
    that is a name: "Google Meet") and the name the user gave it (``named_by`` the user), best first by the value's own
    recency score."""
    try:
        from yuki.skills.learn.values import known

        table = known(store, now=now)
    except Exception:  # noqa: BLE001
        return []
    site_keys = {h.casefold().split(".")[-2] if "." in h else h.casefold() for h in places.hosts}
    ranked: list[tuple[float, list[str]]] = []
    for values in (table or {}).values():
        for v in values or ():
            if not isinstance(v, dict):
                continue
            try:
                w = float(v.get("score") or 0.0)
            except (TypeError, ValueError):
                continue
            names: list[str] = []
            label = " ".join(str(v.get("label") or "").split())
            given = " ".join(str(v.get("name") or "").split()) if v.get("named_by") == "user" else ""
            if given:
                names.append(given)   # the user's own word for it
            if "." in label or label.casefold() in site_keys:   # a host or a site's key: named as its pages write it
                names.append(places.name_of_site(label, [given] if given else []))
            elif _is_name(label):                               # a value that is a name ("Google Meet")
                names.append(label)
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
    """The portrait's people, then the names of their work and interests, each newest first; and the names the user
    himself gave memory as a person's (casefolded): a person fact of origin ``user``, which the portrait writes only
    from the user's own correction or answer (review r1, L3). A model's person fact is not enough: the v3.5 failure was
    a misheard name a portrait run kept as a person, and a later run could make any heard form one; memory never
    wrote the user's nickname for a friend anywhere on screen (measured: no chat title, sender or line has it), so the
    user's own word is the evidence there is."""
    try:
        facts = list(store.portrait_facts(NAME_KINDS, include_corrections=False))
    except Exception:  # noqa: BLE001
        return [], set()
    facts.sort(key=lambda f: -float(getattr(f, "valid_from", 0) or 0))
    people = [f.subject for f in facts if f.kind == "person" and _usable(f.subject)]
    names = [f.subject for f in facts if f.kind != "person" and _is_name(_usable(f.subject))]
    own = {" ".join(str(f.subject).split()).casefold() for f in facts
           if f.kind == "person" and _usable(f.subject) and getattr(f, "origin", None) == "user"}
    return people + names, own


def usage_terms(client: Any, limit: int = 50, *, now: float | None = None,
                counts: dict[str, int] | None = None, tags: dict[str, str] | None = None) -> list[str]:
    """The keyterms for one conversation from what the user uses lately (the module docstring): every live
    correction's right name first (up to :data:`CORRECTION_LEAD_SHARE` of ``limit``), then the rest of them, people,
    tastes, skills, apps and sites, and confirmed resources taken in turn (each source's best first), then the
    portrait while there is room; never a heard form, at most ``limit`` terms and
    :data:`MAX_TOTAL_CHARS` characters. ``client``: a ``MemoryClient`` (its ``store``) or a store. ``counts`` (when
    given) gets the terms each source gave (content-free numbers for the log); ``tags`` (when given) each term's
    source (:func:`name_terms`). Never raises."""
    try:
        return _usage_terms(client, limit, time.time() if now is None else float(now), counts, tags)
    except Exception:  # noqa: BLE001 - recognition works without them
        return []


def _usage_terms(client: Any, limit: int, now: float, counts: dict[str, int] | None,
                 tags: dict[str, str] | None = None) -> list[str]:
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
        if tags is not None:
            tags[out[-1]] = source
        return True

    corrected = _corrected(store, now)
    lead = max(1, round(limit * CORRECTION_LEAD_SHARE)) if limit else 0
    for name in corrected[:lead]:
        if len(out) >= limit:
            break
        add(name, "corrected")
    sources.insert(0, ("corrected", corrected[lead:]))   # past the lead's share: a source of its own, first in turn
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
    for value in portrait:
        if len(out) >= limit:
            break
        add(value, "portrait")
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


#: The sources whose terms are names recognition is known to get wrong or that are a person's (review r1, L1).
NAME_SOURCES = ("corrected", "people")
#: Those terms of the last :func:`memory_keyterms` read (as given to recognition); empty when the read fell back.
_NAME_TERMS: list[str] = []


def name_terms() -> list[str]:
    """The last read's terms that are people's names or corrections' right names (:data:`NAME_SOURCES`), as given to
    recognition; the rest of the keyterms are apps, sites and tastes. For the talker's near-name doubt
    (``pipeline.heard_note``): measured on the user's own turns, a brand's near match is mostly plain command phrasing
    ("you turn" ~ YouTube, "chart" ~ ChatGPT), a person's is the misheard name. Empty until a read, and after a read
    that fell back to ``MemoryClient.speech_names`` (sources unknown)."""
    return list(_NAME_TERMS)


# ---------------------------------------------------------------------------------------------------------------
# voice21 (final-D decision 3, 19 Oct): the user's own apps and sites for the Speech line (contract C11)
# ---------------------------------------------------------------------------------------------------------------

#: Contract C11 (lane names21, ``yuki.skills.names``): the reader of the user's own apps and sites
#: (``own_names(store)``: an app's display name, a site's host, in use first; strings, or mappings / objects with
#: ``name`` or ``names``), and its test of a site (``is_site``: a host). Imported inside ``try/except ImportError``:
#: with it missing (that lane not merged) there are no own names, and the Speech line measures the apps running on the
#: desk only, as before (``pipeline.heard_note`` ``apps``).
C11_MODULE = "yuki.skills.names"
C11_READER = "own_names"
C11_IS_SITE = "is_site"
#: At most this many own names are measured (the reader's best first; never cut by name): C11's own cap. The offline
#: re-measure on the user's 793 spoken turns (docs/research/final-test-2026-10-19/builds/voice21.md) added no near note
#: with the whole list (lane names21's 116 names; a superset of every app and named site of the timeline, 170), once a
#: heard word that is a name's tail letter for letter is no match (``pipeline.heard_note``).
MAX_OWN_NAMES = 200
#: The C11 read waits on memory at most this long (s). It runs beside the keyterms, never in their time: the session
#: waits for the keyterms 1.6 s at most, and the names are read when a heard word is measured.
OWN_READ_TIMEOUT_S = 3.0
#: The own names of the last read (the Speech line's ``own``); empty until a read, and while the C11 reader is missing
#: or gave nothing.
_OWN_NAMES: list[str] = []


def own_names() -> list[str]:
    """The user's own apps and sites as last read beside the keyterms (contract C11), for the Speech line's near test
    (``pipeline.heard_note`` ``own``: the tail measure only). ``[]`` until a read, or with the reader missing."""
    return list(_OWN_NAMES)


def refresh_own_names(memory: Any, *, timeout_s: float = OWN_READ_TIMEOUT_S) -> bool:
    """Start the C11 read of the user's own apps and sites on ``memory`` (a ``MemoryAccess``: its bounded ``call``) in a
    thread of its own; :func:`own_names` has them once it is back (a failed or slow read leaves the last list). With
    the reader missing the list is emptied (today's list: the running apps only). ``True`` when a read started.
    Never raises."""
    reader = c11_reader()
    if reader is None or memory is None or not callable(getattr(memory, "call", None)):
        if reader is None:
            _OWN_NAMES[:] = []
        return False

    def read() -> None:
        try:
            got = memory.call("own names", lambda client: read_own_names(client, reader), timeout_s=timeout_s)
        except Exception:  # noqa: BLE001 - the last list stands
            return
        if got is not None:
            _OWN_NAMES[:] = list(got)

    try:
        import threading

        threading.Thread(target=read, name="yuki-own-names", daemon=True).start()
        return True
    except Exception:  # noqa: BLE001
        return False


def c11_reader() -> Callable[[Any], Any] | None:
    """Lane names21's reader of the user's own apps and sites (:data:`C11_MODULE`, :data:`C11_READER`), imported inside
    ``try/except ImportError``; ``None`` when it is not there (today's list: the running apps only)."""
    import importlib

    try:
        reader = getattr(importlib.import_module(C11_MODULE), C11_READER, None)
    except ImportError:
        return None
    return reader if callable(reader) else None


def _c11_is_site() -> Callable[[str], bool]:
    """C11's own test of a site name (a host), else a host's shape: one word with a dot in it. Never raises."""
    import importlib

    try:
        test = getattr(importlib.import_module(C11_MODULE), C11_IS_SITE, None)
    except ImportError:
        test = None
    if callable(test):
        return test
    return lambda name: " " not in str(name or "").strip() and "." in str(name or "").strip(".")


def said_site(host: str) -> str:
    """A site of the user's (its host, "excalidraw.com", "web.whatsapp.com") as it is said: its registrable name's
    first label, capitalised as a keyterm ("Excalidraw", "Whatsapp"). ``""`` when it has none. Structure only."""
    host = " ".join(str(host or "").split()).casefold().strip(".")
    if host.startswith("www."):
        host = host[4:]
    try:
        from yuki.memory.moments import registrable_domain

        reg = str(registrable_domain(host) or host)
    except Exception:  # noqa: BLE001
        reg = host
    return as_keyterm(reg.split(".")[0]) if reg else ""


def own_names_from(got: Any, limit: int = MAX_OWN_NAMES, is_site: Callable[[str], bool] | None = None) -> list[str]:
    """The names a C11 read gave, each once (case aside), in its order, each one a name recognition can write and a
    person can say (:func:`speakable`, at most :data:`MAX_WORDS` / :data:`MAX_CHARS`), at most ``limit``; a site (its
    host, ``is_site``: C11's own test) as it is said (:func:`said_site`). A mapping's values or an object's ``name`` /
    ``names`` are read as the reader gives them. Never raises."""
    out: list[str] = []
    seen: set[str] = set()

    def add(value: Any) -> None:
        term = _usable(value) if isinstance(value, str) else ""
        try:
            if term and is_site is not None and is_site(term):
                term = said_site(term)
        except Exception:  # noqa: BLE001 - taken as written
            pass
        if term and speakable(term) and term.casefold() not in seen and term.casefold() != WAKE_NAME.casefold():
            seen.add(term.casefold())
            out.append(term)

    def take(item: Any) -> None:
        if isinstance(item, str):
            add(item)
            return
        if isinstance(item, dict):
            names, one = item.get("names"), item.get("name")
        else:
            names, one = getattr(item, "names", None), getattr(item, "name", None)
        values = [one] if isinstance(one, str) else []
        values += list(names) if isinstance(names, (list, tuple)) else []
        for value in values:
            add(value)

    try:
        items = list(got.values()) if isinstance(got, dict) else list(got or [])
        for item in items:
            if len(out) >= limit:
                break
            take(item)
    except Exception:  # noqa: BLE001 - a read of another shape: what was named so far
        pass
    return out[: max(0, int(limit))]


def read_own_names(client: Any, reader: Callable[[Any], Any] | None = None) -> list[str] | None:
    """The C11 read on the memory service's store (``client``: a ``MemoryClient`` or a store): :func:`own_names_from`
    of what the reader gives; ``None`` when there is no reader (today's list). Never raises (``[]`` on an error)."""
    reader = reader if reader is not None else c11_reader()
    if reader is None:
        return None
    try:
        return own_names_from(reader(getattr(client, "store", client)), is_site=_c11_is_site())
    except Exception:  # noqa: BLE001 - recognition and the Speech line work without them
        return []


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
    read in the same call and kept for :func:`name_forms`; the terms that are people's names and corrections are kept
    for :func:`name_terms`.

    Never raises; with memory unavailable the list is empty (the user's names are added by the caller).
    """
    started = time.perf_counter()
    if memory is None:
        return [], {"terms": 0, "error": "no memory"}
    counts: dict[str, int] = {}
    tags: dict[str, str] = {}

    def read(client: Any) -> tuple[list[str], list[str] | None]:
        # lKeys: what the user uses lately (usage_terms), read on the memory service's own store; a client without a
        # store (older ones), or an empty list (a source's reader broke), falls back to MemoryClient.speech_names
        # (counted as its own source in the log), then to the portrait's facts.
        forms = getattr(client, "name_forms", None)
        heard = list(forms(WAKE_NAME))[:MAX_NAME_FORMS] if callable(forms) else None
        has_store = getattr(client, "store", None) is not None
        terms = usage_terms(client, limit, counts=counts, tags=tags) if has_store else []
        if terms:
            return terms, heard
        names: Callable[[int], Any] | None = getattr(client, "speech_names", None)
        if callable(names):
            terms = list(names(limit))
            counts.clear()
            tags.clear()
            counts["speech_names"] = len(terms)
            return terms, heard
        if not has_store:
            return [], heard
        return terms_from_facts(client.store.portrait_facts(NAME_KINDS, include_corrections=False), (), limit), heard

    try:
        got = memory.call("speech keyterms", read, timeout_s=timeout_s)
    except Exception as exc:  # noqa: BLE001 - recognition works without them
        return [], {"terms": 0, "ms": round((time.perf_counter() - started) * 1000), "error": type(exc).__name__}
    terms, heard = got if isinstance(got, tuple) and len(got) == 2 else (got, None)
    if heard is not None:
        _NAME_FORMS[:] = heard
    _NAME_TERMS[:] = [t for t in terms or [] if tags.get(t) in NAME_SOURCES]
    # voice21 (final-D decision 3, contract C11): the user's own apps and sites for the Speech line, a read of its own
    # in its own thread (never in the keyterms' time); none while lane names21's reader is not there
    own = refresh_own_names(memory)
    return list(terms or []), {"terms": len(terms or []), "chars": sum(len(t) for t in terms or []),
                               "name_forms": len(_NAME_FORMS), "sources": dict(counts), "own_read": own,
                               "ms": round((time.perf_counter() - started) * 1000)}
