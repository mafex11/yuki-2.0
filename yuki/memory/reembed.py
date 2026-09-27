"""Move memory's stored vectors to another embedding model, in the background, without a mix (maintenance).

Every stored vector carries the model that made it (``<table>_vec.model``) and a database records
the model its vectors are made with (``embed_state.active_model``, migration 20; see
:func:`yuki.memory.embed.active_model`, which every process embeds with). Changing the model:

1. **Side vectors.** For every row of every vector table, the target model's vector is written
   to ``vec_next`` (keyed by table and row, with the nonce of the old vector it replaces), a small
   batch at a time. Meanwhile the old model keeps serving: searches read only the vector tables,
   and new rows keep getting the old model's vectors (the re-embed picks them up next), so
   nothing is ever compared across models and nothing written during the move is lost.
2. **Flip.** When no row lacks a fresh side vector, one transaction copies them over the old ones
   (only where the old vector is still the one that was re-embedded), sets ``active_model`` to
   the target, bumps ``generation`` (in-memory vector caches reload on it) and empties
   ``vec_next``. Readers see all old or all new. A row written between the last batch and the
   flip makes the flip wait for one more batch.
3. **Sweep.** After a flip, any vector still tagged with another model (written in the few
   seconds before a process noticed the flip) is re-embedded in place with the active model.

The state is in the database, so it resumes after a restart or a kill at any point (a batch is
one transaction). The CLI and the service's background thread can run at the same time.

    python -m yuki.memory.reembed [--db PATH] [--dry-run] [--status] [--batch N] [--model NAME]

The background thread (:class:`ReembedWorker`, ``yuki-memory-reembed``) runs below the service's
already below-normal priority at the lowest thread priority, one ONNX thread, a batch then a pause,
and waits while the process's own embedder is busy (the journal, conversations) or ``busy()``
says so (a game or a call).
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

#: Texts per batch (one transaction each).
BATCH = 16
#: Seconds between batches (the thread's duty cycle).
PAUSE_S = 2.0
#: Seconds between looks when there is nothing to do.
IDLE_S = 900.0
#: Seconds to wait while the process's embedder or ``busy()`` is busy.
BUSY_WAIT_S = 5.0


@dataclass(frozen=True)
class VecTable:
    """One vector table: its row id column, the table the rows belong to, and the text each is embedded as."""

    table: str
    id_col: str
    source: str
    text: Callable[[Any, sqlite3.Row], str]


def _cfact_text(store: Any, row: sqlite3.Row) -> str:
    f = store._cfact(row)
    return f"{f.text} {f.quote or ''}".strip()


def _question_text(store: Any, row: sqlite3.Row) -> str:
    q = store._question_row(row)
    return f"{q.subject}: {q.question}"


def _portrait_text(store: Any, row: sqlite3.Row) -> str:
    from yuki.memory.relevant import portrait_fact_text

    return portrait_fact_text(store._fact(row))


def _turn_text(store: Any, row: sqlite3.Row) -> str:
    return type(store).turn_text(store._turn(row))[:2000]


#: Every vector table and the text its writers embed (journal.py, episodes.py, conversations.py,
#: review.py, questions.py, api.py know-how, relevant.py portrait facts).
VEC_TABLES: tuple[VecTable, ...] = (
    VecTable("journal_vec", "journal_id", "journal", lambda s, r: s._entry(r).fact),
    VecTable("episode_vec", "episode_id", "episodes", lambda s, r: s._episode(r).text),
    VecTable("portrait_fact_vec", "fact_id", "portrait_facts", _portrait_text),
    VecTable("conversation_fact_vec", "fact_id", "conversation_facts", _cfact_text),
    VecTable("conversation_turn_vec", "turn_id", "conversation_turns", _turn_text),
    VecTable("session_summary_vec", "summary_id", "session_summaries", lambda s, r: s._summary(r).text),
    VecTable("knowhow_vec", "knowhow_id", "knowhow", lambda s, r: s._knowhow(r).text),
    VecTable("question_vec", "question_id", "questions", _question_text),
    VecTable("weekly_review_vec", "review_id", "weekly_reviews", lambda s, r: s._review(r).text or ""),
)

_NONCE = "hex(substr(v.vec_ciphertext, 1, 12))"   # the old vector's AES-GCM nonce: new on every write


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _has_table(store: Any, name: str) -> bool:
    return bool(store._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))


def read_state(store: Any) -> dict[str, str]:
    """``embed_state`` as a dict (empty before migration 20)."""
    try:
        return {r[0]: r[1] for r in store._query("SELECT key, value FROM embed_state")}
    except sqlite3.Error:
        return {}


def embed_generation(store: Any) -> str:
    """Bumped at every flip: an in-memory copy of vectors made before it is stale (``"0"`` if never)."""
    return read_state(store).get("generation", "0")


def active_model(store: Any) -> str:
    """The model the database's vectors are made with (the marker, else what its vectors carry)."""
    from yuki.memory import embed

    marker = read_state(store).get("active_model")
    if marker:
        return marker
    for vt in VEC_TABLES:
        try:
            rows = store._query(f"SELECT model FROM {vt.table} WHERE model != '' LIMIT 1")
        except sqlite3.Error:
            continue
        if rows:
            return str(rows[0][0])
    return embed.DEFAULT_MODEL


