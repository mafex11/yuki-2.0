"""Fast memory mid-chat: the few memories that bear on the user's message, before every reply.

OpenClaw's "trigger injection" adapted to Yuki (docs/research/openclaw-memory.md 1.4, 4 2a): the
user's message is embedded once by the shared embedder (the memory service's, over its pipe:
:mod:`yuki.memory.embed_ipc`; the same vector the know-how lookup uses), and compared with every
current portrait fact, journal fact, open or done commitment and episode, of any age. Each is
ranked by

    score = cosine + IMPORTANCE_WEIGHT * (importance - 5) / 5 + RECENCY_WEIGHT * 0.5 ** (age / half-life)

- **cosine**: the local embedder's similarity (measured, no words matched);
- **importance** (1-10): written once, when the item was written, by the model that wrote it
  (the journal's Haiku per fact, the portrait's Sonnet per fact, moved nightly by dreaming); an
  episode takes the highest importance of the journal facts inside it; missing = neutral 5;
- **recency**: a half-life for dated items (journal, episodes, commitments); portrait facts are
  evergreen (the portrait worker keeps them current).

Only items whose cosine clears :data:`MIN_COSINE` are candidates (tuned on a copy of the real
memory, docs/MEMORY.md "Fast memory"), at most :data:`LIMIT` go in, and an item nearly
identical to one already picked (cosine :data:`SAME_AS`) is left out. Journal facts marked
uncertain or tainted (text for or from another AI tool) never go in.

Cost: the index keeps the decrypted vectors in memory and loads only rows added since the last
look (plus id-only liveness queries), so a request costs one embedding round trip over the pipe
and a matrix product over a few thousand rows; only the picked items are decrypted.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import numpy as np

#: Candidates must be at least this similar to the message (cosine, the shared embedder's).
MIN_COSINE = 0.48
#: At most this many items go into the block.
LIMIT = 4
#: How much importance moves the score: (importance - 5) / 5 * this (so +-this at 10 / 0).
IMPORTANCE_WEIGHT = 0.06
#: How much recency moves the score: this * 0.5 ** (age / half-life).
RECENCY_WEIGHT = 0.05
#: Half-life of a dated item's recency bonus, days.
HALF_LIFE_DAYS = 14.0
#: An item at least this similar to one already picked says the same thing: left out.
SAME_AS = 0.90
#: Memories of the conversation's previous message rank this far below the current message's (a follow-up
#: such as "elaborate" names nothing itself; its subject is the previous message's).
FOLLOW_UP_DISCOUNT = 0.05
#: Kinds in the index, and whether their recency decays.
KINDS = ("portrait", "journal", "commitment", "episode")


@dataclass
class _Kind:
    """One kind's rows: ids, vectors and the metadata ranking needs (nothing decrypted but vectors)."""

    ids: list[int] = field(default_factory=list)
    vecs: list[np.ndarray] = field(default_factory=list)
    at: list[float] = field(default_factory=list)
    importance: list[float] = field(default_factory=list)
    usable: list[bool] = field(default_factory=list)
    max_id: int = 0
    matrix: np.ndarray | None = None
    #: Arrays aligned with ``matrix``, rebuilt with it.
    arr: dict[str, np.ndarray] = field(default_factory=dict)

    def dirty(self) -> None:
        self.matrix = None

    def build(self, dim: int) -> None:
        if self.matrix is not None:
            return
        keep = [i for i, v in enumerate(self.vecs) if v.shape[0] == dim]
        if keep:
            m = np.stack([self.vecs[i] for i in keep]).astype(np.float32)
            norms = np.linalg.norm(m, axis=1)
            norms[norms == 0] = 1.0
            self.matrix = m / norms[:, None]
        else:
            self.matrix = np.zeros((0, dim), dtype=np.float32)
        self.arr = {
            "ids": np.asarray([self.ids[i] for i in keep], dtype=np.int64),
            "at": np.asarray([self.at[i] for i in keep], dtype=np.float64),
            "importance": np.asarray([self.importance[i] for i in keep], dtype=np.float32),
            "usable": np.asarray([self.usable[i] for i in keep], dtype=bool),
        }


