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

    store.upsert_thread(app: str, title: str, url: str | None, *, process: str | None = None,
                        scope: str | None = None, kind: str | None = None) -> int
        # thread_id. A thread is (app, scope) when a scope (Extraction.thread_scope) is
        # given, else (app, url) when a url is given, else (app, title); scope == url
        # keys exactly like url. Updates title/url/scope/kind/process and last_seen.
        # `process` is the image name ("chrome.exe"), stored as app_key() ("chrome").
    store.latest_text(thread_id: int) -> str | None
        # decrypted last full text seen for the thread (for computing the next delta)
    store.add_capture(thread_id: int, at: float, trigger: str,
                      full_text: str, delta_text: str, *, kind=None, profile=None) -> int | None
        # pages/documents/lists: capture_id, or None when nothing new was stored:
        # full_text identical to the latest text, delta empty/whitespace, or this
        # exact delta already stored for the thread. Whenever full_text differs,
        # it becomes the thread's latest text (even if no capture row is written).
    store.add_conversation(thread_id, at, trigger, messages, *, kind="conversation", profile=None,
                           first_visit_new_s=300, label_tolerance_s=90) -> ConversationWrite
        # chats/mail: stores messages whose fingerprint the thread has not seen,
        # each labelled new | history | reread; one capture row carries them.
    store.messages_for_captures(capture_ids) -> dict[capture_id, list[StoredMessage]]
    store.message_counts(thread_id) -> dict[status, int]
    store.add_me_names(app, names) -> int;  store.me_names() -> list[str]
        # names the screen showed as the user ("Sudhanshu (you)"), encrypted
    store.add_health(at: float, app: str, trigger: str, outcome: str,
                     reason: str | None, chars: int, ms: float, *, profile=None, kind=None,
                     dropped_chars=None, messages=None, new_messages=None, stats=None) -> None
        # content-free capture health; never pass captured text in any field.

Journal (worker and Yuki tools)::

    store.add_journal(at, thread_id, app, fact, importance, *, host=None,
                      batch_id=None, vector=None) -> int
    store.search_journal(query_vec, since=None, until=None, app=None, limit=10,
                         *, host=None, process=None) -> list[JournalEntry]     # .score = cosine
    store.keyword_journal(text, since=None, until=None, app=None, limit=50,
                          *, host=None, process=None) -> list[JournalEntry]    # every term, casefolded
        # app: one display name or several (any matches, case-insensitive);
        # process: an image name/key; with both, a fact matching either is kept.
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

Portrait (worker and Yuki)::

    store.journal_after_id(after_id, limit=None) -> list[JournalEntry]   # id > after_id, oldest first
    store.journal_count(since=None, until=None) -> int
    store.portrait_facts(kinds=None, *, include_corrections=True) -> list[PortraitFact]   # current (valid_to IS NULL)
    store.portrait_fact(fact_id) -> PortraitFact | None                  # any version
    store.add_correction(text, at=None) -> int                            # user-confirmed, kind "correction"
    store.start_portrait_run(kind, model, at=None) -> int
    store.commit_portrait_changes(run_id, changes: list[FactChange], *, until_journal_id=None)
        -> list[tuple[bool, int | None]]   # (applied, new fact id) per change
        # one transaction: ADD/UPDATE(supersede)/INVALIDATE + open-loop sync + folded corrections retired
    store.finish_portrait_run(run_id, run: PortraitRunStats) -> None
    store.portrait_checkpoint() -> int                                    # highest journal id a run consumed
    store.last_portrait_run_at(kinds=None) -> float | None                # latest successful run start
    store.save_portrait(text, *, run_id=None, model=None, fact_count=0, at=None) -> int
    store.latest_portrait() -> Portrait | None
    store.activity_slots(since, until, slot_s=900) -> dict                # foreground presence for routines

Know-how and open loops::

    app_key(name) -> str | None     # "Arc.exe" / "arc" / " ARC.EXE " -> "arc"
    store.add_knowhow(app, text, *, source_request=None, vector=None, embed_model=None,
                      supersedes=None, at=None) -> int                    # bi-temporal supersede; app -> app_key
    store.knowhow_current(app=None, *, any_app=True) -> list[KnowHow]
    store.search_knowhow(query_vec, limit=8, *, exclude_ids=()) -> list[KnowHow]   # .score = cosine
    store.open_loops(status="open") -> list[OpenLoop]
    store.add_open_loop(person, text, *, opened_at=None, portrait_fact_id=None, source_ids=None) -> int
    store.resolve_open_loop(loop_id, *, at=None, status="resolved") -> None

Timeline (foreground stretches, yuki.memory.timeline) and episodes (yuki.memory.episodes)::

    store.timeline_key(process, host, path, title, withheld=None, *, fullscreen=False, meeting=None) -> str
        # HMAC identity of a stretch's page (a meeting: process + service; full screen marked)
    store.save_timeline(row: TimelineRow) -> int        # insert (row.id None) or update; page_key filled in
    store.timeline_between(since=None, until=None) -> list[TimelineRow]   # overlapping rows, oldest first
    store.start_episode_run(trigger, model, window_start, window_end, final, at=None) -> int
    store.commit_episodes(run_id, window_start, episodes: list[NewEpisode]) -> list[int]
        # one transaction: the window's earlier (open-window) episodes superseded, new ones + vectors written
    store.finish_episode_run(run_id, stats: EpisodeRunStats) -> None
    store.episode_checkpoint() -> float | None          # end of the last final window
    store.episode_runs_for_window(window_start) -> list[dict]
    store.episodes_between(since=None, until=None, *, include_superseded=False) -> list[EpisodeRecord]
    store.search_episodes(query_vec, since=None, until=None, limit=10) -> list[EpisodeRecord]  # .score = cosine
    store.keyword_episodes(text, since=None, until=None, limit=50) -> list[EpisodeRecord]
    store.episodes_without_vectors(limit=64) -> list[EpisodeRecord];  store.add_episode_vectors(items, model)

Outside sources (yuki.memory.warp)::

    store.source_checkpoint(name) -> str | None;  store.set_source_checkpoint(name, value)
    store.add_terminal_commands(checkpoint: (name, value), groups: [(folder, [command dict])], *,
                                app="Warp", process="warp", profile="warp") -> list[capture_id]
        # one transaction: a "terminal" thread per folder, one capture per folder (delta = JSON
        # {"commands": [...]}, encrypted), the checkpoint moved; wakes the journal worker

Status::

    store.activity_today(since) -> dict       # captures, facts, last capture, cost (journal + portrait + episodes)

Maintenance and reporting::

    store.prune(older_than_days: float = 30) -> int     # captures deleted
    store.health_summary(since=None, until=None) -> dict
    store.batch_summary(since=None, until=None) -> dict
    store.db_size_bytes() -> int

