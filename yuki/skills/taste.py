"""Taste: what the user picks by hour of day (PLAN.md D5, Phase T with the Phase L amendment; map-routines T1).

Read side (sCore), the seeds (sWeb, lWire) and phase T's nightly recompute (tTaste).

Table ``tastes`` (migration 41, T1.1's schema): one row per ``(kind, entity_key)``, the display name and its link
encrypted, two hour-of-day histograms (weekday / weekend; 24 decayed counts, content-free), ``n_obs`` (the decayed
count) and, for a YouTube channel, ``fresh_age_h`` (how old the videos the user watched from it were: measured).
Names are decrypted in-process and never logged; logs and returned counts are numbers only.

**Taste kinds come from data, never a list here** (learner design §5: "fill-slot kinds come from capability and site
data"):

* a capability's fill slot that declares an entity ``kind``: ``<connection app>_<kind>`` (``connection.py`` derives it:
  Spotify's playlist slot is ``spotify_playlist``), observed on the capability's end moments of that app
  (:func:`capability_kinds`: ``media_context`` for the playlist);
* a site file's page kind whose template has a speakable variable (``yuki/connect/sites/*.toml`` ``[pages]``; a
  variable named ``id`` is an opaque id, opened only from a link: lSplit's ``page_address`` rule): ``<site>_<page>``
  (:func:`site_kinds`: a YouTube channel's handle page is ``youtube_handle``), observed on the user's ``page`` moments
  on that template; a learned skill bound to that site and page fills its entity slot from it (:func:`slot_kind`);
* a watched video counts for its channel once :mod:`yuki.skills.youtube_feed` knows whose it is (a feed listed it, or
  one oEmbed read): :func:`youtube_feed.attributed <yuki.skills.youtube_feed.attributed>`;
* one entity seen in two browsers is one row: observations are grouped by the moments' ``entity_key`` (D23: the keyed
  digest of where the entity lives and what it is, never of the program), and the row's own key is a keyed digest of
  the kind and the value (:func:`entity_key`), never the value in clear.

**The nightly recompute** (:func:`recompute`, run by :func:`seed`, the memory service's daily hook): per kind, from the
last 60 days of moments, each pick weighted ``0.5 ** (age / 21 days)``; the same entity again within 30 minutes is
the same pick (a pause and resume, a channel page then one of its videos). A pick is the user's: their own moments,
or a request of Yuki's whose values came from their words (its runs' ``filled``: ``words`` / ``again`` / ``told``,
never a taste guess, so a guess never feeds itself; a usual only picks the browser). Never a skill the user marked Not
mine, and (fd, final-F D7) never a moment inside a live test's span. Recomputed, never
incremented: a rerun changes nothing and nothing drifts; an entity whose weight fell under :data:`FLOOR` drops out.
Never with a game in front or a call: then it is tried again later (a one-shot timer, :data:`RETRY_S`).

**Scoring** (T1.3): ``score(entity | now)`` = the histogram of today's kind of day (weekday / weekend) smoothed by a
Gaussian of :data:`SIGMA_MIN` around the clock time; when that kernel mass is under :data:`FALLBACK_MASS` the all-days
sum is used. No fixed bands. :func:`top` fills a slot in code when the words name nothing ("play something": the
usual playlist for this hour, named in the done line); :func:`options` gives the names the talker may match words
against; :func:`offered` the fresh uploads named aloud after a request (``youtube_feed``), for "the Deji one".

Seeds kept from before phase T: :func:`seed_sites` (the site kinds alone) and :func:`note` (one choice the user named
and a skill saw done: a voice channel joined, map-routines T1.2 "his words only").

**Names the user's own app showed** (failure fix-plan Item 7, 10 Oct): :func:`saw` keeps the names a driver read in the
user's own app (Spotify's Library rows) as rows of source :data:`SEEN_SOURCE`, encrypted like every taste: no pick is
behind them, so :func:`top` (the taste fill, a guess) never returns one, while :func:`candidates` lists them after the
picked names and :func:`find` finds them, so the menu and the resolver can offer the user's real playlist names ("ネオン"
when they say "my Japanese playlist"). **What the user called it**: when a request's words named an entity no row was
called and the hand-off job that followed was seen putting one on (the driver decides when), :func:`note` with
``said_as`` keeps those words as another name of that row (a row of kind ``<kind>@said``: the words encrypted, their
keyed digest the key, the row's name encrypted as its target). kw3 (C9, 20 Oct): that pairing is a hint the meaning
judgement reads (:func:`said_target`, :func:`described`'s ``called``, :func:`judge`'s ``hint``: "last time these words
played X"), never a lookup that plays X by the words alone; the newest pairing of the same words wins, and
:func:`unsay` drops one the user's later choice contradicts (what they say wins over what was learned).

**By sound and by meaning** (lSkills, live 17 Oct: "Lumin" for lumen asked "say it again"; "anime playlist" asked which
playlist a minute after "Japanese" was paired with ネオン; "something chill" took 15-29 s through the thinker):

* :func:`heard_as` - a heard name that sounds like exactly one of the user's own listed names is that name (the people
  index's reading of names by sound; data, never a word list): the resolver uses it on the menu's candidates;
* :func:`described` + :func:`judge` - when words name no entity and no pairing holds them, memory describes the user's
  own entities (what they called each before, when they pick it, how much, what played while it ran) and the voice's
  small model judges which one the words name or describe, or that they only ask for a mood (then the hour's usual,
  :func:`top`, plays), or none. One forced tool call with the names as an enum, bounded (:data:`MEANING_WAIT_S`), its
  answer kept for the same words and descriptions (:data:`MEANING_TTL_S`); kw3 (C8): its verdict stands, a ``name``
  the words don't hold as written being Yuki's guess, named in the done line. The Spotify driver asks it after its
  Library read and, once its own check sees the row play, keeps those words as another name of the row (:func:`note`
  ``said_as``): the next judgement of the same words gets it as a hint. Content goes to the model only, never to a
  log.

**Time, not clicks** (kTaste, 17 Oct audit; research 4.3: YouTube says a click misleads): a pick weighs the units of
what then played (:func:`yuki.skills.youtube_feed.units`, five minutes = one): a watched video its page's media seconds,
a playlist the app's own playing spans until the next pick, a visit to an entity's own page alone
:data:`VISIT_UNITS`; a sitting (:data:`SESSION_GAP_S`) sums its picks up to :data:`SESSION_UNITS`. ``n_obs`` is those
decayed units.

**"Something I'd like"** (kTaste, contract C2, audit 17 Oct decision 2): when the user asks Yuki to choose ("a video I
like", "something chill", "put something on"; ``run_skill`` slots ``choose = true`` and ``context``), :func:`choose`
picks like a recommender among the user's own candidates (:data:`CHOOSE_KINDS`: a channel's new or unwatched upload, a
channel, a playlist): their taste at this hour and kind of day plus a share of their all-time weight, halved per recent
skip, a video weighted by how new it is; the user's words judged over the best :data:`CHOOSE_N` by the voice's small
model on what each holds (bounded; the habit order stands when it is late; no word lists). The engine plays the top
pick and says it with a code-written why (:func:`choice_line`: "Putting on Deji's new one - it's out today, and you
usually watch them around lunch."); :func:`chosen` keeps it for the conversation, so "another one" / "not that" takes
the next (another channel first), and notes it, so a pick changed or closed within :data:`SKIP_S` counts as a skip.
Never with a game in front or in a call (:func:`held_line`). Yuki's own pick is a guess (:data:`GUESS_FILLS`): it
never feeds the taste it came from.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Mapping, Sequence

#: The smoothing of the hour histograms when scoring an hour (minutes; map-routines T1.3: sigma = 90 min).
SIGMA_MIN = 90.0
#: Under this kernel mass on today's kind of day, the all-days histogram scores the hour (T1.3).
FALLBACK_MASS = 1.0
#: Candidates the menu lists per slot at most (map-skills S3.1).
MENU_CANDIDATES = 5
#: Names :func:`options` gives at most (T1.3).
OPTIONS_N = 8
#: The source of a row that is only a name the user's own app showed (a Spotify Library row the driver read): no pick
#: behind it (empty histograms, ``n_obs`` 0). Listed and found, never a fill (fix-plan Item 7).
SEEN_SOURCE = "seen"
#: Seen rows kept per kind at most (the oldest go first).
SEEN_MAX = 200
#: The kind suffix of the rows that keep what the user called an entity (:func:`note` ``said_as``; :func:`said_target`).
SAID_SUFFIX = "@said"

LogFn = Callable[..., None]


@dataclass(frozen=True)
class Taste:
    """One entity the user picks: its key (a keyed digest), its name and link (decrypted, RAM only), its score at
    the asked hour and how often it was seen."""

    entity_key: str
    name: str
    ref: str
    score: float
    n_obs: float


def _hist(text: Any) -> list[float]:
    try:
        xs = [float(x) for x in json.loads(text or "[]")]
    except (TypeError, ValueError):
        return [0.0] * 24
    return (xs + [0.0] * 24)[:24]


def hour_score(hist: list[float], hour: float) -> float:
    """The histogram's weight at ``hour`` (0-24, fractional), smoothed by a Gaussian of :data:`SIGMA_MIN` (circular)."""
    sigma_h = SIGMA_MIN / 60.0
    total = 0.0
    for h, v in enumerate(hist):
        d = abs(h + 0.5 - hour)
        d = min(d, 24.0 - d)
        total += v * math.exp(-0.5 * (d / sigma_h) ** 2)
    return total


def score_at(weekday: list[float], weekend: list[float], now: float) -> float:
    """T1.3's score at ``now``: today's kind of day (weekday / weekend) around the clock time, or the all-days sum
    when today's kernel mass is under :data:`FALLBACK_MASS`."""
    stamp = datetime.fromtimestamp(float(now))
    hour = stamp.hour + stamp.minute / 60.0
    today = weekend if stamp.weekday() >= 5 else weekday
    s = hour_score(today, hour)
    if s < FALLBACK_MASS:
        s = hour_score([a + b for a, b in zip(weekday, weekend)], hour)
    return s


def top(store: Any, kind: str, *, now: float | None = None, n: int = 1, until: float | None = None,
        seen: bool = False) -> list[Taste]:
    """The ``n`` entities of ``kind`` the user picks most at this hour (:func:`score_at`), best first. ``until``: rows
    last updated before it only (replays). ``seen``: also the rows that are only names the user's app showed
    (:data:`SEEN_SOURCE`, score 0: last); left out by default, so a taste fill never guesses one. Read only; never
    raises (``[]``)."""
    now = time.time() if now is None else float(now)
    try:
        rows = store._query(
            "SELECT entity_key, name_ciphertext, ref_ciphertext, hist_weekday, hist_weekend, n_obs FROM tastes "
            "WHERE kind = ?" + ("" if seen else " AND source != ?") + (" AND updated_at < ?" if until is not None
                                                                      else ""),
            (kind, *(() if seen else (SEEN_SOURCE,)), *((float(until),) if until is not None else ())))
    except Exception:  # noqa: BLE001
        return []
    scored = []
    for key, name_ct, ref_ct, wd, we, n_obs in rows:
        score = score_at(_hist(wd), _hist(we), now)
        try:
            name = store._dec(name_ct) or ""
            ref = store._dec(ref_ct) or ""
        except Exception:  # noqa: BLE001 - a row the key can't read is skipped
            continue
        scored.append(Taste(str(key), name, ref, round(score, 4), float(n_obs or 0.0)))
    scored.sort(key=lambda t: (-t.score, -t.n_obs, t.entity_key))
    return scored[:max(0, int(n))]


def candidates(store: Any, kind: str, *, now: float | None = None, limit: int = MENU_CANDIDATES,
               by: str = "hour") -> list[str]:
    """The names the menu lists for an entity slot of ``kind`` (the user's own names: their playlists, channels):
    the picked ones first, at most ``limit``, best at this hour first (``by="n_obs"``: most seen first, an order that
    changes only when the table is recomputed (nightly), for a menu that sits in a cached prompt block: the thinker's
    ``run_skill`` description); then every name the user's own app showed with no pick yet (:func:`saw`; in the order
    the app first showed them, a stable order), so a name on the user's own screen is one the resolver accepts and
    the menu can offer (it shows the first few). Never raises (``[]``)."""
    if by == "n_obs":
        rows = top(store, kind, now=now, n=10_000)
        rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
        picked = [t.name for t in rows if t.name][:max(0, int(limit))]
    else:
        picked = [t.name for t in top(store, kind, now=now, n=limit) if t.name]
    have = {_norm(n) for n in picked}
    for name in seen_names(store, kind):
        if _norm(name) not in have:
            have.add(_norm(name))
            picked.append(name)
    return picked


def seen_names(store: Any, kind: str) -> list[str]:
    """The names of ``kind`` the user's own app showed that have no pick yet (:data:`SEEN_SOURCE` rows), in the order
    the app first showed them. Decrypted in RAM; never raises (``[]``)."""
    try:
        rows = store._query("SELECT name_ciphertext FROM tastes WHERE kind = ? AND source = ? ORDER BY last_at, id",
                            (kind, SEEN_SOURCE))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for (name_ct,) in rows:
        try:
            name = " ".join(str(store._dec(name_ct) or "").split())
        except Exception:  # noqa: BLE001 - a row the key can't read is skipped
            continue
        if name:
            out.append(name)
    return out


def options(store: Any, kind: str, *, now: float | None = None, n: int = OPTIONS_N) -> list[str]:
    """T1.3: the names the talker or the model may match the user's words against (their own playlist or channel
    names: picked ones best at this hour first, then the names their own app showed), at most ``n``. Never raises."""
    return candidates(store, kind, now=now, limit=n)[:max(0, int(n))]


def said_kind(kind: str) -> str:
    """The kind of the rows that keep what the user called an entity of ``kind`` (:func:`note` ``said_as``)."""
    return f"{kind}{SAID_SUFFIX}"


def said_target(store: Any, kind: str, words: Any) -> str | None:
    """The name of the entity of ``kind`` the user's words ``words`` were last paired with (:func:`note` ``said_as``:
    they named no row, and that entity then played), or None. Identity of the words (case and spacing folded), by
    their keyed digest. kw3 (C9): a read-only hint for the meaning judgement (:func:`judge` ``hint``) and a fenced
    memory line for the brain, never a lookup that plays it by the words alone. Never raises."""
    folded = _norm(words)
    if not folded or not str(kind or "").strip():
        return None
    try:
        rows = store._query("SELECT ref_ciphertext FROM tastes WHERE kind = ? AND entity_key = ?",
                            (said_kind(kind), entity_key(store, said_kind(kind), folded)))
        name = " ".join(str(store._dec(rows[0][0]) or "").split()) if rows and rows[0][0] else ""
    except Exception:  # noqa: BLE001
        return None
    return name or None


def said_words(store: Any, kind: str, target: Any) -> list[str]:
    """The words paired with the entity ``target`` of ``kind`` (:func:`note` ``said_as``; target matched with case and
    spacing folded), newest first, decrypted in RAM: the driver tells by them whether a request reached that entity
    through a pairing. Never raises (``[]``)."""
    want = _norm(target)
    if not want or not str(kind or "").strip():
        return []
    try:
        rows = store._query("SELECT name_ciphertext, ref_ciphertext FROM tastes WHERE kind = ? "
                            "ORDER BY last_at DESC, id DESC", (said_kind(kind),))
    except Exception:  # noqa: BLE001
        return []
    out: list[str] = []
    for name_ct, ref_ct in rows:
        try:
            if ref_ct and _norm(store._dec(ref_ct)) == want:
                words = " ".join(str(store._dec(name_ct) or "").split())
                if words:
                    out.append(words)
        except Exception:  # noqa: BLE001 - a row the key can't read is skipped
            continue
    return out


