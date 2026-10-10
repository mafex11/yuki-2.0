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


def top(store: Any, kind: str, *, now: float | None = None, n: int = 1, until: float | None = None) -> list[Taste]:
    """The ``n`` entities of ``kind`` the user picks most at this hour (:func:`score_at`), best first. ``until``: rows
    last updated before it only (replays). Read only; never raises (``[]``)."""
    now = time.time() if now is None else float(now)
    try:
        rows = store._query(
            "SELECT entity_key, name_ciphertext, ref_ciphertext, hist_weekday, hist_weekend, n_obs FROM tastes "
            "WHERE kind = ?" + (" AND updated_at < ?" if until is not None else ""),
            (kind, float(until)) if until is not None else (kind,))
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
    """The names the menu lists for an entity slot of ``kind`` (the user's own names: their playlists, channels),
    best at this hour first; ``by="n_obs"``: most seen first, an order that changes only when the table is recomputed
    (nightly), for a menu that sits in a cached prompt block (the thinker's ``run_skill`` description)."""
    if by == "n_obs":
        rows = top(store, kind, now=now, n=10_000)
        rows.sort(key=lambda t: (-t.n_obs, t.entity_key))
        return [t.name for t in rows if t.name][:max(0, int(limit))]
    return [t.name for t in top(store, kind, now=now, n=limit) if t.name]


def options(store: Any, kind: str, *, now: float | None = None, n: int = OPTIONS_N) -> list[str]:
    """T1.3: the names the talker or the model may match the user's words against (their own playlist or channel
    names, best at this hour first). Never raises."""
    return candidates(store, kind, now=now, limit=n)


def find(store: Any, kind: str, name: str) -> Taste | None:
    """The taste row whose name equals ``name`` (case and spacing folded: identity, never meaning)."""
    want = " ".join(str(name or "").split()).casefold()
    if not want:
        return None
    for t in top(store, kind, n=200):
        if " ".join(t.name.split()).casefold() == want:
            return t
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
         source: str = "said") -> bool:
    """One choice the user named and a skill saw done (a voice channel joined): ``kind``'s row for ``name`` gets one
    more observation at that hour (its name and link encrypted). Never raises (False on failure)."""
    clean = " ".join(str(name or "").split())
    if not clean or not str(kind or "").strip():
        return False
    at = time.time() if at is None else float(at)
    weekend, hour = _slot_of(at)
    key = entity_key(store, kind, clean)

    def write(conn: Any) -> None:
        row = conn.execute("SELECT hist_weekday, hist_weekend, n_obs FROM tastes WHERE kind = ? AND entity_key = ?",
                           (kind, key)).fetchone()
        wd, we = (_hist(row[0]), _hist(row[1])) if row else ([0.0] * 24, [0.0] * 24)
        (we if weekend else wd)[hour] += 1.0
        n = float(row[2] or 0.0) + 1.0 if row else 1.0
        if row:
            conn.execute("UPDATE tastes SET name_ciphertext = ?, ref_ciphertext = coalesce(?, ref_ciphertext), "
                         "hist_weekday = ?, hist_weekend = ?, n_obs = ?, last_at = ?, updated_at = ? "
                         "WHERE kind = ? AND entity_key = ?",
                         (store._enc(clean), store._enc(ref) if ref else None, json.dumps(wd), json.dumps(we), n, at,
                          at, kind, key))
        else:
            conn.execute("INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                         "hist_weekend, n_obs, last_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (kind, key, store._enc(clean), store._enc(ref) if ref else None, source, json.dumps(wd),
                          json.dumps(we), n, at, at))

    try:
        store._write(write)
        return True
    except Exception:  # noqa: BLE001
        return False


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


__all__ = ["SIGMA_MIN", "FALLBACK_MASS", "MENU_CANDIDATES", "OPTIONS_N", "Taste", "hour_score", "score_at", "top",
           "candidates", "options", "find", "offered", "find_offered", "takes_offers", "SEED_DAYS", "HALF_LIFE_DAYS",
           "SESSION_GAP_S", "FLOOR", "OPAQUE_VAR", "RETIRED_KINDS", "ASKED_FILLS", "GUESS_FILLS", "OFFER_FIELDS",
           "entity_key", "site_kinds",
           "capability_kinds", "slot_kind", "recompute", "seed_sites", "note", "busy", "seed"]
