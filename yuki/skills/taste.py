"""Taste: what the user picks by hour of day (PLAN.md D5, Phase T; map-routines T1). Read skeleton (sCore), the
seeds that exist before phase T (sWeb), and the taste kinds of phase L (lWire; learner design §5).

Table ``tastes`` (migration 41, T1.1's schema): one row per ``(kind, entity_key)``, the display name and its link
encrypted, and two hour-of-day histograms (weekday / weekend; 24 decayed counts, content-free). Phase T (``tTaste``)
recomputes it nightly from the moments and adds ``recompute`` / ``options``; the read side here serves the resolver's
slot fill and the menu's candidate names. Names are decrypted in-process and never logged.

**Taste kinds come from data, never a list here** (design §5: "fill-slot kinds come from capability and site data"):

* a capability's fill slot that declares an entity ``kind``: ``<connection app>_<kind>`` (``connection.py`` derives it:
  a playlist slot's kind);
* a site file's page kind whose template has a speakable variable (``yuki/connect/sites/*.toml`` ``[pages]``; a
  variable named ``id`` is an opaque id, opened only from a link: lSplit's ``page_address`` rule): ``<site>_<page>``
  (:func:`site_kinds`); a learned skill bound to that site and page fills its entity slot from it (:func:`slot_kind`);
* the same entity seen in two browsers is one row: the key is a keyed digest of the kind and the value, never of the
  program (moments' ``entity_key`` joins them the same way, D23).

**Seeds (no fetching; fresh uploads are phase T):**

* :func:`seed_sites`: per site page kind of :func:`site_kinds`, the entities the user visits, from their own ``page``
  moments on that template (Yuki's own visits never count), recomputed from the last 60 days with a 21-day half-life
  (map-routines T1.2, T1.3), never incremented, so a rerun changes nothing. No network.
* :func:`note`: one choice the user made in their own words that a skill then saw done (a voice channel joined:
  map-routines T1.2 "his words only").

``seed(store)`` runs every seed (the nightly intake's hook). Content-free counts back; names only encrypted.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: The smoothing of the hour histograms when scoring an hour (minutes; map-routines T: sigma = 90 min).
SIGMA_MIN = 90.0
#: Candidates the menu lists per slot at most (map-skills S3.1).
MENU_CANDIDATES = 5


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


def top(store: Any, kind: str, *, now: float | None = None, n: int = 1, until: float | None = None) -> list[Taste]:
    """The ``n`` entities of ``kind`` the user picks most at this hour (weekday or weekend histogram), best first.
    ``until``: rows last updated before it only (replays). Read only; never raises (``[]``)."""
    now = time.time() if now is None else float(now)
    try:
        rows = store._query(
            "SELECT entity_key, name_ciphertext, ref_ciphertext, hist_weekday, hist_weekend, n_obs FROM tastes "
            "WHERE kind = ?" + (" AND updated_at < ?" if until is not None else ""),
            (kind, float(until)) if until is not None else (kind,))
    except Exception:  # noqa: BLE001
        return []
    stamp = datetime.fromtimestamp(now)
    hour = stamp.hour + stamp.minute / 60.0
    weekend = stamp.weekday() >= 5
    scored = []
    for key, name_ct, ref_ct, wd, we, n_obs in rows:
        score = hour_score(_hist(we if weekend else wd), hour)
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


def find(store: Any, kind: str, name: str) -> Taste | None:
    """The taste row whose name equals ``name`` (case and spacing folded: identity, never meaning)."""
    want = " ".join(str(name or "").split()).casefold()
    if not want:
        return None
    for t in top(store, kind, n=200):
        if " ".join(t.name.split()).casefold() == want:
            return t
    return None


# ---------------------------------------------------------------------------------------------------------------------
# Seeds (sWeb): what exists before phase T's nightly recompute
# ---------------------------------------------------------------------------------------------------------------------

#: How far back the seeds look, and the half-life of an observation (map-routines T1.3).
SEED_DAYS = 60.0
HALF_LIFE_DAYS = 21.0
#: A page template variable with this name is an opaque id (a video's), opened only from a link or an offered option's
#: ref, never said (lSplit review r1 L5): its pages make no taste kind.
OPAQUE_VAR = "id"


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def entity_key(store: Any, kind: str, value: Any) -> str:
    """A taste's ``entity_key``: the store's keyed digest of the kind and the normalised id (a handle, a channel's
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


def seed_sites(store: Any, *, now: float | None = None, days: float = SEED_DAYS) -> dict[str, Any]:
    """Recompute the site tastes (source ``observed``) of every :func:`site_kinds` kind from the user's own ``page``
    moments on that template in the last ``days``: per entity, two hour histograms of visits decayed with a
    :data:`HALF_LIFE_DAYS` half-life, its name (the template's value) and link (the site's ``[urls]`` of that kind)
    encrypted. Rows no visit supports any more are removed (per seeded kind only). Idempotent; never raises
    (``{"error": 1}`` on failure). Content-free counts."""
    now = time.time() if now is None else float(now)
    kinds = {f"{s}.{p}": (k, s, p, v) for k, s, p, v in site_kinds()}
    if not kinds:
        return {"kinds": 0, "entities": 0, "visits": 0}
    try:
        rows = store.moments_between(now - float(days) * 86400.0, now + 1.0, kinds=["page"])
    except Exception:  # noqa: BLE001 - no moments table yet
        return {"kinds": len(kinds), "entities": 0, "visits": 0, "error": 1}
    from yuki.connect.templates import fill

    found: dict[tuple[str, str], dict[str, Any]] = {}
    visits = 0
    for r in rows:
        facts = r.get("facts") or {}
        hit = kinds.get(str(facts.get("template") or ""))
        if r.get("by") != "user" or hit is None:
            continue
        kind, site_name, page_kind, var = hit
        try:
            value = " ".join(str(store.moment_entity(r) or "").split())
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        if not value:
            continue
        key = entity_key(store, kind, value)
        entry = found.setdefault((kind, key), {"name": value, "ref": fill(site_name, page_kind, **{var: value}) or "",
                                               "wd": [0.0] * 24, "we": [0.0] * 24, "n": 0.0, "last": 0.0})
        at = float(r["at"])
        weight = math.pow(0.5, max(0.0, now - at) / (HALF_LIFE_DAYS * 86400.0))
        weekend, hour = _slot_of(at)
        entry["we" if weekend else "wd"][hour] += weight
        entry["n"] += weight
        entry["last"] = max(entry["last"], at)
        visits += 1
    seeded = sorted({k for k, _s, _p, _v in kinds.values()})

    def write(conn: Any) -> int:
        marks = ",".join("?" * len(seeded))
        old = {(str(k), str(e)) for k, e in conn.execute(
            f"SELECT kind, entity_key FROM tastes WHERE source = 'observed' AND kind IN ({marks})", seeded).fetchall()}
        for (kind, key), e in found.items():
            conn.execute(
                "INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                "hist_weekend, n_obs, last_at, updated_at) VALUES (?, ?, ?, ?, 'observed', ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, entity_key) DO UPDATE SET name_ciphertext = excluded.name_ciphertext, "
                "ref_ciphertext = excluded.ref_ciphertext, source = 'observed', hist_weekday = excluded.hist_weekday, "
                "hist_weekend = excluded.hist_weekend, n_obs = excluded.n_obs, last_at = excluded.last_at, "
                "updated_at = excluded.updated_at",
                (kind, key, store._enc(e["name"]), store._enc(e["ref"]) if e["ref"] else None,
                 json.dumps([round(x, 4) for x in e["wd"]]), json.dumps([round(x, 4) for x in e["we"]]),
                 round(e["n"], 4), e["last"], now))
        gone = sorted(old - set(found))
        for kind, key in gone:
            conn.execute("DELETE FROM tastes WHERE kind = ? AND source = 'observed' AND entity_key = ?", (kind, key))
        return len(gone)

    try:
        removed = store._write(write)
    except Exception:  # noqa: BLE001
        return {"kinds": len(seeded), "entities": 0, "visits": visits, "error": 1}
    return {"kinds": len(seeded), "entities": len(found), "visits": visits, "removed": int(removed)}


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


def seed(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """Every seed (the memory service's daily hook): content-free counts per seed. The memory service's
    ``taste_seed`` log line reads the counts under the seed's first key (sWeb's), kept as an alias."""
    pages = seed_sites(store, now=now)
    return {"pages": pages, "yt_channel": pages}


__all__ = ["SIGMA_MIN", "MENU_CANDIDATES", "Taste", "hour_score", "top", "candidates", "find", "SEED_DAYS",
           "HALF_LIFE_DAYS", "OPAQUE_VAR", "entity_key", "site_kinds", "slot_kind", "seed_sites", "note", "seed"]