def find(store: Any, kind: str, name: str) -> Taste | None:
    """The taste row whose name equals ``name`` (case and spacing folded: identity of a name, never meaning), the names
    the user's own app showed included (:func:`saw`); None when there is none. kw3 (C9): words the user once said for
    a row (:func:`said_target`) are never looked up here: they are a hint for the meaning judgement only. Never
    raises."""
    want = _norm(name)
    if not want:
        return None
    for t in top(store, kind, n=10_000, seen=True):
        if _norm(t.name) == want:
            return t
    return None


def offered(store: Any, *, now: float | None = None) -> list[Taste]:
    """The fresh uploads named aloud after a request in the last minutes (:func:`yuki.skills.youtube_feed.offered`), as
    tastes: the name said (the channel's), the video's link, its key. What "the Deji one" is matched against, for a
    slot that :func:`takes_offers` (``web.open_video``'s ``video``: the taste fill never guesses a video; one nobody
    named plays only when the user asked Yuki to choose, :func:`choose`). Never raises
    (``[]``)."""
    try:
        from yuki.skills import youtube_feed

        return [Taste(o.key, o.name, o.ref, round(o.score, 4), 1.0) for o in youtube_feed.offered(store, now=now)]
    except Exception:  # noqa: BLE001
        return []


def find_offered(store: Any, name: Any, *, now: float | None = None) -> dict[str, Any] | None:
    """The offered video a picked name means (``{"name", "key", "ref"}``: the slot value with its link), by identity
    with the name said, the channel's handle or the video's title (:func:`yuki.skills.youtube_feed.pick`), or None."""
    try:
        from yuki.skills import youtube_feed

        return youtube_feed.pick(store, name, now=now)
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------------
# lSkills (live 17 Oct): a heard name by sound, and words by meaning
# ---------------------------------------------------------------------------------------------------------------------

#: The judgement of what words mean is bounded by this long (a thread joined at most so; one small model call, about
#: 1-1.5 s from this PC), and kept for the same words and the same names this long (RAM; the library and the restore
#: rung of one request ask once).
MEANING_WAIT_S = 3.0
MEANING_TTL_S = 600.0
#: At most this many of the user's own entities are described to it, each with at most this many titles memory saw
#: playing while it ran, from the last this many days.
MEANING_ITEMS = 16
MEANING_TITLES = 4
MEANING_DAYS = 30.0
#: What the judgement may answer (code words, logged as they are): the words say its ``name`` (or a mishearing of it),
#: what the user ``called`` it before, or describe its ``content``; ``mood`` asks for a feel no entity states (the
#: hour's usual then plays: :func:`top`); ``none`` names something not among them; ``unknown``: no judgement (no model,
#: too slow, an eval).
MEANING_WHYS: tuple[str, ...] = ("name", "called", "content", "mood", "none")


@dataclass(frozen=True)
class Meant:
    """What the user's words were judged to mean among their own entities: ``name`` (one of the names described, or
    ``None``), ``why`` (:data:`MEANING_WHYS`, or ``unknown``) and how long the judgement took (ms)."""

    name: str | None
    why: str
    ms: float = 0.0


def heard_as(names: Any, said: Any, *, loose: bool = False) -> str | None:
    """lSkills (live 17 Oct: "Lumin" heard for lumen while the talker's menu listed ``[playlist: lumen | ネオン]``):
    the one of ``names`` (the user's own names: the menu's taste candidates, the rows an app showed) that ``said``
    sounds like, or ``None`` (none, two, or ``said`` is one of them as written). Sound only, through the people
    index's own reading of names (:func:`yuki.memory.contacts.sounds_in` with ``short``: the same sound key and onset
    and spellings close enough, "Lumin"/"lumen" 0.25); a name in another script, or with digits, never matches. Data,
    never a list of words.

    ``loose`` (fSkills; review M2): with the strict rule finding none, the closed-set tier (:func:`_closest_heard`:
    spelled initialisms, "soccer" for lumen). Opt-in, only where a wrong pick is named at once and harms nothing (the
    resolver's taste lookups of the user's own playlists and channels, a site page's channel); never for a call join
    (Discord's voice channels: a wrong join opens the mic to people the user didn't name) or the judge's name guard."""
    text = " ".join(str(said or "").split())
    if not text:
        return None
    try:
        from yuki.memory.contacts import sounds_in
    except Exception:  # noqa: BLE001
        return None
    pool = [" ".join(str(n or "").split()) for n in names or []]
    if any(_norm(n) == _norm(text) for n in pool if n):
        return None
    hits: dict[str, str] = {}
    for name in pool:
        if not name or _norm(name) in hits:
            continue
        try:
            if sounds_in(name, [text], short=True) is not None:
                hits[_norm(name)] = name
        except Exception:  # noqa: BLE001 - a name that can't be read by sound
            continue
    if hits:
        return next(iter(hits.values())) if len(hits) == 1 else None
    return _closest_heard(pool, text) if loose else None


#: fSkills (live 18 Oct: "MBL's YouTube channel" heard for the user's channel NDL): how each Latin letter is said when
#: a short all-capital word is spelled out letter by letter (an initialism as speech recognition writes it), the
#: alphabet's own spoken names: a format conversion, like :func:`yuki.memory.contacts.romanize`, never a word list.
LETTER_NAMES = {"a": "ay", "b": "bee", "c": "see", "d": "dee", "e": "ee", "f": "ef", "g": "jee", "h": "aych",
                "i": "eye", "j": "jay", "k": "kay", "l": "el", "m": "em", "n": "en", "o": "oh", "p": "pee",
                "q": "kyoo", "r": "ar", "s": "es", "t": "tee", "u": "yoo", "v": "vee", "w": "dubleyoo", "x": "ex",
                "y": "why", "z": "zee"}
#: A word of 2 to this many Latin capitals, nothing else, is read as spelled out (an initialism).
SPELLED_MAX = 5
#: The closed-set tier (:func:`_closest_heard`): two spoken forms at most this far apart (edit distance over the longer
#: one, the closest of their readings: "soccer" read "soker" / "lumen" 0.25 once its final "r" is left unsaid; "MBL"
#: spelled "embeeel" / "endeeel" 0.29)...
LOOSE_DISTANCE = 0.34
#: ...and the best of the user's names at least this much closer than the next one (else two may be meant: none).
LOOSE_MARGIN = 0.12
#: Letters each side needs (a shorter spoken form matches only as written or by the strict rule).
LOOSE_MIN_LETTERS = 4
#: Runs of up to this many words of what was said are compared.
LOOSE_MAX_WORDS = 3
_LOOSE_OPEN = "aeiouhy"


def _spoken_forms(words: Sequence[str]) -> tuple[set[str], bool] | None:
    """The Latin readings of a run of words (lower case, :func:`yuki.memory.contacts.romanize`): an initialism spelled
    out by :data:`LETTER_NAMES`; each with a "c" also read as "k" (English says "soccer" with a k), and a final
    vowel + "r" also unsaid (an accent that drops it, or speech recognition that adds it); each with doubled letters
    once and as written. ``(readings, any word spelled out)``; ``None`` when a word has digits or no letters read."""
    from yuki.memory.contacts import romanize

    parts: list[str] = []
    spelled = False
    for w in words:
        if 2 <= len(w) <= SPELLED_MAX and w.isascii() and w.isalpha() and w.isupper():
            parts.append("".join(LETTER_NAMES.get(ch, "") for ch in w.casefold()))
            spelled = True
            continue
        if any(ch.isdigit() for ch in w):
            return None
        r = romanize(w)
        if not r:
            return None
        parts.append(r)
    base = "".join(parts)
    if not base:
        return None
    out: set[str] = set()
    for v in {base, base.replace("c", "k")}:
        for f in (v, "".join(ch for i, ch in enumerate(v) if i == 0 or ch != v[i - 1])):
            out.add(f)
            if len(f) > 2 and f[-1] == "r" and f[-2] in "aeiou":
                out.add(f[:-1])
    return out, spelled


def _loose_gap(said: Sequence[str], name: str) -> float | None:
    """How far a run of words is from one of the user's names by sound (the closest pair of their readings), or
    ``None`` when they can't be the same: another onset, too few letters, or (unless both were spelled out letter by
    letter) no common sound key (:func:`yuki.memory.contacts.sound_key`). Fixed format rules over the two spellings."""
    from yuki.memory.contacts import _distance, sound_key, spoken_words

    a = _spoken_forms(said)
    b = _spoken_forms([w for _s, w in spoken_words(name)])
    if a is None or b is None:
        return None
    (ra, sa), (rb, sb) = a, b
    if min(min(len(x) for x in ra), min(len(x) for x in rb)) < LOOSE_MIN_LETTERS:
        return None

    def onset(x: str) -> str:
        return "V" if x[0] in _LOOSE_OPEN else sound_key(x[:3])[:1]

    if {onset(x) for x in ra}.isdisjoint({onset(x) for x in rb}):
        return None
    if not (sa and sb) and {sound_key(x) for x in ra}.isdisjoint({sound_key(x) for x in rb}):
        return None
    return min(_distance(x, y) for x in ra for y in rb)


def _closest_heard(pool: Sequence[str], text: str) -> str | None:
    """fSkills (live 18 Oct: "Play soccer on Spotify" for the playlist lumen; "MBL's YouTube channel" for the channel
    NDL): with the strict rule finding none, the one of the user's OWN names (a closed set: their playlists, their
    channels) that a run of ``text`` is closest to by sound (:func:`_loose_gap` within :data:`LOOSE_DISTANCE`), and
    clearly closer than any other of them (:data:`LOOSE_MARGIN`); else ``None``. Only ever among the names the caller
    lists, never a guess at an open name; the done line names what ran, so a wrong pick shows at once."""
    try:
        from yuki.memory.contacts import spoken_words
    except Exception:  # noqa: BLE001
        return None
    words = [w for _s, w in spoken_words(text)]
    runs = [words[i:i + n] for n in range(1, LOOSE_MAX_WORDS + 1) for i in range(len(words) - n + 1)]
    best: dict[str, tuple[float, str]] = {}
    for name in pool:
        if not name:
            continue
        for run in runs:
            try:
                gap = _loose_gap(run, name)
            except Exception:  # noqa: BLE001 - a name that can't be read by sound
                gap = None
            if gap is not None and gap <= LOOSE_DISTANCE and (_norm(name) not in best or gap < best[_norm(name)][0]):
                best[_norm(name)] = (gap, name)
    if not best:
        return None
    ranked = sorted(best.values())
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < LOOSE_MARGIN:
        return None
    return ranked[0][1]


def _peak_hours(hist: list[float]) -> str:
    """When an entity is picked most, from its 24-hour histogram: the hours at least half as busy as its busiest, as
    ranges ("22-01h, 06h"); ``""`` with no picks. A format of numbers, never a judgement."""
    top_v = max(hist) if hist else 0.0
    if top_v <= 0:
        return ""
    on = [v >= 0.5 * top_v for v in hist]
    if all(on):
        return "any hour"
    start = next(i for i in range(24) if not on[i])     # begin after an off hour, so a range across midnight is one
    spans: list[tuple[int, int]] = []
    cur: int | None = None
    for k in range(1, 25):
        h = (start + k) % 24
        if on[h] and cur is None:
            cur = h
        elif not on[h] and cur is not None:
            spans.append((cur, (h - 1) % 24))
            cur = None
    if cur is not None:
        spans.append((cur, (start + 24) % 24))
    return ", ".join(f"{a:02d}h" if a == b else f"{a:02d}-{b:02d}h" for a, b in spans)


def _heard_titles(store: Any, kind: str, now: float, days: float,
                  limit: int | None = MEANING_TITLES) -> dict[str, list[str]]:
    """``{entity name (folded): titles}``: for a capability kind read from a media context (:func:`capability_kinds`:
    Spotify's playlist), the app's own window titles (the timeline's, the user's rows) seen while that entity's context
    ran in the last ``days``, at most ``limit`` each (``None``: all), most seen first; a title seen under two entities
    is the app's own frame, not something one of them holds, and is left out. Decrypted in RAM, never logged; ``{}``
    for any other kind or when it can't be read."""
    hit = next(((app, end) for k, app, end in capability_kinds() if k == kind), None)
    if hit is None:
        return {}
    app, end = hit
    since = now - float(days) * 86400.0
    try:
        moments = store.moments_between(since, now + 1.0, kinds=[end], app=app)
        rows = store._query(
            "SELECT started_at, title_ciphertext FROM timeline WHERE process = ? AND by_yuki = 0 AND started_at >= ? "
            "AND started_at <= ? AND title_ciphertext IS NOT NULL ORDER BY started_at DESC LIMIT 2000",
            (app, since, now))
    except Exception:  # noqa: BLE001
        return {}
    spans: list[tuple[float, float, str]] = []
    for i, m in enumerate(moments):
        try:
            name = _norm(store.moment_entity(m))
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        at = float(m.get("at") or 0.0)
        nxt = float(moments[i + 1].get("at") or at) if i + 1 < len(moments) else now
        spans.append((at, min(nxt, at + 3 * 3600.0), name))
    if not spans:
        return {}
    import bisect

    starts = [s[0] for s in spans]
    seen: dict[str, dict[str, int]] = {}
    for started, title_ct in rows:
        i = bisect.bisect_right(starts, float(started)) - 1
        if i < 0 or float(started) >= spans[i][1]:
            continue
        try:
            title = " ".join(str(store._dec(title_ct) or "").split())
        except Exception:  # noqa: BLE001
            continue
        if title:
            by = seen.setdefault(title, {})
            by[spans[i][2]] = by.get(spans[i][2], 0) + 1
    out: dict[str, dict[str, int]] = {}
    for title, by in seen.items():
        if len(by) == 1:
            name, count = next(iter(by.items()))
            out.setdefault(name, {})[title] = count
    return {name: [t for t, _c in sorted(titles.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]]
            for name, titles in out.items()}


#: fd (final-F D2): how far back the titles that played under a playlist are read for a track's playlist.
TRACK_DAYS = 60.0
#: A window title holds the session's title and artist with at most this many other characters (its separator).
TRACK_SLACK = 6


def _track_fits(window_title: str, title: str, artist: str) -> bool:
    """Whether an app's window title is the media session's track: both the session's title and its artist inside it,
    with no more than :data:`TRACK_SLACK` other characters (the app's own separator). Two measured texts compared,
    never the user's words. Pure."""
    w, t, a = _norm(window_title), _norm(title), _norm(artist)
    return bool(t and a) and t in w and a in w and len(w) <= len(t) + len(a) + TRACK_SLACK


