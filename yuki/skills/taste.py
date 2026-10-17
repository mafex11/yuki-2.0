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
mine. Recomputed, never
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
keyed digest the key, the row's name encrypted as its target), so :func:`find` maps the same words to it next time. An
identity of the user's own words, learned from a seen result, never a list of meanings written here; the newest pairing
of the same words wins, and :func:`unsay` drops one the user's later choice contradicts (what they say wins over what
was learned).

**By sound and by meaning** (lSkills, live 17 Oct: "Lumin" for lumen asked "say it again"; "anime playlist" asked which
playlist a minute after "Japanese" was paired with ネオン; "something chill" took 15-29 s through the thinker):

* :func:`heard_as` - a heard name that sounds like exactly one of the user's own listed names is that name (the people
  index's reading of names by sound; data, never a word list): the resolver uses it on the menu's candidates;
* :func:`described` + :func:`judge` - when words name no entity and no pairing holds them, memory describes the user's
  own entities (what they called each before, when they pick it, how much, what played while it ran) and the voice's
  small model judges which one the words name or describe, or that they only ask for a mood (then the hour's usual,
  :func:`top`, plays), or none. One forced tool call with the names as an enum, bounded (:data:`MEANING_WAIT_S`), its
  answer kept for the same words and descriptions (:data:`MEANING_TTL_S`); a ``name`` verdict stands only when the words
  say that name by identity or sound. The Spotify driver asks it after its Library read and, once its own check sees
  the row play, keeps those words as another name of the row (:func:`note` ``said_as``): the next time is by identity,
  with no judgement. Content goes to the model only, never to a log.
"""

from __future__ import annotations

import json
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

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
#: The kind suffix of the rows that keep what the user called an entity (:func:`note` ``said_as``; :func:`find`).
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
    their keyed digest. Never raises."""
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
    """The taste row whose name equals ``name`` (case and spacing folded: identity, never meaning), the names the
    user's own app showed included (:func:`saw`); else the row the same words were paired with when they named no row
    and that entity then played (:func:`said_target`). None when neither. Never raises."""
    want = _norm(name)
    if not want:
        return None
    rows = top(store, kind, n=10_000, seen=True)
    for t in rows:
        if _norm(t.name) == want:
            return t
    target = said_target(store, kind, want)
    if not target:
        return None
    for t in rows:
        if _norm(t.name) == _norm(target):
            return t
    try:
        return Taste(entity_key(store, kind, target), target, "", 0.0, 0.0)
    except Exception:  # noqa: BLE001
        return None