Service coordination (flag files next to the database, the service's mutex)::

    PAUSE_FLAG, REFRESH_FLAG
    flag_path(db_path, name) -> Path
    service_mutex_name(db_path) -> str
"""

from __future__ import annotations

import hashlib
import json
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


#: Flag files, next to the database. ``paused`` exists while the user has paused
#: memory (the watcher captures nothing); ``refresh_portrait`` asks the service
#: to rebuild the portrait now (the service deletes it when it starts the run).
PAUSE_FLAG = "paused"
REFRESH_FLAG = "refresh_portrait"

#: Prefix of the service's named mutex (one service per session and database).
SERVICE_MUTEX_PREFIX = "Local\\YukiMemorySingleInstance"


def flag_path(db_path: str | Path | None, name: str) -> Path:
    """The flag file ``name`` for the database at ``db_path`` (default location if ``None``)."""
    db = Path(db_path) if db_path is not None else default_db_path()
    return db.parent / name


def service_mutex_name(db_path: str | Path | None) -> str:
    """Name of the named mutex the ``yuki-memory`` service holds for ``db_path``."""
    db = Path(db_path) if db_path is not None else default_db_path()
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:12]
    return f"{SERVICE_MUTEX_PREFIX}-{digest}"


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
    #: Which conversation/page this is (``Extraction.thread_scope``); None for old threads.
    scope: str | None = None
    #: Extraction kind of the latest capture ("conversation", "page", ...); None for old threads.
    kind: str | None = None


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
    #: Extraction kind ("conversation", "email", "page", ...); None for pre-extraction captures.
    kind: str | None = None
    profile: str | None = None


@dataclass
class StoredMessage:
    """One conversation message as stored (decrypted), see :meth:`Store.add_conversation`."""

    id: int
    thread_id: int
    capture_id: int | None
    sender: str | None
    is_me: bool
    time_label: str | None
    at: float | None
    text: str
    first_seen: float
    #: new (sent/received since the watcher last saw the thread) | history (older,
    #: being viewed) | reread (a known message shown again without its time label)
    status: str
    journaled: bool


@dataclass
class ConversationWrite:
    """What :meth:`Store.add_conversation` stored (counts only)."""

    capture_id: int | None
    new: int = 0
    history: int = 0
    reread: int = 0
    seen: int = 0          # fingerprints already stored for the thread
    chars: int = 0         # characters of the capture row written (0 if none)


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


#: Portrait fact kinds the portrait worker writes; ``correction`` is the user's
#: own words (``store.add_correction``), folded into the others by the worker.
#: ``behaviour`` facts are evidence-based patterns in how the user spends time
#: (from episodes and timeline aggregates), with numbers and a confidence.
PORTRAIT_KINDS: tuple[str, ...] = (
    "work", "interest", "person", "routine", "behaviour", "preference", "open_loop",
)
CORRECTION_KIND = "correction"


@dataclass
class PortraitFact:
    """One version of a portrait fact (bi-temporal).

    ``valid_from``/``valid_to`` are valid time (when it was true, from the
    evidence); ``created_at``/``expired_at`` are transaction time (when the
    store learned it / learned it stopped). Current facts have ``valid_to`` None.
    """

    id: int
    kind: str
    subject: str
    text: str
    valid_from: float
    valid_to: float | None
    source_ids: list[int]
    confidence: float | None
    origin: str                     # model | user
    created_at: float | None
    expired_at: float | None
    superseded_by: int | None
    run_id: int | None
    #: Episodes (``episodes.id``) the fact rests on, besides the journal ids.
    episode_ids: list[int] = field(default_factory=list)


@dataclass
class FactChange:
    """One validated portrait operation for :meth:`Store.commit_portrait_changes`.

    ``op``: ADD (new fact), UPDATE (supersede ``fact_id`` with a new version),
    INVALIDATE (end ``fact_id``), NOOP (nothing written; counted by the caller).
    ``at`` is the evidence time: ``valid_from`` of a new version and
    ``valid_to`` of the one it ends. ``folds`` are correction ids this change
    implements; they are retired in the same transaction.
    """

    op: str
    fact_id: int | None = None
    kind: str = ""
    subject: str = ""
    text: str = ""
    confidence: float | None = None
    source_ids: list[int] = field(default_factory=list)
    at: float | None = None
    origin: str = "model"
    folds: list[int] = field(default_factory=list)
    episode_ids: list[int] = field(default_factory=list)


@dataclass
class PortraitRunStats:
    """Content-free accounting for one portrait run (all its model calls)."""

    kind: str
    model: str
    window_since: float | None = None
    window_until: float | None = None
    since_journal_id: int | None = None
    until_journal_id: int | None = None
    journal_facts: int = 0
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    ops_add: int = 0
    ops_update: int = 0
    ops_invalidate: int = 0
    ops_noop: int = 0
    ops_rejected: int = 0
    outcome: str = "ok"             # ok | error | empty
    error: str | None = None


@dataclass
class Portrait:
    id: int
    at: float
    text: str
    run_id: int | None
    model: str | None
    fact_count: int


@dataclass
class KnowHow:
    id: int
    app: str | None
    text: str
    valid_from: float
    valid_to: float | None
    source_request: str | None
    superseded_by: int | None = None
    score: float | None = None      # cosine similarity, set by search_knowhow


@dataclass
class OpenLoop:
    id: int
    person: str | None
    text: str
    status: str                     # open | resolved | superseded
    opened_at: float
    resolved_at: float | None
    portrait_fact_id: int | None
    source_ids: list[int]


@dataclass
class TimelineRow:
    """One foreground stretch (decrypted), see :mod:`yuki.memory.timeline`.

    A stretch is one window (and, in a browser, one page) in front, either
    ``present`` (the user at it: input within the last minute, or no input
    while this app plays media) or ``away`` (no input for over a minute and
    no media from it). Consecutive stretches of the same page and state are
    merged (``segments`` counts them). ``active_s + passive_s + away_s`` is the
    time accounted; ``media_s`` (this app's media playing) overlaps the other
    two; ``media_other`` is ``{app: seconds}`` of other apps' media meanwhile.
    ``withheld`` is the privacy reason when the title/page (and for a
    blocked app, the app) were not recorded.

    ``fullscreen``: a full-screen window was in front (a game, an F11 video);
    while privacy pauses content in full screen such a row carries only the
    process and app (and a host carried over from the same window just
    before). ``meeting``: the meeting service ("Google Meet", "Zoom") when the
    app or page is in the privacy file's ``[meetings]`` list; such a row has
    no title and no path. ``mic_s``: seconds the app in front was using the
    microphone (measured during meetings only).
    """

    id: int | None
    started_at: float
    ended_at: float
    state: str                       # present | away
    process: str | None              # app_key of the image name ("chrome")
    app: str                         # display name ("Google Chrome"); "" when withheld
    title: str | None = None
    host: str | None = None          # http(s) pages only, cleartext metadata
    path: str | None = None          # path + query of the page (encrypted at rest)
    page_key: str | None = None      # HMAC identity (Store.timeline_key)
    active_s: float = 0.0
    passive_s: float = 0.0
    away_s: float = 0.0
    media_s: float = 0.0
    media_other: dict[str, float] = field(default_factory=dict)
    withheld: str | None = None
    segments: int = 1
    fullscreen: bool = False
    meeting: str | None = None
    mic_s: float = 0.0


@dataclass
class NewEpisode:
    """One episode narrative to write in :meth:`Store.commit_episodes`."""

    started_at: float
    ended_at: float
    window_start: float
    window_end: float
    final: bool
    text: str
    aggregates: dict[str, Any]
    vector: np.ndarray | None = None
    embed_model: str | None = None


@dataclass
class EpisodeRecord:
    """A stored episode (decrypted)."""

    id: int
    started_at: float
    ended_at: float
    window_start: float
    window_end: float
    final: bool
    text: str
    aggregates: dict[str, Any]
    run_id: int | None
    created_at: float
    superseded_by: int | None = None
    score: float | None = None       # cosine, set by search_episodes


@dataclass
class EpisodeRunStats:
    """Content-free accounting for one episode run."""

    outcome: str = "ok"              # ok | empty | error | gave_up
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    stop_reason: str | None = None
    episodes: int = 0
    present_s: float = 0.0
    error: str | None = None


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
    # 2: Phase B - portrait runs and renders, know-how vectors, bi-temporal
    # bookkeeping. Additive only (new tables, nullable/defaulted columns), so a
    # service still running migration-1 code keeps working on a migrated file.
    """
    ALTER TABLE threads ADD COLUMN process TEXT;         -- app_key(image name), cleartext metadata
    CREATE INDEX threads_process ON threads(process);

    ALTER TABLE portrait_facts ADD COLUMN origin TEXT NOT NULL DEFAULT 'model';
    ALTER TABLE portrait_facts ADD COLUMN created_at REAL;
    ALTER TABLE portrait_facts ADD COLUMN expired_at REAL;
    ALTER TABLE portrait_facts ADD COLUMN superseded_by INTEGER;
    ALTER TABLE portrait_facts ADD COLUMN run_id INTEGER;

    ALTER TABLE knowhow ADD COLUMN created_at REAL;
    ALTER TABLE knowhow ADD COLUMN expired_at REAL;
    ALTER TABLE knowhow ADD COLUMN superseded_by INTEGER;

    ALTER TABLE open_loops ADD COLUMN portrait_fact_id INTEGER;
    ALTER TABLE open_loops ADD COLUMN source_ids TEXT;
    ALTER TABLE open_loops ADD COLUMN updated_at REAL;
    CREATE INDEX open_loops_fact ON open_loops(portrait_fact_id);

    CREATE TABLE knowhow_vec (
        knowhow_id      INTEGER PRIMARY KEY REFERENCES knowhow(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL            -- encrypted float32[dim], L2-normalised
    );

    CREATE TABLE portrait_runs (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,        -- started
        finished_at        REAL,
        kind               TEXT NOT NULL,        -- first | nightly | weekly | refresh
        model              TEXT NOT NULL,
        window_since       REAL,
        window_until       REAL,
        since_journal_id   INTEGER,
        until_journal_id   INTEGER,              -- checkpoint: highest journal id consumed
        journal_facts      INTEGER NOT NULL DEFAULT 0,
        calls              INTEGER NOT NULL DEFAULT 0,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL,
        latency_ms         REAL NOT NULL DEFAULT 0,
        ops_add            INTEGER NOT NULL DEFAULT 0,
        ops_update         INTEGER NOT NULL DEFAULT 0,
        ops_invalidate     INTEGER NOT NULL DEFAULT 0,
        ops_noop           INTEGER NOT NULL DEFAULT 0,
        ops_rejected       INTEGER NOT NULL DEFAULT 0,
        outcome            TEXT NOT NULL,        -- running | ok | empty | error
        error              TEXT
    );
    CREATE INDEX portrait_runs_at ON portrait_runs(at);

    CREATE TABLE portraits (
        id              INTEGER PRIMARY KEY,
        at              REAL NOT NULL,           -- updated_at of this render
        run_id          INTEGER,
        model           TEXT,
        text_ciphertext TEXT NOT NULL,
        chars           INTEGER NOT NULL,
        fact_count      INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX portraits_at ON portraits(at);
    """,
    # 3: capture extraction (yuki.memory.extract) - threads keyed by the
    # extraction's thread scope, conversations stored message by message with
    # fingerprints, the user's names as learned on screen, richer health.
    # Additive only, like 2.
    """
    ALTER TABLE threads ADD COLUMN scope_ciphertext TEXT;
    ALTER TABLE threads ADD COLUMN kind TEXT;
    ALTER TABLE threads ADD COLUMN messages_seen_at REAL;   -- last conversation read of the thread

    ALTER TABLE captures ADD COLUMN kind TEXT;              -- extraction kind, cleartext metadata
    ALTER TABLE captures ADD COLUMN profile TEXT;

    CREATE TABLE messages (
        id                INTEGER PRIMARY KEY,
        thread_id         INTEGER NOT NULL REFERENCES threads(id),
        capture_id        INTEGER,                 -- the capture that brought it (NULL once pruned)
        fingerprint       TEXT NOT NULL,           -- HMAC(Message.fingerprint)
        content_key       TEXT NOT NULL,           -- HMAC(Message.content_key)
        sender_ciphertext TEXT,
        is_me             INTEGER NOT NULL DEFAULT 0,
        time_label        TEXT,                    -- as shown ("18:12", "Yesterday")
        at                REAL,                    -- absolute estimate, NULL if unknown
        text_ciphertext   TEXT,                    -- NULL once past the capture TTL (tombstone)
        first_seen        REAL NOT NULL,
        status            TEXT NOT NULL,           -- new | history | reread
        journaled         INTEGER NOT NULL DEFAULT 0,
        UNIQUE(thread_id, fingerprint)
    );
    CREATE INDEX messages_capture ON messages(capture_id);
    CREATE INDEX messages_thread_content ON messages(thread_id, content_key);

    CREATE TABLE me_names (
        id              INTEGER PRIMARY KEY,
        name_key        TEXT NOT NULL UNIQUE,      -- HMAC(casefolded name)
        app             TEXT,                      -- app it was first learned in
        name_ciphertext TEXT NOT NULL,
        first_seen      REAL NOT NULL,
        last_seen       REAL NOT NULL
    );

    ALTER TABLE health ADD COLUMN profile TEXT;
    ALTER TABLE health ADD COLUMN kind TEXT;
    ALTER TABLE health ADD COLUMN dropped_chars INTEGER;
    ALTER TABLE health ADD COLUMN messages INTEGER;
    ALTER TABLE health ADD COLUMN new_messages INTEGER;
    ALTER TABLE health ADD COLUMN stats TEXT;              -- JSON, content-free (nodes, ms, source)
    """,
    # 4: contextual memory - the foreground timeline (yuki.memory.timeline),
    # episodes written from it (yuki.memory.episodes) and the episodes a
    # portrait fact rests on. Additive only, like 2 and 3.
    """
    CREATE TABLE timeline (
        id               INTEGER PRIMARY KEY,
        started_at       REAL NOT NULL,
        ended_at         REAL NOT NULL,
        state            TEXT NOT NULL,             -- present | away
        process          TEXT,                      -- app_key(image name), cleartext metadata
        app              TEXT NOT NULL,             -- display name, cleartext metadata ('' when withheld)
        title_ciphertext TEXT,
        host             TEXT,                      -- http(s) page host, cleartext metadata
        path_ciphertext  TEXT,                      -- page path + query
        page_key         TEXT NOT NULL,             -- HMAC(process, host, path | title)
        active_s         REAL NOT NULL DEFAULT 0,
        passive_s        REAL NOT NULL DEFAULT 0,   -- no input, this app's media playing
        away_s           REAL NOT NULL DEFAULT 0,
        media_s          REAL NOT NULL DEFAULT 0,   -- this app's media playing (overlaps the above)
        media_other      TEXT,                      -- JSON {app: seconds}: other apps' media meanwhile
        withheld         TEXT,                      -- privacy reason when title/page were not kept
        segments         INTEGER NOT NULL DEFAULT 1,
        updated_at       REAL NOT NULL
    );
    CREATE INDEX timeline_started ON timeline(started_at);
    CREATE INDEX timeline_ended ON timeline(ended_at);

    CREATE TABLE episode_runs (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,
        finished_at        REAL,
        trigger            TEXT NOT NULL,           -- hour | break | cap | catch_up
        model              TEXT NOT NULL,
        window_start       REAL NOT NULL,
        window_end         REAL NOT NULL,
        final              INTEGER NOT NULL DEFAULT 0,
        present_s          REAL NOT NULL DEFAULT 0,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL,
        latency_ms         REAL NOT NULL DEFAULT 0,
        stop_reason        TEXT,
        episodes           INTEGER NOT NULL DEFAULT 0,
        outcome            TEXT NOT NULL,           -- running | ok | empty | error | gave_up
        error              TEXT
    );
    CREATE INDEX episode_runs_window ON episode_runs(window_start, at);

    CREATE TABLE episodes (
        id                    INTEGER PRIMARY KEY,
        started_at            REAL NOT NULL,
        ended_at              REAL NOT NULL,
        window_start          REAL NOT NULL,
        window_end            REAL NOT NULL,
        final                 INTEGER NOT NULL DEFAULT 0,
        text_ciphertext       TEXT NOT NULL,
        aggregates_ciphertext TEXT,                 -- JSON (holds titles and pages)
        run_id                INTEGER,
        created_at            REAL NOT NULL,
        expired_at            REAL,                 -- set when a later run of the window replaced it
        superseded_by         INTEGER
    );
    CREATE INDEX episodes_span ON episodes(started_at, ended_at);
    CREATE INDEX episodes_window ON episodes(window_start, expired_at);

    CREATE TABLE episode_vec (
        episode_id      INTEGER PRIMARY KEY REFERENCES episodes(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );

    ALTER TABLE portrait_facts ADD COLUMN episode_ids TEXT;   -- JSON list of episode ids
    """,
    # 5: full-screen and meeting stretches in the timeline, and checkpoints of
    # outside sources read by the service (the Warp terminal history).
    # Additive only, like 2-4.
    """
    ALTER TABLE timeline ADD COLUMN fullscreen INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE timeline ADD COLUMN meeting TEXT;                    -- meeting service, cleartext metadata
    ALTER TABLE timeline ADD COLUMN mic_s REAL NOT NULL DEFAULT 0;   -- microphone in use by the app in front

    CREATE TABLE source_checkpoints (
        name       TEXT PRIMARY KEY,                -- e.g. warp.commands
        value      TEXT NOT NULL,                   -- last row consumed (source-specific, content-free)
        updated_at REAL NOT NULL
    );
    """,
)


def _ts(value: TimeArg) -> float | None:
    """Epoch seconds from a float/int/datetime (naive datetime = local time)."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.timestamp()
    return float(value)


def app_key(name: str | None) -> str | None:
    """Normalised process form of an app name: stripped, casefolded, no ``.exe``.

    Know-how is keyed by it and ``threads.process`` stores it, so
    ``"Arc.exe"``, ``"arc.exe"`` and ``"arc"`` are the same app.
    """
    key = (name or "").strip().casefold()
    if key.endswith(".exe"):
        key = key[:-4].rstrip()
    return key or None


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

    def upsert_thread(
        self, app: str, title: str, url: str | None, *, process: str | None = None,
        scope: str | None = None, kind: str | None = None,
    ) -> int:
        """Find or create the thread for this window/page and return its id.

        The identity is ``(app, scope)`` when ``scope`` (the extraction's
        ``thread_scope``: a Slack channel, a Gmail subject, a page URL) is
        given, else ``(app, url)`` when ``url`` is non-empty, else
        ``(app, title)``. A scope equal to the URL keys the thread exactly as a
        URL does, so pages keep the threads (and latest texts) they had before
        scopes existed; a page keeps its thread while its title changes
        (notification counters, "(3) Inbox"). The latest title/url/scope/kind,
        ``process`` (stored as :func:`app_key`, e.g. ``"chrome"``) and
        ``last_seen`` are updated on every call.
        """
        return self._write(
            lambda conn: self._upsert_thread(conn, app, title, url, process=process, scope=scope, kind=kind)
        )

    def _upsert_thread(
        self, conn: sqlite3.Connection, app: str, title: str, url: str | None, *, process: str | None = None,
        scope: str | None = None, kind: str | None = None,
    ) -> int:
        """:meth:`upsert_thread` inside a caller's transaction."""
        app = app or ""
        title = title or ""
        url = url or None
        scope = (scope or "").strip() or None
        proc = app_key(process)
        if scope and scope != url:
            key = self.cipher.digest("scope", app, scope)
        else:
            key = self.cipher.digest("url" if url else "title", app, url or title)
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT id FROM threads WHERE thread_key=?", (key,)).fetchone()
            if row:
                conn.execute(
                    "UPDATE threads SET title_ciphertext=?, url_ciphertext=?, last_seen=?,"
                    " process=coalesce(?, process), scope_ciphertext=coalesce(?, scope_ciphertext),"
                    " kind=coalesce(?, kind) WHERE id=?",
                    (self._enc(title), self._enc(url), now, proc, self._enc(scope), kind, row["id"]),
                )
                return int(row["id"])
            cur = conn.execute(
                "INSERT INTO threads(thread_key, app, host, title_ciphertext, url_ciphertext, first_seen, last_seen,"
                " process, scope_ciphertext, kind) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (key, app, url_host(url), self._enc(title), self._enc(url), now, now, proc, self._enc(scope), kind),
            )
            return int(cur.lastrowid)

        return op(conn)

    def thread_info(self, thread_id: int) -> ThreadInfo | None:
        """Decrypted thread metadata, or ``None``."""
        rows = self._query("SELECT * FROM threads WHERE id=?", (thread_id,))
        if not rows:
            return None
        r = rows[0]
        return ThreadInfo(
            id=r["id"], app=r["app"], host=r["host"], title=self._dec(r["title_ciphertext"]) or "",
            url=self._dec(r["url_ciphertext"]), first_seen=r["first_seen"], last_seen=r["last_seen"],
            scope=self._dec(r["scope_ciphertext"]), kind=r["kind"],
        )

    # -- captures ----------------------------------------------------------

    def latest_text(self, thread_id: int) -> str | None:
        """The last full text stored for ``thread_id`` (decrypted), or ``None``."""
        rows = self._query("SELECT text_ciphertext FROM thread_latest WHERE thread_id=?", (thread_id,))
        return self._dec(rows[0]["text_ciphertext"]) if rows else None

    def add_capture(
        self, thread_id: int, at: float, trigger: str, full_text: str, delta_text: str,
        *, kind: str | None = None, profile: str | None = None,
    ) -> int | None:
        """Store one capture's delta; see the module docstring for the dedup rules.

        ``kind``/``profile``: the extraction that produced the text (cleartext metadata).
        """
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
                "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (thread_id, at, trigger or "", self.cipher.encrypt(delta_text), len(delta_text), delta_hash,
                 kind, profile),
            )
            return int(cur.lastrowid)

        capture_id = self._write(op)
        if capture_id is not None:
            with self._cond:
                self._cond.notify_all()
        return capture_id

    def add_health(
        self, at: float, app: str, trigger: str, outcome: str, reason: str | None, chars: int, ms: float,
        *, profile: str | None = None, kind: str | None = None, dropped_chars: int | None = None,
        messages: int | None = None, new_messages: int | None = None, stats: dict | None = None,
    ) -> None:
        """Record one content-free capture-health row.

        ``profile``/``kind``: the extraction used; ``dropped_chars``: UI chrome
        removed; ``messages``/``new_messages``: messages on screen / stored as
        new; ``stats``: the extraction's diagnostics (node count, ms, source).
        Never pass captured text in any of them.
        """
        self._write(
            lambda conn: conn.execute(
                "INSERT INTO health(at, app, trigger, outcome, reason, chars, ms, profile, kind, dropped_chars,"
                " messages, new_messages, stats) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (float(at), app, trigger, outcome, reason, int(chars or 0), float(ms or 0.0), profile, kind,
                 dropped_chars, messages, new_messages, json.dumps(stats) if stats else None),
            )
        )

    # -- conversations -------------------------------------------------------

    def add_conversation(
        self,
        thread_id: int,
        at: float,
        trigger: str,
        messages: Sequence[Any],
        *,
        kind: str = "conversation",
        profile: str | None = None,
        first_visit_new_s: float = 300.0,
        label_tolerance_s: float = 90.0,
    ) -> ConversationWrite:
        """Store the messages on screen that the thread has not seen; one transaction.

        ``messages`` are :class:`yuki.memory.extract.Message`-shaped objects
        (``fingerprint``, ``content_key``, ``sender``, ``is_me``, ``time_label``,
        ``at``, ``text``) in on-screen order, oldest first.

        A message is stored only if its fingerprint is unseen in the thread.
        An unseen fingerprint whose content key the thread already has, when
        either reading lacks a time label, is the same message read again
        (``reread``: kept as a fingerprint only, never journaled). The others
        are classified against *when the watcher last saw this thread*
        (``threads.messages_seen_at``; a reference point for labelling, not a
        behaviour rule):

        * ``new`` - sent or received since then: its ``at`` is at or after the
          last look (less ``label_tolerance_s``: labels show minutes only), or,
          without an ``at``, it sits below the thread's newest stored message
          on screen. On a thread never seen before, ``new`` means an ``at``
          within ``first_visit_new_s`` of this capture.
        * ``history`` - everything else on screen: older messages being viewed.

        When anything is stored, one capture row (``kind``/``profile``; the
        delta is a plain rendering of the stored messages) carries them to the
        journal worker; ``messages.capture_id`` links them to it.
        """
        at = float(at)
        items = [m for m in messages if (getattr(m, "text", "") or "").strip() and getattr(m, "fingerprint", "")]

        def placeholders(n: int) -> str:
            return ",".join("?" * n)

        def op(conn: sqlite3.Connection) -> ConversationWrite:
            out = ConversationWrite(capture_id=None)
            row = conn.execute("SELECT messages_seen_at FROM threads WHERE id=?", (thread_id,)).fetchone()
            previous = row["messages_seen_at"] if row else None
            fps = [self.cipher.digest("fp", m.fingerprint) for m in items]
            cks = [self.cipher.digest("ck", m.content_key or m.fingerprint) for m in items]
            known: set[str] = set()
            content: dict[str, bool] = {}   # content key -> some stored reading had no time label
            for start in range(0, len(items), 400):
                fchunk, cchunk = fps[start:start + 400], cks[start:start + 400]
                known.update(r["fingerprint"] for r in conn.execute(
                    f"SELECT fingerprint FROM messages WHERE thread_id=? AND fingerprint IN ({placeholders(len(fchunk))})",
                    (thread_id, *fchunk),
                ))
                for r in conn.execute(
                    "SELECT content_key, max(time_label IS NULL) untimed FROM messages"
                    f" WHERE thread_id=? AND content_key IN ({placeholders(len(cchunk))}) GROUP BY content_key",
                    (thread_id, *cchunk),
                ):
                    content[r["content_key"]] = bool(r["untimed"])
            # The thread's newest stored message, when it is on screen: unseen
            # untimed messages below it arrived since the last look.
            anchor = -1
            if previous is not None and known:
                newest = conn.execute(
                    "SELECT fingerprint FROM messages WHERE thread_id=? AND status != 'reread'"
                    " ORDER BY coalesce(at, 0) DESC, id DESC LIMIT 1", (thread_id,),
                ).fetchone()
                if newest is not None and newest["fingerprint"] in known:
                    anchor = max(i for i, f in enumerate(fps) if f == newest["fingerprint"])
            stored: list[tuple[Any, str, int]] = []
            for pos, (m, fp, ck) in enumerate(zip(items, fps, cks)):
                if fp in known:
                    out.seen += 1
                    continue
                known.add(fp)  # the same fingerprint twice on one screen is one message
                if ck in content and (m.time_label is None or content[ck]):
                    status = "reread"
                elif previous is None:
                    status = "new" if m.at is not None and m.at >= at - first_visit_new_s else "history"
                elif m.at is not None:
                    status = "new" if m.at >= previous - label_tolerance_s else "history"
                else:
                    status = "new" if 0 <= anchor < pos else "history"
                content[ck] = content.get(ck, False) or m.time_label is None
                keep = status != "reread"
                cur = conn.execute(
                    "INSERT INTO messages(thread_id, capture_id, fingerprint, content_key, sender_ciphertext, is_me,"
                    " time_label, at, text_ciphertext, first_seen, status, journaled)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        thread_id, None, fp, ck, self._enc(m.sender) if keep else None, int(bool(m.is_me)),
                        m.time_label, m.at, self.cipher.encrypt(m.text) if keep else None, at, status,
                        0 if keep else 1,
                    ),
                )
                if keep:
                    stored.append((m, status, int(cur.lastrowid)))
                setattr(out, status, getattr(out, status) + 1)
            conn.execute(
                "UPDATE threads SET messages_seen_at=?, last_seen=max(last_seen, ?) WHERE id=?",
                (at, at, thread_id),
            )
            if not stored:
                return out
            delta = "\n".join(
                f"[{status}] {m.time_label or '-'} {'the user' if m.is_me else (m.sender or '?')}: {m.text}"
                for m, status, _ in stored
            )
            cur = conn.execute(
                "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (thread_id, at, trigger or "", self.cipher.encrypt(delta), len(delta),
                 self.cipher.digest("conv", *sorted(fps)), kind, profile),
            )
            out.capture_id = int(cur.lastrowid)
            out.chars = len(delta)
            conn.executemany(
                "UPDATE messages SET capture_id=? WHERE id=?", [(out.capture_id, mid) for _, _, mid in stored]
            )
            return out

        result = self._write(op)
        if result.capture_id is not None:
            with self._cond:
                self._cond.notify_all()
        return result

    def messages_for_captures(self, capture_ids: Iterable[int]) -> dict[int, list[StoredMessage]]:
        """Stored (not ``reread``) messages per capture id, in on-screen order, decrypted."""
        ids = [int(i) for i in capture_ids]
        out: dict[int, list[StoredMessage]] = {i: [] for i in ids}
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            rows = self._query(
                "SELECT * FROM messages WHERE capture_id IN (%s) AND status != 'reread' ORDER BY id"
                % ",".join("?" * len(chunk)), chunk,
            )
            for r in rows:
                out[r["capture_id"]].append(StoredMessage(
                    id=r["id"], thread_id=r["thread_id"], capture_id=r["capture_id"],
                    sender=self._dec(r["sender_ciphertext"]), is_me=bool(r["is_me"]), time_label=r["time_label"],
                    at=r["at"], text=self._dec(r["text_ciphertext"]) or "", first_seen=r["first_seen"],
                    status=r["status"], journaled=bool(r["journaled"]),
                ))
        return out

    def message_counts(self, thread_id: int) -> dict[str, int]:
        """Stored messages of one thread by status (content-free)."""
        return {r["status"]: r["n"] for r in self._query(
            "SELECT status, count(*) n FROM messages WHERE thread_id=? GROUP BY status", (int(thread_id),)
        )}

    # -- the user's names ----------------------------------------------------

    def add_me_names(self, app: str | None, names: Iterable[str]) -> int:
        """Remember names the screen showed as the user ("Sudhanshu (you)"); returns how many were new."""
        now = time.time()
        clean = list(dict.fromkeys(n.strip() for n in names if n and n.strip()))

        def op(conn: sqlite3.Connection) -> int:
            added = 0
            for name in clean:
                key = self.cipher.digest("me", name.casefold())
                if conn.execute("UPDATE me_names SET last_seen=? WHERE name_key=?", (now, key)).rowcount == 0:
                    conn.execute(
                        "INSERT INTO me_names(name_key, app, name_ciphertext, first_seen, last_seen) VALUES (?,?,?,?,?)",
                        (key, app, self.cipher.encrypt(name), now, now),
                    )
                    added += 1
            return added

        return self._write(op) if clean else 0

    def me_names(self) -> list[str]:
        """Every name learned as the user's, oldest first."""
        return [self.cipher.decrypt(r["name_ciphertext"]) for r in self._query(
            "SELECT name_ciphertext FROM me_names ORDER BY first_seen, id"
        )]

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
            "SELECT id, thread_id, at, trigger, delta_ciphertext, chars, journal_attempts, kind, profile FROM captures"
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
                attempts=r["journal_attempts"], kind=r["kind"], profile=r["profile"],
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
            conn.executemany("UPDATE messages SET journaled=1 WHERE capture_id=?", [(i,) for i in ids])
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
        self, since: TimeArg, until: TimeArg, app: str | Sequence[str] | None, host: str | None,
        process: str | None = None,
    ) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("j.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("j.at < ?")
            params.append(u)
        names = [app] if isinstance(app, str) else list(app or [])
        names = [n for n in (x.strip() for x in names if x) if n]
        proc = app_key(process)
        either = []
        if names:
            either.append("lower(j.app) IN (%s)" % ",".join("lower(?)" for _ in names))
            params.extend(names)
        if proc:
            either.append("j.thread_id IN (SELECT id FROM threads WHERE process = ?)")
            params.append(proc)
        if either:
            clauses.append("(" + " OR ".join(either) + ")")
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
        app: str | Sequence[str] | None = None,
        limit: int = 10,
        *,
        host: str | None = None,
        process: str | None = None,
    ) -> list[JournalEntry]:
        """Journal facts most similar to ``query_vec`` (cosine), within the filters.

        Vectors in the window are decrypted and scored in memory with numpy;
        rows embedded with a different dimension are skipped.
        """
        where, params = self._journal_filter(since, until, app, host, process)
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
        app: str | Sequence[str] | None = None,
        limit: int = 50,
        *,
        host: str | None = None,
        process: str | None = None,
    ) -> list[JournalEntry]:
        """Facts in the window containing every whitespace-separated term of ``text``.

        Case-insensitive (casefold). Rows are decrypted in memory; there is no
        plaintext index. Newest first.
        """
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._journal_filter(since, until, app, host, process)
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

    def journal_after_id(self, after_id: int, limit: int | None = None) -> list[JournalEntry]:
        """Facts with ``id > after_id``, oldest (lowest id) first.

        Ids only grow and journal rows are never deleted, so this is the
        portrait worker's checkpoint: a fact committed late (its ``at`` is the
        capture time, minutes earlier) is still picked up by the next run.
        """
        sql = "SELECT j.* FROM journal j WHERE j.id > ? ORDER BY j.id"
        params: list[Any] = [int(after_id)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._entry(r) for r in self._query(sql, params)]

    def journal_count(self, since: TimeArg = None, until: TimeArg = None) -> int:
        """Number of journal facts in ``[since, until)``."""
        where, params = self._journal_filter(since, until, None, None)
        return int(self._query("SELECT count(*) FROM journal j" + where, params)[0][0])

    # -- portrait facts ----------------------------------------------------

    def _fact(self, r: sqlite3.Row) -> PortraitFact:
        try:
            sources = [int(i) for i in json.loads(r["source_ids"] or "[]")]
        except (ValueError, TypeError):
            sources = []
        try:
            episodes = [int(i) for i in json.loads(r["episode_ids"] or "[]")]
        except (ValueError, TypeError, IndexError):
            episodes = []
        return PortraitFact(
            id=r["id"], kind=r["kind"], subject=self._dec(r["subject_ciphertext"]) or "",
            text=self.cipher.decrypt(r["text_ciphertext"]), valid_from=r["valid_from"], valid_to=r["valid_to"],
            source_ids=sources, confidence=r["confidence"], origin=r["origin"] or "model",
            created_at=r["created_at"], expired_at=r["expired_at"], superseded_by=r["superseded_by"],
            run_id=r["run_id"], episode_ids=episodes,
        )

    def portrait_facts(
        self, kinds: Sequence[str] | None = None, *, include_corrections: bool = True
    ) -> list[PortraitFact]:
        """Current portrait facts (``valid_to IS NULL``), by kind then age."""
        sql = "SELECT * FROM portrait_facts WHERE valid_to IS NULL"
        params: list[Any] = []
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        if not include_corrections:
            sql += " AND kind != ?"
            params.append(CORRECTION_KIND)
        sql += " ORDER BY kind, valid_from, id"
        return [self._fact(r) for r in self._query(sql, params)]

    def portrait_fact(self, fact_id: int) -> PortraitFact | None:
        """One fact version by id (current or not)."""
        rows = self._query("SELECT * FROM portrait_facts WHERE id=?", (int(fact_id),))
        return self._fact(rows[0]) if rows else None

    def portrait_history(self, limit: int = 200) -> list[PortraitFact]:
        """Every fact version, newest first (current and superseded)."""
        return [self._fact(r) for r in self._query(
            "SELECT * FROM portrait_facts ORDER BY id DESC LIMIT ?", (int(limit),)
        )]

    def _insert_fact(
        self, conn: sqlite3.Connection, *, kind: str, subject: str, text: str, valid_from: float,
        source_ids: Sequence[int], confidence: float | None, origin: str, run_id: int | None, now: float,
        episode_ids: Sequence[int] = (),
    ) -> int:
        cur = conn.execute(
            "INSERT INTO portrait_facts(kind, subject_ciphertext, text_ciphertext, valid_from, valid_to,"
            " source_ids, confidence, origin, created_at, run_id, episode_ids) VALUES (?,?,?,?,NULL,?,?,?,?,?,?)",
            (
                kind, self._enc(subject or ""), self.cipher.encrypt(text), float(valid_from),
                json.dumps([int(i) for i in source_ids]), confidence, origin, now, run_id,
                json.dumps([int(i) for i in episode_ids]) if episode_ids else None,
            ),
        )
        return int(cur.lastrowid)

    def _end_fact(
        self, conn: sqlite3.Connection, fact_id: int, valid_to: float, now: float, superseded_by: int | None
    ) -> bool:
        cur = conn.execute(
            "UPDATE portrait_facts SET valid_to=?, expired_at=?, superseded_by=? WHERE id=? AND valid_to IS NULL",
            (float(valid_to), now, superseded_by, int(fact_id)),
        )
        return cur.rowcount == 1

    def add_correction(self, text: str, at: float | None = None) -> int:
        """Store a user-confirmed correction (kind ``correction``, origin ``user``, confidence 1)."""
        now = time.time()
        when = float(at) if at is not None else now
        return self._write(lambda conn: self._insert_fact(
            conn, kind=CORRECTION_KIND, subject="", text=text, valid_from=when, source_ids=(),
            confidence=1.0, origin="user", run_id=None, now=now,
        ))

    def commit_portrait_changes(
        self, run_id: int | None, changes: Sequence[FactChange], *, until_journal_id: int | None = None
    ) -> list[tuple[bool, int | None]]:
        """Apply validated operations in one transaction; returns ``(applied, new fact id)`` per change.

        ADD inserts; UPDATE inserts the new version and ends the old one
        (``valid_to`` = evidence time, ``superseded_by`` = new id); INVALIDATE
        ends the fact; NOOP writes nothing. A change whose target is no longer
        current is skipped (``(False, None)``). Open loops follow their ``open_loop``
        facts: an ADD opens one, an UPDATE moves the loop to the new version,
        an INVALIDATE (or an UPDATE to another kind) resolves it. Corrections
        listed in ``folds`` are retired (superseded by the first new fact made
        from them). ``until_journal_id`` advances the run's checkpoint.
        """
        now = time.time()

        def op(conn: sqlite3.Connection) -> list[tuple[bool, int | None]]:
            out: list[tuple[bool, int | None]] = []
            folded: dict[int, int | None] = {}
            for ch in changes:
                at = float(ch.at) if ch.at is not None else now
                new_id: int | None = None
                old: sqlite3.Row | None = None
                if ch.op in ("UPDATE", "INVALIDATE"):
                    old = conn.execute(
                        "SELECT * FROM portrait_facts WHERE id=? AND valid_to IS NULL", (ch.fact_id,)
                    ).fetchone()
                    if old is None:
                        out.append((False, None))
                        continue
                if ch.op in ("ADD", "UPDATE"):
                    new_id = self._insert_fact(
                        conn, kind=ch.kind, subject=ch.subject, text=ch.text, valid_from=at,
                        source_ids=ch.source_ids, confidence=ch.confidence, origin=ch.origin,
                        run_id=run_id, now=now, episode_ids=ch.episode_ids,
                    )
                if old is not None:
                    self._end_fact(conn, old["id"], at, now, new_id)
                self._sync_loop(conn, ch, old, new_id, at, now)
                for cid in ch.folds:
                    if cid not in folded or folded[cid] is None:
                        folded[cid] = new_id
                out.append((True, new_id))
            for cid, by in folded.items():
                self._end_fact(conn, cid, now, now, by)
            if run_id is not None and until_journal_id is not None:
                conn.execute(
                    "UPDATE portrait_runs SET until_journal_id=max(coalesce(until_journal_id, 0), ?) WHERE id=?",
                    (int(until_journal_id), run_id),
                )
            return out

        return self._write(op)

    def _sync_loop(
        self, conn: sqlite3.Connection, ch: FactChange, old: sqlite3.Row | None, new_id: int | None,
        at: float, now: float,
    ) -> None:
        sources = json.dumps([int(i) for i in ch.source_ids])
        loop = None
        if old is not None:
            loop = conn.execute(
                "SELECT id FROM open_loops WHERE portrait_fact_id=? AND status='open'", (old["id"],)
            ).fetchone()
        if ch.op == "ADD" and ch.kind == "open_loop":
            conn.execute(
                "INSERT INTO open_loops(person_ciphertext, text_ciphertext, status, opened_at, resolved_at,"
                " portrait_fact_id, source_ids, updated_at) VALUES (?,?,'open',?,NULL,?,?,?)",
                (self._enc(ch.subject or None), self.cipher.encrypt(ch.text), at, new_id, sources, now),
            )
        elif ch.op == "UPDATE" and loop is not None and ch.kind == "open_loop":
            conn.execute(
                "UPDATE open_loops SET person_ciphertext=?, text_ciphertext=?, portrait_fact_id=?, source_ids=?,"
                " updated_at=? WHERE id=?",
                (self._enc(ch.subject or None), self.cipher.encrypt(ch.text), new_id, sources, now, loop["id"]),
            )
        elif ch.op == "UPDATE" and loop is None and ch.kind == "open_loop":
            conn.execute(
                "INSERT INTO open_loops(person_ciphertext, text_ciphertext, status, opened_at, resolved_at,"
                " portrait_fact_id, source_ids, updated_at) VALUES (?,?,'open',?,NULL,?,?,?)",
                (self._enc(ch.subject or None), self.cipher.encrypt(ch.text), at, new_id, sources, now),
            )
        elif loop is not None and (ch.op == "INVALIDATE" or ch.op == "UPDATE"):
            conn.execute(
                "UPDATE open_loops SET status='resolved', resolved_at=?, updated_at=? WHERE id=?",
                (at, now, loop["id"]),
            )

    # -- portrait runs and renders ----------------------------------------

    def start_portrait_run(self, kind: str, model: str, at: float | None = None) -> int:
        """Insert a ``running`` run row and return its id."""
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO portrait_runs(at, kind, model, outcome) VALUES (?,?,?,'running')",
            (float(at) if at is not None else time.time(), kind, model),
        ).lastrowid))

    def finish_portrait_run(self, run_id: int, run: PortraitRunStats) -> None:
        """Record a run's accounting and outcome.

        Only a successful run (``ok``/``empty``) moves the checkpoint to its
        ``until_journal_id``; a failed one keeps what its committed calls
        advanced, so the facts it did not get to are picked up next time.
        """
        self._write(lambda conn: conn.execute(
            "UPDATE portrait_runs SET finished_at=?, window_since=?, window_until=?, since_journal_id=?,"
            " until_journal_id=CASE WHEN ? IN ('ok', 'empty')"
            " THEN max(coalesce(until_journal_id, 0), coalesce(?, 0)) ELSE until_journal_id END,"
            " journal_facts=?, calls=?,"
            " input_tokens=?, output_tokens=?, cache_write_tokens=?, cache_read_tokens=?, cost_usd=?,"
            " latency_ms=?, ops_add=?, ops_update=?, ops_invalidate=?, ops_noop=?, ops_rejected=?,"
            " outcome=?, error=? WHERE id=?",
            (
                time.time(), run.window_since, run.window_until, run.since_journal_id, run.outcome,
                run.until_journal_id, run.journal_facts, run.calls, run.input_tokens, run.output_tokens, run.cache_write_tokens,
                run.cache_read_tokens, run.cost_usd, run.latency_ms, run.ops_add, run.ops_update,
                run.ops_invalidate, run.ops_noop, run.ops_rejected, run.outcome, run.error, int(run_id),
            ),
        ))

    def portrait_checkpoint(self) -> int:
        """Highest journal id any portrait run has consumed (0 if none)."""
        rows = self._query("SELECT coalesce(max(until_journal_id), 0) FROM portrait_runs")
        return int(rows[0][0] or 0)

    def last_portrait_run_at(self, kinds: Sequence[str] | None = None) -> float | None:
        """Start time of the latest successful (``ok``/``empty``) run, optionally of these kinds."""
        sql = "SELECT max(at) FROM portrait_runs WHERE outcome IN ('ok', 'empty')"
        params: list[Any] = []
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        value = self._query(sql, params)[0][0]
        return float(value) if value is not None else None

    def portrait_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Latest run rows (content-free), newest first."""
        return [dict(r) for r in self._query("SELECT * FROM portrait_runs ORDER BY id DESC LIMIT ?", (int(limit),))]

    def save_portrait(
        self, text: str, *, run_id: int | None = None, model: str | None = None, fact_count: int = 0,
        at: float | None = None,
    ) -> int:
        """Store a rendered portrait (encrypted); the newest one is the current portrait."""
        when = float(at) if at is not None else time.time()
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO portraits(at, run_id, model, text_ciphertext, chars, fact_count) VALUES (?,?,?,?,?,?)",
            (when, run_id, model, self.cipher.encrypt(text), len(text), int(fact_count)),
        ).lastrowid))

    def latest_portrait(self) -> Portrait | None:
        """The newest rendered portrait, decrypted, or ``None``."""
        rows = self._query("SELECT * FROM portraits ORDER BY at DESC, id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        return Portrait(id=r["id"], at=r["at"], text=self.cipher.decrypt(r["text_ciphertext"]),
                        run_id=r["run_id"], model=r["model"], fact_count=r["fact_count"])

    def activity_slots(self, since: TimeArg, until: TimeArg, slot_s: float = 900.0) -> dict[str, Any]:
        """Foreground presence in fixed slots: which apps/sites the watcher read, when.

        Returns ``{"apps": {app: set(slot)}, "hosts": {(app, host): set(slot)}, "slot_s": slot_s}``
        where a slot is ``int(at // slot_s)``. Apps come from captures and from
        capture-health rows whose outcome shows the window was read (a backstop
        re-read of unchanged text writes health, not a capture); hosts come
        from captures (health rows carry no host). Content-free metadata only.
        """
        s, u = _ts(since), _ts(until)
        clauses, params = [], []
        if s is not None:
            clauses.append("c.at >= ?")
            params.append(s)
        if u is not None:
            clauses.append("c.at < ?")
            params.append(u)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        apps: dict[str, set[int]] = {}
        hosts: dict[tuple[str, str], set[int]] = {}
        for r in self._query(
            "SELECT c.at, t.app, t.host FROM captures c JOIN threads t ON t.id = c.thread_id" + where, params
        ):
            slot = int(r["at"] // slot_s)
            apps.setdefault(r["app"] or "", set()).add(slot)
            if r["host"]:
                hosts.setdefault((r["app"] or "", r["host"]), set()).add(slot)
        hwhere = where.replace("c.at", "h.at")
        hwhere += (" AND " if hwhere else " WHERE ") + (
            "h.outcome IN ('captured','deduplicated','unchanged','no_new_text','empty') AND coalesce(h.app,'') != ''"
        )
        for r in self._query("SELECT h.at, h.app FROM health h" + hwhere, params):
            apps.setdefault(r["app"], set()).add(int(r["at"] // slot_s))
        return {"apps": apps, "hosts": hosts, "slot_s": slot_s}

    # -- know-how ----------------------------------------------------------

    def _knowhow(self, r: sqlite3.Row, score: float | None = None) -> KnowHow:
        return KnowHow(
            id=r["id"], app=r["app"], text=self.cipher.decrypt(r["text_ciphertext"]), valid_from=r["valid_from"],
            valid_to=r["valid_to"], source_request=self._dec(r["source_request_ciphertext"]),
            superseded_by=r["superseded_by"], score=score,
        )

    def add_knowhow(
        self,
        app: str | None,
        text: str,
        *,
        source_request: str | None = None,
        vector: np.ndarray | None = None,
        embed_model: str | None = None,
        supersedes: int | None = None,
        at: float | None = None,
    ) -> int:
        """Write a know-how entry; with ``supersedes``, end that entry (bi-temporal) in the same transaction.

        ``app`` is stored as :func:`app_key` (``"Arc.exe"`` -> ``"arc"``); ``None``/empty = general.
        """
        now = time.time()
        when = float(at) if at is not None else now
        key = app_key(app)

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO knowhow(app, task_kind, text_ciphertext, valid_from, valid_to,"
                " source_request_ciphertext, created_at) VALUES (?,NULL,?,?,NULL,?,?)",
                (key, self.cipher.encrypt(text), when, self._enc(source_request), now),
            )
            new_id = int(cur.lastrowid)
            if vector is not None:
                vec = np.asarray(vector, dtype=np.float32).ravel()
                conn.execute(
                    "INSERT INTO knowhow_vec(knowhow_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                    (new_id, embed_model or "", int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
                )
            if supersedes is not None:
                conn.execute(
                    "UPDATE knowhow SET valid_to=?, expired_at=?, superseded_by=? WHERE id=? AND valid_to IS NULL",
                    (when, now, new_id, int(supersedes)),
                )
            return new_id

        return self._write(op)

    def knowhow_current(self, app: str | None = None, *, any_app: bool = True) -> list[KnowHow]:
        """Current know-how, newest first.

        ``app`` given: that app's entries only (matched by :func:`app_key`).
        ``app=None``: every entry when ``any_app``, else only the entries
        without an app.
        """
        sql = "SELECT * FROM knowhow WHERE valid_to IS NULL"
        params: list[Any] = []
        key = app_key(app)
        if key is not None:
            sql += " AND app = ?"
            params.append(key)
        elif not any_app:
            sql += " AND app IS NULL"
        sql += " ORDER BY valid_from DESC, id DESC"
        return [self._knowhow(r) for r in self._query(sql, params)]

    def knowhow_vectors(self, ids: Sequence[int]) -> dict[int, np.ndarray]:
        """Stored (decrypted) vectors for these know-how ids."""
        if not ids:
            return {}
        rows = self._query(
            "SELECT knowhow_id, vec_ciphertext FROM knowhow_vec WHERE knowhow_id IN (%s)" % ",".join("?" * len(ids)),
            [int(i) for i in ids],
        )
        return {
            r["knowhow_id"]: np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
            for r in rows
        }

    def search_knowhow(
        self, query_vec: np.ndarray, limit: int = 8, *, exclude_ids: Sequence[int] = ()
    ) -> list[KnowHow]:
        """Current know-how most similar to ``query_vec`` (cosine), best first."""
        rows = self._query(
            "SELECT k.*, v.vec_ciphertext, v.dim FROM knowhow k JOIN knowhow_vec v ON v.knowhow_id = k.id"
            " WHERE k.valid_to IS NULL"
        )
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(q))
        skip = {int(i) for i in exclude_ids}
        keep = [r for r in rows if r["dim"] == q.shape[0] and r["id"] not in skip]
        if not keep or qn == 0.0:
            return []
        matrix = np.stack(
            [np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32) for r in keep]
        )
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ (q / qn)) / norms
        order = np.argsort(-scores)[: max(0, int(limit))]
        return [self._knowhow(keep[i], float(scores[i])) for i in order]

    # -- open loops --------------------------------------------------------

    def _loop(self, r: sqlite3.Row) -> OpenLoop:
        try:
            sources = [int(i) for i in json.loads(r["source_ids"] or "[]")]
        except (ValueError, TypeError):
            sources = []
        return OpenLoop(
            id=r["id"], person=self._dec(r["person_ciphertext"]), text=self.cipher.decrypt(r["text_ciphertext"]),
            status=r["status"], opened_at=r["opened_at"], resolved_at=r["resolved_at"],
            portrait_fact_id=r["portrait_fact_id"], source_ids=sources,
        )

    def open_loops(self, status: str | None = "open") -> list[OpenLoop]:
        """Open loops with this status (``None`` = all), oldest first."""
        sql = "SELECT * FROM open_loops"
        params: list[Any] = []
        if status is not None:
            sql += " WHERE status = ?"
            params.append(status)
        return [self._loop(r) for r in self._query(sql + " ORDER BY opened_at, id", params)]

    def add_open_loop(
        self, person: str | None, text: str, *, opened_at: float | None = None,
        portrait_fact_id: int | None = None, source_ids: Sequence[int] | None = None,
    ) -> int:
        """Open a loop directly (the portrait worker opens them through ``open_loop`` facts)."""
        now = time.time()
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO open_loops(person_ciphertext, text_ciphertext, status, opened_at, resolved_at,"
            " portrait_fact_id, source_ids, updated_at) VALUES (?,?,'open',?,NULL,?,?,?)",
            (self._enc(person), self.cipher.encrypt(text), float(opened_at) if opened_at is not None else now,
             portrait_fact_id, json.dumps([int(i) for i in (source_ids or ())]), now),
        ).lastrowid))

    def resolve_open_loop(self, loop_id: int, *, at: float | None = None, status: str = "resolved") -> None:
        """Close a loop (it is kept, with ``resolved_at``)."""
        now = time.time()
        self._write(lambda conn: conn.execute(
            "UPDATE open_loops SET status=?, resolved_at=?, updated_at=? WHERE id=?",
            (status, float(at) if at is not None else now, now, int(loop_id)),
        ))

    # -- status ------------------------------------------------------------

    def activity_today(self, since: TimeArg) -> dict[str, Any]:
        """Counts since ``since`` (e.g. local midnight) plus the latest capture time."""
        s = _ts(since) or 0.0
        captures = self._query("SELECT count(*) FROM captures WHERE at >= ?", (s,))[0][0]
        facts = self._query("SELECT count(*) FROM journal WHERE at >= ?", (s,))[0][0]
        last = self._query("SELECT max(at) FROM captures")[0][0]
        journal_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM journal_batches WHERE at >= ?", (s,))[0][0]
        portrait_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM portrait_runs WHERE at >= ?", (s,))[0][0]
        episode_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM episode_runs WHERE at >= ?", (s,))[0][0]
        return {
            "captures": int(captures), "facts": int(facts), "last_capture_at": last,
            "journal_cost_usd": float(journal_cost or 0.0), "portrait_cost_usd": float(portrait_cost or 0.0),
            "episode_cost_usd": float(episode_cost or 0.0),
        }

    # -- timeline ------------------------------------------------------------

    def timeline_key(
        self, process: str | None, host: str | None, path: str | None, title: str | None,
        withheld: str | None = None, *, fullscreen: bool = False, meeting: str | None = None,
    ) -> str:
        """The keyed identity of a stretch's page: what "the same page" means for merging and grouping.

        A web page is (process, host, path + query); any other window is
        (process, title); a withheld stretch is its privacy reason (and the
        app when the app itself was kept). A meeting is (process, service);
        a full-screen stretch is marked as such, so it never merges with the
        same window's stretch out of full screen.
        """
        proc = app_key(process) or ""
        if meeting:
            return self.cipher.digest("tl-meeting", proc, meeting, "fs" if fullscreen else "")
        if fullscreen:
            return self.cipher.digest("tl-fullscreen", proc, withheld or "", host or "", path or "", title or "")
        if withheld:
            return self.cipher.digest("tl-withheld", withheld, proc)
        if host or path:
            return self.cipher.digest("tl-page", proc, host or "", path or "")
        return self.cipher.digest("tl-title", proc, title or "")

    def save_timeline(self, row: TimelineRow) -> int:
        """Insert (``row.id`` None) or update one stretch; sets and returns ``row.id``.

        ``row.page_key`` is computed from the row when missing.
        """
        if not row.page_key:
            row.page_key = self.timeline_key(row.process, row.host, row.path, row.title, row.withheld,
                                             fullscreen=row.fullscreen, meeting=row.meeting)
        values = (
            float(row.started_at), float(row.ended_at), row.state, app_key(row.process), row.app or "",
            self._enc(row.title) if row.title else None, row.host, self._enc(row.path) if row.path else None,
            row.page_key, round(float(row.active_s), 3), round(float(row.passive_s), 3),
            round(float(row.away_s), 3), round(float(row.media_s), 3),
            json.dumps({k: round(v, 1) for k, v in sorted(row.media_other.items())}) if row.media_other else None,
            row.withheld, int(row.segments), time.time(), int(bool(row.fullscreen)), row.meeting or None,
            round(float(row.mic_s or 0.0), 3),
        )

        def op(conn: sqlite3.Connection) -> int:
            if row.id is not None:
                cur = conn.execute(
                    "UPDATE timeline SET started_at=?, ended_at=?, state=?, process=?, app=?, title_ciphertext=?,"
                    " host=?, path_ciphertext=?, page_key=?, active_s=?, passive_s=?, away_s=?, media_s=?,"
                    " media_other=?, withheld=?, segments=?, updated_at=?, fullscreen=?, meeting=?, mic_s=?"
                    " WHERE id=?",
                    (*values, int(row.id)),
                )
                if cur.rowcount == 1:
                    return int(row.id)
            cur = conn.execute(
                "INSERT INTO timeline(started_at, ended_at, state, process, app, title_ciphertext, host,"
                " path_ciphertext, page_key, active_s, passive_s, away_s, media_s, media_other, withheld,"
                " segments, updated_at, fullscreen, meeting, mic_s) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                values,
            )
            return int(cur.lastrowid)

        row.id = self._write(op)
        return row.id

    def _timeline_row(self, r: sqlite3.Row) -> TimelineRow:
        try:
            other = {str(k): float(v) for k, v in json.loads(r["media_other"] or "{}").items()}
        except (ValueError, TypeError, AttributeError):
            other = {}
        return TimelineRow(
            id=r["id"], started_at=r["started_at"], ended_at=r["ended_at"], state=r["state"], process=r["process"],
            app=r["app"], title=self._dec(r["title_ciphertext"]), host=r["host"], path=self._dec(r["path_ciphertext"]),
            page_key=r["page_key"], active_s=r["active_s"], passive_s=r["passive_s"], away_s=r["away_s"],
            media_s=r["media_s"], media_other=other, withheld=r["withheld"], segments=r["segments"],
            fullscreen=bool(r["fullscreen"]), meeting=r["meeting"], mic_s=r["mic_s"] or 0.0,
        )

    def timeline_between(self, since: TimeArg = None, until: TimeArg = None) -> list[TimelineRow]:
        """Stretches overlapping ``[since, until)``, oldest first (decrypted, not clipped)."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("ended_at > ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("started_at < ?")
            params.append(u)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._timeline_row(r) for r in self._query(
            "SELECT * FROM timeline" + where + " ORDER BY started_at, id", params
        )]

    def timeline_bounds(self) -> tuple[float | None, float | None]:
        """Earliest start and latest end in the timeline."""
        r = self._query("SELECT min(started_at), max(ended_at) FROM timeline")[0]
        return (r[0], r[1])

    # -- outside sources (the Warp terminal history, yuki.memory.warp) ----------

    def source_checkpoint(self, name: str) -> str | None:
        """The checkpoint of an outside source (content-free), or ``None`` before its first read."""
        rows = self._query("SELECT value FROM source_checkpoints WHERE name=?", (name,))
        return rows[0]["value"] if rows else None

    def set_source_checkpoint(self, name: str, value: str) -> None:
        self._write(lambda conn: self._set_checkpoint(conn, name, value))

    @staticmethod
    def _set_checkpoint(conn: sqlite3.Connection, name: str, value: str) -> None:
        conn.execute(
            "INSERT INTO source_checkpoints(name, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, str(value), time.time()),
        )

    def add_terminal_commands(
        self, checkpoint: tuple[str, str], groups: Sequence[tuple[str, Sequence[dict[str, Any]]]], *,
        app: str = "Warp", process: str = "warp", profile: str = "warp", at: float | None = None,
    ) -> list[int]:
        """Store terminal commands for the journal and move the source checkpoint, in one transaction.

        ``groups``: ``(folder, commands)`` pairs; each folder is one thread
        (``kind`` "terminal", scope = the folder) and gets one capture whose
        delta is ``{"commands": [...]}`` as JSON (encrypted like every capture,
        kept for the capture TTL). A command is a plain dict (``at``, ``command``,
        ``pwd``, ``branch``, ``exit_code``, ``shell``, ...), already filtered
        by the privacy rules. Returns the capture ids.
        """
        name, value = checkpoint
        when = float(at) if at is not None else time.time()

        def op(conn: sqlite3.Connection) -> list[int]:
            ids: list[int] = []
            for folder, commands in groups:
                if not commands:
                    continue
                thread_id = self._upsert_thread(conn, app, folder or "", None, process=process,
                                                scope=f"terminal:{folder or ''}", kind="terminal")
                delta = json.dumps({"commands": list(commands)}, ensure_ascii=False)
                first = min(float(c.get("at") or when) for c in commands)
                cur = conn.execute(
                    "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    (thread_id, first, profile, self.cipher.encrypt(delta), len(delta),
                     self.cipher.digest("terminal", *[str(c.get("id", "")) for c in commands]), "terminal", profile),
                )
                ids.append(int(cur.lastrowid))
                conn.execute("UPDATE threads SET last_seen=max(last_seen, ?) WHERE id=?", (when, thread_id))
            self._set_checkpoint(conn, name, value)
            return ids

        ids = self._write(op)
        if ids:
            with self._cond:
                self._cond.notify_all()
        return ids

    # -- episodes ------------------------------------------------------------

    def start_episode_run(
        self, trigger: str, model: str, window_start: float, window_end: float, final: bool,
        at: float | None = None,
    ) -> int:
        """Insert a ``running`` episode-run row and return its id."""
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO episode_runs(at, trigger, model, window_start, window_end, final, outcome)"
            " VALUES (?,?,?,?,?,?,'running')",
            (float(at) if at is not None else time.time(), trigger, model, float(window_start),
             float(window_end), int(bool(final))),
        ).lastrowid))

    def finish_episode_run(self, run_id: int, stats: EpisodeRunStats) -> None:
        """Record a run's accounting and outcome."""
        self._write(lambda conn: conn.execute(
            "UPDATE episode_runs SET finished_at=?, present_s=?, input_tokens=?, output_tokens=?,"
            " cache_write_tokens=?, cache_read_tokens=?, cost_usd=?, latency_ms=?, stop_reason=?, episodes=?,"
            " outcome=?, error=? WHERE id=?",
            (time.time(), stats.present_s, stats.input_tokens, stats.output_tokens, stats.cache_write_tokens,
             stats.cache_read_tokens, stats.cost_usd, stats.latency_ms, stats.stop_reason, stats.episodes,
             stats.outcome, stats.error, int(run_id)),
        ))

    def episode_checkpoint(self) -> float | None:
        """End of the latest final window that is done (ok, empty or given up)."""
        value = self._query(
            "SELECT max(window_end) FROM episode_runs WHERE final=1 AND outcome IN ('ok','empty','gave_up')"
        )[0][0]
        return float(value) if value is not None else None

    def episode_runs_for_window(self, window_start: float) -> list[dict[str, Any]]:
        """Runs (content-free) of the window starting at ``window_start``, oldest first."""
        return [dict(r) for r in self._query(
            "SELECT * FROM episode_runs WHERE abs(window_start - ?) < 0.001 ORDER BY at, id",
            (float(window_start),),
        )]

    def episode_runs(self, limit: int = 20) -> list[dict[str, Any]]:
        """Latest episode runs (content-free), newest first."""
        return [dict(r) for r in self._query("SELECT * FROM episode_runs ORDER BY id DESC LIMIT ?", (int(limit),))]

    def commit_episodes(self, run_id: int | None, window_start: float, episodes: Sequence[NewEpisode]) -> list[int]:
        """Write a window's episodes; the window's current episodes (from earlier runs) are superseded.

        One transaction. Earlier episodes of the same window are kept, with
        ``expired_at`` and ``superseded_by`` (the first new id) set.
        """
        now = time.time()

        def op(conn: sqlite3.Connection) -> list[int]:
            ids: list[int] = []
            for ep in episodes:
                cur = conn.execute(
                    "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext,"
                    " aggregates_ciphertext, run_id, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (float(ep.started_at), float(ep.ended_at), float(ep.window_start), float(ep.window_end),
                     int(bool(ep.final)), self.cipher.encrypt(ep.text),
                     self.cipher.encrypt(json.dumps(ep.aggregates, ensure_ascii=False, default=str)),
                     run_id, now),
                )
                eid = int(cur.lastrowid)
                ids.append(eid)
                if ep.vector is not None:
                    vec = np.asarray(ep.vector, dtype=np.float32).ravel()
                    conn.execute(
                        "INSERT INTO episode_vec(episode_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                        (eid, ep.embed_model or "", int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
                    )
            placeholders = ",".join("?" * len(ids)) if ids else "NULL"
            conn.execute(
                "UPDATE episodes SET expired_at=?, superseded_by=? WHERE abs(window_start - ?) < 0.001"
                f" AND expired_at IS NULL AND id NOT IN ({placeholders})",
                (now, ids[0] if ids else None, float(window_start), *ids),
            )
            return ids

        return self._write(op)

    def _episode(self, r: sqlite3.Row, score: float | None = None) -> EpisodeRecord:
        try:
            aggregates = json.loads(self._dec(r["aggregates_ciphertext"]) or "{}")
        except ValueError:
            aggregates = {}
        return EpisodeRecord(
            id=r["id"], started_at=r["started_at"], ended_at=r["ended_at"], window_start=r["window_start"],
            window_end=r["window_end"], final=bool(r["final"]), text=self.cipher.decrypt(r["text_ciphertext"]),
            aggregates=aggregates if isinstance(aggregates, dict) else {}, run_id=r["run_id"],
            created_at=r["created_at"], superseded_by=r["superseded_by"], score=score,
        )

    def _episode_filter(self, since: TimeArg, until: TimeArg, include_superseded: bool = False) -> tuple[str, list]:
        clauses, params = [], []
        if not include_superseded:
            clauses.append("e.expired_at IS NULL")
        if (s := _ts(since)) is not None:
            clauses.append("e.ended_at > ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("e.started_at < ?")
            params.append(u)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def episodes_between(
        self, since: TimeArg = None, until: TimeArg = None, *, include_superseded: bool = False
    ) -> list[EpisodeRecord]:
        """Episodes overlapping ``[since, until)`` (current ones unless asked), oldest first."""
        where, params = self._episode_filter(since, until, include_superseded)
        return [self._episode(r) for r in self._query(
            "SELECT e.* FROM episodes e" + where + " ORDER BY e.started_at, e.id", params
        )]

    def search_episodes(
        self, query_vec: np.ndarray, since: TimeArg = None, until: TimeArg = None, limit: int = 10
    ) -> list[EpisodeRecord]:
        """Current episodes most similar to ``query_vec`` (cosine), within the window."""
        where, params = self._episode_filter(since, until)
        rows = self._query(
            "SELECT e.*, v.vec_ciphertext, v.dim FROM episodes e JOIN episode_vec v ON v.episode_id = e.id" + where,
            params,
        )
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(q))
        keep = [r for r in rows if r["dim"] == q.shape[0]]
        if not keep or qn == 0.0:
            return []
        matrix = np.stack(
            [np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32) for r in keep]
        )
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        scores = (matrix @ (q / qn)) / norms
        order = np.argsort(-scores)[: max(0, int(limit))]
        return [self._episode(keep[i], float(scores[i])) for i in order]

    def keyword_episodes(
        self, text: str, since: TimeArg = None, until: TimeArg = None, limit: int = 50
    ) -> list[EpisodeRecord]:
        """Current episodes whose text contains every term of ``text`` (casefolded), newest first."""
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._episode_filter(since, until)
        hits: list[EpisodeRecord] = []
        for r in self._query("SELECT e.* FROM episodes e" + where + " ORDER BY e.started_at DESC, e.id DESC", params):
            ep = self._episode(r)
            folded = ep.text.casefold()
            if all(t in folded for t in terms):
                hits.append(ep)
                if len(hits) >= limit:
                    break
        return hits

    def episodes_without_vectors(self, limit: int = 64) -> list[EpisodeRecord]:
        """Current episodes that have no embedding yet."""
        return [self._episode(r) for r in self._query(
            "SELECT e.* FROM episodes e LEFT JOIN episode_vec v ON v.episode_id = e.id"
            " WHERE v.episode_id IS NULL AND e.expired_at IS NULL ORDER BY e.id LIMIT ?", (int(limit),),
        )]

    def add_episode_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        """Store embeddings for existing episodes (replacing any old one)."""

        def op(conn: sqlite3.Connection) -> int:
            for episode_id, vector in items:
                vec = np.asarray(vector, dtype=np.float32).ravel()
                conn.execute(
                    "INSERT OR REPLACE INTO episode_vec(episode_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                    (int(episode_id), model, int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
                )
            return len(items)

        return self._write(op)

    # -- maintenance -------------------------------------------------------

    def prune(self, older_than_days: float = 30) -> int:
        """Delete captures (and stale latest texts) older than the TTL; returns captures deleted.

        Journal facts are never deleted. A capture past the TTL goes whether or
        not it was journaled: raw captures are a 30-day buffer, not an archive.
        Conversation messages past the TTL keep only their fingerprints, so
        history read again later is still recognised as already seen.
        """
        cutoff = time.time() - float(older_than_days) * 86400.0

        def op(conn: sqlite3.Connection) -> int:
            conn.execute(
                "UPDATE messages SET text_ciphertext=NULL, sender_ciphertext=NULL, capture_id=NULL"
                " WHERE first_seen < ? AND (text_ciphertext IS NOT NULL OR capture_id IS NOT NULL)",
                (cutoff,),
            )
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