def track_owner(store: Any, kind: str, title: str, artist: str, *, now: float | None = None,
                days: float = TRACK_DAYS) -> str:
    """fd (final-F D2; live 20 Oct: Spotify minimised never repainted its rows, so a press that played the asked
    playlist was "couldn't confirm it's X"): the one entity of ``kind`` this track played under, by memory's own data:
    the app's window titles seen while that entity's context ran (:func:`_heard_titles`, every title, a title seen
    under two entities left out) holding the media session's title and artist (:func:`_track_fits`). That entity's
    name, else ``""`` (unknown: never "not that one", since a track may be in several). Decrypted in RAM, never
    logged. Never raises."""
    if not str(title or "").strip() or not str(artist or "").strip():
        return ""
    now = time.time() if now is None else float(now)
    try:
        heard = _heard_titles(store, kind, now, days, limit=None)
    except Exception:  # noqa: BLE001
        return ""
    hits = {name for name, titles in heard.items() if any(_track_fits(t, title, artist) for t in titles)}
    return next(iter(hits)) if len(hits) == 1 else ""


def _test_spans(store: Any) -> list[tuple[float, float]]:
    """fd (final-F D7): the live tests' spans memory keeps (:func:`yuki.memory.testtag.store_test_spans`), oldest
    first; ``[]`` when none or unreadable. Never raises."""
    try:
        from yuki.memory.testtag import store_test_spans

        return sorted((float(a), float(b)) for a, b in store_test_spans(store))
    except Exception:  # noqa: BLE001
        return []


def _in_spans(at: float, spans: Sequence[tuple[float, float]]) -> bool:
    """Whether ``at`` lies inside one of ``spans`` (sorted, as :func:`_test_spans` gives them). Pure."""
    import bisect

    i = bisect.bisect_right([a for a, _b in spans], float(at)) - 1
    return i >= 0 and float(at) <= spans[i][1]


def described(store: Any, kind: str, *, now: float | None = None, names: Any = (), days: float = MEANING_DAYS,
              limit: int = MEANING_ITEMS) -> list[dict[str, Any]]:
    """lSkills: the user's own entities of ``kind`` with what memory knows of each, for a judgement of what words mean
    (:func:`judge`): ``{"name", "called", "hours", "plays", "heard"}`` - what the user called it before (the words
    paired with it: :func:`note` ``said_as``), when they pick it most (:func:`_peak_hours`), how often (the decayed
    count), and what played while it ran (:func:`_heard_titles`). The picked ones first, most picked first, then the
    names their own app showed, then ``names`` (rows a driver read just now) not known yet; at most ``limit``.
    Decrypted in RAM, never logged. Read only; never raises (``[]``)."""
    now = time.time() if now is None else float(now)
    try:
        rows = store._query("SELECT name_ciphertext, hist_weekday, hist_weekend, n_obs, source, last_at FROM tastes "
                            "WHERE kind = ?", (kind,))
        said = store._query("SELECT name_ciphertext, ref_ciphertext FROM tastes WHERE kind = ? "
                            "ORDER BY last_at DESC, id DESC", (said_kind(kind),))
    except Exception:  # noqa: BLE001
        return []
    called: dict[str, list[str]] = {}
    for words_ct, target_ct in said:
        try:
            words = " ".join(str(store._dec(words_ct) or "").split())
            target = _norm(store._dec(target_ct)) if target_ct else ""
        except Exception:  # noqa: BLE001
            continue
        if words and target:
            called.setdefault(target, []).append(words)
    heard = _heard_titles(store, kind, now, days)
    items: list[tuple[tuple[int, float, float], dict[str, Any]]] = []
    for name_ct, wd, we, n_obs, source, last_at in rows:
        try:
            name = " ".join(str(store._dec(name_ct) or "").split())
        except Exception:  # noqa: BLE001
            continue
        if not name:
            continue
        hist = [a + b for a, b in zip(_hist(wd), _hist(we))]
        item: dict[str, Any] = {"name": name}
        if called.get(_norm(name)):
            item["called"] = called[_norm(name)][:4]
        if (hours := _peak_hours(hist)):
            item["hours"] = hours
        if float(n_obs or 0.0) > 0:
            item["plays"] = round(float(n_obs), 1)
        if heard.get(_norm(name)):
            item["heard"] = heard[_norm(name)]
        picked = str(source or "") != SEEN_SOURCE and float(n_obs or 0.0) > 0
        items.append(((0 if picked else 1, -float(n_obs or 0.0), float(last_at or 0.0)), item))
    items.sort(key=lambda kv: kv[0])
    out: list[dict[str, Any]] = []
    have: set[str] = set()
    for _k, item in items:
        # review r1 L6: two rows with the same name would put that name twice in the judgement's enum (a request the
        # API may refuse): the first kept, which the order above makes the picked, most-played row
        if _norm(item["name"]) not in have:
            have.add(_norm(item["name"]))
            out.append(item)
    for n in names or []:
        clean = " ".join(str(n or "").split())
        if clean and _norm(clean) not in have:
            have.add(_norm(clean))
            out.append({"name": clean})
    return out[:max(0, int(limit))]


MEANING_SYSTEM = """You decide which of a person's own {things} their words mean, for a voice assistant about to
play it. Each item is one of theirs: its exact name, and what memory knows about it: the words they called it by before
("called": their own description of it, the strongest clue), when they usually pick it ("hours"), how much ("plays"),
and titles that played while it ran ("heard").
Answer only with the pick tool:
- name: the words say its name, or a mishearing or spelling of it.
- called: the words say what they called it before, or a word for the same language, scene or genre (say "K-pop" for
  an item they called "Korean", or "lofi" for one they called "study beats"), and no other item fits as well.
- content: the words name a language, a genre, a scene or an artist, and its name or its "heard" titles plainly show
  that of exactly one item.
- mood: the words ask for a feel or an activity without naming one of their {things} ("something upbeat", "music
  for a long drive", "something for cooking"), and no item's name or "called" words say it: pick nothing (their usual
  for the hour will play). A feel or an activity is never content.
- none: the words name one of their {things} by a word none of these goes by ("my party playlist" with no item called
  that), or two items fit equally: pick nothing.
"last time", when given, is the item these same words played last time: a strong clue for called, never a rule (a
newer item may fit them better, and words that ask for a mood stay mood).
"turned down", when given, is what they just said no to: it is not among the items. Words that then only ask for
another one, naming none, are mood."""


_meaning_cache: dict[tuple[str, str, str], tuple[float, Meant]] = {}
_meaning_lock = threading.Lock()
_client_box: list[Any] = []
_client_lock = threading.Lock()


def _judge_client(settings: Any = None) -> Any:
    """The small model's client, made once per process (the voice's own kind of failover client; a waited-on call).
    Making it costs about 0.5 s (settings, the failover table), so :func:`prewarm` makes it ahead of the first need."""
    with _client_lock:
        if _client_box:
            return _client_box[0]
        if settings is None:
            from yuki.config import Settings

            settings = Settings()
        from yuki.agent.failover import make_client

        client = make_client(settings, role="voice", waited_on=True)
        _client_box[:] = [client]
        return client


def prewarm() -> None:
    """Make :func:`judge`'s client on a short thread of its own, now (a caller that may need a judgement in a moment:
    the playlist rung, as its Library read starts, when the words name no row it knows). No model call; never raises."""
    if _client_box:
        return

    def make() -> None:
        try:
            _judge_client()
        except Exception:  # noqa: BLE001 - the judgement makes it then, or says unknown
            pass

    threading.Thread(target=make, name="yuki-taste-judge-warm", daemon=True).start()


def _settle(response: Any, names: list[str], text: str, ms: float,
            items: list[dict[str, Any]] | None = None) -> Meant:
    """A judgement's answer as a :class:`Meant`: the name only when it is one described (the tool's enum holds that
    already) and the code word allows one. kw3 (C8): the model's verdict stands (a ``name`` the words don't hold as
    written is a mishearing the model heard: it plays as Yuki's guess, named in the done line), never vetoed by a
    check of the words."""
    call = next((b for b in getattr(response, "content", []) or [] if getattr(b, "type", None) == "tool_use"), None)
    args = dict(getattr(call, "input", {}) or {}) if call is not None else {}
    why = str(args.get("why") or "")
    pick = " ".join(str(args.get("name") or "").split())
    name = next((n for n in names if _norm(n) == _norm(pick)), None) if pick else None
    if why not in MEANING_WHYS:
        why = "none"
    if why in ("mood", "none"):
        name = None
    elif name is None:
        why = "none"
    elif why == "called" and items is not None and not any(
            _norm(i.get("name")) == _norm(name) and i.get("called") for i in items):
        why = "content"     # nothing was ever called that: it can only be what the item holds
    return Meant(name, why, ms)


def judge(said: str, words: Any, items: list[dict[str, Any]], *, kind: str = "spotify_playlist",
          client: Any = None, model: str | None = None, settings: Any = None,
          wait_s: float = MEANING_WAIT_S, hint: str | None = None, not_these: Sequence[str] = ()) -> Meant:
    """lSkills (live 17 Oct: "anime playlist" asked which playlist twice although "Japanese" had just been paired with
    ネオン; "something chill" took 15-29 s): which of the user's own entities ``said`` means, judged by a small model
    (the voice's mouth model) on memory's own descriptions of them (:func:`described`), never by a word list. One
    forced tool call whose name field is an enum of the names described (it can only answer one of them, or none),
    bounded by ``wait_s`` on its own thread; ``Meant(None, "unknown")`` when there is nothing to judge, no client, or no
    answer in time (an answer that comes late is still kept for the same words and the same descriptions, so the next
    ask within :data:`MEANING_TTL_S` has it at once). kw3: the verdict stands as the model gave it (C8, no check of the
    words vetoes it); ``hint`` (C9), the item these same words played last time (:func:`said_target`), rides with the
    words as "last time" when it is one of the items, a clue the model weighs, never a lookup. Content (names, titles,
    words) goes to the model only, never to a log.

    fd (final-F D3; live 20 Oct k_playlist_not_that: "No, not this one. The other playlist." came as ``"Japanese"``,
    the judgement and its "last time" hint picked ネオン, the very row being corrected): ``not_these``, the items a
    correction leaves out (the corrected run's own rows, the engine's trace): never in the enum, never the hint, sent as
    "turned down" so words that only ask for another one read as mood (the hour's usual of the rest)."""
    started = time.perf_counter()
    gone = {_norm(n) for n in not_these or () if _norm(n)}
    items = [i for i in items if _norm(i.get("name")) not in gone]
    # each name once (review r1 L6: a duplicate value in the tool's enum may get the request refused)
    names = list(dict.fromkeys(str(i.get("name") or "") for i in items if i.get("name")))
    text = " ".join(str(said or "").split())
    if not names or not text:
        return Meant(None, "unknown")
    turns = [str(w) for w in (words if isinstance(words, (list, tuple)) else [words]) if str(w or "").strip()]
    # the same words against the same descriptions (a new pairing or play changes them, and the answer may change).
    # skill20 (final-D P5: a late answer judged with one exchange's "No, not this one. The other playlist" was replayed
    # 0.21 s into another exchange's "Play the Japanese playlist" and played the wrong row): the user's turns sent with
    # the request are part of the key, so an answer is reused only for the same request (a retry, the hand-off's own
    # run)
    last = next((n for n in names if hint and _norm(n) == _norm(hint)), "")
    down = sorted(" ".join(str(n).split()) for n in not_these or () if _norm(n))
    key = (str(kind), _norm(text), json.dumps(items, ensure_ascii=False, sort_keys=True),
           json.dumps([_norm(t) for t in turns[-3:]], ensure_ascii=False), _norm(last),
           json.dumps([_norm(n) for n in down], ensure_ascii=False))
    now_m = time.monotonic()
    with _meaning_lock:
        got = _meaning_cache.get(key)
        if got is not None and now_m - got[0] < MEANING_TTL_S:
            return got[1]
    try:
        if settings is None:
            from yuki.config import Settings

            settings = Settings()
        model = model or str(getattr(settings, "voice_mouth_model", "") or "")
        client = client if client is not None else _judge_client(settings)
    except Exception:  # noqa: BLE001 - no model here: no judgement
        return Meant(None, "unknown", round((time.perf_counter() - started) * 1000.0, 1))
    things = "playlists" if kind.endswith("playlist") else (kind.rsplit("_", 1)[-1] + "s")
    tool = {"name": "pick", "description": "Which of their items the words mean (or none).",
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string", "enum": names + [""], "description": "the item's exact name, or empty"},
                "why": {"type": "string", "enum": list(MEANING_WHYS)}}, "required": ["name", "why"]}}
    request = {"model": model, "max_tokens": 80,
               "system": MEANING_SYSTEM.replace("{things}", things),
               "tools": [tool], "tool_choice": {"type": "tool", "name": "pick"},
               "messages": [{"role": "user", "content": json.dumps(
                   {"said": text, "their words": turns[-3:], **({"last time": last} if last else {}),
                    **({"turned down": down} if down else {}), "items": items},
                   ensure_ascii=False)}]}
    box: dict[str, Any] = {}
    box_lock = threading.Lock()

    def keep(meant: Meant) -> None:
        with _meaning_lock:
            at = time.monotonic()
            for k in [k for k, (t, _m) in _meaning_cache.items() if at - t >= MEANING_TTL_S]:
                _meaning_cache.pop(k, None)
            _meaning_cache[key] = (at, meant)

    def run() -> None:
        try:
            response = client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001 - reported as no judgement
            box["error"] = type(exc).__name__
            return
        with box_lock:
            box["response"] = response
            late = bool(box.get("late"))
        if late:
            # the caller stopped waiting: kept for the next ask of the same words (a retry, the hand-off's own run)
            keep(_settle(response, names, text, round((time.perf_counter() - started) * 1000.0, 1), items))

    thread = threading.Thread(target=run, name="yuki-taste-meaning", daemon=True)
    thread.start()
    thread.join(max(0.1, float(wait_s)))
    ms = round((time.perf_counter() - started) * 1000.0, 1)
    with box_lock:
        response = box.get("response")
        if response is None:
            box["late"] = True
    if response is None:
        return Meant(None, "unknown", ms)
    meant = _settle(response, names, text, ms, items)
    keep(meant)
    return meant


def takes_offers(skill: Any, slot: Any) -> bool:
    """Whether ``slot`` is filled from the offered options (:func:`offered`, "the Deji one"): an entity slot with no
    taste kind that every route of ``skill`` runs only with a value carrying a field an offered option's value has and
    a spoken name lacks (:data:`OFFER_FIELDS`: ``when = { slot = "video", field = "ref", has = true }`` on
    ``web.open_video``). A name alone can never run such a slot, so the offers are what it picks from. Data only (the
    routes' own conditions, read through ``connection.py``): ``web.open_page``'s ``item`` (a handle opens by name) and
    a voice channel are not (tTaste r1 M3). Never raises (False)."""
    try:
        if getattr(slot, "type", "") != "entity" or getattr(slot, "taste", ""):
            return False
        name = str(getattr(slot, "name", "") or "")
        routes = tuple(getattr(skill, "routes", None) or ())
        if not name or not routes:
            return False
        return all(any(str(cond.get("slot") or "") == name and str(cond.get("field") or "") in OFFER_FIELDS
                       and bool(cond.get("has", True)) for cond in (getattr(r, "when", None) or ()))
                   for r in routes)
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------------
# Kinds from data
# ---------------------------------------------------------------------------------------------------------------------

