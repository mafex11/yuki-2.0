"""Taste: what the user picks by hour of day (PLAN.md D5, Phase T; map-routines T1). Read skeleton (sCore).

Table ``tastes`` (migration 41, T1.1's schema): one row per ``(kind, entity_key)`` (``yt_channel``,
``spotify_playlist``, ``discord_voice``, ...), the display name and its link encrypted, and two hour-of-day
histograms (weekday / weekend; 24 decayed counts, content-free). Phase T (``tTaste``) recomputes it nightly from the
moments and adds ``recompute`` / ``options``; this file only reads it, for the resolver's slot fill and the menu's
candidate names. Names are decrypted in-process and never logged.
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


__all__ = ["SIGMA_MIN", "MENU_CANDIDATES", "Taste", "hour_score", "top", "candidates", "find"]