def target_model(store: Any) -> str:
    """The model the database is being moved to (default :data:`yuki.memory.embed.DEFAULT_MODEL`)."""
    from yuki.memory import embed

    return read_state(store).get("target_model") or embed.DEFAULT_MODEL


def _set_state(conn: sqlite3.Connection, **values: Any) -> None:
    now = time.time()
    for key, value in values.items():
        conn.execute("INSERT OR REPLACE INTO embed_state(key, value, updated_at) VALUES (?,?,?)",
                     (key, str(value), now))


def set_target(store: Any, model: str) -> None:
    """Record the model to move to (and pin the current one, so it is never inferred again)."""
    from yuki.memory import embed

    embed.model_spec(model)   # raises for an unknown model
    active = active_model(store)
    store._write(lambda conn: _set_state(conn, active_model=active, target_model=model))


# ---------------------------------------------------------------------------
# The work
# ---------------------------------------------------------------------------


def _pending_sql(vt: VecTable, *, side: bool, count: bool) -> str:
    """Rows of ``vt`` still to do: not on the model yet, and (moving) without a fresh side vector."""
    head = "SELECT count(*)" if count else f"SELECT v.{vt.id_col} AS item_id, {_NONCE} AS nonce"
    sql = f"{head} FROM {vt.table} v JOIN {vt.source} s ON s.id = v.{vt.id_col}"
    if side:
        sql += (f" LEFT JOIN vec_next n ON n.tbl = '{vt.table}' AND n.item_id = v.{vt.id_col}"
                " WHERE v.model != ? AND (n.item_id IS NULL OR n.model != ? OR n.src_digest != "
                f"{_NONCE})")
    else:
        sql += " WHERE v.model != ?"
    return sql if count else sql + f" ORDER BY v.{vt.id_col}"


def counts(store: Any, model: str | None = None) -> dict[str, dict[str, int]]:
    """Per vector table: rows, rows still to move to ``model`` (default: the target) and rows on it."""
    model = model or target_model(store)
    side = _has_table(store, "vec_next") and model != active_model(store)
    out: dict[str, dict[str, int]] = {}
    for vt in VEC_TABLES:
        if not _has_table(store, vt.table):
            continue
        rows = store._query(f"SELECT count(*) FROM {vt.table} v JOIN {vt.source} s ON s.id = v.{vt.id_col}")[0][0]
        on = store._query(f"SELECT count(*) FROM {vt.table} v JOIN {vt.source} s ON s.id = v.{vt.id_col}"
                          " WHERE v.model = ?", (model,))[0][0]
        params = (model, model) if side else (model,)
        todo = store._query(_pending_sql(vt, side=side, count=True), params)[0][0]
        done_side = 0
        if side:
            done_side = store._query("SELECT count(*) FROM vec_next WHERE tbl=? AND model=?", (vt.table, model))[0][0]
        out[vt.table] = {"rows": int(rows), "on_model": int(on), "to_do": int(todo), "side": int(done_side)}
    return out