class RelevantIndex:
    """The in-process index behind :meth:`yuki.memory.api.MemoryClient.relevant`. Thread-safe."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._lock = threading.Lock()
        self._kinds = {k: _Kind() for k in KINDS}
        self._episode_imp: dict[int, int] = {}
        self.last_refresh_ms = 0.0
        #: The database's embedding generation the loaded vectors belong to (bumped when the model changes).
        self._generation: str | None = None

    # -- loading -------------------------------------------------------------

    def refresh(self) -> dict[str, int]:
        """Load rows added since the last look and drop the ones no longer live. Returns rows per kind."""
        started = time.perf_counter()
        with self._lock:
            self._check_generation()
            live = self.store.recall_live_ids()
            for kind in KINDS:
                k = self._kinds[kind]
                rows = self.store.recall_vectors(kind, k.max_id)
                if rows:
                    new_eps: list[tuple[int, float, float]] = []
                    for r, vec in rows:
                        k.ids.append(int(r["id"]))
                        k.vecs.append(vec)
                        k.at.append(float(r["at"] or 0.0))
                        imp = r["importance"] if "importance" in r.keys() else None
                        k.importance.append(float(imp) if imp is not None else 5.0)
                        usable = True
                        if kind == "journal":
                            usable = (r["nature"] or "fact") != "uncertain" and not r["tainted"]
                        k.usable.append(usable)
                        k.max_id = max(k.max_id, int(r["id"]))
                        if kind == "episode":
                            new_eps.append((int(r["id"]), float(r["at"]), float(r["ended_at"])))
                    if new_eps:
                        imps = self.store.episode_importance(new_eps)
                        for i, eid in enumerate(k.ids):
                            if eid in imps:
                                k.importance[i] = float(imps[eid])
                    k.dirty()
                dead: set[int] | None = None
                if kind == "journal":
                    gone = live["journal_dead"]
                    drop = [i for i, x in enumerate(k.ids) if x in gone]
                else:
                    alive = live[kind]
                    drop = [i for i, x in enumerate(k.ids) if x not in alive]
                if drop:
                    dead = set(drop)
                    for name in ("ids", "vecs", "at", "importance", "usable"):
                        setattr(k, name, [v for i, v in enumerate(getattr(k, name)) if i not in dead])
                    k.dirty()
            # a portrait fact re-rated in place (dreaming's promote / demote) keeps its id: re-read the ratings
            self._reload_portrait_importance()
            self.last_refresh_ms = round((time.perf_counter() - started) * 1000, 2)
            return {kind: len(self._kinds[kind].ids) for kind in KINDS}

    def _check_generation(self) -> None:
        """A re-embed flipped the database to another model: every loaded vector is stale, load all again."""
        try:
            from yuki.memory.reembed import embed_generation

            generation = embed_generation(self.store)
        except Exception:  # noqa: BLE001 - an older database: no generations
            return
        if self._generation is not None and generation != self._generation:
            self._kinds = {k: _Kind() for k in KINDS}
            self._episode_imp.clear()
        self._generation = generation

    def _reload_portrait_importance(self) -> None:
        k = self._kinds["portrait"]
        if not k.ids:
            return
        rows = self.store._query("SELECT id, importance FROM portrait_facts WHERE valid_to IS NULL")
        rating = {int(r[0]): (float(r[1]) if r[1] is not None else 5.0) for r in rows}
        changed = False
        for i, fid in enumerate(k.ids):
            value = rating.get(fid, k.importance[i])
            if value != k.importance[i]:
                k.importance[i] = value
                changed = True
        if changed:
            k.dirty()

    # -- search --------------------------------------------------------------

    def search(self, query_vec: np.ndarray, *, now: float | None = None, limit: int = LIMIT,
               min_cosine: float = MIN_COSINE, importance_weight: float = IMPORTANCE_WEIGHT,
               recency_weight: float = RECENCY_WEIGHT, half_life_days: float = HALF_LIFE_DAYS,
               same_as: float = SAME_AS, kinds: tuple[str, ...] = KINDS) -> list[dict[str, Any]]:
        """The best items for ``query_vec``: ``[{kind, id, score, cosine, importance, at}]``, best first."""
        now = time.time() if now is None else float(now)
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return []
        q = q / qn
        candidates: list[tuple[float, str, int, float, float, float, np.ndarray]] = []
        with self._lock:
            for kind in kinds:
                k = self._kinds[kind]
                k.build(q.shape[0])
                if k.matrix is None or not len(k.matrix):
                    continue
                cos = k.matrix @ q
                ok = (cos >= min_cosine) & k.arr["usable"]
                if not ok.any():
                    continue
                imp = (k.arr["importance"] - 5.0) / 5.0
                if kind == "portrait":
                    rec = np.ones_like(cos)
                else:
                    age_days = np.maximum(now - k.arr["at"], 0.0) / 86400.0
                    rec = np.power(0.5, age_days / max(half_life_days, 1e-6))
                score = cos + importance_weight * imp + recency_weight * rec
                for i in np.nonzero(ok)[0]:
                    candidates.append((float(score[i]), kind, int(k.arr["ids"][i]), float(cos[i]),
                                       float(k.arr["importance"][i]), float(k.arr["at"][i]), k.matrix[i]))
        candidates.sort(key=lambda c: -c[0])
        picked: list[tuple[float, str, int, float, float, float, np.ndarray]] = []
        for c in candidates:
            if len(picked) >= limit:
                break
            if any(float(np.dot(c[6], p[6])) >= same_as for p in picked):
                continue
            picked.append(c)
        return [{"kind": c[1], "id": c[2], "score": round(c[0], 4), "cosine": round(c[3], 4),
                 "importance": c[4], "at": c[5]} for c in picked]

    def stats(self) -> dict[str, Any]:
        return {"rows": {k: len(v.ids) for k, v in self._kinds.items()}, "refresh_ms": self.last_refresh_ms}


def recency(at: float, now: float, half_life_days: float = HALF_LIFE_DAYS) -> float:
    """``0.5 ** (age / half-life)`` (for tests and tuning)."""
    return math.pow(0.5, max(now - at, 0.0) / 86400.0 / half_life_days)


def portrait_fact_text(fact: Any) -> str:
    """What a portrait fact is embedded as: its subject and its text."""
    subject = str(getattr(fact, "subject", "") or "").strip()
    text = str(getattr(fact, "text", "") or "").strip()
    return f"{subject}: {text}" if subject and subject.casefold() not in text.casefold() else text


def backfill_portrait_vectors(store: Any, embedder: Any, *, limit: int = 256) -> int:
    """Embed current portrait facts that have no vector (new versions, or written before migration 17).

    ``embedder``: anything with ``embed(texts) -> (n, dim)`` and ``model_name`` (the service's
    :func:`~yuki.memory.embed.get_embedder`, or an :class:`~yuki.memory.embed_ipc.EmbedClient`).
    Returns how many were stored; never raises.
    """
    try:
        facts = store.portrait_facts_without_vectors(limit)
        if not facts:
            return 0
        vecs = embedder.embed([portrait_fact_text(f) for f in facts])
        if vecs is None or len(vecs) != len(facts):
            return 0
        return store.add_portrait_fact_vectors(list(zip([f.id for f in facts], vecs)),
                                               getattr(embedder, "model_name", None) or "")
    except Exception:
        return 0
