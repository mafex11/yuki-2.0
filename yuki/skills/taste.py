"""Taste: what the user picks by hour of day (PLAN.md D5, Phase T; map-routines T1). Read skeleton (sCore) and the
seeds that exist before phase T (sWeb).

Table ``tastes`` (migration 41, T1.1's schema): one row per ``(kind, entity_key)`` (``yt_channel``,
``spotify_playlist``, ``discord_voice``, ...), the display name and its link encrypted, and two hour-of-day
histograms (weekday / weekend; 24 decayed counts, content-free). Phase T (``tTaste``) recomputes it nightly from the
moments and adds ``recompute`` / ``options``; the read side here serves the resolver's slot fill and the menu's
candidate names. Names are decrypted in-process and never logged.

**Seeds (phase S, no fetching; fresh uploads are phase T):**

* :func:`seed_youtube`: the YouTube channels the user visits, from their own ``page`` moments on YouTube's
  channel-handle template (``youtube.handle``, ``yuki/connect/sites/youtube.toml``; Yuki's own visits never count),
  recomputed from the last 60 days with a 21-day half-life (map-routines T1.2, T1.3), never incremented, so a rerun
  changes nothing. No network: a handle's channel id is phase T's (one public read per channel).
* :func:`note`: one choice the user made in their own words that a skill then saw done (a Discord voice channel joined,
  kind ``discord_voice``: map-routines T1.2 "his words only").

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
#: The page template whose value is a YouTube channel's handle (``yuki/connect/sites/youtube.toml``'s ``handle``).
YT_HANDLE_TEMPLATE = "youtube.handle"


def _norm(value: Any) -> str:
    return " ".join(str(value or "").split()).casefold()


def entity_key(store: Any, kind: str, value: Any) -> str:
    """A taste's ``entity_key``: the store's keyed digest of the kind and the normalised id (a handle, a channel's
    name), never the value in clear."""
    return str(store.cipher.digest("taste", str(kind), _norm(value)))


def _slot_of(at: float) -> tuple[bool, int]:
    stamp = datetime.fromtimestamp(float(at))
    return stamp.weekday() >= 5, stamp.hour


def seed_youtube(store: Any, *, now: float | None = None, days: float = SEED_DAYS) -> dict[str, int]:
    """Recompute the ``yt_channel`` tastes (source ``observed``) from the user's own ``page`` moments on YouTube's
    handle template in the last ``days``: per handle, two hour histograms of visits decayed with a
    :data:`HALF_LIFE_DAYS` half-life, its name (the handle) and link (the handle page) encrypted. Rows no visit
    supports any more are removed. Idempotent; never raises (``{"error": 1}`` on failure). Content-free counts."""
    now = time.time() if now is None else float(now)
    try:
        rows = store.moments_between(now - float(days) * 86400.0, now + 1.0, kinds=["page"])
    except Exception:  # noqa: BLE001 - no moments table yet
        return {"channels": 0, "visits": 0, "error": 1}
    from yuki.connect.templates import fill

    found: dict[str, dict[str, Any]] = {}
    visits = 0
    for r in rows:
        facts = r.get("facts") or {}
        if r.get("by") != "user" or r.get("service") != "youtube" or facts.get("template") != YT_HANDLE_TEMPLATE:
            continue
        try:
            handle = " ".join(str(store.moment_entity(r) or "").split()).lstrip("@")
        except Exception:  # noqa: BLE001 - a row the key can't read
            continue
        if not handle:
            continue
        key = entity_key(store, "yt_channel", handle)
        entry = found.setdefault(key, {"name": handle, "wd": [0.0] * 24, "we": [0.0] * 24, "n": 0.0, "last": 0.0})
        at = float(r["at"])
        weight = math.pow(0.5, max(0.0, now - at) / (HALF_LIFE_DAYS * 86400.0))
        weekend, hour = _slot_of(at)
        entry["we" if weekend else "wd"][hour] += weight
        entry["n"] += weight
        entry["last"] = max(entry["last"], at)
        visits += 1

    def write(conn: Any) -> int:
        old = {str(k) for (k,) in conn.execute(
            "SELECT entity_key FROM tastes WHERE kind = 'yt_channel' AND source = 'observed'").fetchall()}
        for key, e in found.items():
            ref = fill("youtube", "handle", handle=e["name"]) or ""
            conn.execute(
                "INSERT INTO tastes (kind, entity_key, name_ciphertext, ref_ciphertext, source, hist_weekday, "
                "hist_weekend, n_obs, last_at, updated_at) VALUES ('yt_channel', ?, ?, ?, 'observed', ?, ?, ?, ?, ?) "
                "ON CONFLICT(kind, entity_key) DO UPDATE SET name_ciphertext = excluded.name_ciphertext, "
                "ref_ciphertext = excluded.ref_ciphertext, source = 'observed', hist_weekday = excluded.hist_weekday, "
                "hist_weekend = excluded.hist_weekend, n_obs = excluded.n_obs, last_at = excluded.last_at, "
                "updated_at = excluded.updated_at",
                (key, store._enc(e["name"]), store._enc(ref) if ref else None,
                 json.dumps([round(x, 4) for x in e["wd"]]), json.dumps([round(x, 4) for x in e["we"]]),
                 round(e["n"], 4), e["last"], now))
        gone = sorted(old - set(found))
        for key in gone:
            conn.execute("DELETE FROM tastes WHERE kind = 'yt_channel' AND source = 'observed' AND entity_key = ?",
                         (key,))
        return len(gone)

    try:
        removed = store._write(write)
    except Exception:  # noqa: BLE001
        return {"channels": 0, "visits": visits, "error": 1}
    return {"channels": len(found), "visits": visits, "removed": int(removed)}


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
    """Every phase S seed (the nightly intake's hook): content-free counts per seed."""
    return {"yt_channel": seed_youtube(store, now=now)}


__all__ = ["SIGMA_MIN", "MENU_CANDIDATES", "Taste", "hour_score", "top", "candidates", "find", "SEED_DAYS",
           "HALF_LIFE_DAYS", "YT_HANDLE_TEMPLATE", "entity_key", "seed_youtube", "note", "seed"]