def _texts(store: Any, vt: VecTable, ids: list[int]) -> tuple[list[str], int]:
    """The texts of these rows (in order) and how many could not be read (embedded as "")."""
    rows = {r["id"]: r for r in store._query(
        f"SELECT * FROM {vt.source} WHERE id IN ({','.join('?' * len(ids))})", ids)}
    texts, unreadable = [], 0
    for i in ids:
        try:
            texts.append(vt.text(store, rows[i]) or "")
        except Exception:  # noqa: BLE001 - an unreadable row still gets a vector (of nothing)
            texts.append("")
            unreadable += 1
    return texts, unreadable


class Reembedder:
    """One database's move to a model: :meth:`step` does one batch (or the flip, or a sweep batch)."""

    def __init__(self, store: Any, *, target: str | None = None, batch: int = BATCH,
                 embedder: Any = None, sweeper: Any = None, log: Callable[..., None] | None = None) -> None:
        self.store = store
        self.batch = max(1, int(batch))
        self._target = target
        self._embedder = embedder      # the target model (moving); default: a pinned one-thread Embedder
        self._sweeper = sweeper        # the active model (sweeping); default: the process's embedder
        self._log = log
        self.stats = {"embedded": 0, "swept": 0, "unreadable": 0, "flips": 0, "batches": 0}

    # -- models ----------------------------------------------------------------

    def target(self) -> str:
        return self._target or target_model(self.store)

    def target_embedder(self, model: str) -> Any:
        from yuki.memory.embed import Embedder

        if self._embedder is None or getattr(self._embedder, "model_name", None) != model:
            if self._embedder is not None and hasattr(self._embedder, "unload"):
                self._embedder.unload()
            self._embedder = Embedder(model, threads=1, idle_unload_s=120.0)
        return self._embedder

    def release(self) -> None:
        """Drop the target model's copy (after the flip the process's own embedder serves it)."""
        if self._embedder is not None and hasattr(self._embedder, "unload"):
            try:
                self._embedder.unload()
            except Exception:  # noqa: BLE001
                pass

    def _event(self, kind: str, **fields: Any) -> None:
        if self._log is not None:
            try:
                self._log(kind, **fields)
            except Exception:  # noqa: BLE001
                pass

    # -- one step ------------------------------------------------------------------

    def step(self) -> dict[str, Any]:
        """One batch. Returns ``{"phase": "move"|"flip"|"sweep"|"idle", "done": n, "left": m}``."""
        store = self.store
        active, target = active_model(store), self.target()
        state = read_state(store)
        if state.get("active_model") != active or state.get("target_model") != target:
            store._write(lambda conn: _set_state(conn, active_model=active, target_model=target))
        if target != active:
            for vt in VEC_TABLES:
                rows = store._query(_pending_sql(vt, side=True, count=False) + " LIMIT ?",
                                    (target, target, self.batch))
                if rows:
                    return self._move(vt, rows, target)
            return self._flip(active, target)
        for vt in VEC_TABLES:
            rows = store._query(_pending_sql(vt, side=False, count=False) + " LIMIT ?", (active, self.batch))
            if rows:
                return self._sweep(vt, rows, active)
        return {"phase": "idle", "done": 0, "left": 0, "model": active}

    def _move(self, vt: VecTable, rows: list[sqlite3.Row], target: str) -> dict[str, Any]:
        ids = [int(r["item_id"]) for r in rows]
        texts, unreadable = _texts(self.store, vt, ids)
        vecs = np.asarray(self.target_embedder(target).embed(texts), dtype=np.float32)
        cipher = self.store.cipher
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            for r, vec in zip(rows, vecs):
                vec = vec.ravel()
                conn.execute(
                    "INSERT OR REPLACE INTO vec_next(tbl, item_id, model, dim, vec_ciphertext, src_digest, created_at)"
                    " VALUES (?,?,?,?,?,?,?)",
                    (vt.table, int(r["item_id"]), target, int(vec.shape[0]), cipher.encrypt_bytes(vec.tobytes()),
                     r["nonce"], now))
            return len(rows)

        self.store._write(op)
        self.stats["embedded"] += len(rows)
        self.stats["unreadable"] += unreadable
        self.stats["batches"] += 1
        return {"phase": "move", "table": vt.table, "done": len(rows), "unreadable": unreadable, "model": target}

    def _flip(self, active: str, target: str) -> dict[str, Any]:
        """Copy every side vector over its old one and switch the active model, in one transaction."""
        store = self.store

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            for vt in VEC_TABLES:   # re-checked under the write lock: a row written since the last batch waits
                left = conn.execute(_pending_sql(vt, side=True, count=True), (target, target)).fetchone()[0]
                if left:
                    return {"phase": "move", "done": 0, "left": int(left), "table": vt.table, "model": target}
            copied = 0
            for vt in VEC_TABLES:
                cur = conn.execute(
                    f"UPDATE {vt.table} AS v SET model = n.model, dim = n.dim, vec_ciphertext = n.vec_ciphertext"
                    f" FROM vec_next n WHERE n.tbl = ? AND n.item_id = v.{vt.id_col} AND n.model = ?"
                    f" AND n.src_digest = {_NONCE}", (vt.table, target))
                copied += cur.rowcount
            conn.execute("DELETE FROM vec_next")
            generation = int((conn.execute("SELECT value FROM embed_state WHERE key='generation'").fetchone()
                              or ["0"])[0]) + 1
            _set_state(conn, active_model=target, target_model=target, previous_model=active,
                       generation=generation, flipped_at=time.time())
            return {"phase": "flip", "done": copied, "left": 0, "model": target, "previous": active,
                    "generation": generation}

        result = store._write(op)
        if result["phase"] == "flip":
            from yuki.memory import embed

            embed.invalidate_active_model()
            self.release()
            self.stats["flips"] += 1
            self._event("reembed_flip", model=target, previous=active, rows=result["done"])
        return result

    def _sweep(self, vt: VecTable, rows: list[sqlite3.Row], active: str) -> dict[str, Any]:
        """Re-embed in place rows still on another model (only where the vector was not rewritten meanwhile)."""
        from yuki.memory.embed import get_embedder

        embedder = self._sweeper or get_embedder()
        if getattr(embedder, "model_name", None) != active:
            from yuki.memory import embed

            embed.invalidate_active_model()
        ids = [int(r["item_id"]) for r in rows]
        texts, unreadable = _texts(self.store, vt, ids)
        vecs = np.asarray(embedder.embed(texts), dtype=np.float32)
        model = getattr(embedder, "model_name", None)
        if model != active:   # this process does not see the flip yet: try again later
            return {"phase": "sweep", "done": 0, "left": len(rows), "model": active, "waiting": True}
        cipher = self.store.cipher

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for r, vec in zip(rows, vecs):
                vec = vec.ravel()
                n += conn.execute(
                    f"UPDATE {vt.table} AS v SET model=?, dim=?, vec_ciphertext=? WHERE v.{vt.id_col}=?"
                    f" AND {_NONCE} = ?",
                    (active, int(vec.shape[0]), cipher.encrypt_bytes(vec.tobytes()), int(r["item_id"]), r["nonce"]),
                ).rowcount
            return n

        n = self.store._write(op)
        self.stats["swept"] += n
        self.stats["unreadable"] += unreadable
        return {"phase": "sweep", "table": vt.table, "done": n, "model": active}

    def run_to_end(self, *, pause_s: float = 0.0, on_step: Callable[[dict[str, Any]], None] | None = None,
                   stop: threading.Event | None = None) -> dict[str, Any]:
        """Step until there is nothing left (moved, flipped and swept) or ``stop`` is set."""
        last: dict[str, Any] = {}
        while stop is None or not stop.is_set():
            last = self.step()
            if on_step is not None:
                on_step(last)
            if last["phase"] == "idle" or last.get("waiting"):
                break
            if pause_s:
                (stop.wait(pause_s) if stop is not None else time.sleep(pause_s))
        return last


