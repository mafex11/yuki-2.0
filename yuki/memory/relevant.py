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
import weakref
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
#: The kinds fast memory (:meth:`yuki.memory.api.MemoryClient.relevant`) ranks: the user's life and their own things.
BASE_KINDS = ("portrait", "journal", "commitment", "episode", "resource")
#: Every kind in the index: the whole-memory ranking (:mod:`yuki.memory.related`, migration 23) adds the session
#: summaries and exchanges with Yuki, know-how, call digests, threads of work and links seen.
KINDS = (*BASE_KINDS, "session", "chat", "knowhow", "call", "thread", "link")
#: Kinds that do not age: the portrait worker keeps portrait facts current, a resource of the user's (their
#: site, their email) stays theirs until the conversation worker ends it, and know-how holds until superseded.
EVERGREEN = ("portrait", "resource", "knowhow")
#: Kinds whose rows change in place (a thread's state moves on): reloaded whole when they changed.
VOLATILE = ("thread",)
#: A journal fact or a link that rests only on a window beside the one in front, or on a popup (migration 24:
#: the user may not have looked at it), ranks this much lower: weaker evidence of the user's attention. A
#: starting weight on the scale of the other bonuses (about three importance points); no visible or popup
#: capture existed before 29 Sept to measure it on - re-tune on the first days of them.
ATTENTION_DISCOUNT = 0.04


