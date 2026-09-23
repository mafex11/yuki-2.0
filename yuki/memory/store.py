"""Yuki's memory store: one encrypted SQLite database (WAL).

Location: ``%LOCALAPPDATA%\\Yuki\\memory\\memory.db`` unless a path is given.
The DPAPI-wrapped data key lives next to it as ``memory.key`` (see
:mod:`yuki.memory.crypto`). Content columns (window titles, full URLs, captured
text, facts, embeddings) are AES-256-GCM encrypted; metadata used for filtering
(timestamps, app, URL host, importance, counts) is cleartext. Equality checks
(thread lookup, dedup) use keyed HMAC digests, never plaintext or plain hashes.

Timestamps are Unix epoch seconds (``time.time()``). Every ``since``/``until``
argument also accepts a :class:`datetime.datetime` (naive = local time);
``since`` is inclusive, ``until`` exclusive.

Thread safety: one connection guarded by a re-entrant lock, so the watcher
thread, the journal worker thread and readers may share one :class:`Store`.
Other processes (Yuki itself) open their own :class:`Store` on the same file;
WAL plus a busy timeout makes that safe.

Stable public API
=================

Opening::

    Store.open(path: str | Path | None = None) -> Store     # creates dirs, key, schema
    store.close();  with Store.open() as store: ...
    store.path -> Path;  store.cipher -> FieldCipher

Watcher (capture) side::

    store.upsert_thread(app: str, title: str, url: str | None) -> int
        # thread_id. A thread is (app, url) when a url is given, else (app, title).
        # Updates the stored title/url and last_seen on every call.
    store.latest_text(thread_id: int) -> str | None
        # decrypted last full text seen for the thread (for computing the next delta)
    store.add_capture(thread_id: int, at: float, trigger: str,
                      full_text: str, delta_text: str) -> int | None
        # capture_id, or None when nothing new was stored: full_text identical to
        # the latest text, delta empty/whitespace, or this exact delta already
        # stored for the thread. Whenever full_text differs, it becomes the
        # thread's latest text (even if no capture row is written).
    store.add_health(at: float, app: str, trigger: str, outcome: str,
                     reason: str | None, chars: int, ms: float) -> None
        # content-free capture health; never pass captured text in `reason`.

Journal (worker and Yuki tools)::

    store.add_journal(at, thread_id, app, fact, importance, *, host=None,
                      batch_id=None, vector=None) -> int
    store.search_journal(query_vec, since=None, until=None, app=None, limit=10,
                         *, host=None) -> list[JournalEntry]     # .score = cosine
    store.keyword_journal(text, since=None, until=None, app=None, limit=50,
                          *, host=None) -> list[JournalEntry]    # every term, casefolded
    store.journal_between(since=None, until=None) -> list[JournalEntry]   # oldest first
    store.journal_for_thread(thread_id, limit=10) -> list[JournalEntry]   # newest N, oldest first
    store.journal_without_vectors(limit=256) -> list[JournalEntry]
    store.add_vectors(items: list[(journal_id, vector)], model: str) -> int

Journal worker plumbing::

    store.pending_captures(limit=None, max_attempts=3) -> list[PendingCapture]
    store.pending_count(max_attempts=3) -> int
    store.wait_for_captures(min_pending: int, timeout_s: float) -> int
    store.wake() -> None                          # releases wait_for_captures
    store.thread_info(thread_id) -> ThreadInfo | None
    store.commit_batch(call: ModelCall, capture_ids, facts: list[NewFact]) -> int
        # one transaction: batch row + facts + vectors + captures marked journaled
    store.record_failed_batch(call: ModelCall, capture_ids) -> int
        # batch row with the error; bumps the captures' attempt counters

Maintenance and reporting::

    store.prune(older_than_days: float = 30) -> int     # captures deleted
    store.health_summary(since=None, until=None) -> dict
    store.batch_summary(since=None, until=None) -> dict
    store.db_size_bytes() -> int
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

import numpy as np

from yuki.memory.crypto import FieldCipher, load_or_create_key

KEY_FILENAME = "memory.key"
DB_FILENAME = "memory.db"

TimeArg = float | int | datetime | None


def default_memory_dir() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\memory``."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory"


def default_db_path() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\memory\\memory.db``."""
    return default_memory_dir() / DB_FILENAME


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass
class ThreadInfo:
    id: int
    app: str
    host: str | None
    title: str
    url: str | None
    first_seen: float
    last_seen: float