# ---------------------------------------------------------------------------
# The service's background thread
# ---------------------------------------------------------------------------


def _lower_thread_priority() -> None:
    try:
        import win32api
        import win32con

        win32api.SetThreadPriority(win32api.GetCurrentThread(), win32con.THREAD_PRIORITY_LOWEST)
    except Exception:  # noqa: BLE001 - not on Windows / no pywin32: the process priority still applies
        pass


class ReembedWorker:
    """Runs :class:`Reembedder` in the memory service: small batches, lowest priority, yields to live work.

    Args:
        store: The service's store.
        busy: ``busy() -> bool``: wait while it is true (a full-screen game, a call), or ``None``.
        log: ``log(type, **fields)`` (content-free).
    """

    def __init__(self, store: Any, *, busy: Callable[[], bool] | None = None, log: Callable[..., None] | None = None,
                 batch: int = BATCH, pause_s: float = PAUSE_S, idle_s: float = IDLE_S) -> None:
        self.reembedder = Reembedder(store, batch=batch, log=log)
        self.busy = busy
        self.log = log
        self.pause_s = pause_s
        self.idle_s = idle_s
        self.last: dict[str, Any] = {}

    def _busy(self) -> bool:
        try:
            from yuki.memory.embed import get_embedder

            if get_embedder().busy():   # the journal / conversations are embedding: they go first
                return True
        except Exception:  # noqa: BLE001
            pass
        if self.busy is not None:
            try:
                return bool(self.busy())
            except Exception:  # noqa: BLE001
                return False
        return False

    def run(self, stop: threading.Event) -> None:
        _lower_thread_priority()
        started = time.monotonic()
        announced = False
        while not stop.is_set():
            if self._busy():
                stop.wait(BUSY_WAIT_S)
                continue
            try:
                self.last = self.reembedder.step()
            except Exception as exc:  # noqa: BLE001 - try again later; never takes the service down
                if self.log is not None:
                    self.log("error", where="reembed", error=f"{type(exc).__name__}: {exc}")
                stop.wait(self.idle_s)
                continue
            phase = self.last["phase"]
            if phase in ("move", "flip") and not announced:
                announced = True
                if self.log is not None:
                    self.log("reembed_start", model=self.last.get("model"))
            if phase == "idle" or self.last.get("waiting"):
                if announced and self.log is not None:
                    self.log("reembed_done", model=self.last.get("model"), **self.reembedder.stats,
                             s=round(time.monotonic() - started))
                    announced = False
                self.reembedder.release()
                stop.wait(self.idle_s)
                continue
            stop.wait(self.pause_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open(db: Path | None, *, read_only: bool) -> Any:
    from yuki.memory.crypto import FieldCipher, load_or_create_key
    from yuki.memory.store import KEY_FILENAME, Store, default_db_path

    path = Path(db) if db else default_db_path()
    if not path.exists():
        raise SystemExit(f"no database at {path}")
    if not read_only:
        return Store.open(path)
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return Store(path, conn, FieldCipher(load_or_create_key(path.parent / KEY_FILENAME)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.reembed",
                                     description="Move memory's vectors to another embedding model (resumable).")
    parser.add_argument("--db", type=Path, default=None, help="database (default: memory's own)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be re-embedded; write nothing")
    parser.add_argument("--status", action="store_true", help="show the state and counts; write nothing")
    parser.add_argument("--batch", type=int, default=64, help="texts per batch / transaction (default 64)")
    parser.add_argument("--model", default=None, help="the model to move to (default: the target, else the default)")
    parser.add_argument("--pause", type=float, default=0.0, help="seconds between batches (default 0)")
    args = parser.parse_args(argv)
    from yuki.memory import embed

    if args.model:
        embed.model_spec(args.model)
    read_only = args.dry_run or args.status
    store = _open(args.db, read_only=read_only)
    try:
        embed.use_database(store.path)
        active = active_model(store)
        target = args.model or target_model(store)
        print(f"database {store.path}")
        print(f"active model {active}")
        print(f"target model {target}" + ("  (nothing to move)" if target == active else ""))
        for table, c in counts(store, target).items():
            print(f"  {table:24} rows {c['rows']:6}  on target {c['on_model']:6}  side {c['side']:6}  to do {c['to_do']:6}")
        if read_only:
            return 0
        if args.model:
            set_target(store, args.model)
        started = time.monotonic()
        mover = Reembedder(store, target=target, batch=args.batch)

        def show(step: dict[str, Any]) -> None:
            if step["phase"] in ("move", "sweep") and step.get("done"):
                print(f"  {step['phase']:5} {step.get('table', ''):24} +{step['done']}", flush=True)
            elif step["phase"] == "flip":
                print(f"  flip: {step['done']} vectors now {step['model']} (generation {step['generation']})", flush=True)

        last = mover.run_to_end(pause_s=args.pause, on_step=show)
        mover.release()
        print(f"done in {time.monotonic() - started:.1f} s: {mover.stats}; state {last.get('phase')}")
        return 0
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