@dataclass
class _Kind:
    """One kind's rows: ids, vectors and the metadata ranking needs (nothing decrypted but vectors)."""

    ids: list[int] = field(default_factory=list)
    vecs: list[np.ndarray] = field(default_factory=list)
    at: list[float] = field(default_factory=list)
    importance: list[float] = field(default_factory=list)
    usable: list[bool] = field(default_factory=list)
    #: Added to the score (a weaker-attention item's discount), 0 for most.
    offset: list[float] = field(default_factory=list)
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
            "offset": np.asarray([self.offset[i] if i < len(self.offset) else 0.0 for i in keep], dtype=np.float32),
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
        #: Per volatile kind, what its rows looked like when loaded (count, newest change): reloaded when it moves.
        self._volatile_seen: dict[str, Any] = {}

    # -- loading -------------------------------------------------------------

    def refresh(self) -> dict[str, int]:
        """Load rows added since the last look and drop the ones no longer live. Returns rows per kind."""
        started = time.perf_counter()
        with self._lock:
            self._check_generation()
            live = self.store.recall_live_ids()
            for kind in KINDS:
                k = self._kinds[kind]
                if kind in VOLATILE:
                    seen = self._volatile_signature(kind)
                    if seen != self._volatile_seen.get(kind):
                        self._kinds[kind] = k = _Kind()   # a thread's state moved on: its vector did too
                        self._volatile_seen[kind] = seen
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
                        elif kind == "chat":   # an exchange said to someone else was never Yuki's to learn from;
                            # one nobody has judged yet (a call turn cut off by the next words, 77b) neither, for now
                            usable = r["addressed"] not in ("others", "unanswered")
                        k.usable.append(usable)
                        weak = "attention" in r.keys() and r["attention"] in ("visible", "popup")
                        k.offset.append(-ATTENTION_DISCOUNT if weak else 0.0)
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
                elif kind in live:
                    alive = live[kind]
                    drop = [i for i, x in enumerate(k.ids) if x not in alive]
                else:   # summaries, exchanges, call digests and links are never ended
                    drop = []
                if drop:
                    dead = set(drop)
                    for name in ("ids", "vecs", "at", "importance", "usable", "offset"):
                        setattr(k, name, [v for i, v in enumerate(getattr(k, name)) if i not in dead])
                    k.dirty()
            # a portrait fact re-rated in place (dreaming's promote / demote) keeps its id: re-read the ratings
            self._reload_portrait_importance()
            self.last_refresh_ms = round((time.perf_counter() - started) * 1000, 2)
            return {kind: len(self._kinds[kind].ids) for kind in KINDS}

    def _volatile_signature(self, kind: str) -> Any:
        """How a volatile kind's rows look now (count and newest change), one cheap query."""
        try:
            if kind == "thread":
                row = self.store._query("SELECT count(*), max(updated_at) FROM work_threads")[0]
                return (int(row[0] or 0), float(row[1] or 0.0))
        except Exception:
            return None
        return None

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
               same_as: float = SAME_AS, kinds: tuple[str, ...] = BASE_KINDS,
               boost: dict[str, set[int]] | None = None, boost_weight: float = 0.0,
               per_kind: int | None = None, exclude: dict[str, set[int]] | None = None,
               excluded: set[tuple[str, int]] | None = None) -> list[dict[str, Any]]:
        """The best items for ``query_vec``: ``[{kind, id, score, cosine, importance, at}]``, best first.

        ``boost``: per kind, ids that get ``boost_weight`` added to their score once they clear
        ``min_cosine`` (the whole-memory ranking's thread affinity; it never lets an item in by itself).
        ``per_kind``: at most this many of one kind (a diverse block over many kinds).
        ``exclude``: per kind, ids that are never candidates (Yuki's own conversation going on: left out before the
        near-duplicate check, so an older exchange like it still comes); those that cleared ``min_cosine`` are
        added to ``excluded``.
        """
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
                drop = (exclude or {}).get(kind)
                if drop and ok.any():
                    gone = np.isin(k.arr["ids"], np.fromiter(drop, dtype=np.int64)) & ok
                    if gone.any():
                        if excluded is not None:
                            excluded.update((kind, int(i)) for i in k.arr["ids"][gone])
                        ok = ok & ~gone
                if not ok.any():
                    continue
                imp = (k.arr["importance"] - 5.0) / 5.0
                if kind in EVERGREEN:
                    rec = np.ones_like(cos)
                else:
                    age_days = np.maximum(now - k.arr["at"], 0.0) / 86400.0
                    rec = np.power(0.5, age_days / max(half_life_days, 1e-6))
                score = cos + importance_weight * imp + recency_weight * rec + k.arr["offset"]
                if boost and boost_weight and boost.get(kind):
                    score = score + boost_weight * np.isin(k.arr["ids"], np.fromiter(boost[kind], dtype=np.int64))
                for i in np.nonzero(ok)[0]:
                    candidates.append((float(score[i]), kind, int(k.arr["ids"][i]), float(cos[i]),
                                       float(k.arr["importance"][i]), float(k.arr["at"][i]), k.matrix[i]))
        candidates.sort(key=lambda c: -c[0])
        picked: list[tuple[float, str, int, float, float, float, np.ndarray]] = []
        counts: dict[str, int] = {}
        for c in candidates:
            if len(picked) >= limit:
                break
            if per_kind is not None and counts.get(c[1], 0) >= per_kind:
                continue
            if any(float(np.dot(c[6], p[6])) >= same_as for p in picked):
                continue
            picked.append(c)
            counts[c[1]] = counts.get(c[1], 0) + 1
        return [{"kind": c[1], "id": c[2], "score": round(c[0], 4), "cosine": round(c[3], 4),
                 "importance": c[4], "at": c[5]} for c in picked]

    def vectors_of(self, kind: str, ids: set[int]) -> dict[int, np.ndarray]:
        """The loaded (unit) vectors of these ids of one kind (the ranking's dedupe across queries)."""
        with self._lock:
            k = self._kinds.get(kind)
            if k is not None and k.matrix is None and k.vecs:
                k.build(k.vecs[0].shape[0])   # not searched yet this refresh: build it now
            if k is None or k.matrix is None:
                return {}
            pos = {int(x): i for i, x in enumerate(k.arr.get("ids", []))}
            return {i: k.matrix[pos[i]] for i in ids if i in pos}

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


_SHARED_LOCK = threading.Lock()
_SHARED: "weakref.WeakKeyDictionary[Any, RelevantIndex]" = weakref.WeakKeyDictionary()


def shared_index(store: Any) -> RelevantIndex:
    """The one :class:`RelevantIndex` of ``store`` in this process (the memory service's screen recall and the
    question finders share it: one copy of the vectors in RAM). Thread-safe."""
    with _SHARED_LOCK:
        index = _SHARED.get(store)
        if index is None:
            index = _SHARED[store] = RelevantIndex(store)
        return index