#: How far back the recompute looks, and the half-life of a pick (map-routines T1.3).
SEED_DAYS = 60.0
HALF_LIFE_DAYS = 21.0
#: The same entity picked again within this long is one pick (a pause and resume, a page then its video).
SESSION_GAP_S = 1800.0
#: An entity whose decayed weight is under this drops out (about one pick seven weeks ago).
FLOOR = 0.2
#: kTaste (17 Oct audit, research 4.3: a click misleads, time watched counts): a pick weighs the units of what then
#: played (:func:`yuki.skills.youtube_feed.units`: five minutes = one), a visit to an entity's own page alone
#: :data:`VISIT_UNITS` (and a capability pick at least that: a pick is never weaker than a visit), a session at most
#: :data:`SESSION_UNITS`; a playlist's listening is read at most :data:`LISTEN_SPAN_S` past its pick.
VISIT_UNITS = 0.5
SESSION_UNITS = 8.0
LISTEN_SPAN_S = 3 * 3600.0
#: A page template variable with this name is an opaque id (a video's), opened only from a link or an offered option's
#: ref, never said (lSplit review r1 L5): its pages make no taste kind.
OPAQUE_VAR = "id"
#: Kinds an earlier seed wrote that no seed writes any more (sWeb's YouTube-only seed: now ``youtube_handle`` from the
#: site data): their ``observed`` rows are removed by the recompute (lWire review r1 L4). Our own old codes, never the
#: user's.
RETIRED_KINDS: tuple[str, ...] = ("yt_channel",)
#: A request of Yuki's counts as the user's pick when its runs' values came from these fill sources
#: (``skill_runs.filled``), and never when any came from :data:`GUESS_FILLS` (a guess must not feed itself). Only the
#: taste guess and Yuki's own pick for "something I'd like" (``chosen``, kTaste): ``pref`` fills means slots only
#: (``connection.py`` derives ``Slot.pref`` for ``role = "means"``: which browser, never which entity), so "open Deji's channel" in the usual browser is still the user's pick (tTaste r1 M1).
#: Phase 2.1's own guesses too (aResolve's note): ``menu``, a listed name the words don't hold (a pick from the
#: candidates), and ``reading``, the model's reading that nothing holds; ``card`` and ``screen`` (skill20 review: a
#: name read off a card or the screen, such as a title made a link from the feed or the page), as in
#: ``engine.GUESS_FILLS``.
ASKED_FILLS: frozenset[str] = frozenset({"words", "again", "told"})
GUESS_FILLS: frozenset[str] = frozenset({"taste", "chosen", "menu", "card", "screen", "reading"})
#: The fields an offered option's value carries beyond its name (``youtube_feed.Offer.value``): a slot every route of
#: which needs one of them is filled from the offers (:func:`takes_offers`).
OFFER_FIELDS: frozenset[str] = frozenset({"key", "ref"})
#: A recompute held back by a game or a call is tried again this long after, at most :data:`RETRIES` times.
RETRY_S = 1800.0
RETRIES = 8


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def entity_key(store: Any, kind: str, value: Any) -> str:
    """A taste's ``entity_key``: the store's keyed digest of the kind and the normalised id (a handle, a playlist's
    name), never the value in clear."""
    return str(store.cipher.digest("taste", str(kind), _norm(value)))


def _slot_of(at: float) -> tuple[bool, int]:
    stamp = datetime.fromtimestamp(float(at))
    return stamp.weekday() >= 5, stamp.hour


def site_kinds() -> list[tuple[str, str, str, str]]:
    """``(taste kind, site, page kind, variable)`` for every site file's page kind whose template has one speakable
    variable (``[pages]``; :data:`OPAQUE_VAR` left out), in file order: data only."""
    from yuki.connect.templates import site, sites
    from yuki.skills.connection import template_names

    out: list[tuple[str, str, str, str]] = []
    for name in sites():
        for kind, template in (site(name).get("pages") or {}).items():
            names = template_names(str(template))
            if len(names) != 1 or names[0] == OPAQUE_VAR:
                continue
            out.append((f"{name}_{kind}", name, str(kind), names[0]))
    return out


def capability_kinds(loaded: Any = None) -> list[tuple[str, str, str]]:
    """``(taste kind, app, end kind)`` for every capability fill slot that declares an entity kind and is read from its
    end moment's entity (``bind = { <slot> = "entity" }``): Spotify's ``playlist`` slot is ``("spotify_playlist",
    "spotify", "media_context")``. Data only (the shipped connection files); ``[]`` when they can't be read."""
    try:
        if loaded is None:
            from yuki.skills.connection import load

            loaded = load()
        caps = loaded.capabilities
    except Exception:  # noqa: BLE001
        return []
    out: list[tuple[str, str, str]] = []
    for cap in caps:
        bind = dict(getattr(cap, "bind", None) or {})
        for slot in cap.slots:
            if slot.taste and slot.type == "entity" and bind.get(slot.name) == "entity" and cap.app and cap.end:
                hit = (slot.taste, cap.app, cap.end)
                if hit not in out:
                    out.append(hit)
    return out


def slot_kind(skill: Any, slot: Any) -> str:
    """The taste kind a slot of a catalog entry fills from: the slot's own (``connection.py``: a capability's fill slot
    with an entity ``kind``), else, for a learned skill whose bound site and page are a page kind of
    :func:`site_kinds`, that kind (its entity slot: a channel on a site's channel pages). ``""``: none."""
    own = str(getattr(slot, "taste", "") or "")
    if own or getattr(slot, "type", "") != "entity":
        return own
    bound = dict(getattr(skill, "bound", None) or {})
    bind = dict(getattr(skill, "bind", None) or {})
    site_v = next((bound[s] for s, src in bind.items() if src == "site" and bound.get(s)), None)
    page_v = next((bound[s] for s, src in bind.items() if src == "page" and bound.get(s)), None)
    if not site_v or not page_v:
        return ""
    want = (str(site_v).casefold(), str(page_v).casefold())
    return next((k for k, s, p, _v in site_kinds() if (s.casefold(), p.casefold()) == want), "")


# ---------------------------------------------------------------------------------------------------------------------
# The nightly recompute (tTaste)
# ---------------------------------------------------------------------------------------------------------------------


def _asked_runs(store: Any, since: float) -> dict[str, bool]:
    """``{request_id: the user named its values}`` of Yuki's runs since ``since`` (``skill_runs.filled``): True when a
    run's value came from their words (:data:`ASKED_FILLS`) and no run of that request took a guess
    (:data:`GUESS_FILLS`). Keyed on the request alone, so a moment counts whatever end its run declared: the watch page
    of "the Deji one" belongs to a ``web.open_video`` run whose end is ``media_play`` (tTaste r1 L3)."""
    try:
        rows = store._query("SELECT request_id, filled FROM skill_runs WHERE t0 >= ? AND request_id IS NOT NULL",
                            (float(since) - 3600.0,))
    except Exception:  # noqa: BLE001 - no ledger yet
        return {}
    named: set[str] = set()
    guessed: set[str] = set()
    for request_id, filled in rows:
        try:
            sources = {str(v) for v in (json.loads(filled or "{}") or {}).values()}
        except (TypeError, ValueError, AttributeError):
            sources = set()
        key = str(request_id)
        if sources & GUESS_FILLS:
            guessed.add(key)
        elif sources & ASKED_FILLS:
            named.add(key)
    return {key: key not in guessed for key in named | guessed}


def _not_mine_moments(store: Any) -> set[int]:
    """The end moments of the occasions of skills the user marked Not mine or Forget this (lPage's tombstones): they
    feed no taste (the learner keeps their occasions on the tombstone's id)."""
    try:
        return {int(r[0]) for r in store._query(
            "SELECT o.end_moment_id FROM occasions o JOIN skills s ON s.id = o.skill_id WHERE o.end_moment_id IS NOT "
            "NULL AND (s.deleted_at IS NOT NULL OR s.state IN ('deleted', 'not_mine'))")}
    except Exception:  # noqa: BLE001 - no learner tables yet
        return set()


def _listened(store: Any, heard: list[tuple[str, str, float, str, str, str, float]], rows: list[dict[str, Any]],
              since: float, now: float) -> list[float]:
    """Per capability pick of ``heard`` (``(kind, group, at, value, ref, app, moment at)``), the units of what then
    played (:func:`yuki.skills.youtube_feed.units`): the app's own ``media_play`` spans from the pick until the app's
    next pick of that end kind (any entity, anyone's), at most :data:`LISTEN_SPAN_S`. 1.0 each when they can't be read."""
    if not heard:
        return []
    try:
        from yuki.skills.youtube_feed import units

        out: list[float] = []
        spans: dict[str, list[tuple[float, float]]] = {}
        bounds: dict[str, list[float]] = {}
        for r in rows:
            if r.get("kind") != "page" and r.get("app"):
                bounds.setdefault(str(r["app"]), []).append(float(r["at"]))
        for _k, _g, at, _v, _r, app, seen_at in heard:
            if app not in spans:
                spans[app] = [(float(m["at"]), float(m.get("ended_at") or m["at"])) for m in store.moments_between(
                    since, now + 1.0, kinds=["media_play"], app=app)]
            nxt = next((b for b in sorted(bounds.get(app, [])) if b > seen_at), now)
            end = min(nxt, at + LISTEN_SPAN_S, now)
            secs = sum(max(0.0, min(end, b) - max(at, a)) for a, b in spans[app])
            out.append(units(secs))
        return out
    except Exception:  # noqa: BLE001 - a pick still counts once
        return [1.0] * len(heard)


def _user_picks(store: Any, since: float) -> Callable[[Mapping[str, Any]], bool]:
    """Whether a moment is one of the user's own picks: their own (``by`` user), or a request of Yuki's whose values
    came from their words (:func:`_asked_runs`); never a skill they marked Not mine (:func:`_not_mine_moments`). fd
    (final-F D7; live 20 Oct youtube_channel_deji_arc: the channel page Yuki's own test runs opened, "@Deezy", read as
    one of the user's channels "as written", so the measured swap to Deji never fired): never a moment inside a live
    test's span (:func:`_test_spans`, the spans memory keeps past a purge; links' own rows leave them out the same
    way): what a test or Yuki did while one ran is nobody's taste."""
    asked = _asked_runs(store, since)
    skip = _not_mine_moments(store)
    spans = _test_spans(store)

    def users(r: Mapping[str, Any]) -> bool:
        if r.get("id") is not None and int(r["id"]) in skip:
            return False
        try:
            if spans and _in_spans(float(r.get("at") or 0.0), spans):
                return False
        except (TypeError, ValueError):
            return False
        if r.get("by", "user") == "user":
            return True
        return asked.get(str(r.get("request_id") or ""), False)

    return users