def offered(store: Any, *, now: float | None = None) -> list[Taste]:
    """The fresh uploads named aloud after a request in the last minutes (:func:`yuki.skills.youtube_feed.offered`), as
    tastes: the name said (the channel's), the video's link, its key. What "the Deji one" is matched against, for a
    slot that :func:`takes_offers` (``web.open_video``'s ``video``: a video nobody picked never plays). Never raises
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


def heard_as(names: Any, said: Any) -> str | None:
    """lSkills (live 17 Oct: "Lumin" heard for lumen while the talker's menu listed ``[playlist: lumen | ネオン]``):
    the one of ``names`` (the user's own names: the menu's taste candidates, the rows an app showed) that ``said``
    sounds like, or ``None`` (none, two, or ``said`` is one of them as written). Sound only, through the people
    index's own reading of names (:func:`yuki.memory.contacts.sounds_in` with ``short``: the same sound key and onset
    and spellings close enough, "Lumin"/"lumen" 0.25); a name in another script, or with digits, never matches. Data,
    never a list of words."""
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
    return next(iter(hits.values())) if len(hits) == 1 else None


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


def _heard_titles(store: Any, kind: str, now: float, days: float) -> dict[str, list[str]]:
    """``{entity name (folded): titles}``: for a capability kind read from a media context (:func:`capability_kinds`:
    Spotify's playlist), the app's own window titles (the timeline's, the user's rows) seen while that entity's context
    ran in the last ``days``, at most :data:`MEANING_TITLES` each, most seen first; a title seen under two entities is
    the app's own frame, not something one of them holds, and is left out. Decrypted in RAM, never logged; ``{}`` for
    any other kind or when it can't be read."""
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
    return {name: [t for t, _c in sorted(titles.items(), key=lambda kv: (-kv[1], kv[0]))[:MEANING_TITLES]]
            for name, titles in out.items()}


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
    out = [item for _k, item in items]
    have = {_norm(i["name"]) for i in out}
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
- mood: the words ask for a feel or an activity without naming one of their {things} ("something chill", "something
  to work out to", "something to sleep to"), and no item's name or "called" words say it: pick nothing (their usual
  for the hour will play). A feel or an activity is never content.
- none: the words name one of their {things} by a word none of these goes by ("my gym playlist" with no item called
  that), or two items fit equally: pick nothing."""


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


def _name_said(said: str, name: str) -> bool:
    """Whether ``said`` says ``name`` itself: its letters and digits in a row ("the 3 AM one" holds "3AM"), or a run of
    it sounds like the name (:func:`heard_as`: "Lumin" for lumen). Identity and sound, never meaning: the check behind a
    judgement that the words name an item (a model once called "workout" a mishearing of lumen)."""
    def squash(text: str) -> str:
        return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())

    if squash(name) and squash(name) in squash(said):
        return True
    return heard_as([name], said) is not None


def _settle(response: Any, names: list[str], text: str, ms: float,
            items: list[dict[str, Any]] | None = None) -> Meant:
    """A judgement's answer as a :class:`Meant`: the name only when it is one described (the tool's enum holds that
    already) and the code word allows one; a ``name`` verdict only when the words say that name by identity or sound
    (:func:`_name_said`), else ``none``."""
    call = next((b for b in getattr(response, "content", []) or [] if getattr(b, "type", None) == "tool_use"), None)
    args = dict(getattr(call, "input", {}) or {}) if call is not None else {}
    why = str(args.get("why") or "")
    pick = " ".join(str(args.get("name") or "").split())
    name = next((n for n in names if _norm(n) == _norm(pick)), None) if pick else None
    if why not in MEANING_WHYS:
        why = "none"
    if why in ("mood", "none"):
        name = None
    elif name is None or (why == "name" and not _name_said(text, name)):
        why, name = "none", None
    elif why == "called" and items is not None and not any(
            _norm(i.get("name")) == _norm(name) and i.get("called") for i in items):
        why = "content"     # nothing was ever called that: it can only be what the item holds
    return Meant(name, why, ms)


def judge(said: str, words: Any, items: list[dict[str, Any]], *, kind: str = "spotify_playlist",
          client: Any = None, model: str | None = None, settings: Any = None,
          wait_s: float = MEANING_WAIT_S) -> Meant:
    """lSkills (live 17 Oct: "anime playlist" asked which playlist twice although "Japanese" had just been paired with
    ネオン; "something chill" took 15-29 s): which of the user's own entities ``said`` means, judged by a small model
    (the voice's mouth model) on memory's own descriptions of them (:func:`described`), never by a word list. One
    forced tool call whose name field is an enum of the names described (it can only answer one of them, or none),
    bounded by ``wait_s`` on its own thread; ``Meant(None, "unknown")`` when there is nothing to judge, no client, or no
    answer in time (an answer that comes late is still kept for the same words and the same descriptions, so the next
    ask within :data:`MEANING_TTL_S` has it at once). A ``name`` verdict stands only when the words say that name by
    identity or sound (:func:`_name_said`). Content (names, titles, words) goes to the model only, never to a log."""
    started = time.perf_counter()
    names = [str(i.get("name") or "") for i in items if i.get("name")]
    text = " ".join(str(said or "").split())
    if not names or not text:
        return Meant(None, "unknown")
    # the same words against the same descriptions (a new pairing or play changes them, and the answer may change)
    key = (str(kind), _norm(text), json.dumps(items, ensure_ascii=False, sort_keys=True))
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
    turns = [str(w) for w in (words if isinstance(words, (list, tuple)) else [words]) if str(w or "").strip()]
    tool = {"name": "pick", "description": "Which of their items the words mean (or none).",
            "input_schema": {"type": "object", "properties": {
                "name": {"type": "string", "enum": names + [""], "description": "the item's exact name, or empty"},
                "why": {"type": "string", "enum": list(MEANING_WHYS)}}, "required": ["name", "why"]}}
    request = {"model": model, "max_tokens": 80,
               "system": MEANING_SYSTEM.replace("{things}", things),
               "tools": [tool], "tool_choice": {"type": "tool", "name": "pick"},
               "messages": [{"role": "user", "content": json.dumps(
                   {"said": text, "their words": turns[-3:], "items": items}, ensure_ascii=False)}]}
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
#: A page template variable with this name is an opaque id (a video's), opened only from a link or an offered option's
#: ref, never said (lSplit review r1 L5): its pages make no taste kind.
OPAQUE_VAR = "id"
#: Kinds an earlier seed wrote that no seed writes any more (sWeb's YouTube-only seed: now ``youtube_handle`` from the
#: site data): their ``observed`` rows are removed by the recompute (lWire review r1 L4). Our own old codes, never the
#: user's.
RETIRED_KINDS: tuple[str, ...] = ("yt_channel",)
#: A request of Yuki's counts as the user's pick when its runs' values came from these fill sources
#: (``skill_runs.filled``), and never when any came from :data:`GUESS_FILLS` (a guess must not feed itself). Only the
#: taste guess: ``pref`` fills means slots only (``connection.py`` derives ``Slot.pref`` for ``role = "means"``: which
#: browser, never which entity), so "open Deji's channel" in the usual browser is still the user's pick (tTaste r1 M1).
ASKED_FILLS: frozenset[str] = frozenset({"words", "again", "told"})
GUESS_FILLS: frozenset[str] = frozenset({"taste"})
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


class _Picks:
    """The picks gathered per ``(kind, group)``: ``(at, value, ref)``, later collapsed into sessions and decayed."""

    def __init__(self) -> None:
        self.by: dict[tuple[str, str], list[tuple[float, str, str]]] = {}

    def add(self, kind: str, group: str, at: float, value: str, ref: str) -> None:
        self.by.setdefault((kind, group), []).append((float(at), value, ref))

    def rows(self, store: Any, now: float) -> dict[tuple[str, str], dict[str, Any]]:
        """``{(kind, entity_key): row}``: two decayed hour histograms, the decayed count, the newest name and link."""
        out: dict[tuple[str, str], dict[str, Any]] = {}
        for (kind, _group), picks in self.by.items():
            picks.sort(key=lambda p: p[0])
            wd, we = [0.0] * 24, [0.0] * 24
            total, last, kept = 0.0, None, 0
            name, ref = "", ""
            for at, value, link in picks:
                if last is not None and at - last < SESSION_GAP_S:
                    last = at
                    continue
                last = at
                weight = math.pow(0.5, max(0.0, now - at) / (HALF_LIFE_DAYS * 86400.0))
                weekend, hour = _slot_of(at)
                (we if weekend else wd)[hour] += weight
                total += weight
                kept += 1
                name, ref = value or name, link or ref
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
    asked = _asked_runs(store, since)
    skip = _not_mine_moments(store)

    def users(r: dict[str, Any]) -> bool:
        if r.get("id") is not None and int(r["id"]) in skip:
            return False
        if r.get("by", "user") == "user":
            return True
        return asked.get(str(r.get("request_id") or ""), False)

    from yuki.connect.templates import fill

    picks = _Picks()
    cap_of = {(a, e): k for k, a, e in caps}
    page_rows: list[dict[str, Any]] = []
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
            picks.add(kind, group, float(r["at"]), value, fill(site_name, page_kind, **{var: value}) or "")
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
        picks.add(kind, str(r.get("entity_key") or _norm(value)), at, value, ref or "")
        counts["picks"] += 1
    channel_kind = ""
    fresh: dict[str, float] = {}
    if page_rows:
        try:
            from yuki.skills import youtube_feed

            channel_kind = youtube_feed.CHANNEL_KIND if youtube_feed.CHANNEL_KIND in kinds else ""
            if channel_kind:
                for at, handle, ref, group in youtube_feed.attributed(store, page_rows, users):
                    picks.add(channel_kind, group, at, handle, ref)
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


__all__ = ["SIGMA_MIN", "FALLBACK_MASS", "MENU_CANDIDATES", "OPTIONS_N", "SEEN_SOURCE", "SEEN_MAX", "SAID_SUFFIX",
           "Taste", "hour_score", "score_at", "top", "candidates", "seen_names", "options", "said_kind", "said_target",
           "said_words", "find", "offered", "find_offered", "takes_offers", "MEANING_WAIT_S", "MEANING_TTL_S",
           "MEANING_ITEMS", "MEANING_TITLES", "MEANING_DAYS", "MEANING_WHYS", "Meant", "heard_as", "described", "judge",
           "prewarm",
           "SEED_DAYS", "HALF_LIFE_DAYS",
           "SESSION_GAP_S", "FLOOR", "OPAQUE_VAR", "RETIRED_KINDS", "ASKED_FILLS", "GUESS_FILLS", "OFFER_FIELDS",
           "entity_key", "site_kinds",
           "capability_kinds", "slot_kind", "recompute", "seed_sites", "note", "saw", "unsay", "busy", "seed"]