@dataclass
class PendingCapture:
    """A capture the journal worker has not processed yet (delta decrypted)."""

    id: int
    thread_id: int
    at: float
    trigger: str
    delta: str
    chars: int
    attempts: int


@dataclass
class JournalEntry:
    id: int
    at: float
    thread_id: int | None
    app: str
    host: str | None
    fact: str
    importance: int
    batch_id: int | None
    score: float | None = None      # cosine similarity, set by search_journal


@dataclass
class NewFact:
    """One fact to write in :meth:`Store.commit_batch`."""

    at: float
    thread_id: int | None
    app: str
    host: str | None
    fact: str
    importance: int
    vector: np.ndarray | None = None
    embed_model: str | None = None


@dataclass
class ModelCall:
    """Content-free accounting for one journal model call."""

    at: float
    thread_id: int | None
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    input_chars: int = 0
    stop_reason: str | None = None
    outcome: str = "ok"             # ok | error | skipped
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

#: Ordered migrations; ``PRAGMA user_version`` records how many have run.
MIGRATIONS: tuple[str, ...] = (
    # 1: Phase A tables plus the Phase B/C shapes from docs/MEMORY.md.
    """
    CREATE TABLE threads (
        id              INTEGER PRIMARY KEY,
        thread_key      TEXT NOT NULL UNIQUE,     -- HMAC(app, url|title)
        app             TEXT NOT NULL,
        host            TEXT,                     -- URL host, cleartext for filtering
        title_ciphertext TEXT NOT NULL,
        url_ciphertext  TEXT,
        first_seen      REAL NOT NULL,
        last_seen       REAL NOT NULL
    );
    CREATE INDEX threads_last_seen ON threads(last_seen);

    CREATE TABLE thread_latest (
        thread_id       INTEGER PRIMARY KEY REFERENCES threads(id),
        at              REAL NOT NULL,
        text_ciphertext TEXT NOT NULL,
        hash            TEXT NOT NULL             -- HMAC(full text)
    );

    CREATE TABLE captures (
        id               INTEGER PRIMARY KEY,
        thread_id        INTEGER NOT NULL REFERENCES threads(id),
        at               REAL NOT NULL,
        trigger          TEXT NOT NULL,
        delta_ciphertext TEXT NOT NULL,
        chars            INTEGER NOT NULL,
        hash             TEXT NOT NULL,           -- HMAC(delta)
        journal_batch_id INTEGER,                 -- set once journaled (checkpoint)
        journal_attempts INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX captures_thread_hash ON captures(thread_id, hash);
    CREATE INDEX captures_pending ON captures(journal_batch_id, at);
    CREATE INDEX captures_at ON captures(at);

    CREATE TABLE journal_batches (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,
        thread_id          INTEGER,
        model              TEXT NOT NULL,
        capture_count      INTEGER NOT NULL,
        input_chars        INTEGER NOT NULL,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL,
        latency_ms         REAL NOT NULL DEFAULT 0,
        stop_reason        TEXT,
        facts              INTEGER NOT NULL DEFAULT 0,
        outcome            TEXT NOT NULL,
        error              TEXT
    );
    CREATE INDEX journal_batches_at ON journal_batches(at);

    CREATE TABLE journal (
        id              INTEGER PRIMARY KEY,
        at              REAL NOT NULL,
        thread_id       INTEGER REFERENCES threads(id),
        app             TEXT NOT NULL,
        host            TEXT,
        fact_ciphertext TEXT NOT NULL,
        importance      INTEGER NOT NULL,
        batch_id        INTEGER REFERENCES journal_batches(id),
        created_at      REAL NOT NULL
    );
    CREATE INDEX journal_at ON journal(at);
    CREATE INDEX journal_app_at ON journal(app, at);

    CREATE TABLE journal_vec (
        journal_id      INTEGER PRIMARY KEY REFERENCES journal(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL            -- encrypted float32[dim], L2-normalised
    );

    CREATE TABLE portrait_facts (
        id                 INTEGER PRIMARY KEY,
        kind               TEXT NOT NULL,
        subject_ciphertext TEXT,
        text_ciphertext    TEXT NOT NULL,
        valid_from         REAL NOT NULL,
        valid_to           REAL,
        source_ids         TEXT,                 -- JSON list of journal ids
        confidence         REAL
    );
    CREATE INDEX portrait_facts_valid ON portrait_facts(valid_to, kind);

    CREATE TABLE knowhow (
        id                        INTEGER PRIMARY KEY,
        app                       TEXT,
        task_kind                 TEXT,
        text_ciphertext           TEXT NOT NULL,
        valid_from                REAL NOT NULL,
        valid_to                  REAL,
        source_request_ciphertext TEXT
    );
    CREATE INDEX knowhow_app ON knowhow(app, valid_to);

    CREATE TABLE open_loops (
        id                INTEGER PRIMARY KEY,
        person_ciphertext TEXT,
        text_ciphertext   TEXT NOT NULL,
        status            TEXT NOT NULL,
        opened_at         REAL NOT NULL,
        resolved_at       REAL
    );
    CREATE INDEX open_loops_status ON open_loops(status, opened_at);

    CREATE TABLE health (
        id       INTEGER PRIMARY KEY,
        at       REAL NOT NULL,
        app      TEXT,
        trigger  TEXT,
        outcome  TEXT NOT NULL,
        reason   TEXT,
        chars    INTEGER NOT NULL DEFAULT 0,
        ms       REAL NOT NULL DEFAULT 0
    );
    CREATE INDEX health_at ON health(at);
    """,
)