class _Picks:
    """The picks gathered per ``(kind, group)``: ``(at, value, ref, units)``, later collapsed into sessions and decayed.
    A session (the same entity again within :data:`SESSION_GAP_S`) weighs its picks' units summed, at most
    :data:`SESSION_UNITS` (kTaste: time watched or listened, not clicks: two long videos of one channel in a sitting
    weigh more than one, a channel page alone little)."""

    def __init__(self) -> None:
        self.by: dict[tuple[str, str], list[tuple[float, str, str, float]]] = {}

    def add(self, kind: str, group: str, at: float, value: str, ref: str, units: float = 1.0) -> None:
        self.by.setdefault((kind, group), []).append((float(at), value, ref, max(0.0, float(units))))

    def rows(self, store: Any, now: float) -> dict[tuple[str, str], dict[str, Any]]:
        """``{(kind, entity_key): row}``: two decayed hour histograms, the decayed units, the newest name and link."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for (kind, _group), picks in self.by.items():
            picks.sort(key=lambda p: p[0])
            sessions: list[list[float]] = []         # [first at, units]
            last = None
            name, ref = "", ""
            for at, value, link, units in picks:
                if last is not None and at - last < SESSION_GAP_S and sessions:
                    sessions[-1][1] = min(SESSION_UNITS, sessions[-1][1] + units)
                else:
                    sessions.append([at, min(SESSION_UNITS, units)])
                last = at
                name, ref = value or name, link or ref
            wd, we = [0.0] * 24, [0.0] * 24
            total, kept = 0.0, 0
            for at, units in sessions:
                weight = units * math.pow(0.5, max(0.0, now - at) / (HALF_LIFE_DAYS * 86400.0))
                weekend, hour = _slot_of(at)
                (we if weekend else wd)[hour] += weight
                total += weight
                kept += 1
            if total < FLOOR or not name:
                continue
            key = entity_key(store, kind, name)
            row = out.get((kind, key))
            if row is not None:   # two groups, one name (a digest-only entity met its named twin): one row
                row["wd"] = [a + b for a, b in zip(row["wd"], wd)]
                row["we"] = [a + b for a, b in zip(row["we"], we)]
                row["n"] += total
                row["picks"] += kept
                row["last"] = max(row["last"], picks[-1][0])
                continue
            out[(kind, key)] = {"name": name, "ref": ref, "wd": wd, "we": we, "n": total, "picks": kept,
                                "last": picks[-1][0]}
        return out


def recompute(store: Any, *, now: float | None = None, days: float = SEED_DAYS, only: str | None = None,
              loaded: Any = None) -> dict[str, Any]:
    """Recompute the ``observed`` taste rows of every kind from data (:func:`capability_kinds`, :func:`site_kinds`, and
    the watched videos :mod:`~yuki.skills.youtube_feed` attributed to a channel) from the last ``days`` of moments (see
    the module docstring). ``only="site"``: the site kinds alone (:func:`seed_sites`). Rows of a computed kind that no
    pick supports any more are removed, and so are the ``observed`` rows of :data:`RETIRED_KINDS`; ``said`` rows of
    other kinds (:func:`note`) are left alone. Idempotent; content-free counts back; never raises (``{"error": 1}``)."""
    now = time.time() if now is None else float(now)
    started = time.perf_counter()
    since = now - float(days) * 86400.0
    caps = [] if only == "site" else capability_kinds(loaded)
    sites = {f"{s}.{p}": (k, s, p, v) for k, s, p, v in site_kinds()}
    kinds = sorted({k for k, _a, _e in caps} | {k for k, _s, _p, _v in sites.values()})
    counts: dict[str, Any] = {"kinds": len(kinds), "entities": 0, "picks": 0, "visits": 0, "removed": 0}
    if not kinds:
        return counts
    ends = sorted({e for _k, _a, e in caps} | ({"page"} if sites else set()))
    try:
        rows = store.moments_between(since, now + 1.0, kinds=ends)
    except Exception:  # noqa: BLE001 - no moments table yet
        return {**counts, "error": 1}
    users = _user_picks(store, since)

    from yuki.connect.templates import fill

    picks = _Picks()
    cap_of = {(a, e): k for k, a, e in caps}
    page_rows: list[dict[str, Any]] = []
    heard: list[tuple[str, str, float, str, str, str, float]] = []
    for r in rows:
        if r.get("kind") == "page":
            page_rows.append(r)
        if not users(r):
            continue
        if r.get("kind") == "page":
            hit = sites.get(str((r.get("facts") or {}).get("template") or ""))
            if hit is None:
                continue
            kind, site_name, page_kind, var = hit
            try:
                value = " ".join(str(store.moment_entity(r) or "").split())
            except Exception:  # noqa: BLE001 - a row the key can't read
                continue
            if not value:
                continue
            group = str(r.get("entity_key") or _norm(value))
            picks.add(kind, group, float(r["at"]), value, fill(site_name, page_kind, **{var: value}) or "",
                      VISIT_UNITS)
            counts["visits"] += 1
            continue
        kind = cap_of.get((str(r.get("app") or ""), str(r.get("kind") or "")))
        if kind is None:
            continue
        try:
            value = " ".join(str(store.moment_entity(r) or "").split())
            facts = r.get("facts") or {}
            ref = store._dec(facts["uri_ciphertext"]) if facts.get("uri_ciphertext") else ""
        except Exception:  # noqa: BLE001
            continue
        if not value:   # a digest-only entity (a Spotify-owned list): nothing to say or fill by name
            continue
        if facts.get("initial"):   # already playing when the reader started: when it was picked is unknown
            continue
        # the reader's own fact: when the media read saw the music start (the pick), else the moment's time
        since_at = facts.get("since")
        ok_since = isinstance(since_at, (int, float)) and not isinstance(since_at, bool) and since_at <= float(r["at"])
        at = float(since_at) if ok_since else float(r["at"])
        heard.append((kind, str(r.get("entity_key") or _norm(value)), at, value, ref or "", str(r.get("app") or ""),
                      float(r["at"])))
        counts["picks"] += 1
    # kTaste: a playlist pick weighs the time it then played (the app's own media spans until the next pick), not 1;
    # the pick itself at least :data:`VISIT_UNITS`, as a visit to an entity's page (review M3: a list picked once and
    # played briefly stays known, over :data:`FLOOR`, for weeks)
    for (kind, group, at, value, ref, app, seen_at), got in zip(heard, _listened(store, heard, rows, since, now)):
        picks.add(kind, group, at, value, ref, max(VISIT_UNITS, got))
    channel_kind = ""
    fresh: dict[str, float] = {}
    if page_rows:
        try:
            from yuki.skills import youtube_feed

            channel_kind = youtube_feed.CHANNEL_KIND if youtube_feed.CHANNEL_KIND in kinds else ""
            if channel_kind:
                for at, handle, ref, group, got in youtube_feed.attributed(store, page_rows, users, now=now):
                    picks.add(channel_kind, group, at, handle, ref, got)
                    counts["picks"] += 1
                fresh = youtube_feed.fresh_ages(store)
        except Exception:  # noqa: BLE001 - the channel picks from pages alone
            channel_kind = ""
    found = picks.rows(store, now)

    def write(conn: Any) -> int:
        marks = ",".join("?" * len(kinds))
        old = {(str(k), str(e)) for k, e in conn.execute(
            f"SELECT kind, entity_key FROM tastes WHERE source = 'observed' AND kind IN ({marks})", kinds).fetchall()}
        for (kind, key), e in found.items():
            conn.execute(
                "INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                "hist_weekend, n_obs, last_at, fresh_age_h, updated_at) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?, ?, "
                "?) ON CONFLICT(kind, entity_key) DO UPDATE SET name_ciphertext = excluded.name_ciphertext, "
                "ref_ciphertext = coalesce(excluded.ref_ciphertext, ref_ciphertext), source = 'observed', "
                "hist_weekday = excluded.hist_weekday, hist_weekend = excluded.hist_weekend, n_obs = excluded.n_obs, "
                "last_at = excluded.last_at, fresh_age_h = excluded.fresh_age_h, updated_at = excluded.updated_at",
                (kind, key, store._enc(e["name"]), store._enc(e["ref"]) if e["ref"] else None,
                 json.dumps([round(x, 4) for x in e["wd"]]), json.dumps([round(x, 4) for x in e["we"]]),
                 round(e["n"], 4), e["last"], fresh.get(key) if kind == channel_kind else None, now))
        gone = sorted(old - set(found))
        for kind, key in gone:
            conn.execute("DELETE FROM tastes WHERE kind = ? AND source = 'observed' AND entity_key = ?", (kind, key))
        retired = conn.execute("DELETE FROM tastes WHERE source = 'observed' AND kind IN"
                               f" ({','.join('?' * len(RETIRED_KINDS))})", RETIRED_KINDS).rowcount
        return len(gone) + max(0, int(retired or 0))

    try:
        removed = store._write(write)
    except Exception:  # noqa: BLE001
        return {**counts, "error": 1}
    counts.update(entities=len(found), removed=int(removed), ms=round((time.perf_counter() - started) * 1000, 1))
    return counts


def seed_sites(store: Any, *, now: float | None = None, days: float = SEED_DAYS) -> dict[str, Any]:
    """The site kinds alone (:func:`recompute` with ``only="site"``): kept for the callers from before phase T."""
    return recompute(store, now=now, days=days, only="site")


def note(store: Any, kind: str, name: str, *, ref: str | None = None, at: float | None = None,
         source: str = "said", said_as: str | None = None) -> bool:
    """One choice the user named and a skill saw done (a voice channel joined): ``kind``'s row for ``name`` gets one
    more observation at that hour (its name and link encrypted); a row that was only a name their app showed
    (:data:`SEEN_SOURCE`) becomes a pick of ``source``. ``said_as``: the words the request named it by when those
    words named no row (fix-plan Item 7: "my Japanese playlist" for "ネオン", seen playing after the hand-off): kept as
    another name of this row (:func:`said_target`; the newest pairing of the same words wins). Never raises (False on
    failure)."""
    clean = " ".join(str(name or "").split())
    if not clean or not str(kind or "").strip():
        return False
    at = time.time() if at is None else float(at)
    weekend, hour = _slot_of(at)
    key = entity_key(store, kind, clean)
    said = " ".join(str(said_as or "").split())
    said = said if said and _norm(said) != _norm(clean) else ""
    said_key = entity_key(store, said_kind(kind), said) if said else ""

    def write(conn: Any) -> None:
        row = conn.execute("SELECT hist_weekday, hist_weekend, n_obs FROM tastes WHERE kind = ? AND entity_key = ?",
                           (kind, key)).fetchone()
        wd, we = (_hist(row[0]), _hist(row[1])) if row else ([0.0] * 24, [0.0] * 24)
        (we if weekend else wd)[hour] += 1.0
        n = float(row[2] or 0.0) + 1.0 if row else 1.0
        if row:
            conn.execute("UPDATE tastes SET name_ciphertext = ?, ref_ciphertext = coalesce(?, ref_ciphertext), "
                         "hist_weekday = ?, hist_weekend = ?, n_obs = ?, last_at = ?, updated_at = ?, "
                         "source = CASE WHEN source = ? THEN ? ELSE source END WHERE kind = ? AND entity_key = ?",
                         (store._enc(clean), store._enc(ref) if ref else None, json.dumps(wd), json.dumps(we), n, at,
                          at, SEEN_SOURCE, source, kind, key))
        else:
            conn.execute("INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                         "hist_weekend, n_obs, last_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (kind, key, store._enc(clean), store._enc(ref) if ref else None, source, json.dumps(wd),
                          json.dumps(we), n, at, at))
        if said:
            # what they called it: the words (encrypted) keyed by their digest, the row's name (encrypted) as the target
            zero = json.dumps([0.0] * 24)
            conn.execute("INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                         "hist_weekend, n_obs, last_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?) "
                         "ON CONFLICT(kind, entity_key) DO UPDATE SET name_ciphertext = excluded.name_ciphertext, "
                         "ref_ciphertext = excluded.ref_ciphertext, n_obs = tastes.n_obs + 1, "
                         "last_at = excluded.last_at, updated_at = excluded.updated_at",
                         (said_kind(kind), said_key, store._enc(said), store._enc(clean), source, zero, zero, at, at))

    try:
        store._write(write)
        return True
    except Exception:  # noqa: BLE001
        return False


def saw(store: Any, kind: str, names: Any, *, at: float | None = None) -> int:
    """Names the user's own app showed for an entity ``kind`` (the Library rows Spotify's driver read: their own
    playlists, fix-plan Item 7), kept as :data:`SEEN_SOURCE` rows (name encrypted, keyed digest, no pick): only names
    with no row of ``kind`` yet are written (a picked row is never touched), in the order shown; at most
    :data:`SEEN_MAX` seen rows per kind (the oldest go). :func:`candidates` and :func:`find` then know them; a
    :func:`note` on one turns it into a pick. The nightly recompute leaves them alone: it rewrites ``observed`` rows
    only, keyed by the moments' own digest, so an observed row of the same name may sit beside a seen one
    (:func:`candidates` lists a name once). Returns how many were added; never raises (0)."""
    if not str(kind or "").strip():
        return 0
    clean: list[str] = []
    for n in names or []:
        c = " ".join(str(n or "").split())
        if c and _norm(c) not in {_norm(x) for x in clean}:
            clean.append(c)
    if not clean:
        return 0
    at = time.time() if at is None else float(at)
    try:
        keyed = [(entity_key(store, kind, n), n) for n in clean]
    except Exception:  # noqa: BLE001 - no key: nothing is kept
        return 0
    zero = json.dumps([0.0] * 24)

    def write(conn: Any) -> int:
        have = {str(r[0]) for r in conn.execute("SELECT entity_key FROM tastes WHERE kind = ?", (kind,)).fetchall()}
        added = 0
        for i, (key, name) in enumerate(keyed):
            if key in have:
                continue
            stamp = at + i * 1e-3          # the order shown, kept by the time (the menu lists them in that order)
            got = conn.execute("INSERT OR IGNORE INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, "
                               "source, hist_weekday, hist_weekend, n_obs, last_at, updated_at) VALUES (?, ?, ?, NULL, "
                               "?, ?, ?, 0, ?, ?)",
                               (kind, key, store._enc(name), SEEN_SOURCE, zero, zero, stamp, stamp))
            added += max(0, int(got.rowcount or 0))
            have.add(key)
        extra = conn.execute("SELECT id FROM tastes WHERE kind = ? AND source = ? ORDER BY last_at DESC, id DESC "
                             "LIMIT -1 OFFSET ?", (kind, SEEN_SOURCE, SEEN_MAX)).fetchall()
        for (rid,) in extra:
            conn.execute("DELETE FROM tastes WHERE id = ?", (rid,))
        return added

    try:
        return int(store._write(write) or 0)
    except Exception:  # noqa: BLE001
        return 0


def unsay(store: Any, kind: str, target: str, *, words: str | None = None) -> int:
    """Drop what the user's words were taken to mean (:func:`note` ``said_as``) when their later choice contradicts it
    (fix-plan Item 7, review r1 M2: what they say wins over what was learned): the ``<kind>@said`` rows whose target is
    ``target`` (case and spacing folded); with ``words``, only those words' row, and only while it still points at
    ``target`` (a newer pairing of the same words is kept). The words then name no row again, so the driver hands
    over once more and may learn again. The target row's own picks stay (it did play). Returns how many were dropped;
    never raises (0)."""
    want = _norm(target)
    if not want or not str(kind or "").strip():
        return 0
    sk = said_kind(kind)
    try:
        key = entity_key(store, sk, words) if words is not None and _norm(words) else None
    except Exception:  # noqa: BLE001 - no key: nothing to match
        return 0

    def write(conn: Any) -> int:
        if key is not None:
            rows = conn.execute("SELECT id, ref_ciphertext FROM tastes WHERE kind = ? AND entity_key = ?",
                                (sk, key)).fetchall()
        else:
            rows = conn.execute("SELECT id, ref_ciphertext FROM tastes WHERE kind = ?", (sk,)).fetchall()
        gone = 0
        for rid, ref_ct in rows:
            try:
                name = _norm(store._dec(ref_ct)) if ref_ct else ""
            except Exception:  # noqa: BLE001 - a row the key can't read is left alone
                continue
            if name == want:
                conn.execute("DELETE FROM tastes WHERE id = ?", (rid,))
                gone += 1
        return gone

    try:
        return int(store._write(write) or 0)
    except Exception:  # noqa: BLE001
        return 0


def busy(store: Any, now: float | None = None) -> str | None:
    """A code word when the nightly pass must wait, else None: ``game`` (a game in front, D9's local read),
    ``call`` (a call or a game's voice chat open: :func:`yuki.memory.portrait.scheduled_busy`, the nightly portrait's
    own rule), ``unknown`` (the game check failed: fails closed). The learner's ``quiet`` without its idle rule."""
    now = time.time() if now is None else float(now)
    try:
        from yuki.memory.gamegate import game_in_front

        if game_in_front():
            return "game"
    except Exception:  # noqa: BLE001
        return "unknown"
    try:
        from yuki.memory.portrait import scheduled_busy

        if scheduled_busy(store, None, now):
            return "call"
    except Exception:  # noqa: BLE001 - a store without calls: nothing holds it
        pass
    return None


_retry_lock = threading.Lock()
_retry: dict[str, Any] = {"timer": None, "left": RETRIES}


def _retry_later(store: Any, log: LogFn | None) -> bool:
    """One one-shot timer (no loop) that runs :func:`seed` again in :data:`RETRY_S`, at most :data:`RETRIES` times in a
    row. False when none is left or one is already set."""
    with _retry_lock:
        if _retry["timer"] is not None or int(_retry["left"]) <= 0:
            return False
        _retry["left"] = int(_retry["left"]) - 1

        def again() -> None:
            with _retry_lock:
                _retry["timer"] = None
            got = seed(store, log=log)
            if log is not None:
                try:
                    log("taste_seed", retry=1, **_numbers(got.get("yt_channel") or {}))
                except Exception:  # noqa: BLE001
                    pass

        timer = threading.Timer(RETRY_S, again)
        timer.daemon = True
        timer.name = "yuki-taste-retry"
        _retry["timer"] = timer
    timer.start()
    return True


def _numbers(d: dict[str, Any]) -> dict[str, float]:
    return {k: v for k, v in d.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def seed(store: Any, *, now: float | None = None, log: LogFn | None = None, retry: bool = True,
         network: bool = True) -> dict[str, Any]:
    """The nightly taste pass (the memory service's daily hook): never with a game in front or a call (then a one-shot
    retry, :data:`RETRY_S`); else :func:`recompute`, then YouTube's nightly part
    (:func:`yuki.skills.youtube_feed.nightly`: channel ids and watched videos' channels, within the shared cap of
    reads; the offers' outcomes; pruning). ``network=False``: no reads (a copy, a replay). Content-free counts; the
    memory service's ``taste_seed`` log line reads the numbers under ``yt_channel`` (the first key sWeb's, kept as
    an alias for the whole pass)."""
    hold = busy(store, now)
    if hold is not None:
        retried = _retry_later(store, log) if retry else False
        flat = {"held": 1, "retry_set": int(retried)}
        return {"held": hold, "yt_channel": flat}
    with _retry_lock:
        _retry["left"] = RETRIES
    got = recompute(store, now=now)
    yt: dict[str, Any] = {}
    try:
        from yuki.skills import youtube_feed

        yt = youtube_feed.nightly(store, now=now, log=log, network=network)
    except Exception:  # noqa: BLE001 - the tastes stand without YouTube's part
        yt = {"error": 1}
    flat = {**_numbers(got), **{f"yt_{k}": v for k, v in _numbers(yt).items()}}
    return {"pages": got, "youtube": yt, "yt_channel": flat}


# ---------------------------------------------------------------------------------------------------------------------
# kTaste: "something I'd like" (contract C2; audit 17 Oct decision 2, research 4.3)
# ---------------------------------------------------------------------------------------------------------------------

#: The capabilities a request may ask Yuki to choose for (``slots.choose = true``) and what each picks: a YouTube video
#: (it plays), a YouTube channel (its page opens), a playlist of the user's own (it plays). Generic capabilities only;
#: which videos, channels and playlists exist is learned.
CHOOSE_KINDS: dict[str, str] = {"web.open_video": "video", "web.open_page": "channel", "spotify.playlist": "playlist"}
#: The slot each kind fills.
CHOOSE_SLOTS: dict[str, str] = {"video": "video", "channel": "item", "playlist": "playlist"}
#: The picks of one conversation are remembered this long after the last one (RAM): "another one" / "not that" takes
#: the next candidate, never one already put on.
CONVO_S = 1800.0
#: A pick changed this soon (asked again, the video closed, another playlist started) was skipped: negative feedback,
#: its weight halving every :data:`SKIP_HALF_LIFE_DAYS`.
SKIP_S = 20.0
SKIP_HALF_LIFE_DAYS = 14.0
SKIP_DAYS = 60.0
#: How a candidate scores: its taste at this hour (:func:`score_at`, time watched or listened, decayed) plus this share of
#: its all-time weight (a favourite still counts at an odd hour); halved per recent skip; for a video, times
#: ``FRESH_FLOOR + exp(-age / AGE_DAYS)`` (new first, an unwatched older upload still a candidate).
TOTAL_W = 0.1
FRESH_FLOOR = 0.3
AGE_DAYS = 7.0
#: A video candidate comes from the feeds of the best this-many channels (read when older than an hour, within
#: :data:`CHOOSE_FEED_S`), at most two unwatched uploads per channel (the second weighs this much less).
CHOOSE_CHANNELS = 8
SECOND_W = 0.7
#: How long a pick waits for those feeds (a slower one finishes on its own and serves the next pick; feeds are kept an
#: hour, so only the first pick of an hour waits).
CHOOSE_FEED_S = 1.0
#: The judgement of the user's words over the candidates: at most this many, bounded by this long (one small model
#: call; the habit order stands when it is late).
CHOOSE_N = 8
CHOOSE_WAIT_S = 2.5
#: The whole pick (feeds, judgement) ends within this long of its start (the engine waits a little more,
#: ``engine.CHOOSE_TIMEOUT_S``): the judgement gets what is left of it, and none under :data:`JUDGE_MIN_S`.
CHOOSE_DEADLINE_S = 4.0
JUDGE_MIN_S = 0.6
#: A candidate's hour fit at least this share of its own best hour today: "you usually ... around now".
HABIT_SHARE = 0.5
#: Phase 2.2: where a given channel or playlist came from when it is Yuki's own pick, not their words: asked to choose
#: (``choose`` true), the talker or the planner gave the card's fitting pick for now (:func:`now_picks`) by name (the
#: engine's tag for a name under choose that they never said; the resolver's source tag of the same name, phase 2.1).
FROM_CARD = "card"

CHOOSE_SYSTEM = """You help a voice assistant pick what to put on for a person who asked it to choose for them. Their
own {things} are listed in the order their habits suggest for right now (best first): what they watch or play at this
hour, how much, how recently, and what is new. Rank them for what they asked. Keep the habit order unless their words
say what they are doing or the mood they want and some items plainly suit that better than others (a long video to
eat along to, something calm when they want it chill, music when they want music). Judge by what each item holds (the
titles they watched or that played, what they called it, how long they usually stay), never by its name alone. Answer
only with the rank tool: every id once, best first."""


@dataclass
class Choice:
    """Yuki's pick for "something I'd like": its ``kind`` (:data:`CHOOSE_KINDS`), the slot it fills and that slot's
    value (``{"name", "key", "ref"}``), other slot values it sets (``extra``: a channel's page kind), the name said, its
    key and the key feedback attaches to (``group``: the channel or the playlist), the code-written why, how old a
    video is (days), its title (RAM only), whether the user's words were judged, among how many, how long it took,
    whether it is the channel or playlist they named (``named``: their pick, filled ``words``), and where a given
    channel or playlist came from (``source``: :data:`FROM_CARD` when the talker took it from the card's picks for
    now, a guess of Yuki's like the recommender's own: never ``named``, never feeding the taste it came from)."""

    kind: str
    slot: str
    value: dict[str, Any]
    extra: dict[str, Any]
    name: str
    key: str
    group: str
    why: str = ""
    fresh_d: float | None = None
    title: str = ""
    judged: bool = False
    n: int = 0
    ms: float = 0.0
    named: bool = False
    source: str = ""


@dataclass
class _Cand:
    name: str
    key: str
    group: str
    value: dict[str, Any]
    score: float
    fit: float
    peak: float
    total: float
    last_at: float
    hist: list[float]
    fresh_d: float | None = None
    title: str = ""
    desc: dict[str, Any] = field(default_factory=dict)


_convo_lock = threading.Lock()
_convo: dict[tuple[str, str], dict[str, Any]] = {}


def _convo_state(session: str, kind: str, now: float) -> dict[str, Any]:
    """The picks of this conversation for ``kind`` (``{"keys", "last", "at", "context"}``), forgotten
    :data:`CONVO_S` after the last one."""
    with _convo_lock:
        for k in [k for k, v in _convo.items() if now - float(v.get("at") or 0.0) > CONVO_S]:
            _convo.pop(k, None)
        return dict(_convo.get((str(session or ""), kind)) or {})


def _peak(wd: list[float], we: list[float], now: float) -> float:
    """The best hour fit of today's kind of day (:func:`score_at` at each hour of today): what "usually" is measured
    against."""
    stamp = datetime.fromtimestamp(float(now))
    base = now - (stamp.hour * 3600 + stamp.minute * 60 + stamp.second)
    return max(score_at(wd, we, base + h * 3600.0 + 1800.0) for h in range(24))


def _skip_weights(store: Any, now: float) -> dict[str, float]:
    """``{group: decayed skips}``: picks of Yuki's the user skipped (asked again within :data:`SKIP_S`, or the video's
    page shown under :data:`SKIP_S` in all, or another playlist within :data:`SKIP_S`), halving every
    :data:`SKIP_HALF_LIFE_DAYS`. Never raises (``{}``)."""
    try:
        events = store.taste_events(since=now - SKIP_DAYS * 86400.0, until=now + 1.0, kinds=["chose", "skipped"])
    except Exception:  # noqa: BLE001
        return {}
    # one skip per pick (review L2): a pick asked away again (``skipped``, its ``chose_at``) is not counted again when
    # its page also closed fast
    again = {(str(d.get("key") or ""), round(float(d.get("chose_at") or 0.0), 1))
             for d in (e.get("detail") or {} for e in events if e.get("kind") == "skipped")}
    out: dict[str, float] = {}
    for e in events:
        d = e.get("detail") or {}
        group, at = str(d.get("group") or ""), float(e.get("at") or 0.0)
        if not group:
            continue
        if e.get("kind") == "chose" and ((str(d.get("key") or ""), round(at, 1)) in again
                                         or not _closed_fast(store, d, at, now)):
            continue
        out[group] = out.get(group, 0.0) + math.pow(0.5, max(0.0, now - at) / (SKIP_HALF_LIFE_DAYS * 86400.0))
    return out


#: A chosen video's page opened at most this long before :func:`chosen` stamped the pick (the route ran and its
#: done-check saw the page first; a page moment's ``at`` is its visit's first row): the quick-close window starts then.
CHOSE_LEAD_S = 15.0


def _closed_fast(store: Any, detail: Mapping[str, Any], at: float, now: float) -> bool:
    """Whether a pick Yuki put on was closed or changed within :data:`SKIP_S`: a video whose watch page was in front
    under that long in all within ten minutes (read once ten minutes have passed), a playlist another one replaced that
    fast."""
    try:
        if detail.get("kind") == "video":
            if now - at < 600.0:
                return False
            rows = [r for r in store.moments_between(at - CHOSE_LEAD_S, at + 600.0, kinds=["page"])
                    if str(r.get("entity_key") or "") == str(detail.get("key") or "")]
            return bool(rows) and sum(float(r.get("value") or 0.0) for r in rows) < SKIP_S
        if detail.get("kind") == "playlist" and detail.get("taste_kind"):
            for r in store.moments_between(at + 1.0, at + SKIP_S, kinds=["media_context"]):
                name = store.moment_entity(r)
                if name and entity_key(store, str(detail["taste_kind"]), name) != str(detail.get("group") or ""):
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _rows_of(store: Any, kind: str) -> list[tuple[str, str, str, list[float], list[float], float, float]]:
    """``(key, name, ref, weekday, weekend, units, last_at)`` of the picked rows of a taste kind (never the names an
    app only showed). Decrypted in RAM; never raises (``[]``)."""
    try:
        rows = store._query("SELECT entity_key, name_ciphertext, ref_ciphertext, hist_weekday, hist_weekend, n_obs, "
                            "last_at FROM tastes WHERE kind = ? AND source != ? AND n_obs > 0", (kind, SEEN_SOURCE))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for key, name_ct, ref_ct, wd, we, n_obs, last_at in rows:
        try:
            name = " ".join(str(store._dec(name_ct) or "").split())
            ref = str(store._dec(ref_ct) or "") if ref_ct else ""
        except Exception:  # noqa: BLE001
            continue
        if name:
            out.append((str(key), name, ref, _hist(wd), _hist(we), float(n_obs or 0.0), float(last_at or 0.0)))
    return out


def _squash(text: Any) -> str:
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _base(rows: list[tuple[str, str, str, list[float], list[float], float, float]], now: float,
          skips: Mapping[str, float]) -> list[_Cand]:
    out = []
    for key, name, ref, wd, we, total, last_at in rows:
        fit = score_at(wd, we, now)
        score = (fit + TOTAL_W * total) * math.pow(0.5, skips.get(key, 0.0))
        out.append(_Cand(name, key, key, {"name": name, "key": key, "ref": ref}, score, fit, _peak(wd, we, now),
                         total, last_at, [a + b for a, b in zip(wd, we)]))
    out.sort(key=lambda c: (-c.score, -c.total, c.key))
    return out


def _same_name(text: Any, name: Any) -> bool:
    """Whether ``text`` IS ``name``: the same letters and digits, a leading "@" and a trailing possessive aside
    ("Deji's", "@Deji" = "Deji"). A format of the text, never a guess at meaning; a longer text that only holds the
    name ("the Sidemen video about the island") is not it (review H2)."""
    said = " ".join(str(text or "").split()).lstrip("@")
    forms = {_squash(said)}
    for tail in ("'s", "’s", "'", "’"):
        if said.casefold().endswith(tail) and len(said) > len(tail):
            forms.add(_squash(said[: -len(tail)]))
            break
    want = _squash(str(name or "").lstrip("@"))
    return bool(want) and want in forms


def _channels(store: Any, now: float, skips: Mapping[str, float], only: str) -> list[_Cand]:
    """The user's YouTube channels as candidates (their display name when a feed or oEmbed gave one), best first;
    ``only``: the channel that text IS (:func:`_same_name` on its display name or handle: "Sidemen", "Deji's"), else
    none."""
    from yuki.skills import youtube_feed

    cands = _base(_rows_of(store, youtube_feed.CHANNEL_KIND), now, skips)
    try:
        known = store.yt_channels([c.key for c in cands])
    except Exception:  # noqa: BLE001
        known = {}
    for c in cands:
        info = known.get(c.key) or {}
        handle = c.name
        c.name = " ".join(str(info.get("name") or handle).split())
        c.value = {"name": handle, "key": c.key, "ref": c.value.get("ref") or ""}
        c.desc = {"handle": handle}
    if only:
        cands = [c for c in cands if _same_name(only, c.name) or _same_name(only, c.desc.get("handle"))]
    return cands


def _videos(store: Any, channels: list[_Cand], now: float, *, network: bool, log: LogFn | None,
            budget_s: float, stats: dict[str, Any] | None = None) -> list[_Cand]:
    """The unwatched uploads of the best :data:`CHOOSE_CHANNELS` channels (their feeds read first when older than an
    hour and ``network``), at most two per channel, scored by the channel's score and the upload's age. ``stats``
    (skill20, final-D B5): filled with content-free counts of why none may be left (``channels``, the feed reads'
    ``feed_ok`` / ``feed_failed``, ``uploads`` cached unwatched, ``seen`` of them already seen on a page)."""
    from yuki.connect.templates import fill
    from yuki.skills import youtube_feed

    stats = stats if stats is not None else {}
    top_ch = channels[:CHOOSE_CHANNELS]
    stats["channels"] = len(top_ch)
    if not top_ch:
        return []
    if network and budget_s > 0 and youtube_feed.blocked() is None:
        got = youtube_feed.refresh(store, [c.key for c in top_ch], now=now, budget_s=budget_s, log=log)
        if isinstance(got, Mapping):
            stats["feed_ok"] = int(got.get("ok") or 0)
            stats["feed_failed"] = int(got.get("failed") or 0)
    by = {c.key: c for c in top_ch}
    try:
        vids = store.yt_videos(channel_keys=list(by), unwatched=True)
        seen = store.page_entity_keys_seen([v["video_key"] for v in vids])
    except Exception:  # noqa: BLE001
        stats["error"] = True
        return []
    stats["uploads"] = len(vids)
    stats["seen"] = sum(1 for v in vids if str(v.get("video_key")) in seen)
    per: dict[str, int] = {}
    out: list[_Cand] = []
    for v in vids:      # newest first
        ch, pub, key = by.get(str(v.get("channel_key") or "")), v.get("published_at"), str(v["video_key"])
        if ch is None or key in seen or not v.get("video_id") or pub is None or float(pub) > now:
            continue
        if per.get(ch.key, 0) >= 2:
            continue
        ref = fill(youtube_feed.SITE, youtube_feed.WATCH_PAGE, id=str(v["video_id"])) or ""
        if not ref:
            continue
        age = max(0.0, (now - float(pub)) / 86400.0)
        score = ch.score * (FRESH_FLOOR + math.exp(-age / AGE_DAYS)) * (SECOND_W if per.get(ch.key) else 1.0)
        per[ch.key] = per.get(ch.key, 0) + 1
        title = " ".join(str(v.get("title") or "").split())
        out.append(_Cand(ch.name, key, ch.key, {"name": ch.name, "key": key, "ref": ref}, score, ch.fit, ch.peak,
                         ch.total, ch.last_at, ch.hist, round(age, 2), title, dict(ch.desc)))
    out.sort(key=lambda c: (-c.score, c.fresh_d or 0.0, c.key))
    return out


def _describe(store: Any, kind: str, taste_kind: str, cands: list[_Cand], now: float) -> list[dict[str, Any]]:
    """What the judgement sees of each candidate (RAM only, never logged): when they usually pick it, what they watched
    of it or what played while it ran and what they called it, how long they stay; a video's own title and age."""
    items = []
    if kind == "playlist":
        known = {_norm(i.get("name")): i for i in described(store, taste_kind, now=now, names=[c.name for c in cands],
                                                            limit=len(cands) + MEANING_ITEMS)}
    else:
        from yuki.skills import youtube_feed

        watched = youtube_feed.by_channel(store, since=now - SEED_DAYS * 86400.0, now=now)
    for i, c in enumerate(cands):
        item: dict[str, Any] = {"id": f"c{i + 1}", "name": c.name}
        if (hours := _peak_hours(c.hist)):
            item["usually"] = hours
        if kind == "playlist":
            got = known.get(_norm(c.name)) or {}
            for k in ("called", "heard"):
                if got.get(k):
                    item[k] = got[k]
        else:
            w = watched.get(c.group) or {}
            if w.get("titles"):
                item["they watched"] = w["titles"]
            if w.get("videos"):
                item["minutes per video they watch"] = round(float(w["secs"]) / 60.0 / int(w["videos"]))
            if kind == "video":
                item["this video"] = c.title
                item["days old"] = c.fresh_d
        items.append(item)
    return items


def _judge_order(store: Any, kind: str, taste_kind: str, cands: list[_Cand], words: Any, context: str, now: float,
                 *, client: Any = None, model: str | None = None, settings: Any = None,
                 wait_s: float = CHOOSE_WAIT_S) -> list[_Cand] | None:
    """The user's words judged over the candidates by the voice's small model (one forced tool call, the ids as an
    enum, bounded by ``wait_s`` on its own thread): the candidates in its order, or None (no model, late, malformed).
    Content goes to the model only."""
    try:
        if settings is None:
            from yuki.config import Settings

            settings = Settings()
        model = model or str(getattr(settings, "voice_mouth_model", "") or "")
        client = client if client is not None else _judge_client(settings)
    except Exception:  # noqa: BLE001
        return None
    items = _describe(store, kind, taste_kind, cands, now)
    ids = [i["id"] for i in items]
    turns = [str(w) for w in (words if isinstance(words, (list, tuple)) else [words]) if str(w or "").strip()]
    stamp = datetime.fromtimestamp(now)
    things = {"video": "YouTube channels' new videos", "channel": "YouTube channels", "playlist": "playlists"}[kind]
    request = {"model": model, "max_tokens": 120, "system": CHOOSE_SYSTEM.replace("{things}", things),
               "tools": [{"name": "rank", "description": "Their items, best fit first.", "input_schema": {
                   "type": "object", "properties": {"order": {"type": "array", "items": {"type": "string",
                                                                                          "enum": ids}}},
                   "required": ["order"]}}],
               "tool_choice": {"type": "tool", "name": "rank"},
               "messages": [{"role": "user", "content": json.dumps(
                   {"their words": turns[-3:], "what they asked": context, "now": stamp.strftime("%A %H:%M"),
                    "items": items}, ensure_ascii=False)}]}
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["response"] = client.messages.create(**request)
        except Exception as exc:  # noqa: BLE001
            box["error"] = type(exc).__name__

    thread = threading.Thread(target=run, name="yuki-taste-choose", daemon=True)
    thread.start()
    thread.join(max(0.1, float(wait_s)))
    response = box.get("response")
    if response is None:
        return None
    call = next((b for b in getattr(response, "content", []) or [] if getattr(b, "type", None) == "tool_use"), None)
    order = [str(x) for x in (dict(getattr(call, "input", {}) or {}).get("order") or []) if str(x) in ids]
    if not order:
        return None
    by = dict(zip(ids, cands))
    ranked = [by[i] for i in dict.fromkeys(order)]
    return ranked + [c for c in cands if c not in ranked]


#: The parts of the day as said, ``(from hour, to hour, noun, phrase)``; any other hour is late at night. A format of
#: the clock, never a judgement.
_DAY_PARTS = ((5, 11, "morning", "in the morning"), (11, 15, "lunch", "around lunch"),
              (15, 18, "afternoon", "in the afternoon"), (18, 22, "evening", "in the evening"))


def _day_part(now: float) -> tuple[str, str]:
    """``(noun, phrase)`` of the part of the day at ``now``: ``("lunch", "around lunch")``."""
    h = datetime.fromtimestamp(now).hour
    return next(((noun, phrase) for a, b, noun, phrase in _DAY_PARTS if a <= h < b), ("late-night", "late at night"))


def _time_word(now: float) -> str:
    """The part of the day as said: a format of the clock, never a judgement."""
    return _day_part(now)[1]


#: fd (final-F D6; live 20 Oct k_video_another_one: "you usually watch them late at night" for a channel watched on one
#: evening): a habit why ("usually", "usual") needs the user's own use of it on at least this many distinct days.
USUAL_DAYS = 2
#: The distinct days per channel or playlist are read once per database and kind for this long (RAM).
DAYS_TTL_S = 600.0
_days_lock = threading.Lock()
_days_cache: dict[tuple[str, str, str], tuple[float, dict[str, int]]] = {}


def _days_seen(store: Any, kind: str, taste_kind: str, now: float) -> dict[str, int]:
    """fd (final-F D6): ``{group: distinct days}``, the local dates in the last :data:`SEED_DAYS` the user had each
    candidate group on: a playlist's own media-context picks (:func:`_user_picks`: theirs, never a test's), a channel's
    watched videos (:func:`yuki.skills.youtube_feed.watched`, by the channel memory knows each video is from, outside
    the live tests' spans). Counts only, cached :data:`DAYS_TTL_S`; ``{}`` when unread. Never raises."""
    cache_key = (str(getattr(store, "path", id(store))), str(kind), str(taste_kind))
    at = time.monotonic()
    with _days_lock:
        hit = _days_cache.get(cache_key)
        if hit is not None and at - hit[0] < DAYS_TTL_S:
            return hit[1]
    since = float(now) - SEED_DAYS * 86400.0
    dates: dict[str, set[Any]] = {}
    try:
        if kind == "playlist":
            hit_cap = next(((app, end) for k, app, end in capability_kinds() if k == taste_kind), None)
            if hit_cap is not None:
                users = _user_picks(store, since)
                for m in store.moments_between(since, float(now) + 1.0, kinds=[hit_cap[1]], app=hit_cap[0]):
                    if not users(m):
                        continue
                    name = " ".join(str(store.moment_entity(m) or "").split())
                    if name:
                        dates.setdefault(entity_key(store, taste_kind, name), set()).add(
                            datetime.fromtimestamp(float(m["at"])).date())
        else:
            from yuki.skills import youtube_feed

            spans = _test_spans(store)
            seen = youtube_feed.watched(store, since=since, now=float(now))
            vids = {str(v["video_key"]): str(v["channel_key"]) for v in store.yt_videos(video_keys=list(seen))
                    if v.get("channel_key")}
            for key, e in seen.items():
                ch = vids.get(str(key))
                if not ch:
                    continue
                for span in e.get("spans") or []:
                    start = float(span[0])
                    if not (spans and _in_spans(start, spans)):
                        dates.setdefault(ch, set()).add(datetime.fromtimestamp(start).date())
    except Exception:  # noqa: BLE001 - unread: no habit is claimed from it
        return {}
    out = {k: len(v) for k, v in dates.items()}
    with _days_lock:
        if len(_days_cache) > 16:
            _days_cache.clear()
        _days_cache[cache_key] = (at, out)
    return out


def _usual_days(c: _Cand, days: Mapping[str, int] | None) -> bool:
    """Whether a candidate's habit may be said as a usual: no day count read (``None``: as before), or its group on at
    least :data:`USUAL_DAYS` distinct days. Pure."""
    return days is None or int(days.get(c.group, 0) or 0) >= USUAL_DAYS


def _why(c: _Cand, kind: str, cands: list[_Cand], now: float, named: bool,
         days: Mapping[str, int] | None = None) -> str:
    """The short why of a pick, from its own numbers: how new it is (a video), and its habit (watched or played at
    this hour, the most lately, or recently). fd (D6): "usually" only with ``days`` (:func:`_days_seen`) showing its
    use on :data:`USUAL_DAYS` distinct days or more."""
    parts = []
    if kind == "video" and c.fresh_d is not None:
        if c.fresh_d < 1:
            parts.append("it's out today")
        elif c.fresh_d < 2:
            parts.append("it came out yesterday")
        elif c.fresh_d < 7:
            parts.append("it's new this week")
    if not named:
        verb, past, obj = ("play", "played", "it") if kind == "playlist" else ("watch", "watched", "them")
        if c.fit > 0 and c.fit >= HABIT_SHARE * c.peak and _usual_days(c, days):
            parts.append(f"you usually {verb} {obj} {_time_word(now)}")
        elif cands and c.total >= max(x.total for x in cands):
            parts.append(f"it's what you've {past} most lately")
        elif now - c.last_at < 3 * 86400.0:
            parts.append(f"you {past} {obj} recently")
    return ", and ".join(parts)


def _possessive(name: str) -> str:
    return f"{name}'" if name.endswith(("s", "S")) else f"{name}'s"


def _what(choice: Choice) -> str:
    """The pick as said: "Deji's new one", "one of Deji's you haven't seen", "Deji's channel", "lumen"."""
    if choice.kind == "video":
        if choice.fresh_d is not None and choice.fresh_d < 7:
            return f"{_possessive(choice.name)} new one"
        return f"one of {_possessive(choice.name)} you haven't seen"
    if choice.kind == "channel":
        return f"{_possessive(choice.name)} channel"
    return choice.name


def choice_line(choice: Choice, *, where: str = "") -> str:
    """The done line of a pick (code-written, one sentence): "Putting on Deji's new one - it's out today, and you
    usually watch them around lunch." ``where``: the route's own browser words (" in Arc"). A pick the talker made
    from the card (``source`` :data:`FROM_CARD`) carries no why here: its say_first said it."""
    verb = "Opening" if choice.kind == "channel" else "Putting on"
    why = f" - {choice.why}" if choice.why and choice.source != FROM_CARD else ""
    return f"{verb} {_what(choice)}{where}{why}."


def held_line(choice: Choice | None, why: str) -> str:
    """Decision 2: never with a game in front or in a call. The line says so and names the pick kept for later
    (:func:`keep_for_later`: the next ask of this conversation plays it)."""
    head = ("You're in a game, so I won't put anything on now." if why == "game"
            else "You're on a call, so I held off.")
    if choice is None:
        return head
    what = _what(choice)
    return f"{head} I'll keep {what} for after."


def choose(store: Any, kind: str, *, words: Any = (), context: str = "", session: str | None = None, only: str = "",
           asked: bool = False, taste_kind: str = "", now: float | None = None, settings: Any = None,
           log: LogFn | None = None, network: bool = True, judge_wait_s: float = CHOOSE_WAIT_S,
           feed_budget_s: float | None = None, client: Any = None, model: str | None = None,
           source: str = "") -> Choice | None:
    """Yuki's pick for "something I'd like" of ``kind`` (:data:`CHOOSE_KINDS`'s values), like a recommender: the
    user's own candidates scored by what they watch or play at this hour and day, how much (time, not clicks), how
    recently, how new (a video), less what they skipped; the ones already put on in this conversation left out ("another
    one", "not that": the next); the user's words (``context``: theirs or the understanding's, and ``words``) judged
    over the best :data:`CHOOSE_N` by a small model when there are two or more (bounded; the habit order stands when it
    is late). ``only``: the channel or playlist the user named, exactly (:func:`_same_name`: "Deji's new video" ->
    "Deji"): that channel's newest unwatched upload, no judgement; ``asked`` (they asked Yuki to choose): an ``only``
    that is none of theirs ("something chill" given as the video) is their words for the judgement and the pick is free
    (review M1), else None. ``source`` :data:`FROM_CARD` (asked, a name they never said): ``only`` is the talker's or
    the planner's own pick, the card's pick for now (:func:`now_picks`): the same upload or playlist, a guess (never
    ``named``); one that is none of theirs is their words for the judgement, as above, and (skill20) one of theirs
    with nothing playable widens the pick to their other candidates (it joins the judgement's words). A pick kept from
    a held request (:func:`keep_for_later`) comes first. None when there is nothing to pick. Nothing plays here; the
    only thing noted is a ``skipped`` event (once per pick) when this asks again within :data:`SKIP_S` of the last pick
    (:func:`chosen` notes the pick, once it played). Content-free logs; never raises."""
    started = time.perf_counter()
    now = time.time() if now is None else float(now)
    if judge_wait_s > 0 and client is None and (asked or not only):
        prewarm()           # the judgement's client is made while the feeds are read
    try:
        state = _convo_state(str(session or ""), kind, now)
        last = state.get("last") or None
        if last and now - float(last[2]) <= SKIP_S and state.get("skipped") != last[2]:
            # asked again this soon: the last pick was skipped (negative feedback for its channel or playlist), once
            with _convo_lock:
                (_convo.get((str(session or ""), kind)) or {})["skipped"] = last[2]
            store.add_taste_event("skipped", {"kind": kind, "group": str(last[1]), "key": str(last[0]),
                                              "chose_at": float(last[2]), "how": "again"}, at=now)
        skips = _skip_weights(store, now)
        if kind == "playlist":
            if not taste_kind:
                return None
            every: list[_Cand] | None = _base(_rows_of(store, taste_kind), now, skips)
            named = [c for c in every if _same_name(only, c.name)] if only else every
        else:
            every, named = None, _channels(store, now, skips, only)
        cands = named
        if only and not named and asked:
            # they asked Yuki to choose and the text given names none of theirs: their words, for the judgement
            if _norm(only) not in _norm(context):
                context = " ".join(f"{context or ''} {only}".split())
            only = ""
            cands = every if every is not None else _channels(store, now, skips, "")
        base_n = len(cands)          # the channels or playlists the pick is made among
        stats: dict[str, Any] = {}
        budget = CHOOSE_FEED_S if feed_budget_s is None else float(feed_budget_s)
        if kind == "video":
            cands = _videos(store, cands, now, network=network, log=log, budget_s=budget, stats=stats)
        given = set(state.get("keys") or [])
        left = _left_of(cands, given, last, state)
        widened = None
        if not left and only and asked and source == FROM_CARD:
            # skill20 (final-D B5: "Just sat down to eat" -> the agent's own {video: "<a channel>", choose: true};
            # that channel had no unwatched upload cached, n=0, and the run ended no_route): the user asked Yuki to
            # choose and never named that channel (the caller's pick, FROM_CARD), so it is a hint, not a limit: the
            # pick widens to their other candidates (the hint joins the judgement's words), said as Yuki's own pick.
            # A channel or playlist the user named themselves stays the limit (None: the normal way finds one)
            widened = _empty_why(kind, base_n, stats, given)
            if _norm(only) not in _norm(context):
                context = " ".join(f"{context or ''} {only}".split())
            only = ""
            cands = every if every is not None else _channels(store, now, skips, "")
            base_n = len(cands)
            if kind == "video":
                stats = {}
                spent = time.perf_counter() - started
                cands = _videos(store, cands, now, network=network, log=log, budget_s=max(0.0, budget - spent),
                                stats=stats)
            left = _left_of(cands, given, last, state)
        if not left:
            # skill20 (final-D B5): why nothing is left, as a code word (never a name)
            _log_choose(log, kind, n=0, started=started, why=_empty_why(kind, base_n, stats, given), widened=widened)
            return None
        # the pick named for later when a game or a call held it (:func:`keep_for_later`): first, as said (the
        # judgement still weighs new words against it: "go on then" keeps it, "something chill" may not)
        kept = [c for c in left if c.key == state.get("held")] if not only else []
        if kept:
            left = kept + [c for c in left if c is not kept[0]]
        judged = moved = False
        said = " ".join(str(context or "").split())
        wait = min(float(judge_wait_s), CHOOSE_DEADLINE_S - (time.perf_counter() - started))
        if not only and len(left) > 1 and wait >= JUDGE_MIN_S and (said or words):
            got = _judge_order(store, kind, taste_kind, left[:CHOOSE_N], words, said, now, client=client,
                               model=model, settings=settings, wait_s=wait)
            if got is not None:
                judged, moved = True, got[0] is not left[0]
                left = got + left[CHOOSE_N:]
        best = left[0]
        extra = {"page": "handle"} if kind == "channel" else {}
        value = dict(best.value)
        card = bool(only) and source == FROM_CARD
        # fd (D6): the days it was on are read only when its numbers would say "usually" (one bounded, cached read)
        days = _days_seen(store, kind, taste_kind, now) \
            if not only and best.fit > 0 and best.fit >= HABIT_SHARE * best.peak else None
        choice = Choice(kind, CHOOSE_SLOTS[kind], value, extra, best.name, best.key, best.group,
                        _why(best, kind, cands, now, bool(only), days), best.fresh_d, best.title, judged, len(left),
                        round((time.perf_counter() - started) * 1000.0, 1), bool(only) and not card,
                        FROM_CARD if card else "")
        _log_choose(log, kind, n=len(left), started=started, judged=judged, moved=moved, fresh_d=best.fresh_d,
                    skips=len(skips), again=len(given), kept=bool(kept) or None, card=card or None,
                    widened=widened)
        return choice
    except Exception as exc:  # noqa: BLE001 - no pick: the request goes the normal way
        _log_choose(log, kind, n=0, started=started, error=type(exc).__name__)
        return None


def _left_of(cands: list[_Cand], given: set[str], last: Any, state: Mapping[str, Any]) -> list[_Cand]:
    """The candidates not yet put on in this conversation ("another one", "not that": the next), another channel or
    playlist before a second of one already put on; every one put on already: around again, never the one playing."""
    left = [c for c in cands if c.key not in given]
    if not left and cands and given:
        left = [c for c in cands if not last or c.key != last[0]]
    groups = set(state.get("groups") or [])
    return [c for c in left if c.group not in groups] + [c for c in left if c.group in groups]


def _empty_why(kind: str, base_n: int, stats: Mapping[str, Any], given: set[str]) -> str:
    """skill20 (final-D B5): why a pick found nothing, as one code word for the content-free log: ``no_candidates``
    (no channel or playlist of theirs to pick among), ``read_failed`` (the cached uploads couldn't be read),
    ``feed_failed`` (no upload cached, and this request's feed reads all failed), ``no_uploads`` (no unwatched upload
    cached), ``all_seen`` (every one already seen on a page), ``no_playable`` (none with an id and a past publish
    time), ``all_given`` (every candidate already put on in this conversation)."""
    if base_n <= 0:
        return "no_candidates"
    if kind != "video":
        return "all_given" if given else "no_candidates"
    if stats.get("error"):
        return "read_failed"
    uploads, seen = int(stats.get("uploads") or 0), int(stats.get("seen") or 0)
    if uploads <= 0:
        failed, ok = int(stats.get("feed_failed") or 0), int(stats.get("feed_ok") or 0)
        return "feed_failed" if failed and not ok else "no_uploads"
    if seen >= uploads:
        return "all_seen"
    return "all_given" if given else "no_playable"


def keep_for_later(choice: Choice, *, session: str | None = None, now: float | None = None) -> None:
    """A pick a game or a call held (:func:`held_line` named it): kept for this conversation (RAM, :data:`CONVO_S`),
    so the next ask to choose ("go on then", after the game) puts that one on. Never raises."""
    now = time.time() if now is None else float(now)
    try:
        with _convo_lock:
            st = _convo.setdefault((str(session or ""), choice.kind), {"keys": [], "groups": []})
            st["held"] = choice.key
            st["at"] = now
    except Exception:  # noqa: BLE001
        pass


def _log_choose(log: LogFn | None, kind: str, *, n: int, started: float, **fields: Any) -> None:
    """The content-free ``taste_choose`` line: the kind, how many candidates, how long, code words and numbers."""
    if log is None:
        return
    try:
        log("taste_choose", kind=kind, n=int(n), ms=round((time.perf_counter() - started) * 1000.0, 1),
            **{k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass


def chosen(store: Any, choice: Choice, *, session: str | None = None, taste_kind: str = "",
           now: float | None = None) -> None:
    """A pick was put on and its done-check saw it: kept for this conversation ("another one" takes the next) and noted
    (``taste_events`` ``chose``: digests and code words only), so a quick close or change counts as a skip later
    (:func:`_skip_weights`). Never raises."""
    now = time.time() if now is None else float(now)
    with _convo_lock:
        key = (str(session or ""), choice.kind)
        st = _convo.setdefault(key, {"keys": [], "groups": []})
        if choice.key not in st["keys"]:
            st["keys"].append(choice.key)
        if choice.group not in st.setdefault("groups", []):
            st["groups"].append(choice.group)
        st["last"] = (choice.key, choice.group, now)
        st["at"] = now
        st.pop("held", None)
    _forget_now(store)       # the card's picks for now read it again (an upload just put on is no longer unwatched)
    try:
        store.add_taste_event("chose", {"kind": choice.kind, "key": choice.key, "group": choice.group,
                                        "taste_kind": taste_kind or None, "judged": bool(choice.judged),
                                        "n": int(choice.n)}, at=now)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------------------------------
# Phase 2.2 (audit 18 Oct, reasoning.md 4.2 and 5.2 item 3): the picks for now, for the talker to choose from
# ---------------------------------------------------------------------------------------------------------------------

#: Candidates per kind :func:`now_picks` keeps (a card shows 3 to 5 of them).
NOW_N = 5
#: Kept in RAM this long per database (a pick put on clears it: :func:`chosen`).
NOW_TTL_S = 300.0
#: A habit why ("your lunch usual") needs at least this much use at this hour (time units, decayed: about five
#: minutes), on top of :data:`HABIT_SHARE` of its own best hour: one old visit is no usual.
USUAL_MIN = 1.0
#: A pick at most this long ago is named by its day ("watched today", "yesterday", "Tuesday"); the decayed skips
#: from which a why says "skipped lately".
RECENT_S = 7 * 86400.0
SKIPPED_W = 0.5


@dataclass(frozen=True)
class NowPick:
    """One candidate for now (:func:`now_picks`): its ``kind`` (:data:`CHOOSE_KINDS`' values), the ``name`` a run
    takes (a channel's name: its newest unwatched upload for ``video``, its page for ``channel``; a playlist's name),
    the short code-made why in two parts (:func:`why_parts`, from the scorer's own numbers): ``why``, the habit of
    the channel or playlist ("lunch usual", "watched yesterday"; ``""`` for a video with none), and for a video
    ``age``, how new the upload is ("new today"); the recommender's ``score``, its hour ``fit`` (time units at this
    hour: comparable across kinds), and for a video its ``title`` and age in days (RAM only, never logged)."""

    kind: str
    name: str
    why: str
    score: float
    fit: float
    title: str = ""
    fresh_d: float | None = None
    age: str = ""


_now_lock = threading.Lock()
#: ``db path -> (computed at, {kind: [NowPick]})``
_now_cache: dict[str, tuple[float, dict[str, list[NowPick]]]] = {}


def _forget_now(store: Any) -> None:
    with _now_lock:
        _now_cache.pop(str(getattr(store, "path", "") or ""), None)


def why_parts(c: _Cand, kind: str, *, top_total: float, now: float, skipped: float = 0.0,
              days: Mapping[str, int] | None = None) -> tuple[str, str]:
    """A candidate's why from the scorer's own numbers, ``(age, habit)``: how new a video's upload is ("new today",
    "a week old": every video candidate is unwatched; ``""`` for other kinds), and the habit of its channel or
    playlist: this part of the day ("lunch usual": its fit at this hour at least :data:`HABIT_SHARE` of its best hour
    and :data:`USUAL_MIN`, and fd D6: with ``days`` given, on :data:`USUAL_DAYS` distinct days or more), the most time
    overall ("most watched"), or a recent pick by its day ("played yesterday", "watched Tuesday"); a recent skip says so
    ("skipped lately"); ``""`` for a video with none. A format of numbers, never a judgement of what it holds."""
    age = ""
    if kind == "video" and c.fresh_d is not None:
        d, weeks = float(c.fresh_d), int(float(c.fresh_d) // 7)
        age = ("new today" if d < 1 else "new yesterday" if d < 2 else "new this week" if d < 7
               else "a week old" if weeks == 1 else f"{weeks} weeks old")
    past = "played" if kind == "playlist" else "watched"
    habit = ""
    if skipped >= SKIPPED_W:
        habit = "skipped lately"
    elif c.fit >= USUAL_MIN and c.fit >= HABIT_SHARE * c.peak and _usual_days(c, days):
        habit = f"{_day_part(now)[0]} usual"
    elif top_total > 0 and c.total >= top_total:
        habit = f"most {past}"
    elif 0 <= now - c.last_at < RECENT_S:
        last = datetime.fromtimestamp(c.last_at)
        days = (datetime.fromtimestamp(now).date() - last.date()).days
        habit = f"{past} " + ("today" if days <= 0 else "yesterday" if days == 1 else f"{last:%A}")
    elif not age:
        habit = f"{past} now and then"
    return age, habit


def short_why(c: _Cand, kind: str, *, top_total: float, now: float, skipped: float = 0.0) -> str:
    """A candidate's why in 2 to 5 words (:func:`why_parts`, its strongest factor first): "new today, lunch usual"."""
    return ", ".join(b for b in why_parts(c, kind, top_total=top_total, now=now, skipped=skipped) if b)


def _taste_kind_of(kind: str) -> str:
    """The taste kind a pick kind reads (the capability's own slot data, as the engine reads it): a playlist's
    ``spotify_playlist``; a video's and a channel's the YouTube channel kind. ``""`` when not loaded."""
    if kind in ("video", "channel"):
        from yuki.skills import youtube_feed

        return youtube_feed.CHANNEL_KIND
    try:
        from yuki.skills.catalog import load

        cap = next((c for c, k in CHOOSE_KINDS.items() if k == kind), "")
        skill = load().get(cap)
        slot = skill.slot(CHOOSE_SLOTS[kind]) if skill is not None else None
        return str(getattr(slot, "taste", "") or "")
    except Exception:  # noqa: BLE001
        return ""


def _now_picks(store: Any, now: float) -> dict[str, list[NowPick]]:
    skips = _skip_weights(store, now)
    out: dict[str, list[NowPick]] = {}

    def picks(kind: str, cands: list[_Cand], every: list[_Cand], taste_kind: str = "") -> None:
        top_total = max((c.total for c in every), default=0.0)
        got = []
        # fd (D6): the distinct days each was on, read once, only when one of them would be called a usual
        days = _days_seen(store, kind, taste_kind, now) if any(
            c.name and c.fit >= USUAL_MIN and c.fit >= HABIT_SHARE * c.peak for c in cands[:NOW_N]) else None
        for c in cands[:NOW_N]:
            if c.name:
                age, habit = why_parts(c, kind, top_total=top_total, now=now, skipped=skips.get(c.group, 0.0),
                                       days=days)
                got.append(NowPick(kind, c.name, habit, round(c.score, 4), round(c.fit, 3),
                                   c.title if kind == "video" else "", c.fresh_d if kind == "video" else None, age))
        if got:
            out[kind] = got

    channels = _channels(store, now, skips, "")
    if channels:
        # the cached feeds only (no read here: feeds are read on a request, within their own hourly cap); one upload a
        # channel, its best (the newest unwatched), and only a fresh one (the offers' own bar): an older upload of a
        # feed read long ago says little of what the channel has now (its channel stays a candidate)
        from yuki.skills import youtube_feed

        vids, groups = [], set()
        for c in _videos(store, channels, now, network=False, log=None, budget_s=0.0):
            if c.group not in groups and c.fresh_d is not None and c.fresh_d <= youtube_feed.FRESH_DAYS:
                groups.add(c.group)
                vids.append(c)
        picks("video", vids, channels)
        picks("channel", channels, channels)
    playlist_kind = _taste_kind_of("playlist")
    if playlist_kind:
        lists = _base(_rows_of(store, playlist_kind), now, skips)
        picks("playlist", lists, lists, playlist_kind)
    return out


def now_picks(store: Any, *, now: float | None = None, n: int = NOW_N) -> dict[str, list[NowPick]]:
    """Phase 2.2: the user's best candidates for right now per pick kind (``{"video" | "channel" | "playlist":
    [NowPick]}``, best first, at most ``n`` each; a kind with none left out), scored exactly as :func:`choose` scores
    them before any judgement (their time at this hour and kind of day, a share of their all-time time, halved per
    recent skip; a video by how new it is), each with a short code-made why (:func:`why_parts`). Videos come from the
    feeds already read (no network here; :func:`choose` reads fresh ones for "pick for me"), one per channel: running
    ``web.open_video`` with ``video`` the channel's name plays that same upload. What the cards show for the talker
    and the planner to choose from. No model call; cached in RAM per database for :data:`NOW_TTL_S`; names and titles
    decrypted in RAM, never logged. Never raises (``{}``)."""
    now = time.time() if now is None else float(now)
    path = str(getattr(store, "path", "") or "")
    hit = None
    if path:
        with _now_lock:
            hit = _now_cache.get(path)
    if hit is not None and 0.0 <= now - hit[0] < NOW_TTL_S:
        got = hit[1]
    else:
        try:
            got = _now_picks(store, now)
        except Exception:  # noqa: BLE001
            return {}
        if path:
            with _now_lock:
                _now_cache[path] = (now, got)
    k = max(0, int(n))
    return {kind: list(items[:k]) for kind, items in got.items() if items[:k]}


__all__ = ["SIGMA_MIN", "FALLBACK_MASS", "MENU_CANDIDATES", "OPTIONS_N", "SEEN_SOURCE", "SEEN_MAX", "SAID_SUFFIX",
           "Taste", "hour_score", "score_at", "top", "candidates", "seen_names", "options", "said_kind", "said_target",
           "said_words", "find", "offered", "find_offered", "takes_offers", "MEANING_WAIT_S", "MEANING_TTL_S",
           "MEANING_ITEMS", "MEANING_TITLES", "MEANING_DAYS", "MEANING_WHYS", "Meant", "heard_as",
           "described", "judge", "track_owner", "TRACK_DAYS", "TRACK_SLACK", "USUAL_DAYS",
           "prewarm",
           "SEED_DAYS", "HALF_LIFE_DAYS",
           "SESSION_GAP_S", "FLOOR", "OPAQUE_VAR", "RETIRED_KINDS", "ASKED_FILLS", "GUESS_FILLS", "OFFER_FIELDS",
           "entity_key", "site_kinds",
           "capability_kinds", "slot_kind", "recompute", "seed_sites", "note", "saw", "unsay", "busy", "seed",
           "VISIT_UNITS", "SESSION_UNITS", "LISTEN_SPAN_S", "CHOOSE_KINDS", "CHOOSE_SLOTS", "CONVO_S", "SKIP_S",
           "CHOOSE_N", "CHOOSE_WAIT_S", "CHOSE_LEAD_S", "Choice", "choose", "chosen", "keep_for_later", "choice_line",
           "held_line", "FROM_CARD", "NOW_N", "NOW_TTL_S", "USUAL_MIN", "NowPick", "why_parts", "short_why",
           "now_picks"]