def _ts(value: TimeArg) -> float | None:
    """Epoch seconds from a float/int/datetime (naive datetime = local time)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def url_host(url: str | None) -> str | None:
    """Lower-cased host of ``url``, or ``None``."""
    if not url:
        return None
    try:
        host = urlsplit(url if "//" in url else "//" + url).hostname
    except ValueError:
        return None
    return host or None


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class Store:
    """The memory database. Construct with :meth:`Store.open`."""

    def __init__(self, path: Path, conn: sqlite3.Connection, cipher: FieldCipher) -> None:
        self.path = path
        self.cipher = cipher
        self._conn = conn
        self._lock = threading.RLock()
        self._cond = threading.Condition(threading.Lock())
        self._wake_seq = 0

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    def open(cls, path: str | Path | None = None) -> Store:
        """Open (creating if needed) the store at ``path`` (default location if ``None``)."""
        db_path = Path(path) if path is not None else default_db_path()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        cipher = FieldCipher(load_or_create_key(db_path.parent / KEY_FILENAME))
        conn = sqlite3.connect(str(db_path), timeout=10.0, isolation_level=None, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=10000")
        store = cls(db_path, conn, cipher)
        store._migrate()
        return store

    def close(self) -> None:
        """Close the connection and release any waiter."""
        self.wake()
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for index in range(version, len(MIGRATIONS)):
                self._conn.execute("BEGIN IMMEDIATE")
                try:
                    # re-check inside the write lock: another process may have migrated
                    current = self._conn.execute("PRAGMA user_version").fetchone()[0]
                    if current > index:
                        self._conn.execute("COMMIT")
                        continue
                    for statement in _split_sql(MIGRATIONS[index]):
                        self._conn.execute(statement)
                    self._conn.execute(f"PRAGMA user_version={index + 1}")
                    self._conn.execute("COMMIT")
                except BaseException:
                    self._conn.execute("ROLLBACK")
                    raise

    # -- small helpers -----------------------------------------------------

    def _write(self, fn, *args, **kwargs):
        """Run ``fn(conn, ...)`` inside one IMMEDIATE transaction."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                result = fn(self._conn, *args, **kwargs)
                self._conn.execute("COMMIT")
                return result
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _enc(self, text: str | None) -> str | None:
        return None if text is None else self.cipher.encrypt(text)

    def _dec(self, stored: str | None) -> str | None:
        return None if stored is None else self.cipher.decrypt(stored)

    # -- threads -----------------------------------------------------------

    def upsert_thread(self, app: str, title: str, url: str | None) -> int:
        """Find or create the thread for this window/page and return its id.

        The identity is ``(app, url)`` when ``url`` is non-empty, else
        ``(app, title)``: a page keeps its thread while its title changes
        (notification counters, "(3) Inbox"), and an app without a URL gets one
        thread per window title. The latest title/url and ``last_seen`` are
        updated on every call.
        """
        app = app or ""
        title = title or ""
        url = url or None
        key = self.cipher.digest("url" if url else "title", app, url or title)
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT id FROM threads WHERE thread_key=?", (key,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE threads SET title_ciphertext=?, url_ciphertext=?, last_seen=? WHERE id=?",
                    (self._enc(title), self._enc(url), now, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO threads(thread_key, app, host, title_ciphertext, url_ciphertext, first_seen, last_seen)"
                " VALUES (?,?,?,?,?,?,?)",
                (key, app, url_host(url), self._enc(title), self._enc(url), now, now),
            )
            return int(cur.lastrowid)

        return self._write(op)

    def thread_info(self, thread_id: int) -> ThreadInfo | None:
        """Decrypted thread metadata, or ``None``."""
        rows = self._query("SELECT * FROM threads WHERE id=?", (thread_id,))
        if not rows:
            return None
        r = rows[0]
        return ThreadInfo(
            id=r["id"], app=r["app"], host=r["host"], title=self._dec(r["title_ciphertext"]) or "",
            url=self._dec(r["url_ciphertext"]), first_seen=r["first_seen"], last_seen=r["last_seen"],
        )

    # -- captures ----------------------------------------------------------

    def latest_text(self, thread_id: int) -> str | None:
        """The last full text stored for ``thread_id`` (decrypted), or ``None``."""
        rows = self._query("SELECT text_ciphertext FROM thread_latest WHERE thread_id=?", (thread_id,))
        return self._dec(rows[0]["text_ciphertext"]) if rows else None

    def add_capture(
        self, thread_id: int, at: float, trigger: str, full_text: str, delta_text: str
    ) -> int | None:
        """Store one capture's delta; see the module docstring for the dedup rules."""
        full_text = full_text or ""
        delta_text = delta_text or ""
        full_hash = self.cipher.digest(full_text)
        delta_hash = self.cipher.digest(delta_text)
        at = float(at)

        def op(conn: sqlite3.Connection) -> int | None:
            latest = conn.execute("SELECT hash FROM thread_latest WHERE thread_id=?", (thread_id,)).fetchone()
            if latest is not None and latest["hash"] == full_hash:
                conn.execute("UPDATE threads SET last_seen=max(last_seen, ?) WHERE id=?", (at, thread_id))
                return None
            conn.execute(
                "INSERT INTO thread_latest(thread_id, at, text_ciphertext, hash) VALUES (?,?,?,?)"
                " ON CONFLICT(thread_id) DO UPDATE SET at=excluded.at,"
                " text_ciphertext=excluded.text_ciphertext, hash=excluded.hash",
                (thread_id, at, self.cipher.encrypt(full_text), full_hash),
            )
            conn.execute("UPDATE threads SET last_seen=max(last_seen, ?) WHERE id=?", (at, thread_id))
            if not delta_text.strip():
                return None
            dup = conn.execute(
                "SELECT 1 FROM captures WHERE thread_id=? AND hash=? LIMIT 1", (thread_id, delta_hash)
            ).fetchone()
            if dup:
                return None
            cur = conn.execute(
                "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash)"
                " VALUES (?,?,?,?,?,?)",
                (thread_id, at, trigger or "", self.cipher.encrypt(delta_text), len(delta_text), delta_hash),
            )
            return int(cur.lastrowid)

        capture_id = self._write(op)
        if capture_id is not None:
            with self._cond:
                self._cond.notify_all()
        return capture_id

    def add_health(
        self, at: float, app: str, trigger: str, outcome: str, reason: str | None, chars: int, ms: float
    ) -> None:
        """Record one content-free capture-health row."""
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO health(at, app, trigger, outcome, reason, chars, ms) VALUES (?,?,?,?,?,?,?)",
                (float(at), app, trigger, outcome, reason, int(chars or 0), float(ms or 0.0)),
            )
        )

    # -- journal worker plumbing ------------------------------------------

    def pending_count(self, max_attempts: int = 3) -> int:
        """Captures not yet journaled (and not given up on)."""
        rows = self._query(
            "SELECT count(*) FROM captures WHERE journal_batch_id IS NULL AND journal_attempts < ?",
            (max_attempts,),
        )
        return int(rows[0][0])

    def pending_captures(self, limit: int | None = None, max_attempts: int = 3) -> list[PendingCapture]:
        """Unjournaled captures, oldest first, deltas decrypted."""
        sql = (
            "SELECT id, thread_id, at, trigger, delta_ciphertext, chars, journal_attempts FROM captures"
            " WHERE journal_batch_id IS NULL AND journal_attempts < ? ORDER BY at, id"
        )
        params: list[Any] = [max_attempts]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [
            PendingCapture(
                id=r["id"], thread_id=r["thread_id"], at=r["at"], trigger=r["trigger"],
                delta=self.cipher.decrypt(r["delta_ciphertext"]), chars=r["chars"],
                attempts=r["journal_attempts"],
            )
            for r in self._query(sql, params)
        ]

    def wait_for_captures(self, min_pending: int, timeout_s: float) -> int:
        """Block until ``min_pending`` captures are pending, :meth:`wake` is called, or timeout.

        Returns the pending count at return time. Condition-based: woken by
        :meth:`add_capture` in this process; captures written by another process
        are seen at the timeout.
        """
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._cond:
            seq = self._wake_seq
            while True:
                count = self.pending_count()
                remaining = deadline - time.monotonic()
                if count >= min_pending or remaining <= 0 or self._wake_seq != seq:
                    return count
                self._cond.wait(remaining)

    def wake(self) -> None:
        """Release every :meth:`wait_for_captures` call now."""
        with self._cond:
            self._wake_seq += 1
            self._cond.notify_all()

    def _insert_batch(self, conn: sqlite3.Connection, call: ModelCall, capture_count: int, facts: int) -> int:
        cur = conn.execute(
            "INSERT INTO journal_batches(at, thread_id, model, capture_count, input_chars, input_tokens,"
            " output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, latency_ms, stop_reason,"
            " facts, outcome, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                call.at, call.thread_id, call.model, capture_count, call.input_chars, call.input_tokens,
                call.output_tokens, call.cache_write_tokens, call.cache_read_tokens, call.cost_usd,
                call.latency_ms, call.stop_reason, facts, call.outcome, call.error,
            ),
        )
        return int(cur.lastrowid)

    def _insert_journal(self, conn: sqlite3.Connection, fact: NewFact, batch_id: int | None) -> int:
        cur = conn.execute(
            "INSERT INTO journal(at, thread_id, app, host, fact_ciphertext, importance, batch_id, created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (
                float(fact.at), fact.thread_id, fact.app or "", fact.host, self.cipher.encrypt(fact.fact),
                int(fact.importance), batch_id, time.time(),
            ),
        )
        journal_id = int(cur.lastrowid)
        if fact.vector is not None:
            vec = np.asarray(fact.vector, dtype=np.float32).ravel()
            conn.execute(
                "INSERT INTO journal_vec(journal_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                (journal_id, fact.embed_model or "", int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
            )
        return journal_id

    def commit_batch(self, call: ModelCall, capture_ids: Iterable[int], facts: Sequence[NewFact]) -> int:
        """Atomically record a batch, its facts (+vectors) and mark its captures journaled."""
        ids = list(capture_ids)

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_batch(conn, call, len(ids), len(facts))
            for fact in facts:
                self._insert_journal(conn, fact, batch_id)
            conn.executemany("UPDATE captures SET journal_batch_id=? WHERE id=?", [(batch_id, i) for i in ids])
            return batch_id

        return self._write(op)

    def record_failed_batch(self, call: ModelCall, capture_ids: Iterable[int]) -> int:
        """Record a failed call and bump its captures' attempt counters (they stay pending)."""
        ids = list(capture_ids)

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_batch(conn, call, len(ids), 0)
            conn.executemany(
                "UPDATE captures SET journal_attempts=journal_attempts+1 WHERE id=?", [(i,) for i in ids]
            )
            return batch_id

        return self._write(op)

    # -- journal -----------------------------------------------------------

    def add_journal(
        self,
        at: float,
        thread_id: int | None,
        app: str,
        fact: str,
        importance: int,
        *,
        host: str | None = None,
        batch_id: int | None = None,
        vector: np.ndarray | None = None,
        embed_model: str | None = None,
    ) -> int:
        """Write one journal fact (optionally with its embedding) and return its id."""
        new = NewFact(at=at, thread_id=thread_id, app=app, host=host, fact=fact, importance=importance,
                      vector=vector, embed_model=embed_model)
        return self._write(lambda conn: self._insert_journal(conn, new, batch_id))

    def _journal_filter(
        self, since: TimeArg, until: TimeArg, app: str | None, host: str | None
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("j.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("j.at < ?")
            params.append(u)
        if app:
            clauses.append("lower(j.app) = lower(?)")
            params.append(app)
        if host:
            clauses.append("(j.host = lower(?) OR j.host LIKE '%.' || lower(?))")
            params.extend([host, host])
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def _entry(self, r: sqlite3.Row, score: float | None = None) -> JournalEntry:
        return JournalEntry(
            id=r["id"], at=r["at"], thread_id=r["thread_id"], app=r["app"], host=r["host"],
            fact=self.cipher.decrypt(r["fact_ciphertext"]), importance=r["importance"],
            batch_id=r["batch_id"], score=score,
        )

    def search_journal(
        self,
        query_vec: np.ndarray,
        since: TimeArg = None,
        until: TimeArg = None,
        app: str | None = None,
        limit: int = 10,
        *,
        host: str | None = None,
    ) -> list[JournalEntry]:
        """Journal facts most similar to ``query_vec`` (cosine), within the filters.

        Vectors in the window are decrypted and scored in memory with numpy;
        rows embedded with a different dimension are skipped.
        """
        where, params = self._journal_filter(since, until, app, host)
        rows = self._query(
            "SELECT j.*, v.vec_ciphertext, v.dim FROM journal j JOIN journal_vec v ON v.journal_id = j.id" + where,
            params,
        )
        if not rows:
            return []
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return []
        q = q / qn
        keep = [r for r in rows if r["dim"] == q.shape[0]]
        if not keep:
            return []
        matrix = np.stack(
            [np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32) for r in keep]
        )
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ q) / norms
        order = np.argsort(-scores)[: max(0, int(limit))]
        return [self._entry(keep[i], float(scores[i])) for i in order]

    def keyword_journal(
        self,
        text: str,
        since: TimeArg = None,
        until: TimeArg = None,
        app: str | None = None,
        limit: int = 50,
        *,
        host: str | None = None,
    ) -> list[JournalEntry]:
        """Facts in the window containing every whitespace-separated term of ``text``.

        Case-insensitive (casefold). Rows are decrypted in memory; there is no
        plaintext index. Newest first.
        """
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._journal_filter(since, until, app, host)
        hits: list[JournalEntry] = []
        for r in self._query("SELECT j.* FROM journal j" + where + " ORDER BY j.at DESC, j.id DESC", params):
            entry = self._entry(r)
            folded = entry.fact.casefold()
            if all(t in folded for t in terms):
                hits.append(entry)
                if len(hits) >= limit:
                    break
        return hits

    def journal_between(self, since: TimeArg = None, until: TimeArg = None) -> list[JournalEntry]:
        """Every fact in ``[since, until)``, oldest first."""
        where, params = self._journal_filter(since, until, None, None)
        return [self._entry(r) for r in self._query("SELECT j.* FROM journal j" + where + " ORDER BY j.at, j.id", params)]

    def journal_for_thread(self, thread_id: int, limit: int = 10) -> list[JournalEntry]:
        """The ``limit`` newest facts of one thread, returned oldest first."""
        rows = self._query(
            "SELECT j.* FROM journal j WHERE j.thread_id=? ORDER BY j.at DESC, j.id DESC LIMIT ?",
            (thread_id, int(limit)),
        )
        return [self._entry(r) for r in reversed(rows)]

    def journal_without_vectors(self, limit: int = 256) -> list[JournalEntry]:
        """Facts that have no embedding yet (e.g. the embedder failed when they were written)."""
        rows = self._query(
            "SELECT j.* FROM journal j LEFT JOIN journal_vec v ON v.journal_id = j.id"
            " WHERE v.journal_id IS NULL ORDER BY j.id LIMIT ?",
            (int(limit),),
        )
        return [self._entry(r) for r in rows]

    def add_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        """Store embeddings for existing journal ids (replacing any old one); returns count."""

        def op(conn: sqlite3.Connection) -> int:
            for journal_id, vector in items:
                vec = np.asarray(vector, dtype=np.float32).ravel()
                conn.execute(
                    "INSERT OR REPLACE INTO journal_vec(journal_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                    (int(journal_id), model, int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
                )
            return len(items)

        return self._write(op)

    # -- maintenance -------------------------------------------------------

    def prune(self, older_than_days: float = 30) -> int:
        """Delete captures (and stale latest texts) older than the TTL; returns captures deleted.

        Journal facts are never deleted. A capture past the TTL goes whether or
        not it was journaled: raw captures are a 30-day buffer, not an archive.
        """
        cutoff = time.time() - float(older_than_days) * 86400.0

        def op(conn: sqlite3.Connection) -> int:
            deleted = conn.execute("DELETE FROM captures WHERE at < ?", (cutoff,)).rowcount
            conn.execute(
                "DELETE FROM thread_latest WHERE thread_id IN (SELECT id FROM threads WHERE last_seen < ?)",
                (cutoff,),
            )
            return int(deleted)

        return self._write(op)

    def health_summary(self, since: TimeArg = None, until: TimeArg = None) -> dict[str, Any]:
        """Capture-health totals: overall, by app, by outcome."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("at < ?")
            params.append(u)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        total = self._query(
            "SELECT count(*) n, avg(ms) avg_ms, max(ms) max_ms, sum(chars) chars FROM health" + where, params
        )[0]
        by_app = self._query(
            "SELECT coalesce(app,'') app, count(*) n, avg(ms) avg_ms, sum(chars) chars FROM health"
            + where + " GROUP BY app ORDER BY n DESC",
            params,
        )
        by_outcome = self._query(
            "SELECT outcome, count(*) n FROM health" + where + " GROUP BY outcome ORDER BY n DESC", params
        )
        return {
            "count": total["n"], "avg_ms": total["avg_ms"], "max_ms": total["max_ms"], "chars": total["chars"] or 0,
            "by_app": [dict(r) for r in by_app],
            "by_outcome": {r["outcome"]: r["n"] for r in by_outcome},
        }

    def batch_summary(self, since: TimeArg = None, until: TimeArg = None) -> dict[str, Any]:
        """Journal model-call totals (tokens, cost, facts) for the window."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("at < ?")
            params.append(u)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        r = self._query(
            "SELECT count(*) calls, coalesce(sum(input_tokens),0) input_tokens,"
            " coalesce(sum(output_tokens),0) output_tokens, coalesce(sum(cache_write_tokens),0) cache_write_tokens,"
            " coalesce(sum(cache_read_tokens),0) cache_read_tokens, coalesce(sum(cost_usd),0) cost_usd,"
            " coalesce(sum(facts),0) facts, coalesce(sum(capture_count),0) captures,"
            " coalesce(avg(latency_ms),0) avg_latency_ms,"
            " sum(CASE WHEN outcome != 'ok' THEN 1 ELSE 0 END) failures FROM journal_batches" + where,
            params,
        )[0]
        out = dict(r)
        out["failures"] = out["failures"] or 0
        out["captures_pending"] = self.pending_count()
        return out

    def db_size_bytes(self) -> int:
        """Size of the database plus its WAL/SHM files."""
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (the scripts contain no ';' in literals)."""
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
