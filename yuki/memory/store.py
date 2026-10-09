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
        # names the screen showed as the user ("Kian (you)"), encrypted
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
    store.commit_batch(call: ModelCall, capture_ids, facts: list[NewFact], *, supersedes=()) -> int
        # one transaction: batch row + facts + vectors + captures marked journaled; each fact has a
        # nature (fact | trait | story | humor | uncertain, migration 14); supersedes: a re-journal's
        # earlier batches, whose facts are superseded (kept; every journal reader leaves them out)
    store.journaled_captures(thread_kind=, since=, until=, batch_ids=) -> [(PendingCapture, batch id)]
    store.journal_of_batches(batch_ids, include_superseded=False);  store.superseded_journal(ids) -> {id: batch}
    store.timeline_states(since, until) -> list[TimelineRow]   # content-free, nothing decrypted (the coach's resume)
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
    # the portrait audit (migration 15): its findings with the reasoning, and its evidence helpers
    store.add_audit_findings(run_id, findings) -> ids;  store.audit_findings(run_id=None, limit=100)
    store.journal_by_ids(ids);  store.similar_journal(vectors, limit=6) -> [[JournalEntry]]
    store.batch_captures(batch_ids, per_batch=3) -> {batch: [capture dicts]};  store.first_capture_text(thread_id, before=None)
    store.journal_superseded_since(ids, since) -> set;  store.questions_changed_since(since)
        -> list[tuple[bool, int | None]]   # (applied, new fact id) per change
        # one transaction: ADD/UPDATE(supersede)/INVALIDATE + open-loop sync + folded corrections retired
    store.finish_portrait_run(run_id, run: PortraitRunStats) -> None
    store.portrait_checkpoint() -> int                                    # highest journal id a run consumed
    store.last_portrait_run_at(kinds=None) -> float | None                # latest successful run start
    store.save_portrait(text, *, run_id=None, model=None, fact_count=0, at=None, sections=None, brief=None) -> int
    store.latest_portrait() -> Portrait | None        # .sections (migration 27), .brief (the personal brief)
    store.portrait_at(at) -> Portrait | None          # the page (and its brief) current at a moment
    store.activity_slots(since, until, slot_s=900) -> dict                # foreground presence for routines

What each reply and card was written from (migration 31, yuki.memory.brief)::

    note_card_context(brief_ids, fact_ids, brief_at=)  # a card writer notes its context; the next card stored in
                                                        # that thread writes it (record_checkin), ids only
    store.add_surfaced(kind, ref, *, at, brief_ids, fact_ids, session_id, brief_at) -> int   # a reply's row
    store.surfaced(since, until, *, unjudged=False, kinds=None) -> list[dict]
    store.judge_surfaced(id, verdict, judge=None) -> bool   # the nightly judge: personal | generic | wrong | plain
    store.work_threads_touched(since, until, limit=8) -> [(thread id, events, last at)]   # the activity header

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
    store.add_power_spans([(state, start, end, source)]) -> int   # PC off/asleep/unknown rows (yuki.memory.power)
    store.power_spans(since=None, until=None) -> list[TimelineRow];  store.timeline_last_end_before(at)
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

Conversations with Yuki (yuki.memory.conversations; yuki.memory.api.MemoryClient.log_turn)::

    store.add_turn(session_id, request_id, at, user_text, reply_text=None, actions=(), outcome=None,
                   *, busy_timeout_s=0.008) -> int
        # encrypted; a dedicated connection that waits at most busy_timeout_s, else a background
        # writer retries (the id, from a microsecond clock, is returned either way); flush_turns()
    signal_turns(db_path) -> bool;  turns_event_name(db_path) -> str   # wakes the service's worker
    store.turns_pending(max_attempts=3) / turns_unsummarized() / turns_for_session(sid, before=, since=, limit=)
    store.turns_by_ids(ids) / latest_turn() / session_span(sid) / search_turns(vec, since, until, limit)
    store.keyword_turns(text, since, until, limit)
    store.conversation_facts(kinds=None, statuses=("active",)) -> list[ConversationFact]
    store.conversation_facts_ended(since, kinds=None);  conversation_fact(id);  conversation_fact_vectors(ids)
    store.add_conversation_fact(kind, text, *, quote=, origin="user", source_turn_ids=, supersedes=, vector=) -> int
    store.end_conversation_fact(fact_id, status, *, at=None, note=None) -> bool
    store.commit_conversation_batch(call, turn_ids, changes, journal_facts, *, turn_vectors=, embed_model=)
        # one transaction: batch row + ADD/UPDATE/INVALIDATE + journal facts (thread "Yuki") + turns extracted
    store.record_failed_conversation_batch(call, *, extract_turn_ids=(), summary_turn_ids=())
    store.commit_session_summary(call, session_id, started_at, ended_at, text, turn_ids, *, vector=) -> int
    store.session_summaries(session_id=None, since=None, until=None, limit=None) / search_summaries / keyword_summaries
    store.turns/summaries/conversation_facts_without_vectors();  add_conversation_vectors(what, items, model)
    store.conversation_changed_since(at) -> bool;  conversation_batches(limit);  conversation_thread_id()

The coach (yuki.memory.nudges; yuki.memory.api.MemoryClient.todos / pending_nudges / ...)::

    store.add_todo(text, *, due_at=None, all_day=False) -> int;  store.todos(statuses=("open",), *, done_since=None)
    store.labels_missing(surfaces=None) -> {surface: [id]}   # items without their short label (migration 21)
    store.set_labels(surface, {id: {"title" | "summary": text}}, *, overwrite=False) -> int   # yuki.memory.titles
    store.todo(id);  store.complete_todo(id) -> bool
    store.complete_open_loop(loop_id) -> bool      # loop row done + its portrait fact invalidated, one transaction
    store.open_loops_done_since(since) -> list[OpenLoop]
    store.record_checkin(checkin: NudgeCheckin, nudge: NewNudge | None) -> (checkin_id, nudge_id | None)
    store.nudges(since=None, until=None, *, kinds=None, pending=False, limit=None) -> list[NudgeRecord]
    store.nudge(id);  store.react_to_nudge(id, reaction);  store.expire_nudges(before, kinds) -> int
    store.set_nudge_outcome(id, outcome, only_if=None) -> bool    # what came of a typed offer (migration 26)
    store.reminded(ref, due_at) -> bool;  store.nudge_checkins(since=None, limit=None) -> list[dict]
    store.nudge_state(name) / set_nudge_state(name, value | None)
    store.captures_between(since, until=None, *, limit=4) -> [{"at", "app", "host", "kind", "text"}]   # not by_yuki

Questions for the user (yuki.memory.questions; migration 13)::

    store.add_questions([NewQuestion]) -> [(id | None, added | merged | reopened | known | rejected:...)]
        # deduped by subject HMAC and "subject: question" embedding; a subject the user stated is known
    store.commit_batch(..., questions=, question_outcomes=)   # journal questions in the batch's transaction
    store.commit_conversation_batch(..., questions=, answers=, question_outcomes=)
    store.questions(statuses=None, limit=None) / question(id) / next_question(min_importance=, since=)
    store.answer_question(id, "answered" | "skipped" | "never", words) -> correction id | 0 | None
    store.record_checkin(checkin, nudge, question=(id, "asked" | "dropped"))
    store.reopen_unshown_questions() / expire_questions(before)
    store.drop_question(id, fact_id=None) -> bool   # memory answers it already (the vetting, questions.revet)
    store.questions_near(vectors, statuses=("open", "asked"), limit=12) -> [(QuestionRecord, cosine)]

The weekly review (yuki.memory.review; MemoryClient.weekly_review)::

    store.start_weekly_review(week, period_start, period_end, trigger, model, *, at=None) -> int
    store.finish_weekly_review(id, *, outcome, stats: ReviewStats, text=, sections=, teaser=, numbers=,
                               candidates=, error=, vector=, embed_model=)   # one transaction, encrypted
    store.weekly_review(id);  store.weekly_reviews(*, week=None, outcomes=("ok",), limit=None)  # newest first
    store.set_review_nudge(id, nudge_id)
    store.review_candidates_pending(since=None) / mark_review_candidates(review_ids, run_id)   # -> portrait
    store.search_reviews(vec, since, until, limit) / keyword_reviews(text, since, until, limit)
    store.reviews_without_vectors() / add_review_vectors(items, model)
    store.episodes_by_ids(ids);  store.turn_times(since, until) -> [(at, session_id)]

Calls, both sides and game voice chats (yuki.memory.calls; MemoryClient.call_status / answer_call / stop_call)::

    store.start_call(app=, process=, via=, service=None, at=None, status="asking", consent=None, ...,
                     hold_since=None) -> int;  store.call_max_seq(id) -> int
    store.update_call(id, **plumbing columns);  store.call_record(id);  store.calls(statuses=, since=, limit=)
    store.answer_call(id, "yes"|"no"|"always"|"never"|"none", by="card") -> bool   # always/never kept per app
    store.request_call_stop(id=None) -> int | None
    store.call_pref(app) / set_call_pref(app, "always"|"never"|None, name=) / call_prefs()
    store.add_call_side(call_id, app=, process=, started_at=, ended_at=, utterances=, final=) -> capture_id
        # kind my_call_side, one thread per call; text only (JSON, encrypted), never audio
    store.commit_call_batch(call, capture_ids, facts, commitments) -> (batch_id, commitment ids)
        # journal facts + the user's own commitments (conversation facts, origin "call") in one transaction
    call_event_name(db_path);  CALL_READY_EVENT;  signal_event(name)

Call timeline digests (migration 19, yuki.memory.calldigest)::

    store.call_side_captures(call_id) -> [{"id", "at", "journaled", "data"}]   # the call's words, decrypted
    store.call_digest_candidates(max_attempts=3, journaled_only=True, include_done=False) -> [{"call_id", "app", "capture_ids"}]
    store.claim_call_digest(call_id, app=, capture_ids=, owner=) -> bool
    store.save_call_digest(call_id, app=, capture_ids=, status=, gist=, blocks=, ...) / fail_call_digest(...)
    store.call_digests(ids) -> {id: {...}};  store.call_digest(id);  store.digested_call_threads() -> {thread_id: call_id}

People and routes (migration 28, yuki.memory.contacts reads and writes them through the store's own helpers)::

    contacts / contact_routes / contact_prefs tables: who the user talks to, where (service, chat, kind, threads),
    and the user's lasting route choice; derived (rebuilt by ``python -m yuki.memory.contacts --rebuild``), no counts
    stored (computed from ``messages`` when read)

Practice memory (migration 29, yuki.memory.practice / yuki.memory.practice_learn read and write them through the
store's own helpers)::

    runs (the structured trace of agent requests and voice hands, one row per step, 30-day TTL with the captures),
    playbooks + playbook_vec (procedures, habits, pitfalls, style notes; derived columns computed from the ledger),
    playbook_events (the evidence ledger: observed, ran_ok, taught, ran_trouble, user_corrected, contradicted,
    injected, cited, ...); written only by the practice pipeline (MemoryClient.record_run / record_hand_trace /
    workflow_edit and the service's practice worker), never by hand

The user's goals and Yuki's check-ins (migration 30, yuki.memory.checkins)::

    store.goals(statuses=("open", "unknown"), *, scope=None, day=None, work_thread_id=None, ids=None) -> [Goal]
    store.goal(id);  store.goal_events(id) -> [dict]
    store.add_goal(scope, text, *, source, ...) -> int;  store.update_goal(id, *, type, ...) -> bool
        # written only by the check-in pipeline (a check-in that rests on a goal memory shows, the fold-in of the
        # user's answer), never by hand
    store.record_checkin_question(checkin, nudge, question, *, moment, goal) -> (checkin, nudge, question, goal)
        # one transaction: the check-in question (origin "checkin", status asked), its goal, the card
    store.checkin_answers_pending() -> [QuestionRecord];  store.mark_checkin_folded(id, tone, at=None)
    store.expire_checkins(before) -> int      # unanswered check-ins expire (and an unshown one is never reopened)

Status::

    store.activity_today(since) -> dict       # captures, facts, last capture, cost (journal + portrait + episodes + conversations + nudges)

Maintenance and reporting::

    store.prune(older_than_days: float = 30) -> int     # captures deleted (run traces past the TTL go too)
    store.health_summary(since=None, until=None) -> dict
    store.batch_summary(since=None, until=None) -> dict
    store.db_size_bytes() -> int

Service coordination (flag files next to the database, the service's mutex)::

    PAUSE_FLAG, REFRESH_FLAG, RENDER_FLAG, REVIEW_FLAG, AUDIT_FLAG
    flag_path(db_path, name) -> Path
    request_render(db_path) -> bool          # the render_portrait flag (debounced render)
    service_mutex_name(db_path) -> str
"""

from __future__ import annotations

import bisect
import hashlib
import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from collections.abc import Iterable, Sequence
from urllib.parse import urlsplit

import numpy as np

from yuki.core.appnames import shown as _shown   # stored app names as they read now ("WhatsApp.Root" -> "WhatsApp")
from yuki.memory.crypto import FieldCipher, load_or_create_key
from yuki.memory.secret_filter import redact_text, redact_utterances

KEY_FILENAME = "memory.key"
DB_FILENAME = "memory.db"

TimeArg = float | int | datetime | None


#: Environment override of memory's default folder (tests and harnesses point it at a temp dir,
#: so nothing that forgets to pass a path can write into the real memory; tests/conftest.py).
MEMORY_DIR_ENV = "YUKI_MEMORY_DIR"


def default_memory_dir() -> Path:
    """``%LOCALAPPDATA%\\Yuki\\memory``, or ``%YUKI_MEMORY_DIR%`` when that is set."""
    override = os.environ.get(MEMORY_DIR_ENV)
    if override:
        return Path(override)
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
#: ``render_portrait`` asks the service to re-render the portrait page from the
#: current facts (no operations call: one render), after the flag has been left
#: alone for a short debounce, so a few rule changes in a row cost one render.
#: Written after a standing rule is saved or dropped (the Relationship section).
RENDER_FLAG = "render_portrait"
#: ``run_weekly_review`` asks the service to write a weekly review now (it deletes the flag when it starts).
REVIEW_FLAG = "run_weekly_review"
#: ``audit_portrait`` asks the service's portrait scheduler for a portrait audit now ("connect the dots";
#: the file's text "all" audits every fact, anything else only the facts new evidence touches).
AUDIT_FLAG = "audit_portrait"

#: Prefix of the service's named mutex (one service per session and database).
SERVICE_MUTEX_PREFIX = "Local\\YukiMemorySingleInstance"


def flag_path(db_path: str | Path | None, name: str) -> Path:
    """The flag file ``name`` for the database at ``db_path`` (default location if ``None``)."""
    db = Path(db_path) if db_path is not None else default_db_path()
    return db.parent / name


def request_render(db_path: str | Path | None) -> bool:
    """Write the ``render_portrait`` flag (restarting its debounce); False if it could not be written."""
    try:
        path = flag_path(db_path, RENDER_FLAG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
        return True
    except OSError:
        return False


def service_mutex_name(db_path: str | Path | None) -> str:
    """Name of the named mutex the ``yuki-memory`` service holds for ``db_path``."""
    db = Path(db_path) if db_path is not None else default_db_path()
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:12]
    return f"{SERVICE_MUTEX_PREFIX}-{digest}"


#: Prefix of the named auto-reset event Yuki sets after writing a conversation turn.
TURNS_EVENT_PREFIX = "Local\\YukiMemoryTurns"


def turns_event_name(db_path: str | Path | None) -> str:
    """Name of the event that wakes the service's conversation worker for ``db_path``."""
    db = Path(db_path) if db_path is not None else default_db_path()
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:12]
    return f"{TURNS_EVENT_PREFIX}-{digest}"


#: Prefix of the named auto-reset event the UI sets after answering a call's consent card
#: or asking to stop remembering a call (the service's call worker creates it).
CALL_EVENT_PREFIX = "Local\\YukiMemoryCall"
#: Named auto-reset event the service sets when a call's consent card or recording state changed
#: (the UI's call watcher creates or opens it, like ``Local\\YukiNudgeReady``).
CALL_READY_EVENT = "Local\\YukiCallReady"


def call_event_name(db_path: str | Path | None) -> str:
    """Name of the event that wakes the service's call worker for ``db_path``."""
    db = Path(db_path) if db_path is not None else default_db_path()
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:12]
    return f"{CALL_EVENT_PREFIX}-{digest}"


def signal_event(name: str) -> bool:
    """Set an existing named event; False when nobody created it (or on any error)."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.OpenEventW(0x0002, False, name)   # EVENT_MODIFY_STATE
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


def signal_turns(db_path: str | Path | None) -> bool:
    """Set the conversation worker's wake-up event (microseconds); False when no service holds it."""
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.OpenEventW(0x0002, False, turns_event_name(db_path))   # EVENT_MODIFY_STATE
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


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
    #: Captured while Yuki was acting on the desktop for the user (yuki.memory.acting),
    #: and the request it was carrying out.
    by_yuki: bool = False
    yuki_request: str | None = None


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
    #: Drawn from what Yuki itself put on screen at the user's request (not the user's own activity).
    by_yuki: bool = False
    #: What the item is (yuki.memory.personality.NATURES): fact | trait | story | humor | uncertain.
    #: Rows recorded before natures existed read as "fact".
    nature: str = "fact"
    #: The extractor judged it records something the user finished, shipped, merged, fixed or sent.
    milestone: bool = False
    #: Where it came from (migration 17): ``{kind, by, quote, where, at}``, or ``None`` (older rows).
    source: dict[str, Any] | None = None
    #: Text the user typed into, or read from, another AI tool: never evidence of who the user is.
    tainted: bool = False
    #: The thread of work it belongs to (migration 23), or ``None``.
    work_thread_id: int | None = None
    #: ``front`` | ``visible`` | ``popup``: the attention its source had (migration 24; older rows read ``front``).
    attention: str = "front"
    #: When the user answered it as a "maybe misheard" entry (migration 33), or ``None``.
    reviewed_at: float | None = None


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
    by_yuki: bool = False
    nature: str = "fact"
    milestone: bool = False
    #: Provenance of the cited source (:func:`yuki.memory.provenance.source_of`), stored encrypted.
    source: dict[str, Any] | None = None
    tainted: bool = False
    #: How much of the user's attention the cited source had (:func:`capture_attention`, migration 24):
    #: ``front`` | ``visible`` | ``popup``.
    attention: str = "front"
    #: Admission (yuki.memory.admission): why the fact is held back until the user's yes (``call`` | ``sensitive`` |
    #: ``drop``), or ``None`` (into memory now). A held fact is written and superseded at once by a ``hold:<reason>``
    #: batch in the same transaction (:meth:`Store._hold_journal`), so no journal reader sees it.
    held: str | None = None
    #: The journal model's sensitivity mark (yuki.memory.admission.SENSITIVE_KINDS); "none" for nearly all.
    sensitive: str = "none"


#: Where a capture's window was when the watcher read it (migration 24; the watcher's own trigger format, a fixed
#: machine format): ``front`` - the window in front, the one the user works in; ``visible`` - on screen beside it
#: (trigger ``visible:<why>``), the user may not have looked at it; ``popup`` - something that appeared by itself
#: (trigger ``popup``: a notification toast, a dialog). The two last are weaker evidence of the user's attention.
ATTENTIONS: tuple[str, ...] = ("front", "visible", "popup")


def capture_attention(trigger: str | None) -> str:
    """``front`` | ``visible`` | ``popup`` for a capture's trigger (see :data:`ATTENTIONS`)."""
    t = str(trigger or "")
    if t == "popup":
        return "popup"
    if t.startswith("visible:"):
        return "visible"
    return "front"


#: A work thread's status (migration 23): active (being worked on), paused (set aside, or quiet for days),
#: done (finished), merged (tidied into another thread: ``merged_into``), dismissed (the nightly tidy judged it no
#: piece of work: its facts no longer point to it).
WORK_STATUSES: tuple[str, ...] = ("active", "paused", "done", "merged", "dismissed")
#: What a journal batch (or the nightly pass) did to a thread; each is one ``work_thread_events`` row.
#: ``seen``: the thread came up only in a window beside the one in front or a popup (migration 24): what it said
#: may update where the thread stands, but the user's own last touch and its status stay as they were.
WORK_ACTIONS: tuple[str, ...] = ("starts", "continues", "pauses", "ends", "reopens", "merged", "closed", "renamed",
                                 "seen", "undone")


@dataclass
class WorkThread:
    """One thread of work (decrypted), see :mod:`yuki.memory.workthreads`."""

    id: int
    title: str
    kind: str | None
    status: str
    state: str
    open_items: list[str]
    entities: list[str]
    first_seen: float
    last_seen: float
    importance: int | None
    origin: str
    merged_into: int | None
    events: int
    created_at: float
    updated_at: float
    score: float | None = None
    #: Where it stands, said to the user (migration 33); ``None`` until written or after the state changed.
    shown: str | None = None
    #: The concrete next step to continue it, said to the user (migration 35); ``None`` until written.
    next: str | None = None
    #: Who closed or dismissed it last (``user`` | ``model`` | ``code``) and when (migration 35); ``None`` when open.
    closed_by: str | None = None
    closed_at: float | None = None


@dataclass
class WorkOp:
    """What one journal batch (or the backfill) does to one thread of work, validated by
    :mod:`yuki.memory.workthreads`; applied by :meth:`Store.commit_batch` in the batch's transaction.

    ``fact_indexes``: the batch's facts (their index in its fact list) that belong to the thread;
    ``journal_ids``: the same, for facts already stored (the backfill).
    """

    action: str                          # starts | continues | pauses | ends
    work_thread_id: int | None           # None: a new thread (starts)
    title: str = ""
    state: str = ""
    open_items: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    kind: str | None = None
    at: float = 0.0
    fact_indexes: list[int] = field(default_factory=list)
    journal_ids: list[int] = field(default_factory=list)
    capture_thread_id: int | None = None
    app: str | None = None
    host: str | None = None
    importance: int | None = None
    origin: str = "journal"
    vector: np.ndarray | None = None
    embed_model: str | None = None
    #: The attention its sources had (:func:`capture_attention`): ``front`` when any was in front.
    attention: str = "front"
    #: When memory saw its sources (their newest capture time), ``None`` when not known (the backfill): a thread is
    #: never dated after it was seen (a list row or calendar line printed "08 Nov" is a date inside the content,
    #: not when the user worked on it; revamp A9).
    seen_at: float | None = None
    #: What the store made of it (set in the transaction): the thread id written, and ``started`` |
    #: ``continued`` | ``paused`` | ``ended`` | ``reopened`` | ``rejected:<why>``.
    result_id: int | None = None
    outcome: str = ""


@dataclass
class LinkSeen:
    """One page the user had in front, or a link shared in a conversation (decrypted), migration 23."""

    id: int
    url: str
    title: str | None
    host: str | None
    app: str | None
    how: str                             # viewed | shared
    sender: str | None
    where: str | None
    page_key: str | None
    capture_thread_id: int | None
    work_thread_id: int | None
    by_yuki: bool
    first_seen: float
    last_seen: float
    visits: int
    score: float | None = None
    #: ``None``: the user had it in front; ``visible``: only ever on screen beside the window in front (migration 24).
    attention: str | None = None


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
    "work", "interest", "person", "routine", "behaviour", "preference", "personality", "open_loop",
)
CORRECTION_KIND = "correction"
#: How a name alias of Yuki's OWN name starts its ``why`` (report 88: "UK" heard for Yuki). Such a form is for the
#: wake word and the voice's fast path only (``Store.aliases(name_forms=True)``); every other reader of the aliases
#: (call digests, the correction pass, questions, the people index) never sees it, so the country UK stays the country.
NAME_FORM_WHY = "speech recognition heard Yuki's name so"
#: Yuki's own name, as a name alias's ``name`` (second review V2-2: an alias of Yuki's name written before the name
#: forms had their ``why`` mark, e.g. a typed correction, is a name form by its data all the same).
WAKE_NAME = "Yuki"


def is_name_form(alias: dict[str, Any]) -> bool:
    """Whether a name alias is a heard form of Yuki's own name: its ``why`` carries :data:`NAME_FORM_WHY`, or its
    ``name`` is Yuki's (case and punctuation aside). Data, never words of the heard form."""
    if str(alias.get("why") or "").startswith(NAME_FORM_WHY):
        return True
    words = "".join(ch if ch.isalnum() else " " for ch in str(alias.get("name") or "").casefold()).split()
    return words == [WAKE_NAME.casefold()]
#: What a journal item is (migration 14; yuki.memory.personality): told as true, how the user is,
#: an anecdote they told, a joke or bit (never a fact), possibly misheard or ambiguous (never a fact).
JOURNAL_NATURES: tuple[str, ...] = ("fact", "trait", "story", "humor", "uncertain")
#: The parts of the portrait's Personality section a ``personality`` fact belongs to.
PERSONALITY_ASPECTS: tuple[str, ...] = ("talk", "cares", "joke", "story", "people")


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
    #: ``personality`` facts: which part of the Personality section it is
    #: (yuki.memory.personality.ASPECTS: talk | cares | joke | story | people).
    aspect: str | None = None
    #: 1-10, how much it matters for helping the user (migration 17): written by the model that
    #: wrote the fact, moved by the nightly dreaming pass; ``None`` for facts written before.
    importance: int | None = None
    #: The app's scannable line for the fact (migration 21, :mod:`yuki.memory.titles`): at most eight
    #: words, model-written with the fact (an ``open_loop``: its three-to-six-word title); ``None`` until
    #: written (the titles pass fills it).
    summary: str | None = None
    #: The fact as the app shows it, said to the user (migration 33, :data:`yuki.memory.titles.SHOWN_RULES`);
    #: ``None`` until written.
    shown: str | None = None
    #: The user's pin (migration 33): protected from the model's rewrites (it changes only on a correction or
    #: later evidence). Separate from ``origin`` ("you told me").
    pinned: bool = False
    #: When the user answered it in the "To review" queue (migration 33), or ``None``.
    reviewed_at: float | None = None


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
    aspect: str | None = None
    #: 1-10; ``None`` on an UPDATE keeps the old version's.
    importance: int | None = None
    #: The new version's short line (migration 21); ``None``: the titles pass writes it later.
    summary: str | None = None
    #: The new version as the app shows it, said to the user (migration 33); ``None``: the titles pass writes it.
    shown: str | None = None


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
    #: The page's sections as fields (migration 27; ``yuki.memory.portrait.PORTRAIT_FIELDS``), ``None`` for a page
    #: rendered before.
    sections: dict[str, str] | None = None
    #: The personal brief written by the same render (``yuki.memory.brief``: with_him, people, own_bits, now, routine,
    #: careful), stored inside the sections' JSON under :data:`BRIEF_SECTION`; ``None`` for a page rendered before it.
    brief: dict[str, Any] | None = None


#: The key the personal brief is kept under inside a portrait's sections JSON (no column of its own: migration-free).
BRIEF_SECTION = "_brief"

#: The card context noted by the card writers in this thread (``note_card_context``), written with the next card
#: stored from it (:meth:`Store.record_checkin`): which brief lines and memory items the card was written from.
_CARD_CONTEXT = threading.local()
#: A noted context older than this when the card is stored belongs to a look that never made a card.
CARD_CONTEXT_MAX_AGE_S = 300.0


def note_card_context(brief_ids: Sequence[str] = (), fact_ids: Sequence[str] = (), *,
                      brief_at: float | None = None) -> None:
    """Note, for the card about to be written in this thread, the ids of the brief lines and memory items its
    writer was shown (revamp 09 P4.1). Ids only; never raises."""
    try:
        prev = getattr(_CARD_CONTEXT, "value", None)
        fresh = prev is not None and time.time() - prev["noted"] <= CARD_CONTEXT_MAX_AGE_S
        brief = list(dict.fromkeys([*(prev["brief_ids"] if fresh else []), *(str(i) for i in brief_ids if i)]))
        facts = list(dict.fromkeys([*(prev["fact_ids"] if fresh else []), *(str(i) for i in fact_ids if i)]))
        _CARD_CONTEXT.value = {"noted": time.time(), "brief_ids": brief, "fact_ids": facts,
                               "brief_at": brief_at if brief_at is not None else (prev or {}).get("brief_at")}
    except Exception:
        pass


def _take_card_context() -> dict[str, Any] | None:
    value = getattr(_CARD_CONTEXT, "value", None)
    _CARD_CONTEXT.value = None
    if value is None or time.time() - value["noted"] > CARD_CONTEXT_MAX_AGE_S:
        return None
    return value


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
    #: Three to six words naming what it does (migration 21, :mod:`yuki.memory.titles`); ``None`` until written.
    title: str | None = None
    #: The way that works, in at most eight words (migration 21); ``None`` until written.
    summary: str | None = None


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
    #: Its portrait fact's short title (migration 21: the ``open_loop`` fact's summary); ``None`` until written.
    title: str | None = None


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
    #: Yuki was acting on the desktop for the user during this stretch
    #: (yuki.memory.acting): the window in front was Yuki's doing, at the
    #: user's request ``yuki_request``, not the user's own activity.
    by_yuki: bool = False
    yuki_request: str | None = None
    #: Power rows only (``state`` off | asleep | unknown, yuki.memory.power):
    #: what Windows' own power history says the span was ("shutdown",
    #: "sleep", "modern_standby", "unexpected_shutdown", "no_record").
    source: str | None = None


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
    #: The session's short title and its concise summary said to the user (migration 35), or ``None``.
    title: str | None = None
    summary: str | None = None


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
    #: A few words naming the stretch, and one or two short sentences said to the user (migration 35), or ``None``.
    title: str | None = None
    summary: str | None = None


#: Kinds of conversation facts (``conversation_facts.kind``). ``rule`` and
#: ``preference`` are the user's own words about how Yuki should talk or
#: behave; ``commitment`` is something Yuki is to do later. ``feedback`` is what the
#: user said back to one of Yuki's cards (``nudge_id``), or about the coach's cards in
#: general: it bears on that kind of card, never on everything Yuki says (a "stfu" to a
#: wrong call-back is not a gag on the coach). ``fact`` is reserved: durable facts about
#: the user from conversations go to the journal instead.
#: ``resource`` (migration 22): one of the user's own sites, accounts, emails, repos or project links, with its
#: confirmed spelling (``value``), e.g. their portfolio "janedoe.dev".
CONVERSATION_FACT_KINDS: tuple[str, ...] = ("rule", "preference", "commitment", "feedback", "fact", "resource")
#: The kinds the conversation worker maintains (and ``standing_context`` shows).
STANDING_KINDS: tuple[str, ...] = ("rule", "preference", "commitment", "feedback", "resource")
#: ``conversation_facts.status``: active (a rule in force, an open commitment) or how it ended.
CONVERSATION_FACT_STATUSES: tuple[str, ...] = ("active", "done", "revoked", "superseded", "dropped", "wrong")
#: How the user can end one of their to-dos or one of Yuki's commitments by hand (migration 18): done,
#: not needed any more ("dropped"), or never a real to-do - captured by mistake ("wrong"; the extractor sees
#: it among the recently ended items and does not capture the like again).
TODO_END_STATUSES: tuple[str, ...] = ("done", "dropped", "wrong")
#: A commitment's owner (migration 18): Yuki's own promise, or the user's own plan / to-do.
COMMITMENT_OWNERS: tuple[str, ...] = ("yuki", "user")


#: The values of ``conversation_turns.addressed`` (migration 18; "unanswered" since report 77b, no schema change:
#: the column is free text and older rows keep theirs).
ADDRESSED = ("yuki", "others", "unanswered")


@dataclass
class ConversationTurn:
    """One exchange between the user and Yuki (decrypted), see :meth:`Store.add_turn`.

    ``user_text`` is the user's words (with a card in brackets before them when they replied to one); the notes Yuki
    put after them are ``envelope`` (migration 27; read apart from older rows by :func:`split_envelope`), and
    :attr:`full_text` is the request as it was sent.
    """

    id: int
    session_id: str
    request_id: int
    at: float
    user_text: str
    reply_text: str | None
    actions: list[str]
    outcome: str | None
    extracted: bool
    attempts: int
    summary_id: int | None
    created_at: float
    score: float | None = None       # cosine, set by search_turns
    #: Who a voice turn was said to (migration 18): "yuki", "others" (the people on a call, dictation into
    #: another app), "unanswered" (in a call, the next words came before Yuki answered: nobody judged it, report
    #: 77b), or None when not known (typed turns are to Yuki).
    addressed: str | None = None
    #: Yuki's own notes after the user's words (migration 27), or ``None``.
    envelope: str | None = None

    @property
    def full_text(self) -> str:
        """The request as Yuki sent it: the user's words and Yuki's notes after them."""
        return (self.user_text or "") + (self.envelope or "")


@dataclass
class ConversationFact:
    """One version of a rule, preference or commitment (bi-temporal).

    ``valid_from``/``valid_to``: when it was in force (from the exchanges);
    ``created_at``/``expired_at``: when the store learned it / learned it ended.
    ``quote`` is the user's own words for rules and preferences.
    """

    id: int
    kind: str
    subject: str
    text: str
    quote: str | None
    status: str                      # active | done | revoked | superseded
    valid_from: float
    valid_to: float | None
    due_at: float | None
    source_turn_ids: list[int]
    origin: str                      # model (conversation worker) | user (remember_rule)
    created_at: float | None
    expired_at: float | None
    superseded_by: int | None
    end_note: str | None             # the words or reason that ended it
    score: float | None = None
    #: feedback: the card (``nudges.id``) the user was replying to; None for feedback on the coach in general.
    nudge_id: int | None = None
    #: commitment (migration 18): "yuki" (what Yuki promised or was handed to do), "user" (the user's own plan
    #: or to-do), or None on older rows until the conversation worker judges them.
    owner: str | None = None
    #: commitment: its three-to-six-word title (migration 21, :mod:`yuki.memory.titles`); ``None`` until written.
    title: str | None = None
    #: resource (migration 22): the identifier itself, as confirmed ("janedoe.dev", "jane@janedoe.dev").
    value: str | None = None
    #: resource: the words speech recognition must hear right ("janedoe"), model-chosen.
    keyterms: list[str] = field(default_factory=list)
    #: commitment (migration 37): when the user last put it back after it ended (Not done yet, Put it back, Undo);
    #: memory closes it again only on evidence dated after this. ``None``: never put back.
    reopened_at: float | None = None


@dataclass
class ConversationChange:
    """One validated conversation-fact operation for :meth:`Store.commit_conversation_batch`.

    ``op``: ADD, UPDATE (supersede ``fact_id``), INVALIDATE (end ``fact_id`` as
    ``end_as`` = done | revoked). ``at`` is the evidence time (the exchange).
    """

    op: str
    fact_id: int | None = None
    kind: str = ""
    subject: str = ""
    text: str = ""
    quote: str | None = None
    due_at: float | None = None
    source_turn_ids: list[int] = field(default_factory=list)
    at: float | None = None
    end_as: str | None = None
    end_note: str | None = None
    origin: str = "model"
    vector: np.ndarray | None = None
    embed_model: str | None = None
    nudge_id: int | None = None
    #: commitment: "yuki" | "user" (migration 18); None keeps an UPDATE's old owner.
    owner: str | None = None
    #: commitment: the new version's title (migration 21); None: the titles pass writes it later.
    title: str | None = None
    #: resource (migration 22): the identifier and the words speech recognition must hear right.
    value: str | None = None
    keyterms: list[str] = field(default_factory=list)


@dataclass
class SessionSummary:
    """A summary of one conversation session (a run of exchanges without a 30-minute gap)."""

    id: int
    session_id: str
    started_at: float
    ended_at: float
    text: str
    turn_count: int
    created_at: float
    score: float | None = None


@dataclass
class ConversationCall:
    """Content-free accounting for one conversation-worker model call."""

    at: float
    purpose: str                     # extract | summary
    model: str
    session_id: str | None = None
    turn_count: int = 0
    input_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    stop_reason: str | None = None
    ops_applied: int = 0
    ops_noop: int = 0
    ops_rejected: int = 0
    journal_facts: int = 0
    outcome: str = "ok"              # ok | error
    error: str | None = None


@dataclass
class TodoItem:
    """A to-do the user added themselves (``todos``, decrypted)."""

    id: int
    text: str
    due_at: float | None
    due_all_day: bool
    status: str                      # open | done
    created_at: float
    done_at: float | None
    #: Three to six words (migration 21, :mod:`yuki.memory.titles`); ``None`` until written.
    title: str | None = None


#: ``nudges.kind``: praise (encouragement, an acknowledgement), nudge (a call-back,
#: a call-out, a break suggestion), reminder (a due to-do or commitment), review
#: (the teaser of a new weekly review, yuki.memory.review; ``ref`` = "review:<id>")
#: or resume (picking up after the PC was off or asleep; ``ref`` = "resume:<gap end>"), question, or notice
#: (a plain observation, maybe with one offer, yuki.memory.presence; ``trigger`` = its channel, ``ref`` = what it
#: was about, ``inputs.offer`` = the instruction its "Do it" runs).
NUDGE_KINDS: tuple[str, ...] = ("praise", "nudge", "reminder", "review", "resume", "question", "notice")
#: ``timeline.state`` of the power rows (yuki.memory.power): the PC itself was
#: off (shutdown to start), asleep (sleep to wake) or stopped without a clean
#: shutdown (unknown: a crash, a power cut). Not the user's stretches.
POWER_STATES: tuple[str, ...] = ("off", "asleep", "unknown")
#: ``nudges.reaction`` values the UI reports (``expired``: never shown in time). ``accepted``: the user pressed
#: "Do it" on the card's offer (it ran as their instruction); ``less``: "Less of these" (feedback on that kind of
#: card: fewer of them, never a mute of the rest).
NUDGE_REACTIONS: tuple[str, ...] = ("shown", "dismissed", "replied", "snoozed", "accepted", "less")


@dataclass
class NudgeRecord:
    """One nudge written for the UI (decrypted)."""

    id: int
    at: float
    kind: str
    trigger: str | None
    text: str
    reason: str | None
    inputs: dict[str, Any]
    ref: str | None
    due_at: float | None
    checkin_id: int | None
    shown_at: float | None
    reaction: str | None
    reacted_at: float | None
    #: What the user wrote back when they replied to the card (their words), if known.
    reply: str | None = None
    #: What came of the card's typed offer after the click (migration 26, :data:`NUDGE_OUTCOMES`), and when.
    outcome: str | None = None
    outcome_at: float | None = None


#: ``nudges.outcome``: what came of a card's typed offer (yuki.memory.offers) - ``done`` (it ran), ``undone`` (the
#: user pressed Undo), ``prefilled`` (an ``ask`` suggestion went into the ask bar), ``stopped`` (the request sent
#: from it was stopped within a minute), ``abandoned`` (the ask bar closed without sending it), ``expired`` (a click
#: after the offer's time ran out: nothing ran).
NUDGE_OUTCOMES: tuple[str, ...] = ("done", "undone", "prefilled", "stopped", "abandoned", "expired")


@dataclass
class NudgeCheckin:
    """Content-free accounting for one coach check-in (one model call, or a skipped one)."""

    at: float
    trigger: str                     # periodic | transition | follow_up | reminder
    outcome: str                     # none | praise | nudge | reminder | suppressed:<why> | skipped:<why> | error
    model: str | None = None
    input_chars: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float | None = None
    latency_ms: float = 0.0
    stop_reason: str | None = None
    error: str | None = None
    #: The coach's one-line why, for every outcome (none included): stored encrypted.
    reason: str | None = None


@dataclass
class NewNudge:
    """A nudge to write with its check-in (:meth:`Store.record_checkin`)."""

    kind: str
    text: str
    reason: str | None = None
    inputs: dict[str, Any] = field(default_factory=dict)
    trigger: str | None = None
    ref: str | None = None
    due_at: float | None = None


@dataclass
class WeeklyReview:
    """One weekly review run (decrypted), see :mod:`yuki.memory.review`."""

    id: int
    at: float
    finished_at: float | None
    week: str                        # ISO week of the period's last day, "2026-W39"
    period_start: float
    period_end: float
    trigger: str                     # scheduled | demand
    model: str | None
    text: str | None
    sections: dict[str, Any]
    teaser: str | None
    numbers: dict[str, Any]
    candidates: list[dict[str, Any]]
    candidates_run_id: int | None
    nudge_id: int | None
    outcome: str                     # running | ok | empty | error
    error: str | None
    cost_usd: float | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    score: float | None = None       # cosine, set by search_reviews


@dataclass
class ReviewStats:
    """Content-free accounting for one weekly review run (all its model calls)."""

    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    stop_reason: str | None = None


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


#: Capture / thread kind of a call's transcript (yuki.memory.calls), transcribed on this PC. The name is from the
#: time only the user's microphone was recorded (24 Sept-3 Oct 2026) and stays, because stored captures and
#: threads carry it. Since 3 Oct a capture holds both sides: ``utterances`` the user's own lines (what every older
#: reader takes as "the user said"), ``lines`` every line of every track in time order, each with ``who``
#: (user | others), ``app`` and ``track_kind`` (call | game) - see :meth:`Store.call_lines`.
CALL_SIDE_KIND = "my_call_side"
#: The same kind under a name that says what it is now.
CALL_TRANSCRIPT_KIND = CALL_SIDE_KIND
#: ``calls.status`` values of a call that is still going on. ``ask_later``: a game's voice chat whose consent card
#: waits for a moment it can be seen (not over a full-screen game).
CALL_OPEN_STATUSES: tuple[str, ...] = ("asking", "recording", "waiting_headphones", "declined", "unanswered",
                                       "not_asked", "stopped", "ask_later")
#: ``calls.kind``: a call (an app's voice call) or a game (a game's own voice chat).
CALL_KINDS: tuple[str, ...] = ("call", "game")


def user_side_text(kind: str | None, text: str | None) -> str | None:
    """A capture's text as the generic readers may show it: a call transcript (:data:`CALL_SIDE_KIND`) without its
    ``lines`` - only the user's own ``utterances``, as every capture read before 3 Oct 2026 - so the other people's
    words reach no prompt until the call readers are taught who spoke (report 61). Other kinds unchanged."""
    if kind != CALL_SIDE_KIND or not text:
        return text
    try:
        data = json.loads(text)
    except ValueError:
        return text
    if not isinstance(data, dict) or "lines" not in data:
        return text
    return json.dumps({k: v for k, v in data.items() if k != "lines"}, ensure_ascii=False)
#: ``call_chunks.side`` / an utterance's ``who``: the user's microphone, or the app's own audio (the other people).
CALL_SIDES: tuple[str, ...] = ("user", "others")
#: Answers the user can give on the consent card (``always``/``never`` are also kept per app).
CALL_ANSWERS: tuple[str, ...] = ("yes", "no", "always", "never", "none")
#: ``calls`` columns :meth:`Store.update_call` may set (plumbing whitelist).
_CALL_COLUMNS: frozenset[str] = frozenset({
    "status", "consent", "consent_by", "consent_at", "output_form_factor", "output_device", "headphones",
    "recording_started_at", "recording_stopped_at", "stop_requested", "stop_reason", "ended_at", "thread_id",
    "chunks", "chunks_silent", "speech_s", "audio_s", "utterances", "transcribe_ms", "stt_model", "stt_device",
    "captures", "via", "hold_since", "last_alive_at",
})


@dataclass
class CallRecord:
    """One call the user was in (content-free: no audio, no words; see ``calls``)."""

    id: int
    app: str                          # display name ("Zoom", "Discord")
    process: str | None               # app_key of the process holding the microphone
    service: str | None               # meeting service when a timeline meeting row named it
    via: str                          # mic | meeting (older rows) | screen | screen_stale (the evidence) | game
    detected_at: float
    ended_at: float | None
    status: str                       # asking | ask_later | recording | waiting_headphones | declined | unanswered | not_asked | stopped | ended
    consent: str | None               # yes | no | always | never | none
    consent_by: str | None            # card | pref | quiet | ui_timeout | call_ended
    consent_at: float | None
    output_form_factor: str | None    # headphones | headset | speakers | ...
    output_device: str | None
    headphones: bool | None
    recording_started_at: float | None
    recording_stopped_at: float | None
    stop_requested: bool
    stop_reason: str | None
    thread_id: int | None
    chunks: int
    chunks_silent: int
    speech_s: float
    audio_s: float
    utterances: int
    transcribe_ms: float
    stt_model: str | None
    stt_device: str | None
    captures: int
    updated_at: float
    #: The start of the microphone hold the call is in (the ConsentStore record's ``since``; moved on a re-grab of
    #: the same call), migration 26; None for older rows.
    hold_since: float | None = None
    #: The last moment the call worker saw the call alive (at most 15 s stale while it runs), migration 26.
    last_alive_at: float | None = None
    #: ``call`` | ``game`` (a game's own voice chat), migration 34; older rows are calls.
    kind: str = "call"


#: ``questions.kind``: what memory does not know - who a person is to the user,
#: what a name (product, project, place, organisation) is, whose task or material
#: something is, or another fact it would otherwise have to guess.
QUESTION_KINDS: tuple[str, ...] = ("person", "meaning", "ownership", "fact", "personality")
#: ``questions.status``: open (waiting to be asked), asked (on a card), answered,
#: skipped / never (the user's choice on the card or in words), expired (never
#: asked in time) or dropped (memory turned out to know it already).
QUESTION_STATUSES: tuple[str, ...] = ("open", "asked", "answered", "skipped", "never", "expired", "dropped")
#: A question whose "subject: question" embedding is at least this close to an
#: existing one is the same question. Measured 2026-09-25 (multilingual MiniLM):
#: the same question reworded 0.73-0.78 ("Bro" / "Bro (WhatsApp)", "Ravi" /
#: "Ravi2099", two wordings of the OpenSchool one), different people and names at
#: most 0.64 (questions about two different people).
QUESTION_DUPLICATE_COSINE = 0.70


def question_subject_key(cipher: FieldCipher, subject: str) -> str:
    """The HMAC identity of a question's subject: casefolded, whitespace and edge punctuation squashed."""
    norm = " ".join((subject or "").casefold().split()).strip(" \"'.,;:!?()[]{}")
    return cipher.digest("question", norm)


@dataclass
class NewQuestion:
    """A candidate question for the user (:meth:`Store.add_questions`), from the journal,
    conversation memory or the portrait."""

    at: float                         # the evidence's time
    kind: str                         # QUESTION_KINDS
    subject: str                      # the unknown, as shown ("Bro", "Fable")
    question: str                     # neutral, third person ("Who is Bro to the user?")
    evidence: str                     # what memory saw, briefly ("they talk about football plans on WhatsApp")
    reason: str                       # what memory would otherwise have to guess
    importance: int                   # 1-10
    origin: str                       # journal | conversation | portrait
    source_ids: list[int] = field(default_factory=list)   # journal ids (turn ids for conversation)
    fact_ids: list[int] = field(default_factory=list)     # portrait facts it bears on
    vector: np.ndarray | None = None  # embedding of "subject: question"
    embed_model: str | None = None
    #: Journal only: indexes (into the batch's facts) of the facts from the same source; their
    #: journal ids become ``source_ids`` inside the batch's transaction.
    fact_indexes: list[int] = field(default_factory=list)


@dataclass
class QuestionRecord:
    """One stored question (decrypted)."""

    id: int
    at: float
    created_at: float
    kind: str
    subject: str
    question: str
    evidence: str | None
    reason: str | None
    importance: int
    origin: str
    source_ids: list[int]
    fact_ids: list[int]
    status: str
    seen: int
    nudge_id: int | None
    asked_at: float | None
    answered_at: float | None
    answer: str | None
    answer_fact_id: int | None
    updated_at: float
    #: A check-in (origin "checkin", migration 30): its moment (yuki.memory.checkins.MOMENTS), the goal it is about,
    #: when its answer was folded into the goals, and how the user took being asked (welcome | neutral | unwelcome |
    #: skipped | never), judged by the fold-in's model or read from their Skip / "Don't ask".
    moment: str | None = None
    goal_id: int | None = None
    folded_at: float | None = None
    reply_tone: str | None = None

    @property
    def ref(self) -> str:
        return f"question:{self.id}"


#: ``goals.scope``: a goal for one day (``day`` says which: "today's goals" on that day), a thread of work's goal
#: (``work_thread_id``), or a longer goal of the user's with no thread (a project, a plan).
GOAL_SCOPES: tuple[str, ...] = ("day", "thread", "project")
#: ``goals.status``: open (they are on it), done, dropped, unknown (memory inferred it from a thread, a plan or a
#: commitment and the user has not said yet).
GOAL_STATUSES: tuple[str, ...] = ("open", "done", "dropped", "unknown")
#: ``goals.source``: where the goal came from - the user's answer to a check-in, a stated plan in a call digest, a
#: commitment (a to-do of theirs), or a thread of work's open item.
GOAL_SOURCES: tuple[str, ...] = ("answer", "call", "commitment", "thread")


@dataclass
class Goal:
    """One of the user's goals (decrypted), see :mod:`yuki.memory.checkins`."""

    id: int
    scope: str                        # GOAL_SCOPES
    day: str | None                   # "YYYY-MM-DD" for a day goal
    work_thread_id: int | None
    text: str                         # the goal, in the user's words when they stated it
    words: str | None                 # the user's own words it rests on (a quote of their answer), if any
    source: str                       # GOAL_SOURCES
    source_ref: str | None            # question:12 | commit:5 | D50.3 | W12
    status: str                       # GOAL_STATUSES
    evidence: list[str]               # ids it rests on (machine formats)
    due_at: float | None
    next_checkin_at: float | None
    last_checkin_at: float | None
    checkins: int
    created_at: float
    updated_at: float
    ended_at: float | None = None

    @property
    def ref(self) -> str:
        return f"G{self.id}"


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
    # 6: conversation memory - Yuki's own exchanges with the user, the rules,
    # preferences and commitments extracted from them (bi-temporal), session
    # summaries, their vectors and the worker's accounting
    # (yuki.memory.conversations). Additive only, like 2-5.
    """
    CREATE TABLE conversation_batches (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,
        purpose            TEXT NOT NULL,           -- extract | summary
        session_id         TEXT,
        model              TEXT NOT NULL,
        turn_count         INTEGER NOT NULL DEFAULT 0,
        input_chars        INTEGER NOT NULL DEFAULT 0,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL,
        latency_ms         REAL NOT NULL DEFAULT 0,
        stop_reason        TEXT,
        ops_applied        INTEGER NOT NULL DEFAULT 0,
        ops_noop           INTEGER NOT NULL DEFAULT 0,
        ops_rejected       INTEGER NOT NULL DEFAULT 0,
        journal_facts      INTEGER NOT NULL DEFAULT 0,
        outcome            TEXT NOT NULL,           -- ok | error
        error              TEXT
    );
    CREATE INDEX conversation_batches_at ON conversation_batches(at);

    CREATE TABLE conversation_turns (
        id                 INTEGER PRIMARY KEY,     -- allocated by the writer (microsecond clock)
        session_id         TEXT NOT NULL,           -- Yuki's session id, cleartext metadata
        request_id         INTEGER NOT NULL,
        at                 REAL NOT NULL,
        user_ciphertext    TEXT NOT NULL,
        reply_ciphertext   TEXT,
        actions_ciphertext TEXT,                    -- JSON list of one-line tool summaries
        outcome            TEXT,                    -- cleartext (done, cancelled, error, ...)
        extracted          INTEGER NOT NULL DEFAULT 0,
        extract_attempts   INTEGER NOT NULL DEFAULT 0,
        extract_batch_id   INTEGER,
        summary_id         INTEGER,                 -- session_summaries.id once summarised
        summary_attempts   INTEGER NOT NULL DEFAULT 0,
        created_at         REAL NOT NULL
    );
    CREATE INDEX conversation_turns_session ON conversation_turns(session_id, at);
    CREATE INDEX conversation_turns_pending ON conversation_turns(extracted, at);
    CREATE INDEX conversation_turns_at ON conversation_turns(at);
    CREATE INDEX conversation_turns_summary ON conversation_turns(summary_id);

    CREATE TABLE conversation_facts (
        id                 INTEGER PRIMARY KEY,
        kind               TEXT NOT NULL,           -- rule | preference | commitment | fact
        subject_ciphertext TEXT,
        text_ciphertext    TEXT NOT NULL,
        quote_ciphertext   TEXT,                    -- the user's own words (rules, preferences)
        status             TEXT NOT NULL,           -- active | done | revoked | superseded
        valid_from         REAL NOT NULL,
        valid_to           REAL,
        due_at             REAL,                    -- commitments: when it is due, if known
        source_turn_ids    TEXT,                    -- JSON list of conversation_turns ids
        origin             TEXT NOT NULL DEFAULT 'model',   -- model | user
        created_at         REAL NOT NULL,
        expired_at         REAL,
        superseded_by      INTEGER,
        end_note_ciphertext TEXT,                   -- the words or reason that ended it
        batch_id           INTEGER
    );
    CREATE INDEX conversation_facts_status ON conversation_facts(status, kind);

    CREATE TABLE session_summaries (
        id              INTEGER PRIMARY KEY,
        session_id      TEXT NOT NULL,
        started_at      REAL NOT NULL,
        ended_at        REAL NOT NULL,
        turn_count      INTEGER NOT NULL DEFAULT 0,
        text_ciphertext TEXT NOT NULL,
        batch_id        INTEGER,
        created_at      REAL NOT NULL
    );
    CREATE INDEX session_summaries_span ON session_summaries(started_at, ended_at);
    CREATE INDEX session_summaries_session ON session_summaries(session_id, ended_at);

    CREATE TABLE conversation_turn_vec (
        turn_id         INTEGER PRIMARY KEY REFERENCES conversation_turns(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    CREATE TABLE session_summary_vec (
        summary_id      INTEGER PRIMARY KEY REFERENCES session_summaries(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    CREATE TABLE conversation_fact_vec (
        fact_id         INTEGER PRIMARY KEY REFERENCES conversation_facts(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    """,
    # 7: Yuki's own actions (yuki.memory.acting). Captures and timeline
    # stretches made while Yuki was carrying out a request with its hands are
    # marked by_yuki, with the request's text; journal facts drawn from them
    # carry the mark, so the portrait never takes them for the user's own
    # activity. Additive only, like 2-6.
    """
    ALTER TABLE captures ADD COLUMN by_yuki INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE captures ADD COLUMN yuki_request_ciphertext TEXT;
    ALTER TABLE timeline ADD COLUMN by_yuki INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE timeline ADD COLUMN yuki_request_ciphertext TEXT;
    ALTER TABLE journal ADD COLUMN by_yuki INTEGER NOT NULL DEFAULT 0;
    """,
    # 8: the coach (yuki.memory.nudges) - to-dos the user added themselves,
    # the nudges written for the UI (praise, nudge, reminder) with the user's
    # reaction, the content-free accounting of every check-in, and small
    # coach state (the snooze). Additive only, like 2-7.
    """
    CREATE TABLE todos (
        id              INTEGER PRIMARY KEY,
        text_ciphertext TEXT NOT NULL,
        due_at          REAL,                      -- local time as epoch seconds; NULL = no due date
        due_all_day     INTEGER NOT NULL DEFAULT 0, -- 1: due_at is a day (its local midnight), no time
        status          TEXT NOT NULL,             -- open | done
        created_at      REAL NOT NULL,
        done_at         REAL,
        updated_at      REAL NOT NULL
    );
    CREATE INDEX todos_status ON todos(status, due_at);

    CREATE TABLE nudge_checkins (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,
        trigger            TEXT NOT NULL,           -- periodic | transition | follow_up | reminder
        outcome            TEXT NOT NULL,           -- none | praise | nudge | reminder | suppressed:<why> | skipped:<why> | error
        model              TEXT,
        input_chars        INTEGER NOT NULL DEFAULT 0,
        input_tokens       INTEGER NOT NULL DEFAULT 0,
        output_tokens      INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens  INTEGER NOT NULL DEFAULT 0,
        cost_usd           REAL,
        latency_ms         REAL NOT NULL DEFAULT 0,
        stop_reason        TEXT,
        nudge_id           INTEGER,
        error              TEXT
    );
    CREATE INDEX nudge_checkins_at ON nudge_checkins(at);

    CREATE TABLE nudges (
        id                INTEGER PRIMARY KEY,
        at                REAL NOT NULL,
        kind              TEXT NOT NULL,            -- praise | nudge | reminder
        trigger           TEXT,
        text_ciphertext   TEXT NOT NULL,
        reason_ciphertext TEXT,
        inputs_ciphertext TEXT,                     -- JSON: what the decision rested on (activity, numbers, to-dos)
        ref               TEXT,                     -- reminders: the item reminded ("commit:5", "todo:3")
        due_at            REAL,                     -- reminders: the due time reminded (once per item and due)
        checkin_id        INTEGER,
        shown_at          REAL,
        reaction          TEXT,                     -- shown | dismissed | replied | snoozed | expired
        reacted_at        REAL
    );
    CREATE INDEX nudges_at ON nudges(at);
    CREATE INDEX nudges_pending ON nudges(shown_at, reaction, at);
    CREATE INDEX nudges_ref ON nudges(ref, due_at);

    CREATE TABLE nudge_state (
        name       TEXT PRIMARY KEY,                -- e.g. quiet_until
        value      TEXT NOT NULL,
        updated_at REAL NOT NULL
    );
    """,
    # 9: the weekly review (yuki.memory.review) - one row per run, the review
    # text and the numbers it rests on encrypted, the behaviour-pattern
    # candidates it proposes for the portrait (consumed by the next portrait
    # run), the teaser nudge it was delivered with, and its embedding for
    # recall. Additive only, like 2-8.
    """
    CREATE TABLE weekly_reviews (
        id                    INTEGER PRIMARY KEY,
        at                    REAL NOT NULL,        -- started
        finished_at           REAL,
        week                  TEXT NOT NULL,        -- ISO week of the period's last day, e.g. 2026-W39
        period_start          REAL NOT NULL,        -- local midnight of the first day
        period_end            REAL NOT NULL,        -- when the numbers stop (the run's now)
        trigger               TEXT NOT NULL,        -- scheduled | demand
        model                 TEXT,
        text_ciphertext       TEXT,                 -- the review as shown
        sections_ciphertext   TEXT,                 -- JSON: about, time, focus, done, still_open, suggestions, mentions
        teaser_ciphertext     TEXT,
        numbers_ciphertext    TEXT,                 -- JSON: the computed numbers (site and app names)
        candidates_ciphertext TEXT,                 -- JSON: behaviour-pattern candidates for the portrait
        candidates_run_id     INTEGER,              -- portrait run that was given them
        nudge_id              INTEGER,              -- the teaser nudge, once delivered
        calls                 INTEGER NOT NULL DEFAULT 0,
        input_tokens          INTEGER NOT NULL DEFAULT 0,
        output_tokens         INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens    INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens     INTEGER NOT NULL DEFAULT 0,
        cost_usd              REAL,
        latency_ms            REAL NOT NULL DEFAULT 0,
        stop_reason           TEXT,
        outcome               TEXT NOT NULL,        -- running | ok | empty | error
        error                 TEXT
    );
    CREATE INDEX weekly_reviews_week ON weekly_reviews(week, at);
    CREATE INDEX weekly_reviews_at ON weekly_reviews(at);

    CREATE TABLE weekly_review_vec (
        review_id       INTEGER PRIMARY KEY REFERENCES weekly_reviews(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    """,
    # 10: the user's own side of calls (yuki.memory.calls) - one content-free
    # row per call (app, times, consent and who gave it, the output device's
    # form factor, chunk and timing counts), and the user's standing
    # "always"/"never" per app. The words themselves are captures of kind
    # my_call_side (encrypted, capture TTL); the audio waits encrypted in the spool (migration 11) until transcribed.
    # Additive only, like 2-9.
    """
    CREATE TABLE calls (
        id                   INTEGER PRIMARY KEY,
        app                  TEXT NOT NULL,
        process              TEXT,
        service              TEXT,
        via                  TEXT NOT NULL,          -- mic | meeting
        detected_at          REAL NOT NULL,
        ended_at             REAL,
        status               TEXT NOT NULL,          -- asking | recording | waiting_headphones | declined | unanswered | not_asked | stopped | ended
        consent              TEXT,                   -- yes | no | always | never | none
        consent_by           TEXT,                   -- card | pref | quiet | ui_timeout | call_ended
        consent_at           REAL,
        output_form_factor   TEXT,
        output_device        TEXT,
        headphones           INTEGER,
        recording_started_at REAL,
        recording_stopped_at REAL,
        stop_requested       INTEGER NOT NULL DEFAULT 0,
        stop_reason          TEXT,
        thread_id            INTEGER,
        chunks               INTEGER NOT NULL DEFAULT 0,
        chunks_silent        INTEGER NOT NULL DEFAULT 0,
        speech_s             REAL NOT NULL DEFAULT 0,
        audio_s              REAL NOT NULL DEFAULT 0,
        utterances           INTEGER NOT NULL DEFAULT 0,
        transcribe_ms        REAL NOT NULL DEFAULT 0,
        stt_model            TEXT,
        stt_device           TEXT,
        captures             INTEGER NOT NULL DEFAULT 0,
        updated_at           REAL NOT NULL
    );
    CREATE INDEX calls_status ON calls(status, detected_at);

    CREATE TABLE call_prefs (
        app        TEXT PRIMARY KEY,                 -- app_key of the call app
        decision   TEXT NOT NULL,                    -- always | never
        name       TEXT,                             -- display name, for the UI
        updated_at REAL NOT NULL
    );
    """,
    # 11: deferred call transcription (yuki.memory.calls / yuki.memory.transcribe).
    # During a call only the user's own speech is kept, as encrypted Opus
    # chunks in the spool folder next to the database; each chunk has a row
    # here (cleartext metadata only). The deferred transcriber writes the
    # chunk's words (encrypted) and deletes its audio in the same step; once a
    # call's chunks are all done its words go to the journal as my_call_side
    # and the rows keep only their accounting. Additive only, like 2-10.
    """
    CREATE TABLE call_chunks (
        id              INTEGER PRIMARY KEY,
        call_id         INTEGER NOT NULL,
        seq             INTEGER NOT NULL,
        start_at        REAL NOT NULL,
        audio_s         REAL NOT NULL DEFAULT 0,
        speech_s        REAL NOT NULL DEFAULT 0,
        file            TEXT,                        -- spool file name while the audio exists
        bytes           INTEGER NOT NULL DEFAULT 0,
        codec           TEXT,                        -- opus | pcm16-zlib
        status          TEXT NOT NULL,               -- spooled | done | journaled | failed | expired | capped | erased | lost
        attempts        INTEGER NOT NULL DEFAULT 0,
        text_ciphertext TEXT,                        -- JSON utterances, until journaled
        lang            TEXT,
        transcribe_ms   REAL,
        stt_model       TEXT,
        stt_device      TEXT,
        error           TEXT,
        created_at      REAL NOT NULL,
        done_at         REAL,
        removed_at      REAL
    );
    CREATE INDEX call_chunks_status ON call_chunks(status, start_at);
    CREATE INDEX call_chunks_call ON call_chunks(call_id, seq);
    """,
    # 12: the PC's own power history (yuki.memory.power) in the timeline: rows
    # with state off | asleep | unknown, read from Windows' System event log,
    # so a hole in the timeline says whether the PC was off, asleep or the
    # service was simply not running. One row per span, unique on its kind and
    # end (re-reading the log never doubles one). Additive only, like 2-11.
    """
    ALTER TABLE timeline ADD COLUMN source TEXT;     -- power rows: shutdown | sleep | modern_standby | unexpected_shutdown | no_record
    CREATE UNIQUE INDEX timeline_power ON timeline(state, ended_at) WHERE state IN ('off', 'asleep', 'unknown');
    """,
    # 13: getting to know the user (yuki.memory.questions) - questions memory
    # would otherwise answer by guessing (who a person is to the user, what a
    # name is, whose task something is), found by the journal, conversation
    # memory or the portrait, asked on a card of kind "question" and answered in
    # the user's own words (the answer becomes a user-origin portrait
    # correction). Text encrypted like everything else; the subject's HMAC and
    # a local embedding dedupe them. Additive only, like 2-12.
    """
    CREATE TABLE questions (
        id                  INTEGER PRIMARY KEY,
        at                  REAL NOT NULL,          -- when the evidence was
        created_at          REAL NOT NULL,
        kind                TEXT NOT NULL,          -- person | meaning | ownership | fact
        subject_ciphertext  TEXT NOT NULL,
        subject_key         TEXT NOT NULL,          -- HMAC(normalised subject), for dedupe
        question_ciphertext TEXT NOT NULL,
        evidence_ciphertext TEXT,
        reason_ciphertext   TEXT,
        importance          INTEGER NOT NULL,
        origin              TEXT NOT NULL,          -- journal | conversation | portrait
        source_ids          TEXT,                   -- JSON: journal ids (turn ids for conversation)
        fact_ids            TEXT,                   -- JSON: portrait facts it bears on
        status              TEXT NOT NULL,          -- open | asked | answered | skipped | never | expired | dropped
        seen                INTEGER NOT NULL DEFAULT 1,
        nudge_id            INTEGER,                -- the card it was asked on
        asked_at            REAL,
        answered_at         REAL,
        answer_ciphertext   TEXT,                   -- the user's own words
        answer_fact_id      INTEGER,                -- the portrait correction the answer became
        updated_at          REAL NOT NULL
    );
    CREATE INDEX questions_status ON questions(status, importance, at);
    CREATE INDEX questions_subject ON questions(subject_key);

    CREATE TABLE question_vec (
        question_id     INTEGER PRIMARY KEY REFERENCES questions(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    """,
    # 14: what each journal item is (yuki.memory.personality): its nature - fact
    # (told as true), trait (how the user is), story (an anecdote they told),
    # humor (a joke, bit, tease or boast: kept as humour, never a fact) or
    # uncertain (possibly misheard or ambiguous: never used as a fact). NULL is a
    # fact recorded before natures existed. A re-journal (yuki.memory.rejournal)
    # supersedes what the same captures produced before, keeping the old rows
    # (superseded_at, superseded_by = the new batch) and recording on the new
    # batch which batches it replaced. The portrait's personality facts carry an
    # aspect (talk | cares | joke | story | people). Additive only, like 2-13.
    """
    ALTER TABLE journal ADD COLUMN nature TEXT;
    ALTER TABLE journal ADD COLUMN superseded_at REAL;
    ALTER TABLE journal ADD COLUMN superseded_by INTEGER;       -- the journal_batches row that replaced it
    CREATE INDEX journal_superseded ON journal(superseded_at);
    ALTER TABLE journal_batches ADD COLUMN supersedes TEXT;     -- JSON: the batch ids a re-journal replaced
    ALTER TABLE portrait_facts ADD COLUMN aspect TEXT;
    """,
    # 15: the portrait audit (yuki.memory.portrait, "connect the dots"): each finding
    # of an audit run - a contradiction, a misattribution (whose account, course,
    # task or document), a relationship, a stale status, an agent's work told as the
    # user's - with the facts and evidence it rests on, the model's reasoning
    # (encrypted), what was decided (resolved | asked | kept | rejected) and the fact
    # versions and questions it produced. The superseded facts keep their history
    # as always (valid_to, superseded_by); this is the why. Additive only, like 2-14.
    """
    CREATE TABLE portrait_audits (
        id                   INTEGER PRIMARY KEY,
        run_id               INTEGER,              -- portrait_runs row (kind 'audit')
        at                   REAL NOT NULL,
        finding              TEXT NOT NULL,        -- contradiction | misattribution | relationship | stale | agent_work
        decision             TEXT NOT NULL,        -- resolved | asked | kept | rejected
        fact_ids             TEXT,                 -- JSON: the facts it is about
        journal_ids          TEXT,                 -- JSON: the evidence
        new_fact_ids         TEXT,                 -- JSON: fact versions written for it
        question_ids         TEXT,                 -- JSON: questions raised for it
        reasoning_ciphertext TEXT
    );
    CREATE INDEX portrait_audits_at ON portrait_audits(at);
    """,
    # 16: the coach listens (docs/research/coach-and-answers-review.md): a reply to one of
    # Yuki's cards is feedback about that kind of card, not a global rule - a conversation
    # fact of kind 'feedback' carries the card it answered (nudge_id); the user's reply is
    # kept on the card itself (reply, encrypted) so the coach reads their words, not a
    # count; every check-in keeps the coach's one-line why (reason, encrypted), so the agent
    # can say why the coach went quiet; a journal fact the extractor judged to record
    # something the user finished, shipped, merged, fixed or sent is a milestone, which
    # wakes the coach. Additive only, like 2-15.
    """
    ALTER TABLE conversation_facts ADD COLUMN nudge_id INTEGER;
    ALTER TABLE nudges ADD COLUMN reply_ciphertext TEXT;
    ALTER TABLE nudge_checkins ADD COLUMN reason_ciphertext TEXT;
    ALTER TABLE journal ADD COLUMN milestone INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX journal_milestone ON journal(milestone, at);
    """,
    # 17: OpenClaw-inspired memory (docs/MEMORY.md, "Fast memory, provenance, dreaming"):
    # - journal.source: where each fact came from (the cited source's kind, who said it, a short
    #   quote, the conversation or page), encrypted JSON, so every answer can say where and when;
    # - journal.tainted: text the user typed into, or read from, another AI tool (judged by the
    #   journal model from the content), never evidence of who the user is;
    # - portrait_facts.importance (1-10, written by the model that writes the fact; the nightly
    #   dreaming pass promotes and demotes it) and portrait_fact_vec, for the per-message recall;
    # - standing_requests: "tell me when Varun replies", checked by the journal's own model call;
    # - yuki_persona: Yuki's own short, evidence-cited "how Yuki talks" block, bi-temporal;
    # - memory_changelog: the visible diary of what dreaming and the persona pass changed;
    # - knowhow.origin: agent (remember_how) or conversation (the post-job extraction).
    # Additive only, like 2-16.
    """
    ALTER TABLE journal ADD COLUMN source_ciphertext TEXT;
    ALTER TABLE journal ADD COLUMN tainted INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE portrait_facts ADD COLUMN importance INTEGER;
    CREATE TABLE portrait_fact_vec (
        fact_id         INTEGER PRIMARY KEY REFERENCES portrait_facts(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    CREATE TABLE standing_requests (
        id                   INTEGER PRIMARY KEY,
        created_at           REAL NOT NULL,
        condition_ciphertext TEXT NOT NULL,       -- what to watch for, as the agent wrote it
        quote_ciphertext     TEXT NOT NULL,       -- the user's own words asking for it
        source_request_ciphertext TEXT,
        status               TEXT NOT NULL,       -- open | fired | cancelled | expired
        max_fires            INTEGER NOT NULL DEFAULT 1,
        fires                INTEGER NOT NULL DEFAULT 0,
        cooldown_s           REAL NOT NULL,
        expires_at           REAL,
        last_fired_at        REAL,
        last_nudge_id        INTEGER,
        ended_at             REAL,
        updated_at           REAL NOT NULL
    );
    CREATE INDEX standing_requests_status ON standing_requests(status, created_at);
    CREATE TABLE yuki_persona (
        id                  INTEGER PRIMARY KEY,
        text_ciphertext     TEXT NOT NULL,
        evidence_ciphertext TEXT,                 -- JSON: turn ids, quotes, card ids it rests on
        valid_from          REAL NOT NULL,
        valid_to            REAL,
        superseded_by       INTEGER,
        run_id              INTEGER,
        created_at          REAL NOT NULL
    );
    CREATE TABLE memory_changelog (
        id              INTEGER PRIMARY KEY,
        at              REAL NOT NULL,
        run_id          INTEGER,
        pass            TEXT NOT NULL,            -- dream | persona
        action          TEXT NOT NULL,            -- merge | retire | promote | demote | add | update | remove
        fact_ids        TEXT,                     -- JSON: the facts (or persona items) it is about
        new_fact_ids    TEXT,                     -- JSON: the versions it wrote
        cites           TEXT,                     -- JSON: journal ids / turn ids it rests on
        text_ciphertext TEXT NOT NULL             -- one human-readable diary line
    );
    CREATE INDEX memory_changelog_at ON memory_changelog(at);
    ALTER TABLE knowhow ADD COLUMN origin TEXT;
    """,
    # 18 (2026-09-27, the user's review of the Yuki window): a correction applies everywhere, to-dos
    # apart from Yuki's own commitments, and talk not meant for Yuki kept out of memory:
    # - correction_passes: one row per user correction the correction-apply pass
    #   (yuki.memory.corrections) has taken up, with its state for the UI ("Updating..." -> done) and
    #   what it changed (encrypted JSON: surface, old id, new id, old and new wording);
    # - name_aliases: a misheard or wrong form of a name and the right one ("Barun" -> "Varun"),
    #   from a correction; given to the journal and extraction models and to speech recognition;
    # - conversation_facts.owner: who a commitment belongs to - "yuki" (Yuki promised or was handed it)
    #   or "user" (the user's own plan or to-do, said to Yuki or on a call); NULL on older rows until
    #   the conversation worker judges them;
    # - conversation_turns.addressed: who a voice turn was said to ("yuki" | "others"), from voice's
    #   turn_closed; NULL when not known (typed turns are to Yuki).
    # Additive only, like 2-17.
    """
    CREATE TABLE correction_passes (
        correction_id     INTEGER PRIMARY KEY,     -- the portrait_facts row of kind correction
        status            TEXT NOT NULL,           -- queued | applying | done | failed | skipped
        queued_at         REAL NOT NULL,
        started_at        REAL,
        finished_at       REAL,
        attempts          INTEGER NOT NULL DEFAULT 0,
        changes_ciphertext TEXT,                   -- JSON list of what changed where
        cost_usd          REAL NOT NULL DEFAULT 0,
        error             TEXT,
        updated_at        REAL NOT NULL
    );
    CREATE INDEX correction_passes_status ON correction_passes(status, queued_at);
    CREATE TABLE name_aliases (
        id                INTEGER PRIMARY KEY,
        heard_key         TEXT NOT NULL,           -- HMAC of the casefolded wrong form
        heard_ciphertext  TEXT NOT NULL,
        name_ciphertext   TEXT NOT NULL,           -- the right name
        why_ciphertext    TEXT,                    -- "speech recognition misheard it", "a nickname", ...
        correction_id     INTEGER,
        created_at        REAL NOT NULL,
        ended_at          REAL
    );
    CREATE INDEX name_aliases_heard ON name_aliases(heard_key);
    ALTER TABLE conversation_facts ADD COLUMN owner TEXT;
    ALTER TABLE conversation_turns ADD COLUMN addressed TEXT;
    """,
    # 19 (2026-09-27, "what happened on my Discord call on Friday" got four lines): one timeline digest per
    # call row (yuki.memory.calldigest), written by Haiku from the call's my_call_side captures once they
    # land: time blocks the model cut where the talk moved, each with short English lines of what the user
    # said, asked, decided, planned or joked about, each dated by the utterance it rests on; plus a one-line
    # gist. Encrypted, never pruned (the transcript it was made from goes with the 30-day capture TTL).
    # capture_ids says which captures it read, so a call that gains words is digested again. Additive only.
    """
    CREATE TABLE call_digests (
        call_id           INTEGER PRIMARY KEY REFERENCES calls(id),
        app               TEXT NOT NULL,
        started_at        REAL,                    -- first utterance of the call
        ended_at          REAL,                    -- end of its last utterance
        speech_s          REAL NOT NULL DEFAULT 0, -- the user's speech: utterance seconds added up
        utterances        INTEGER NOT NULL DEFAULT 0,
        kept              INTEGER NOT NULL DEFAULT 0,  -- utterances shown to the model (exact repeats left out)
        capture_ids       TEXT NOT NULL,           -- JSON: the my_call_side captures it was written from
        status            TEXT NOT NULL,           -- running | ok | empty | failed
        gist_ciphertext   TEXT,                    -- one line: what the call was mostly about, the user's side
        blocks_ciphertext TEXT,                    -- JSON [{start, end, first, last, topic, lines: [{at, text, nature}]}]
        model             TEXT,
        requests          INTEGER NOT NULL DEFAULT 0,
        input_tokens      INTEGER NOT NULL DEFAULT 0,
        output_tokens     INTEGER NOT NULL DEFAULT 0,
        cache_write_tokens INTEGER NOT NULL DEFAULT 0,
        cache_read_tokens INTEGER NOT NULL DEFAULT 0,
        cost_usd          REAL NOT NULL DEFAULT 0,
        latency_ms        REAL NOT NULL DEFAULT 0,
        attempts          INTEGER NOT NULL DEFAULT 0,
        error             TEXT,
        claimed_by        TEXT,                    -- the process writing it while status = running
        claimed_at        REAL,
        created_at        REAL NOT NULL,
        updated_at        REAL NOT NULL
    );
    CREATE INDEX call_digests_started ON call_digests(started_at);
    CREATE INDEX call_digests_updated ON call_digests(updated_at);
    """,
    # 20 (2026-09-27, the smaller embedder): a change of embedding model is a background re-embed
    # (yuki.memory.reembed), never a mix. embed_state holds the model the stored vectors are made with
    # ("active_model"; read by yuki.memory.embed.active_model, so every process embeds with it), the one
    # being moved to ("target_model") and a "generation" bumped at each flip (in-memory vector caches
    # reload on it). vec_next holds the target model's vectors while the old model keeps serving: one per
    # (vector table, row), with a digest of the old vector it replaces (a row re-embedded meanwhile is
    # done again). The flip copies them over in one transaction and empties it. Additive only.
    """
    CREATE TABLE embed_state (
        key             TEXT PRIMARY KEY,        -- active_model | target_model | generation | flipped_at | ...
        value           TEXT NOT NULL,
        updated_at      REAL NOT NULL
    );
    CREATE TABLE vec_next (
        tbl             TEXT NOT NULL,           -- the vector table: journal_vec, episode_vec, ...
        item_id         INTEGER NOT NULL,        -- that table's row id
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL,           -- encrypted float32[dim], L2-normalised
        src_digest      TEXT NOT NULL,           -- digest of the replaced vector's ciphertext when embedded
        created_at      REAL NOT NULL,
        PRIMARY KEY (tbl, item_id)
    );
    """,
    # 21 (2026-09-28, the app's Me and Yuki pages showed first sentences cut off): model-written short labels,
    # encrypted like the text they label (yuki.memory.titles). A portrait fact's summary (at most eight words;
    # an open_loop's is its three-to-six-word title, which its open_loops row and to-do show), a commitment's,
    # a user to-do's and a know-how entry's title (three to six words), and a know-how entry's summary (its way,
    # at most eight words). Written with the item by the model
    # call that writes it where there is one, else by the batched titles pass; NULL until then. Additive only.
    """
    ALTER TABLE portrait_facts ADD COLUMN summary_ciphertext TEXT;
    ALTER TABLE conversation_facts ADD COLUMN title_ciphertext TEXT;
    ALTER TABLE todos ADD COLUMN title_ciphertext TEXT;
    ALTER TABLE knowhow ADD COLUMN title_ciphertext TEXT;
    ALTER TABLE knowhow ADD COLUMN summary_ciphertext TEXT;
    """,
    # 22 (2026-09-28, "open my portfolio" after the user had Yuki open devmafex.com: memory kept nothing): the
    # user's own resources - their sites, accounts and emails, repos, project links - as conversation facts of
    # kind "resource", with the identifier as confirmed (the URL that opened, the user's own correction) and the
    # words speech recognition must hear right, both encrypted. Additive only.
    """
    ALTER TABLE conversation_facts ADD COLUMN value_ciphertext TEXT;
    ALTER TABLE conversation_facts ADD COLUMN keyterms_ciphertext TEXT;
    """,
    # 23 (2026-09-29, "connect the dots from the entire memory", docs/research/human-like-review.md §5 Phase 3):
    # - work_threads: the user's threads of work (a project, task or workflow they pursue over days: "Yuki voice
    #   pipeline", "OpenSchool maths for Ravi", a PR review), each with where it stands, what is open, the apps,
    #   sites, repos and people involved, first and last seen and a status (active | paused | done | merged).
    #   Kept by the journal's own per-batch call (yuki.memory.workthreads) and tidied nightly; bi-temporal in
    #   spirit: nothing is deleted, a merged thread points at the one it went into, every change is an event.
    # - work_thread_events: every time a batch continued, started, paused or ended a thread (and the nightly
    #   merges and closes): when, from which window, page, conversation or call (capture thread), and where the
    #   thread stood after it - the thread's own timeline.
    # - journal.work_thread_id: the thread a journal fact belongs to.
    # - links_seen: pages the user had in front (from the timeline and the capture threads) and links shared in
    #   conversations (a URL in a message, with who sent it), queryable ("that link I was looking at").
    # - work_thread_vec, link_vec, call_digest_vec: local embeddings for the whole-memory ranking.
    # Additive only.
    """
    CREATE TABLE work_threads (
        id                  INTEGER PRIMARY KEY,
        title_ciphertext    TEXT NOT NULL,
        kind                TEXT,                    -- the model's word for it: project | task | study | review | helping | errand | other
        status              TEXT NOT NULL,           -- active | paused | done | merged
        state_ciphertext    TEXT,                    -- where it stands now, one or two sentences
        open_ciphertext     TEXT,                    -- JSON list: what is still to do or waited on
        entities_ciphertext TEXT,                    -- JSON list: apps, sites, repos, files, people involved
        first_seen          REAL NOT NULL,
        last_seen           REAL NOT NULL,
        importance          INTEGER,
        origin              TEXT NOT NULL DEFAULT 'journal',   -- journal | backfill | consolidate
        merged_into         INTEGER,
        events              INTEGER NOT NULL DEFAULT 0,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    );
    CREATE INDEX work_threads_status ON work_threads(status, last_seen);
    CREATE TABLE work_thread_events (
        id               INTEGER PRIMARY KEY,
        work_thread_id   INTEGER NOT NULL,
        at               REAL NOT NULL,              -- the newest source it rests on
        action           TEXT NOT NULL,              -- starts | continues | pauses | ends | reopens | merged | closed | renamed
        batch_id         INTEGER,                    -- the journal batch (NULL for the nightly pass)
        thread_id        INTEGER,                    -- the capture thread: window, page, conversation, terminal folder, call
        app              TEXT,
        host             TEXT,
        state_ciphertext TEXT,                       -- where the thread stood after it
        origin           TEXT NOT NULL DEFAULT 'journal',
        created_at       REAL NOT NULL
    );
    CREATE INDEX work_thread_events_thread ON work_thread_events(work_thread_id, at);
    CREATE INDEX work_thread_events_capture ON work_thread_events(thread_id, at);
    CREATE INDEX work_thread_events_at ON work_thread_events(at);
    CREATE TABLE work_thread_vec (
        work_thread_id  INTEGER PRIMARY KEY REFERENCES work_threads(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    ALTER TABLE journal ADD COLUMN work_thread_id INTEGER;
    CREATE INDEX journal_work_thread ON journal(work_thread_id, at);
    CREATE TABLE links_seen (
        id                INTEGER PRIMARY KEY,
        url_key           TEXT NOT NULL UNIQUE,      -- HMAC(how, the URL)
        url_ciphertext    TEXT NOT NULL,
        title_ciphertext  TEXT,
        host              TEXT,                      -- cleartext metadata, as in threads and the timeline
        app               TEXT,
        how               TEXT NOT NULL,             -- viewed (in front) | shared (in a message)
        sender_ciphertext TEXT,                      -- shared: who sent it, as the app showed them
        where_ciphertext  TEXT,                      -- shared: the conversation it was in
        page_key          TEXT,                      -- viewed: the timeline's page identity (time in front)
        capture_thread_id INTEGER,
        work_thread_id    INTEGER,
        by_yuki           INTEGER NOT NULL DEFAULT 0,
        first_seen        REAL NOT NULL,
        last_seen         REAL NOT NULL,
        visits            INTEGER NOT NULL DEFAULT 0,
        updated_at        REAL NOT NULL
    );
    CREATE INDEX links_seen_last ON links_seen(last_seen);
    CREATE INDEX links_seen_host ON links_seen(host, last_seen);
    CREATE TABLE link_vec (
        link_id         INTEGER PRIMARY KEY REFERENCES links_seen(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    CREATE TABLE call_digest_vec (
        call_id         INTEGER PRIMARY KEY REFERENCES calls(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    """,
    # 24 (2026-09-29, the eyes read every visible window and popup, not only the one in front): how much of the
    # user's attention a journal fact or a link rests on. NULL = the window was in front (the user worked in it);
    # 'visible' = only on screen beside the window in front (trigger visible:<why>; they may not have looked);
    # 'popup' = something that appeared by itself (a toast, a dialog). Weaker evidence of attention, weighed by
    # the whole-memory ranking (yuki.memory.relevant.ATTENTION_DISCOUNT). Additive only.
    """
    ALTER TABLE journal ADD COLUMN attention TEXT;
    ALTER TABLE links_seen ADD COLUMN attention TEXT;
    """,
    # 25 (2026-10-01, "use what's around the call": the user's own half of a call is thin context): what the
    # digest read from the screen around the call (yuki.memory.callcontext) - who was on it, as the call screen
    # showed them, and where (the app, server/channel or meeting title) - as encrypted JSON. Additive only.
    """
    ALTER TABLE call_digests ADD COLUMN context_ciphertext TEXT;
    """,
    # 26 (2026-10-03, one call = one session, docs/research/revamp-2026-10-02/05-call-pill-timer.md §7; typed
    # offers, 06-proactive-do-it.md §4.5): the microphone hold a call row is in (calls.hold_since: the ConsentStore
    # record's start, moved on a re-grab of the same call) and the last moment the call worker saw it alive
    # (calls.last_alive_at, written at most every 15 s), so a restarted service adopts a call whose hold goes on and
    # ends one that did not at its last alive moment, never at the restart; and what came of a card's typed offer
    # (nudges.outcome: done | undone | prefilled | stopped | abandoned | expired, nudges.outcome_at), read by the
    # per-(channel, offer) learning (yuki.memory.offers). Additive only.
    """
    ALTER TABLE calls ADD COLUMN hold_since REAL;
    ALTER TABLE calls ADD COLUMN last_alive_at REAL;
    ALTER TABLE nudges ADD COLUMN outcome TEXT;
    ALTER TABLE nudges ADD COLUMN outcome_at REAL;
    """,
    # 27 (2026-10-03, revamp A7/A9; docs/research/revamp-2026-10-02/22-build-A6-A9.md):
    # - threads.ident_key: a capture thread's identity by the app's stable identity (its process image, app_key) and
    #   its scope, URL or title - no longer by the app's display name. On 2 Oct the name Windows gave
    #   whatsapp.root changed from "WhatsApp.Root" to "WhatsApp" and every WhatsApp chat (Hemanth, RES) split into
    #   two threads. threads.merged_into: a thread that was one of such a split points at the one it went into
    #   (its captures, facts, messages and links now on that one). The Python step after this SQL
    #   (:meth:`Store._migrate_27`) fills ident_key and merges the splits; thread_key keeps its old value, so code
    #   from before this migration still finds its rows.
    # - conversation_turns.envelope_ciphertext: the notes Yuki itself put around the user's words in a request (the
    #   voice hand-off's references, heard words, done-before log and guide; ~1.5-2.8k characters), kept apart from
    #   the words, so recall lines, the resume and the turn's vector carry the user's words, not Yuki's boilerplate.
    # - portraits.sections_ciphertext: the rendered page's sections as fields (work, interests, routines, behaviour,
    #   people, preferences, relationship, personality, open loops; JSON), each written within its own stated budget
    #   (revamp 09 P0.2: the page used to be cut blind, 30-70% of what the model wrote, every night). Consumers read
    #   the fields; the page text keeps its old shape for the ones that read the page.
    # Additive only.
    """
    ALTER TABLE threads ADD COLUMN ident_key TEXT;
    ALTER TABLE threads ADD COLUMN merged_into INTEGER;
    CREATE INDEX threads_ident ON threads(ident_key);
    ALTER TABLE conversation_turns ADD COLUMN envelope_ciphertext TEXT;
    ALTER TABLE portraits ADD COLUMN sections_ciphertext TEXT;
    """,
    # 28 (2026-10-03, revamp Phase B1 "people and routes", docs/research/revamp-2026-10-02/24-build-B.md;
    # yuki.memory.contacts): who the user talks to and where, derived from rows memory already holds (capture threads,
    # their messages, person facts, name aliases, call digests) by one model pass that links people across apps, its
    # output checked against the stored strings. Rebuilt by Yuki's own pipeline (python -m yuki.memory.contacts
    # --rebuild), refreshed after the nightly audit; no counts are stored (code computes them from messages when read).
    # - contacts: one person (canonical name, relation in a person fact's own words, aliases incl. names speech
    #   recognition misheard, how the user writes to them, the person facts it rests on); name_key = HMAC of the
    #   casefolded name, so a route preference survives a rebuild. ended_at: retired (rebuilt, merged, unevidenced).
    # - contact_routes: one place the user talks with a person (contact_id) - or, with contact_id NULL, a group chat or
    #   channel the user takes part in ("my usual channel") - by service (the URL host or the app's key, cleartext),
    #   chat name and kind (dm | group | channel), with every capture thread it spans (display-name variants) and the
    #   journal entries that put a person in a group they never wrote in. route_key = HMAC(service, casefolded chat).
    # - contact_prefs: the user's lasting choice of route for a person ("no, the other one"), in their own words,
    #   written only through MemoryClient.prefer_route (the agent's about tool), never by hand.
    # Additive only.
    """
    CREATE TABLE contacts (
        id                  INTEGER PRIMARY KEY,
        name_key            TEXT NOT NULL,
        name_ciphertext     TEXT NOT NULL,
        relation_ciphertext TEXT,
        aliases_ciphertext  TEXT,
        tone_ciphertext     TEXT,
        fact_ids            TEXT,
        run_id              INTEGER,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL,
        ended_at            REAL
    );
    CREATE INDEX contacts_live ON contacts(ended_at, name_key);
    CREATE TABLE contact_routes (
        id                  INTEGER PRIMARY KEY,
        contact_id          INTEGER REFERENCES contacts(id),
        route_key           TEXT NOT NULL,
        service             TEXT NOT NULL,
        kind                TEXT NOT NULL,
        chat_ciphertext     TEXT NOT NULL,
        handle_ciphertext   TEXT,
        thread_ids          TEXT NOT NULL,
        evidence            TEXT,
        run_id              INTEGER,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL,
        ended_at            REAL
    );
    CREATE INDEX contact_routes_contact ON contact_routes(ended_at, contact_id);
    CREATE TABLE contact_prefs (
        id                  INTEGER PRIMARY KEY,
        name_key            TEXT NOT NULL,
        route_key           TEXT NOT NULL,
        quote_ciphertext    TEXT NOT NULL,
        source              TEXT NOT NULL,
        created_at          REAL NOT NULL,
        ended_at            REAL
    );
    CREATE INDEX contact_prefs_name ON contact_prefs(name_key, ended_at);
    """,
    # 29 (2026-10-03, revamp Phase C "practice memory", docs/research/revamp-2026-10-02/27-build-C.md;
    # yuki.memory.practice, yuki.memory.practice_learn): how this user does things on this PC, learned from Yuki's own
    # runs and from what memory already observes, and never a mistake twice.
    # - runs: the structured trace of every agent request and voice hand, kept for the learners (30-day TTL, pruned
    #   with the captures; text secret-filtered then encrypted). One row per step: step 0 is the request itself (the
    #   user's words, the outcome, wall time, model calls, the request's cost, the measured signals, the playbooks shown
    #   and cited, and where learning stands); steps 1.. are its tool calls in order (tool, the target app as app_key,
    #   the control's role and name and the window's title - never a window number as meaning -, an input summary, the
    #   verdict and why, result facts up to ~300 characters, duration, the share of its model call's cost).
    # - playbooks: the practice objects, four kinds (procedure | habit | pitfall | style): intent (a few plain words)
    #   + vector, scope (apps as app_keys in clear like threads.process, entities encrypted), body (steps, default,
    #   text), avoid, the family a lesson generalises to; derived columns (counts, last evidence/use, confidence, state)
    #   computed from the ledger by code, never written by a model; bi-temporal valid_from/valid_to/superseded_by.
    # - playbook_vec: the intent vectors (encrypted float32, like knowhow_vec).
    # - playbook_events: the evidence ledger (observed, observed_day, ran_ok, taught, ran_trouble, user_corrected,
    #   contradicted, injected, cited, superseded, retired_by, user_*), each with its weight and the run it came from.
    # Additive only; the know-how rows stay (the practice pipeline converts them, python -m yuki.memory.practice_learn
    # --intake).
    """
    CREATE TABLE runs (
        id                 INTEGER PRIMARY KEY,
        run_key            TEXT NOT NULL,
        session_id         TEXT,
        request_id         TEXT,
        source             TEXT NOT NULL,
        lane               TEXT,
        step               INTEGER NOT NULL,
        at                 REAL NOT NULL,
        ended_at           REAL,
        tool               TEXT NOT NULL,
        kind               TEXT,
        app                TEXT,
        target_ciphertext  TEXT,
        input_ciphertext   TEXT,
        ok                 INTEGER,
        verdict            INTEGER,
        why_ciphertext     TEXT,
        result_ciphertext  TEXT,
        ms                 REAL,
        cost_usd           REAL,
        calls              INTEGER,
        outcome            TEXT,
        signals            TEXT,
        playbooks          TEXT,
        note_ciphertext    TEXT,
        learn              TEXT,
        learn_at           REAL,
        learn_note         TEXT
    );
    CREATE INDEX runs_key ON runs(run_key, step);
    CREATE INDEX runs_learn ON runs(learn, at);
    CREATE INDEX runs_at ON runs(at);
    CREATE INDEX runs_session ON runs(session_id, at);
    CREATE TABLE playbooks (
        id                  INTEGER PRIMARY KEY,
        kind                TEXT NOT NULL,
        intent_ciphertext   TEXT NOT NULL,
        apps                TEXT,
        entities_ciphertext TEXT,
        body_ciphertext     TEXT NOT NULL,
        avoid_ciphertext    TEXT,
        family_ciphertext   TEXT,
        origin              TEXT NOT NULL,
        sources_ciphertext  TEXT,
        n_observed          INTEGER NOT NULL DEFAULT 0,
        n_days              INTEGER NOT NULL DEFAULT 0,
        n_ran_ok            INTEGER NOT NULL DEFAULT 0,
        n_ran_trouble       INTEGER NOT NULL DEFAULT 0,
        n_corrected         INTEGER NOT NULL DEFAULT 0,
        n_used              INTEGER NOT NULL DEFAULT 0,
        last_evidence_at    REAL,
        last_used_at        REAL,
        last_verified_at    REAL,
        confidence          REAL,
        state               TEXT NOT NULL DEFAULT 'candidate',
        disabled_at         REAL,
        valid_from          REAL NOT NULL,
        valid_to            REAL,
        superseded_by       INTEGER,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    );
    CREATE INDEX playbooks_live ON playbooks(valid_to, kind);
    CREATE TABLE playbook_vec (
        playbook_id     INTEGER PRIMARY KEY REFERENCES playbooks(id),
        model           TEXT NOT NULL,
        dim             INTEGER NOT NULL,
        vec_ciphertext  BLOB NOT NULL
    );
    CREATE TABLE playbook_events (
        id              INTEGER PRIMARY KEY,
        at              REAL NOT NULL,
        playbook_id     INTEGER NOT NULL REFERENCES playbooks(id),
        type            TEXT NOT NULL,
        weight          REAL NOT NULL DEFAULT 0,
        run_key         TEXT,
        day             TEXT,
        refs_ciphertext TEXT
    );
    CREATE INDEX playbook_events_pb ON playbook_events(playbook_id, at);
    CREATE INDEX playbook_events_run ON playbook_events(run_key);
    """,
    # 30 (2026-10-03, proactive check-ins, docs/research/revamp-2026-10-02/33-build-checkins.md; yuki.memory.checkins):
    # the user: "it should connect this on-screen context with more data on what I am trying to achieve ... and then
    # ask me 'did you achieve what you were trying to do'".
    # - goals: what the user is going for - a day's goal ("today's goals", ``day``), a thread of work's goal, or a
    #   longer one - in their own words when they said it (text and the quote it rests on, encrypted), where it came
    #   from (source: answer | call | commitment | thread, with the id), its status (open | done | dropped | unknown:
    #   inferred, not confirmed), the evidence ids, and when Yuki may check in on it next. Written only by the
    #   check-in pipeline: a check-in that rests on a goal memory shows creates it (unknown), the fold-in of the
    #   user's answer (a model pass over their words, checked in code) adds and updates them. Never by hand.
    # - goal_events: every change to a goal, with the question (check-in) it came from and the evidence ids.
    # - questions.moment / goal_id / folded_at / reply_tone: a check-in is a question of origin "checkin" (so the
    #   existing answer paths - the card's Answer, the agent's answer_question, conversation memory - carry its
    #   answer), with the moment that woke it, the goal it is about, when its answer was folded into the goals, and
    #   how the user took being asked (what lowers that kind's budget).
    # Additive only.
    """
    CREATE TABLE goals (
        id                  INTEGER PRIMARY KEY,
        scope               TEXT NOT NULL,
        day                 TEXT,
        work_thread_id      INTEGER,
        text_ciphertext     TEXT NOT NULL,
        words_ciphertext    TEXT,
        source              TEXT NOT NULL,
        source_ref          TEXT,
        status              TEXT NOT NULL,
        evidence            TEXT,
        due_at              REAL,
        next_checkin_at     REAL,
        last_checkin_at     REAL,
        checkins            INTEGER NOT NULL DEFAULT 0,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL,
        ended_at            REAL
    );
    CREATE INDEX goals_live ON goals(status, next_checkin_at);
    CREATE INDEX goals_thread ON goals(work_thread_id);
    CREATE INDEX goals_day ON goals(day);
    CREATE TABLE goal_events (
        id                  INTEGER PRIMARY KEY,
        goal_id             INTEGER NOT NULL REFERENCES goals(id),
        at                  REAL NOT NULL,
        type                TEXT NOT NULL,
        status              TEXT,
        question_id         INTEGER,
        evidence            TEXT,
        note_ciphertext     TEXT
    );
    CREATE INDEX goal_events_goal ON goal_events(goal_id, at);
    ALTER TABLE questions ADD COLUMN moment TEXT;
    ALTER TABLE questions ADD COLUMN goal_id INTEGER;
    ALTER TABLE questions ADD COLUMN folded_at REAL;
    ALTER TABLE questions ADD COLUMN reply_tone TEXT;
    """,
    # 31 (2026-10-03, making Yuki personal, docs/research/revamp-2026-10-02/30c-build-D-personal.md; yuki.memory.brief):
    # - surfaced: for each reply and each card, which lines of the personal brief and which memory items were in the
    #   context it was written from (ids only, never text: ``brief_ids`` like "people.2", ``fact_ids`` like "F12",
    #   "J345", "W12"), and, once the nightly judge read it (at most 20 a night), its verdict (personal | generic |
    #   wrong) with the judge's reading (encrypted). ``ref`` is "nudge:N" for a card, "turn:<session>:<request>" for a
    #   reply. Written by code only (the card store, the agent's turn writer); pruned with the captures.
    # Additive only.
    """
    CREATE TABLE surfaced (
        id                  INTEGER PRIMARY KEY,
        at                  REAL NOT NULL,
        kind                TEXT NOT NULL,
        ref                 TEXT NOT NULL,
        session_id          TEXT,
        brief_at            REAL,
        brief_ids           TEXT NOT NULL,
        fact_ids            TEXT NOT NULL,
        judged_at           REAL,
        verdict             TEXT,
        judge_ciphertext    TEXT
    );
    CREATE INDEX surfaced_at ON surfaced(at);
    CREATE INDEX surfaced_ref ON surfaced(ref);
    """,
    # 32 (2026-10-03, review 34 fixes, docs/research/revamp-2026-10-02/35-build-review-fixes.md): two indexes.
    # - journal(batch_id, thread_id): withdrawn_journal looks up the batch that replaced an entry (M9: a correlated
    #   SCAN of the whole journal per id before; also the re-journal readers that ask by batch).
    # - messages(thread_id, first_seen): the People list's per-route and per-day counts read a thread's messages in a
    #   time range (M11).
    # Additive only.
    """
    CREATE INDEX IF NOT EXISTS journal_batch_thread ON journal(batch_id, thread_id);
    CREATE INDEX IF NOT EXISTS messages_thread_seen ON messages(thread_id, first_seen);
    """,
    # 33 (2026-10-03, the 3 Oct feedback, Builder A: docs/research/revamp-2026-10-02/38-feedback-root-causes.md §2-§9,
    # §12, 43-build-appA-memory.md): what the app shows the user, said to them.
    # - portrait_facts.shown_ciphertext: the fact as the app shows it - second person, natural dates, others by name
    #   (yuki.memory.titles.SHOWN_RULES) - written by the same model call that writes the fact (or the titles pass);
    #   the stored text stays third person for every prompt that reads it.
    # - portrait_facts.pinned: the user's pin, its own flag (before, "pinned" was origin == 'user', which also means
    #   "you told me", so it could not be turned off). Set for the current user-origin facts so nothing changes.
    # - portrait_facts.reviewed_at: when the user answered it in the "To review" queue (Right / Wrong / Not mine).
    # - work_threads.shown_ciphertext: where the thread stands, said to the user (cleared when the state changes).
    # - call_digests.gist_shown_ciphertext: the call's gist, said to the user.
    # - journal.reviewed_at: when the user answered a "maybe misheard" entry (Right / Wrong) - a direct op, no model.
    # - contact_routes.use_ciphertext: one sentence, what the user uses that chat for with the person.
    # - day_digests: one short digest per user day (days start at 04:00; yuki.memory.daydigest), the memory form
    #   (text) and the form shown to the user (shown), with the ids it rests on; the line that connects one day's
    #   chat to the next for the user and for Yuki (resume_context).
    # Additive only.
    """
    ALTER TABLE portrait_facts ADD COLUMN shown_ciphertext TEXT;
    ALTER TABLE portrait_facts ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE portrait_facts ADD COLUMN reviewed_at REAL;
    UPDATE portrait_facts SET pinned = 1 WHERE origin = 'user' AND kind != 'correction';
    ALTER TABLE work_threads ADD COLUMN shown_ciphertext TEXT;
    ALTER TABLE call_digests ADD COLUMN gist_shown_ciphertext TEXT;
    ALTER TABLE journal ADD COLUMN reviewed_at REAL;
    ALTER TABLE contact_routes ADD COLUMN use_ciphertext TEXT;
    CREATE TABLE day_digests (
        day                 TEXT PRIMARY KEY,
        text_ciphertext     TEXT NOT NULL,
        shown_ciphertext    TEXT,
        refs                TEXT,
        model               TEXT,
        cost_usd            REAL,
        created_at          REAL NOT NULL,
        updated_at          REAL NOT NULL
    );
    """,
    # 34 (2026-10-03, calls record both sides, docs/research/revamp-2026-10-02/60-build-call-others-capture.md;
    # the owner's decision, handbook rule 9): the user's microphone plus the call app's own audio (process
    # loopback), and a game's own voice chat as a voice session of its own.
    # - calls.kind: call | game (a game's voice chat, started by the measured gaming_with_voice signal, asked with
    #   the same per-app consent). Old rows read as calls.
    # - call_chunks.side: user (the microphone) | others (an app's own audio); old rows read as the user's.
    #   track_app / track_process / track_kind: the app the chunk's audio came from (for the user's chunks, the
    #   voice session's row it was recorded for); track_call_id: the row of that app (a game's chunks are kept in
    #   the call's row while a call is recorded, so one transcript holds both conversations in time order).
    # - mic_spans: when each app was actually taking the microphone (its capture session active), and when that
    #   was being watched (kind "watch"): the transcriber marks each of the user's lines with the apps that heard
    #   it (heard_by). Content-free; pruned with the spool.
    # Additive only; one self-contained entry (renumber freely at a merge).
    """
    ALTER TABLE calls ADD COLUMN kind TEXT NOT NULL DEFAULT 'call';
    ALTER TABLE call_chunks ADD COLUMN side TEXT NOT NULL DEFAULT 'user';
    ALTER TABLE call_chunks ADD COLUMN track_app TEXT;
    ALTER TABLE call_chunks ADD COLUMN track_process TEXT;
    ALTER TABLE call_chunks ADD COLUMN track_kind TEXT;
    ALTER TABLE call_chunks ADD COLUMN track_call_id INTEGER;
    CREATE TABLE mic_spans (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,
        process     TEXT,
        app         TEXT,
        pid         INTEGER,
        started_at  REAL NOT NULL,
        ended_at    REAL
    );
    CREATE INDEX mic_spans_time ON mic_spans(started_at);
    """,
    # 35 (2026-10-04, the user's feedback round 3 W1-W3, docs/research/revamp-2026-10-02/56-build-appMem2.md):
    # threads you can close, sessions with a title, corrections in your own words.
    # - work_threads.next_ciphertext: the concrete next step to continue the thread, said to the user (English),
    #   written with its shown form (the nightly review, the titles pass); cleared with it when the state changes.
    # - work_threads.closed_by / closed_at: who closed or dismissed it last (user | model | code) and when; cleared
    #   when the thread is reopened. A thread the user closed is offered to the journal as such.
    # - undo_images: the before-image (and after-image) of an item a pass or the user's own op changed, per pass run
    #   (run_id) or per user op (run_id NULL): a thread (its row, the journal facts, events and links that pointed at
    #   it, its vector), a shown form, a session's title and summary, a journal row's provenance. Every close,
    #   dismiss, merge, rename and rewrite is reversible (Reopen, --undo RUN_ID); a later change is never undone.
    # - episodes.title_ciphertext / summary_ciphertext: a work session's short distinct title (a few words) and its
    #   concise summary said to the user (one or two short sentences, English), written by the episode call itself.
    # - correction_passes.target: the item the user was correcting in their own words (W12, E40, P7, J300): the
    #   correction pass always judges it first.
    # Additive only; one self-contained entry (renumber freely at a merge).
    """
    ALTER TABLE work_threads ADD COLUMN next_ciphertext TEXT;
    ALTER TABLE work_threads ADD COLUMN closed_by TEXT;
    ALTER TABLE work_threads ADD COLUMN closed_at REAL;
    CREATE TABLE undo_images (
        id                 INTEGER PRIMARY KEY,
        run_id             INTEGER,
        surface            TEXT NOT NULL,          -- thread | fact | gist | episode | journal
        item_id            INTEGER NOT NULL,
        action             TEXT NOT NULL,
        by                 TEXT NOT NULL,          -- user | model | code
        at                 REAL NOT NULL,
        before_ciphertext  TEXT NOT NULL,
        after_ciphertext   TEXT,
        undone_at          REAL
    );
    CREATE INDEX undo_images_run ON undo_images(run_id);
    CREATE INDEX undo_images_item ON undo_images(surface, item_id, at);
    ALTER TABLE episodes ADD COLUMN title_ciphertext TEXT;
    ALTER TABLE episodes ADD COLUMN summary_ciphertext TEXT;
    ALTER TABLE correction_passes ADD COLUMN target TEXT;
    """,
    # 36 (2026-10-04, the Work page, docs/research/revamp-2026-10-02/68-build-appWork.md): work_items, one row per
    # thing the Work page shows that needs a judgement or a mark of the user's (yuki.memory.workboard):
    # - a chat (key "chat:<capture thread id>"): whether its newest messages wait on the user's reply, who and the
    #   message to quote, judged on its newest message (basis); the user's marks: no_reply (No reply needed),
    #   opened (Open chat); a mark lapses when a newer message arrives (mark_basis);
    # - an open loop ("loop:<id>"): who owes what (waiting on someone, the user's own promise, asked of the user) in
    #   a few words, and the nudge draft for one the user waits on, judged on the loop's last change;
    # - "meetings": the meetings and calls coming up, read from what memory saw on screen, judged on the journal;
    # - a commitment or loop the user moved to their list ("commit:<id>" / "loop:<id>", mark listed).
    # Verdicts and words are encrypted; basis, marks and times are content-free. Additive only; one self-contained
    # entry (renumber freely at a merge).
    """
    CREATE TABLE work_items (
        id              INTEGER PRIMARY KEY,
        key             TEXT NOT NULL UNIQUE,
        kind            TEXT NOT NULL,           -- reply | loop | meetings | place
        basis           TEXT,                    -- what the verdict was judged on (content-free)
        data_ciphertext TEXT,                    -- the verdict as JSON
        judged_at       REAL,
        mark            TEXT,                    -- the user's: no_reply | opened | listed
        mark_basis      TEXT,                    -- the basis the mark was set on
        mark_at         REAL,
        updated_at      REAL NOT NULL
    );
    CREATE INDEX work_items_kind ON work_items(kind, updated_at);
    """,
    # 37 (2026-10-04, leftMem, docs/research/revamp-2026-10-02/70-build-leftMem.md): conversation_facts.reopened_at,
    # when the user last put an ended commitment back (the Work page's Not done yet, Put it back, Undo): memory closes
    # it again only on evidence dated after that moment, never on what closed it before (yuki.memory.commitments).
    # Content-free; carried to a commitment's new version. Additive only; one self-contained entry (renumber freely
    # at a merge).
    """
    ALTER TABLE conversation_facts ADD COLUMN reopened_at REAL;
    """,
    # 38 (2026-10-07, capC1, docs/research/revamp-2026-10-02/96-build-capC1.md; design: report 78 §8): the action
    # trace, in shadow (nothing reads it to change behaviour yet; yuki.memory.trace). Written only once the user
    # turned on Settings -> Record how I work.
    # - ui_actions: one row per action of the user's (or Yuki's, labelled by source): kind (click | dclick | rclick
    #   | drag | scroll | keys | type | hover | menu | invoke | select | copy | paste | file_open | file_save | switch
    #   | nav | command), the element by role, keyed digests and position (names encrypted, only for command
    #   controls, menu/tab/title/pane names until the 3-day rule promotes them; content controls digest only), the
    #   chord or named key (never typed text: typed is a count), clipboard sequence and format names (never
    #   content), the file path encrypted, the outcome class and the machine-checked facts, what was on screen
    #   (capture_id) and the page (page_key) at button-down, the episode (attached later), the source (user |
    #   user_injected | yuki) and whether a Yuki run was in progress, and the pointer: travel_px and gap_ms since the
    #   previous action, dwell_ms of a hover, a drag's end element (end_*). 30-day TTL (decision D4).
    # - ui_elements: per (app, element key) counts and distinct days; a name only for command controls, or once
    #   the same digest was used on 3 distinct days. Deleted after 60 days unused.
    # - ui_links: cross-app links (clipboard | file | url), the table only: phase C2 fills it.
    # - ui_game_processes: the hard game gate's learned set with its evidence (yuki.memory.gamegate).
    # - ui_files: what was read from a project file the user opened or saved (decision D6: FL Studio .flp names,
    #   tempo, plugins, patterns; never audio or sample content), encrypted, linked to the file action.
    # Additive only; one self-contained entry (renumber freely at a merge).
    """
    CREATE TABLE IF NOT EXISTS ui_actions (
        id                   INTEGER PRIMARY KEY,
        at                   REAL NOT NULL,
        ended_at             REAL,
        process              TEXT NOT NULL,
        page_key             TEXT,
        kind                 TEXT NOT NULL,
        source               TEXT NOT NULL,
        via                  TEXT,
        run_key              TEXT,
        run_step             INTEGER,
        during_run           INTEGER NOT NULL DEFAULT 0,
        role                 TEXT,
        name_ciphertext      TEXT,
        name_digest          TEXT,
        position             TEXT,
        aid_ciphertext       TEXT,
        aid_digest           TEXT,
        element_key          TEXT,
        container_digest     TEXT,
        point                TEXT,
        button               TEXT,
        chord                TEXT,
        typed                INTEGER,
        n                    INTEGER,
        clip_seq             INTEGER,
        clip_formats         TEXT,
        file_ciphertext      TEXT,
        file_ext             TEXT,
        file_digest          TEXT,
        command_ciphertext   TEXT,
        exit_code            INTEGER,
        outcome              TEXT,
        outcome_ciphertext   TEXT,
        facts                TEXT,
        capture_id           INTEGER,
        episode_id           INTEGER,
        resolved             TEXT,
        lookup_ms            REAL,
        travel_px            INTEGER,
        gap_ms               INTEGER,
        dwell_ms             INTEGER,
        end_role             TEXT,
        end_name_ciphertext  TEXT,
        end_name_digest      TEXT,
        end_element_key      TEXT,
        end_position         TEXT,
        proc_started         REAL
    );
    CREATE INDEX IF NOT EXISTS ui_actions_at ON ui_actions(at);
    CREATE INDEX IF NOT EXISTS ui_actions_el ON ui_actions(element_key, at);
    CREATE INDEX IF NOT EXISTS ui_actions_process ON ui_actions(process, at);
    CREATE TABLE IF NOT EXISTS ui_elements (
        element_key      TEXT PRIMARY KEY,
        process          TEXT,
        role             TEXT,
        name_class       TEXT,
        name_ciphertext  TEXT,
        aid_ciphertext   TEXT,
        days             INTEGER NOT NULL DEFAULT 0,
        n                INTEGER NOT NULL DEFAULT 0,
        first_at         REAL,
        last_at          REAL,
        last_day         TEXT
    );
    CREATE INDEX IF NOT EXISTS ui_elements_last ON ui_elements(last_at);
    CREATE TABLE IF NOT EXISTS ui_links (
        id           INTEGER PRIMARY KEY,
        from_action  INTEGER,
        to_action    INTEGER,
        kind         TEXT,
        lag_s        REAL,
        created_at   REAL
    );
    CREATE TABLE IF NOT EXISTS ui_game_processes (
        process     TEXT PRIMARY KEY,
        first_seen  REAL,
        last_seen   REAL,
        evidence    TEXT
    );
    CREATE TABLE IF NOT EXISTS ui_files (
        id               INTEGER PRIMARY KEY,
        at               REAL NOT NULL,
        action_id        INTEGER,
        file_digest      TEXT,
        kind             TEXT NOT NULL,
        outcome          TEXT NOT NULL,
        data_ciphertext  TEXT,
        parse_ms         REAL
    );
    CREATE INDEX IF NOT EXISTS ui_files_at ON ui_files(at);
    """,
    # 39 (2026-10-07, capC2, docs/research/revamp-2026-10-02/98-build-capC2.md; design: report 78 §5.1-5.4, §8): links
    # and tasks, the "why" of the trace (yuki.memory.trace_links, trace_tasks, trace_families).
    # - ui_links (migration 38's table) gets its indexes: one row per exact machine join (clipboard | file | url);
    #   ui_actions gets two (by kind, and by file digest) so the joins never scan the trace.
    # - ui_tasks: the user's steps in one finished episode window grouped into a task by the task pass: its span, the
    #   apps (process keys), the goal in plain words (encrypted; the pass's own goal kept apart once the user renamed
    #   it, so the correction can be shown to later passes as a worked example), the outcome fact chosen only from
    #   facts code recorded (or 'unclear'), the family, the user's verdict from the Day page (renamed | not_one |
    #   not_mine) and the goal's embedding (encrypted). 30-day TTL (decision D4). AUTOINCREMENT: an id is never used
    #   again, so a family member (below) keyed by its task's id stays that task's after the row expires.
    # - ui_task_actions: a task's action id ranges (detour = 1: a detour inside it). 30-day TTL.
    # - ui_families: tasks with the same goal and route: a short label (encrypted), the usual path and its branches
    #   (element keys and machine fields only, never names), the centroid of the goals' embeddings (encrypted).
    #   Deleted after 90 days unused. label_task: the task its label was taken from (a forget of that task relabels).
    #   path_n: how many routes the usual path was built from (at most 12; its steps are counted against that).
    #   AUTOINCREMENT, like ui_tasks: a forget's undo puts a family back under its own id, never over a newer one.
    # - ui_family_members: each task counted in a family, kept as its route (element keys and machine fields only, no
    #   goal, no names), apps, span and the goal's embedding (encrypted), so a family keeps its count and usual path
    #   after its tasks expire at 30 days (they are kept 90 days, like the family; a forget or the user's rejection
    #   takes a member out).
    # - ui_transitions: per app, element key -> next element key, count, median gap, the last gaps, last use. Digests
    #   only. Deleted after 60 days unused.
    # - ui_task_runs: one row per task pass on a window: cost (the $0.40 a day cap is checked against it), tasks,
    #   unclear, refused (over the cap), ok, outcome. 30-day TTL.
    # Additive only; one self-contained entry (renumber freely at a merge).
    """
    CREATE INDEX IF NOT EXISTS ui_links_from ON ui_links(from_action);
    CREATE INDEX IF NOT EXISTS ui_links_to ON ui_links(to_action);
    CREATE UNIQUE INDEX IF NOT EXISTS ui_links_pair ON ui_links(from_action, to_action, kind);
    CREATE INDEX IF NOT EXISTS ui_actions_kind ON ui_actions(kind, at);
    CREATE INDEX IF NOT EXISTS ui_actions_file ON ui_actions(file_digest, at) WHERE file_digest IS NOT NULL;
    CREATE TABLE IF NOT EXISTS ui_tasks (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        episode_id             INTEGER,
        window_start           REAL,
        window_end             REAL,
        started_at             REAL NOT NULL,
        ended_at               REAL NOT NULL,
        apps                   TEXT,
        goal_ciphertext        TEXT,
        model_goal_ciphertext  TEXT,
        outcome_fact           TEXT,
        outcome_action         INTEGER,
        outcome_target         TEXT,
        family_id              INTEGER,
        user_verdict           TEXT,
        verdict_at             REAL,
        actions                INTEGER,
        vec_ciphertext         BLOB,
        embed_model            TEXT,
        run_id                 INTEGER,
        created_at             REAL NOT NULL,
        updated_at             REAL,
        valid_to               REAL
    );
    CREATE INDEX IF NOT EXISTS ui_tasks_span ON ui_tasks(started_at);
    CREATE INDEX IF NOT EXISTS ui_tasks_family ON ui_tasks(family_id);
    CREATE INDEX IF NOT EXISTS ui_tasks_window ON ui_tasks(window_start);
    CREATE TABLE IF NOT EXISTS ui_task_actions (
        task_id       INTEGER NOT NULL,
        first_action  INTEGER NOT NULL,
        last_action   INTEGER NOT NULL,
        detour        INTEGER NOT NULL DEFAULT 0
    );
    CREATE INDEX IF NOT EXISTS ui_task_actions_task ON ui_task_actions(task_id);
    CREATE TABLE IF NOT EXISTS ui_families (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        label_ciphertext TEXT,
        n_tasks          INTEGER NOT NULL DEFAULT 0,
        apps             TEXT,
        path_keys        TEXT,
        variants_keys    TEXT,
        playbook_id      INTEGER,
        vec_ciphertext   BLOB,
        embed_model      TEXT,
        last_at          REAL,
        updated_at       REAL,
        label_task       INTEGER,
        path_n           INTEGER
    );
    CREATE TABLE IF NOT EXISTS ui_family_members (
        task_id          INTEGER PRIMARY KEY,
        family_id        INTEGER NOT NULL,
        started_at       REAL NOT NULL,
        ended_at         REAL NOT NULL,
        apps             TEXT,
        route            TEXT,
        vec_ciphertext   BLOB
    );
    CREATE INDEX IF NOT EXISTS ui_family_members_family ON ui_family_members(family_id);
    CREATE TABLE IF NOT EXISTS ui_transitions (
        process       TEXT NOT NULL,
        from_key      TEXT NOT NULL,
        to_key        TEXT NOT NULL,
        n             INTEGER NOT NULL DEFAULT 0,
        median_gap_s  REAL,
        gaps          TEXT,
        last_at       REAL,
        PRIMARY KEY (process, from_key, to_key)
    );
    CREATE INDEX IF NOT EXISTS ui_transitions_last ON ui_transitions(last_at);
    CREATE TABLE IF NOT EXISTS ui_task_runs (
        id            INTEGER PRIMARY KEY,
        at            REAL NOT NULL,
        episode_id    INTEGER,
        window_start  REAL,
        window_end    REAL,
        model         TEXT,
        cost_usd      REAL NOT NULL DEFAULT 0,
        tasks         INTEGER NOT NULL DEFAULT 0,
        unclear       INTEGER NOT NULL DEFAULT 0,
        pieces        INTEGER,
        actions       INTEGER,
        refused       INTEGER NOT NULL DEFAULT 0,
        ok            INTEGER NOT NULL DEFAULT 0,
        outcome       TEXT,
        error         TEXT
    );
    CREATE INDEX IF NOT EXISTS ui_task_runs_at ON ui_task_runs(at);
    CREATE INDEX IF NOT EXISTS ui_task_runs_window ON ui_task_runs(window_start);
    """,
    # 40 (2026-10-10, fMoments; docs/research/audit-2026-10-09/PLAN.md Phase F item 4, decisions D1, D6, D7, D18;
    # spec round3/map-foundations.md F1.2): moments, one table of state changes (yuki.memory.moments).
    # - Derived kinds (call_join, game_voice, page, message_sent) are computed from rows that already exist (calls,
    #   timeline, messages) and point back to them (source, source_id): `python -m yuki.memory.moments rebuild`
    #   recomputes them, so a rule change is a code change, never an edit of memory.
    # - Live kinds (site_front, media_play, media_context, volume_change) are their own source, written once when the
    #   change is seen (source_id: the event time in ms, unique per source).
    # - Content-free columns in clear (kind, app key, a calling service's product name, times, levels, code words);
    #   an entity (a page's template value, a playlist name) only encrypted, with a keyed digest for equality. Media
    #   titles are never kept, only their digest ([moments] media_titles).
    # - timeline gets an index on updated_at (the extractor reads past its cursor every minute) and a partial index on
    #   its meeting rows by (process, ended_at) (an app's join candidates near a time, without scanning the history).
    # Additive only; one self-contained entry (renumber freely at a merge).
    """
    CREATE TABLE IF NOT EXISTS moments (
        id                 INTEGER PRIMARY KEY,
        at                 REAL NOT NULL,
        ended_at           REAL,
        seen_at            REAL NOT NULL,
        kind               TEXT NOT NULL,
        app                TEXT,
        service            TEXT,
        entity_digest      TEXT,
        entity_ciphertext  TEXT,
        value              REAL,
        value_from         REAL,
        means              TEXT,
        by                 TEXT NOT NULL DEFAULT 'user',
        request_id         TEXT,
        source             TEXT NOT NULL,
        source_id          INTEGER,
        evidence           TEXT,
        facts              TEXT,
        UNIQUE(source, source_id, kind)
    );
    CREATE INDEX IF NOT EXISTS moments_at ON moments(at);
    CREATE INDEX IF NOT EXISTS moments_kind ON moments(kind, at);
    CREATE INDEX IF NOT EXISTS timeline_updated ON timeline(updated_at);
    CREATE INDEX IF NOT EXISTS timeline_meeting_rows ON timeline(process, ended_at) WHERE meeting IS NOT NULL;
    """,
)

#: Python steps that run right after a migration's SQL, in the same transaction (index into :data:`MIGRATIONS`
#: -> the :class:`Store` method's name): data moves that need the store's key (an HMAC identity, a decryption).
PY_MIGRATIONS: dict[int, str] = {26: "_migrate_27"}
#: When the timeline began judging meetings by call evidence live (written by migration 27): meeting rows that ended
#: before it are checked against the call rows when read (:meth:`Store._meetings_proven`).
MEETING_GATE_CHECKPOINT = "meeting_evidence_since"


def split_envelope(text: str | None) -> tuple[str, str | None]:
    """``(words, envelope)`` of a request Yuki composed: the notes it put after the user's words (each in brackets
    after a blank line: the voice hand-off's references, heard words, done-before log and guide, a coach note), or
    ``None``. ``words + envelope`` is the text again. A card in brackets on its own line before the words stays with
    the words (``yuki.memory.conversations.has_card_context`` reads it). Parsing Yuki's own request format, never
    the user's words (the same cut :func:`yuki.memory.conversations.own_words` makes)."""
    t = text or ""
    start = t.index("]\n") + 2 if t.startswith("[") and "]\n" in t else 0
    cut = t.find("\n\n[", start)
    if cut < 0:
        return t, None
    return t[:cut], t[cut:]


#: A commitment done within this many days is on the Work page's Recently done (``yuki.memory.nudges.DONE_DAYS``), so
#: the titles pass gives it a title too (leftMem: never memory's third-person text there).
COMMIT_DONE_TITLE_DAYS = 14

#: What carries a model-written short label (migration 21, :mod:`yuki.memory.titles`): surface ->
#: (table, {label: column}, which rows are current). An open loop's title is its portrait fact's summary. A
#: commitment is current while open and while it is on Recently done (:data:`COMMIT_DONE_TITLE_DAYS`); older done
#: ones get theirs from the one-off backfill (``python -m yuki.memory.done_titles``).
LABEL_SURFACES: dict[str, tuple[str, dict[str, str], str]] = {
    "fact": ("portrait_facts", {"summary": "summary_ciphertext"}, f"valid_to IS NULL AND kind != '{CORRECTION_KIND}'"),
    "commit": ("conversation_facts", {"title": "title_ciphertext"},
               "kind = 'commitment' AND (status = 'active' OR (status = 'done' AND valid_to >= "
               f"CAST(strftime('%s', 'now') AS REAL) - {COMMIT_DONE_TITLE_DAYS * 86400}))"),
    "todo": ("todos", {"title": "title_ciphertext"}, "status = 'open'"),
    "knowhow": ("knowhow", {"title": "title_ciphertext", "summary": "summary_ciphertext"}, "valid_to IS NULL"),
}

#: What carries a form shown to the user, said to them (migration 33, :data:`yuki.memory.titles.SHOWN_RULES`):
#: surface -> (table, key column, shown column, which rows are current). Kept apart from :data:`LABEL_SURFACES`: a
#: shown form is a sentence or two in the user's voice, not a short label.
SHOWN_SURFACES: dict[str, tuple[str, str, str, str]] = {
    "fact": ("portrait_facts", "id", "shown_ciphertext", f"valid_to IS NULL AND kind != '{CORRECTION_KIND}'"),
    "thread": ("work_threads", "id", "shown_ciphertext",
               "status NOT IN ('merged', 'dismissed') AND state_ciphertext IS NOT NULL"),
    "gist": ("call_digests", "call_id", "gist_shown_ciphertext", "status = 'ok' AND gist_ciphertext IS NOT NULL"),
}

#: Open loops with their title: the summary of the ``open_loop`` portrait fact each follows (migration 21).
_LOOPS_WITH_TITLES = ("SELECT l.*, f.summary_ciphertext AS title_ciphertext FROM open_loops l"
                      " LEFT JOIN portrait_facts f ON f.id = l.portrait_fact_id")


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


def _request_key(request_id: object) -> int | str:
    """A turn's request id as stored: an int as given, else its text.

    Yuki names its requests by lane and number ("worker-1", "front_desk-3"),
    two lanes sharing one session; until 2026-09-24 this was ``int(...)``,
    which raised on every such id, so no exchange was ever stored.
    """
    if isinstance(request_id, bool) or request_id is None:
        return 0
    if isinstance(request_id, int):
        return request_id
    text = str(request_id).strip()
    return int(text) if text.isdigit() else text


class Store:
    """The memory database. Construct with :meth:`Store.open`."""

    def __init__(self, path: Path, conn: sqlite3.Connection, cipher: FieldCipher) -> None:
        self.path = path
        self.cipher = cipher
        self._conn = conn
        self._lock = threading.RLock()
        self._cond = threading.Condition(threading.Lock())
        self._wake_seq = 0
        # conversation turns: a second connection with a short busy timeout, so
        # writing a turn never waits behind the shared connection's lock or a long
        # write transaction of another process (see add_turn)
        self._turn_conn: sqlite3.Connection | None = None
        self._turn_lock = threading.Lock()
        self._id_lock = threading.Lock()
        self._last_turn_id = 0
        self._deferred_turns: list[list[Any]] = []
        self._deferred_lock = threading.Lock()
        self._turn_thread: threading.Thread | None = None
        #: Turns the background writer had to drop (content-free counters for diagnostics).
        self.turn_write_errors = 0
        self.last_turn_error: str | None = None
        #: Called with the new capture ids after every capture write of this Store (the service's screen
        #: recall, :mod:`yuki.memory.screenrecall`): must return at once and never raise into a write.
        self._capture_listeners: list[Any] = []

    def add_capture_listener(self, fn: Any) -> None:
        """Call ``fn(capture_ids)`` after each capture this Store writes (plumbing; ``fn`` must not block)."""
        self._capture_listeners.append(fn)

    def _captured(self, ids: Sequence[int | None]) -> None:
        """Wake the journal's wait and tell the capture listeners (never raises)."""
        with self._cond:
            self._cond.notify_all()
        new = [int(i) for i in ids if i is not None]
        for fn in list(self._capture_listeners):
            try:
                fn(new)
            except Exception:
                pass

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
        try:
            self.flush_turns(2.0)
        except Exception:
            pass
        with self._turn_lock:
            if self._turn_conn is not None:
                self._turn_conn.close()
                self._turn_conn = None
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
                    if index in PY_MIGRATIONS:
                        getattr(self, PY_MIGRATIONS[index])(self._conn)
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

    def thread_identity(self, app: str | None, title: str | None, url: str | None, *, process: str | None = None,
                        scope: str | None = None) -> tuple[str, str]:
        """``(identity key, display-name key)`` of a capture thread (migration 27).

        The identity is the app's stable identity - its process image (:func:`app_key`: "whatsapp.root"), the
        app's display name only when the process is unknown - with the thread's scope (a chat, a channel, a mail
        subject), else its URL, else its title. Keyed by the display name instead, the same chat became two threads
        when Windows renamed the app ("WhatsApp.Root" -> "WhatsApp", 2 Oct). The second key is the old formula, by
        the display name, which rows written before migration 27 are found by (``thread_key``).
        """
        app = app or ""
        title = title or ""
        url = url or None
        scope = (scope or "").strip() or None
        proc = app_key(process)
        ident = f"proc:{proc}" if proc else app
        if scope and scope != url:
            return self.cipher.digest("scope", ident, scope), self.cipher.digest("scope", app, scope)
        kind = "url" if url else "title"
        return self.cipher.digest(kind, ident, url or title), self.cipher.digest(kind, app, url or title)

    def find_thread(self, app: str | None, title: str | None, url: str | None, *, process: str | None = None,
                    scope: str | None = None) -> int | None:
        """The capture thread of this window or page, if memory has one (read only: nothing is created). The live
        screen (the Now board's window in front) is anchored to its thread of work through it."""
        ident, old = self.thread_identity(app, title, url, process=process, scope=scope)
        try:
            row = self._query("SELECT id FROM threads WHERE ident_key=? AND merged_into IS NULL ORDER BY id LIMIT 1",
                              (ident,))
            if not row:
                row = self._query("SELECT coalesce(merged_into, id) FROM threads WHERE thread_key=?", (old,))
        except sqlite3.OperationalError:   # a database before migration 27
            row = self._query("SELECT id FROM threads WHERE thread_key=?", (old,))
        return int(row[0][0]) if row else None

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
        ident, old = self.thread_identity(app, title, url, process=process, scope=scope)
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            row = conn.execute("SELECT id FROM threads WHERE ident_key=? AND merged_into IS NULL ORDER BY id LIMIT 1",
                               (ident,)).fetchone()
            if row is None:
                # a row written under the display-name key (before migration 27, or by an older build since): it is
                # this thread; a split one that was merged points at the thread it went into
                found = conn.execute("SELECT id, merged_into FROM threads WHERE thread_key IN (?, ?)"
                                     " ORDER BY merged_into IS NOT NULL, id LIMIT 1", (old, ident)).fetchone()
                if found is not None:
                    target = int(found["merged_into"] or found["id"])
                    conn.execute("UPDATE threads SET ident_key=coalesce(ident_key, ?) WHERE id=? AND merged_into IS NULL",
                                 (ident, target))
                    row = {"id": target}
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
                " process, scope_ciphertext, kind, ident_key) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ident, app, url_host(url), self._enc(title), self._enc(url), now, now, proc, self._enc(scope), kind,
                 ident),
            )
            return int(cur.lastrowid)

        return op(conn)

    #: Every column that holds a capture thread's id (migration 27 moves a merged thread's references with it).
    _THREAD_REFS: tuple[tuple[str, str], ...] = (
        ("captures", "thread_id"), ("journal", "thread_id"), ("journal_batches", "thread_id"), ("calls", "thread_id"),
        ("work_thread_events", "thread_id"), ("links_seen", "capture_thread_id"),
    )

    def _migrate_27(self, conn: sqlite3.Connection) -> dict[str, int]:
        """Migration 27's data step (inside its transaction): every capture thread gets its identity key
        (:meth:`thread_identity`), and threads that are one window, page or chat of one app split by a change of
        the app's display name are merged into the oldest: its captures, journal facts, messages, calls, thread
        events and links move to it, the newest title and address stay, the others point at it
        (``merged_into``). A message both had is kept once (the other copy marked a re-read, as the watcher marks
        a message read again). Nothing is deleted. Returns counts."""
        self._set_checkpoint(conn, MEETING_GATE_CHECKPOINT, f"{time.time():.3f}")   # the live meeting gate from now
        rows = conn.execute("SELECT id, app, process, title_ciphertext, url_ciphertext, scope_ciphertext,"
                            " first_seen, last_seen, messages_seen_at FROM threads ORDER BY first_seen, id").fetchall()
        groups: dict[str, list[sqlite3.Row]] = {}
        for r in rows:
            try:
                title, url, scope = (self._dec(r["title_ciphertext"]), self._dec(r["url_ciphertext"]),
                                     self._dec(r["scope_ciphertext"]))
            except Exception:   # a row that cannot be read keeps its own identity
                groups.setdefault(f"row:{r['id']}", []).append(r)
                continue
            ident, _old = self.thread_identity(r["app"], title, url, process=r["process"], scope=scope)
            groups.setdefault(ident, []).append(r)
        merged = 0
        for ident, members in groups.items():
            keep = members[0]
            if not ident.startswith("row:"):
                conn.execute("UPDATE threads SET ident_key=? WHERE id=?", (ident, keep["id"]))
            if len(members) < 2:
                continue
            newest = max(members, key=lambda m: (m["last_seen"], m["id"]))
            for other in members[1:]:
                self._merge_thread_rows(conn, int(keep["id"]), int(other["id"]))
                merged += 1
            conn.execute(
                "UPDATE threads SET first_seen=?, last_seen=?, messages_seen_at=?, title_ciphertext=?,"
                " url_ciphertext=?, scope_ciphertext=coalesce(?, scope_ciphertext) WHERE id=?",
                (min(m["first_seen"] for m in members), max(m["last_seen"] for m in members),
                 max((m["messages_seen_at"] for m in members if m["messages_seen_at"] is not None), default=None),
                 newest["title_ciphertext"], newest["url_ciphertext"], newest["scope_ciphertext"], keep["id"]))
        return {"threads": len(rows), "merged": merged}

    def _merge_thread_rows(self, conn: sqlite3.Connection, keep: int, other: int) -> None:
        """Move one capture thread's references onto another (migration 27's merge; see :meth:`_migrate_27`)."""
        for table, column in self._THREAD_REFS:
            try:
                conn.execute(f"UPDATE {table} SET {column}=? WHERE {column}=?", (keep, other))
            except sqlite3.OperationalError:   # a table a test database does not have
                continue
        conn.execute("UPDATE OR IGNORE messages SET thread_id=? WHERE thread_id=?", (keep, other))
        conn.execute("UPDATE messages SET status='reread' WHERE thread_id=?", (other,))
        latest = conn.execute("SELECT thread_id, at FROM thread_latest WHERE thread_id IN (?, ?)", (keep, other)).fetchall()
        mine = next((r["at"] for r in latest if r["thread_id"] == keep), None)
        theirs = next((r["at"] for r in latest if r["thread_id"] == other), None)
        if theirs is not None and (mine is None or theirs > mine):
            conn.execute("INSERT OR REPLACE INTO thread_latest(thread_id, at, text_ciphertext, hash)"
                         " SELECT ?, at, text_ciphertext, hash FROM thread_latest WHERE thread_id=?", (keep, other))
        conn.execute("UPDATE threads SET merged_into=?, ident_key=NULL WHERE id=?", (keep, other))

    def thread_info(self, thread_id: int) -> ThreadInfo | None:
        """Decrypted thread metadata, or ``None``."""
        rows = self._query("SELECT * FROM threads WHERE id=?", (thread_id,))
        if not rows:
            return None
        r = rows[0]
        return ThreadInfo(
            id=r["id"], app=_shown(r["app"], r["process"]), host=r["host"], title=self._dec(r["title_ciphertext"]) or "",
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
        *, kind: str | None = None, profile: str | None = None, by_yuki: bool = False,
        yuki_request: str | None = None,
    ) -> int | None:
        """Store one capture's delta; see the module docstring for the dedup rules.

        ``kind``/``profile``: the extraction that produced the text (cleartext metadata).
        ``by_yuki``/``yuki_request``: captured while Yuki was acting for the user
        (:mod:`yuki.memory.acting`), and the request it was carrying out.
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
                "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile,"
                " by_yuki, yuki_request_ciphertext) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (thread_id, at, trigger or "", self.cipher.encrypt(delta_text), len(delta_text), delta_hash,
                 kind, profile, int(bool(by_yuki)), self._enc(yuki_request) if by_yuki and yuki_request else None),
            )
            return int(cur.lastrowid)

        capture_id = self._write(op)
        if capture_id is not None:
            self._captured([capture_id])
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
        by_yuki: bool = False,
        yuki_request: str | None = None,
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
                "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile,"
                " by_yuki, yuki_request_ciphertext) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (thread_id, at, trigger or "", self.cipher.encrypt(delta), len(delta),
                 self.cipher.digest("conv", *sorted(fps)), kind, profile, int(bool(by_yuki)),
                 self._enc(yuki_request) if by_yuki and yuki_request else None),
            )
            out.capture_id = int(cur.lastrowid)
            out.chars = len(delta)
            conn.executemany(
                "UPDATE messages SET capture_id=? WHERE id=?", [(out.capture_id, mid) for _, _, mid in stored]
            )
            return out

        result = self._write(op)
        if result.capture_id is not None:
            self._captured([result.capture_id])
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
        """Remember names the screen showed as the user ("Kian (you)"); returns how many were new."""
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
            "SELECT id, thread_id, at, trigger, delta_ciphertext, chars, journal_attempts, kind, profile, by_yuki,"
            " yuki_request_ciphertext FROM captures"
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
                by_yuki=bool(r["by_yuki"]), yuki_request=self._dec(r["yuki_request_ciphertext"]),
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
        nature = getattr(fact, "nature", None)
        cur = conn.execute(
            "INSERT INTO journal(at, thread_id, app, host, fact_ciphertext, importance, batch_id, created_at,"
            " by_yuki, nature, milestone, source_ciphertext, tainted, attention) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                float(fact.at), fact.thread_id, fact.app or "", fact.host, self.cipher.encrypt(fact.fact),
                int(fact.importance), batch_id, time.time(), int(bool(getattr(fact, "by_yuki", False))),
                nature if nature in JOURNAL_NATURES else "fact", int(bool(getattr(fact, "milestone", False))),
                self._enc(json.dumps(fact.source, ensure_ascii=False)) if getattr(fact, "source", None) else None,
                int(bool(getattr(fact, "tainted", False))),
                getattr(fact, "attention", "front") if getattr(fact, "attention", "front") in ("visible", "popup")
                else None,
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

    def _supersede_batches(self, conn: sqlite3.Connection, batch_id: int, supersedes: Sequence[int]) -> int:
        """A re-journal: the facts the replaced batches wrote are superseded by ``batch_id`` (kept, never deleted)."""
        old = sorted({int(b) for b in supersedes if b is not None and int(b) != batch_id})
        if not old:
            return 0
        conn.execute("UPDATE journal_batches SET supersedes=? WHERE id=?", (json.dumps(old), batch_id))
        self._forget_withdrawn()
        # facts of the replaced batches still held back (yuki.memory.admission) and not answered are replaced too: no
        # longer offered. An answered one stays with its hold batch (its Undo still works) and its answer is carried
        # to the new batch's fact on the same line (_hold_journal; review F7)
        conn.execute(
            "UPDATE journal SET superseded_by=? WHERE batch_id IN (%s) AND reviewed_at IS NULL AND superseded_by IN"
            " (SELECT id FROM journal_batches WHERE model LIKE 'hold:%%')" % ",".join("?" * len(old)),
            (batch_id, *old))
        return conn.execute(
            "UPDATE journal SET superseded_at=?, superseded_by=? WHERE batch_id IN (%s) AND superseded_at IS NULL"
            % ",".join("?" * len(old)), (time.time(), batch_id, *old)).rowcount

    @staticmethod
    def _line_key(source: Any) -> tuple[str, float, str] | None:
        """The call line a fact rests on, from its provenance alone (the call, the line's time, whose line): how a
        re-journal's fact meets the earlier fact on the same line (metadata, never words). ``None`` off a call."""
        if not isinstance(source, dict) or source.get("kind") != "call" or source.get("at") is None:
            return None
        try:
            at = round(float(source["at"]), 1)
        except (TypeError, ValueError):
            return None
        return (str(source.get("call") or ""), at, "others" if source.get("who") == "others" else "user")

    @staticmethod
    def _match_line_facts(old: Sequence[tuple[Any, Any, bool]], new: Sequence[tuple[Any, Any, bool]]
                          ) -> dict[int, int]:
        """Pair the facts of a replaced batch with a re-journal's facts on the same call line, one to one: ``old`` and
        ``new`` are ``(line key, vector or None, held)``; returns ``{old index: new index}``. On a line, the closest
        pair by the stored embeddings is matched first (cosine; no words read), a tie going to a pair that is held or
        admitted alike, then to the same place among that line's facts (review V2: two facts resting on one line, a
        plan admitted and a claim held, each meet their own). A fact off a call (no key) is never matched."""
        def unit(v: Any) -> np.ndarray | None:
            if v is None:
                return None
            a = np.asarray(v, dtype=np.float32).ravel()
            n = float(np.linalg.norm(a))
            return a / n if n > 0 else None

        def ranks(items: Sequence[tuple[Any, Any, bool]]) -> list[int]:
            seen: dict[Any, int] = {}
            out_r = []
            for key, _, _ in items:
                out_r.append(seen.get(key, 0))
                seen[key] = seen.get(key, 0) + 1
            return out_r

        old_u = [unit(v) for _, v, _ in old]
        new_u = [unit(v) for _, v, _ in new]
        old_r, new_r = ranks(old), ranks(new)
        pairs: list[tuple[float, int, int, int, int]] = []
        for i, (key, _, held) in enumerate(old):
            if key is None:
                continue
            for j, (key2, _, held2) in enumerate(new):
                if key2 != key:
                    continue
                a, b = old_u[i], new_u[j]
                cos = float(a @ b) if a is not None and b is not None and a.shape == b.shape else 0.0
                pairs.append((-round(cos, 6), 0 if bool(held) == bool(held2) else 1, abs(old_r[i] - new_r[j]), i, j))
        pairs.sort()
        out: dict[int, int] = {}
        taken: set[int] = set()
        for _, _, _, i, j in pairs:
            if i in out or j in taken:
                continue
            out[i] = j
            taken.add(j)
        return out

    def _line_snapshot(self, conn: sqlite3.Connection, supersedes: Sequence[int]) -> list[dict[str, Any]]:
        """The current and held facts of the batches a re-journal replaces, read before they are superseded (call
        facts only): ``{"id", "key" (:meth:`_line_key`), "vec", "held", "answered": (reason, reviewed_at) or None}``.
        :meth:`_hold_journal` matches the re-journal's facts against them (:meth:`_match_line_facts`)."""
        old = sorted({int(b) for b in supersedes if b is not None})
        if not old:
            return []
        out: list[dict[str, Any]] = []
        for r in conn.execute(
                "SELECT j.id, j.source_ciphertext, j.reviewed_at, j.superseded_at, b.model AS hold_model,"
                " v.vec_ciphertext FROM journal j LEFT JOIN journal_batches b ON b.id = j.superseded_by"
                " LEFT JOIN journal_vec v ON v.journal_id = j.id WHERE j.batch_id IN (%s)"
                " AND (j.superseded_at IS NULL OR b.model LIKE 'hold:%%')" % ",".join("?" * len(old)), old).fetchall():
            try:
                src = json.loads(self._dec(r["source_ciphertext"]) or "{}") if r["source_ciphertext"] else {}
            except (TypeError, ValueError):
                src = {}
            key = self._line_key(src)
            if key is None:
                continue
            held = str(r["hold_model"] or "").startswith("hold:")
            vec = None
            if r["vec_ciphertext"] is not None:
                try:
                    vec = np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
                except Exception:
                    vec = None
            answered = ((str(r["hold_model"])[len("hold:"):], float(r["reviewed_at"]))
                        if held and r["reviewed_at"] is not None else None)
            out.append({"id": int(r["id"]), "key": key, "vec": vec, "held": held, "answered": answered})
        return out

    def _hold_journal(self, conn: sqlite3.Connection, facts: Sequence[NewFact], journal_ids: Sequence[int],
                      thread_id: int | None, prior: Sequence[dict[str, Any]] = ()) -> int:
        """Admission (yuki.memory.admission): the facts held back until the user's yes are superseded at once by one
        ``hold:<reason>`` batch per reason (no tokens, no cost), in the caller's transaction. Returns how many.

        A re-journal (``prior``, :meth:`_line_snapshot` of the replaced batches): the user's answer to a held fact is
        carried to the one new fact that fact meets on its line (:meth:`_match_line_facts`; review F7) - held under
        the same reason and marked answered, so a Not true is not admitted again and a Keep (whose kept copy stays) is
        not stored twice. Never to every fact on the line: another fact resting on the same line (a plan the judge
        admits beside a claim answered Not true) meets its own earlier fact and goes in as judged (review V2)."""
        held: dict[str, list[int]] = {}
        carried: list[tuple[float, int]] = []
        carry: dict[int, tuple[str, float]] = {}
        if prior and any(p["answered"] for p in prior):
            new = [(self._line_key(getattr(f, "source", None)), getattr(f, "vector", None),
                    bool(getattr(f, "held", None))) for f in facts]
            pairs = self._match_line_facts([(p["key"], p["vec"], p["held"]) for p in prior], new)
            carry = {j: prior[i]["answered"] for i, j in pairs.items() if prior[i]["answered"]}
        for n, (fact, jid) in enumerate(zip(facts, journal_ids)):
            reason = getattr(fact, "held", None)
            answer = carry.get(n)
            if answer is not None:
                reason = reason or answer[0]
                carried.append((answer[1], int(jid)))
            if reason:
                held.setdefault(str(reason), []).append(int(jid))
        now = time.time()
        for reason, ids in held.items():
            batch = self._insert_batch(conn, ModelCall(at=now, thread_id=thread_id, model=f"hold:{reason}",
                                                       cost_usd=0.0), 0, 0)
            conn.executemany("UPDATE journal SET superseded_at=?, superseded_by=? WHERE id=?",
                             [(now, batch, i) for i in ids])
        if carried:
            conn.executemany("UPDATE journal SET reviewed_at=? WHERE id=?", carried)
        return sum(len(v) for v in held.values())

    def commit_batch(self, call: ModelCall, capture_ids: Iterable[int], facts: Sequence[NewFact],
                     questions: Sequence[NewQuestion] = (),
                     question_outcomes: list[tuple[int | None, str]] | None = None,
                     *, supersedes: Sequence[int] = (), work: Sequence[WorkOp] = ()) -> int:
        """Atomically record a batch, its facts (+vectors), its questions for the user (deduped,
        see :meth:`add_questions`; each gets the journal ids of the facts from its source) and
        mark its captures journaled. ``question_outcomes`` receives ``(id, outcome)`` per question.
        ``supersedes``: a re-journal's earlier batches of the same captures, whose facts it replaces
        (:meth:`_supersede_batches`, same transaction). ``work``: what the batch did to the user's threads
        of work (:meth:`_apply_work`, same transaction; each op's ``result_id`` / ``outcome`` are set)."""
        ids = list(capture_ids)
        known = self.question_known_keys() if questions else set()

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_batch(conn, call, len(ids), len(facts))
            prior = self._line_snapshot(conn, supersedes)   # read before the re-journal supersedes them
            self._supersede_batches(conn, batch_id, supersedes)
            journal_ids = [self._insert_journal(conn, fact, batch_id) for fact in facts]
            self._hold_journal(conn, facts, journal_ids, call.thread_id, prior)
            if work:
                self._apply_work(conn, work, batch_id=batch_id, journal_ids=journal_ids)
            for q in questions:
                q.source_ids = sorted({*q.source_ids, *(journal_ids[i] for i in q.fact_indexes
                                                        if 0 <= i < len(journal_ids))})
            outcomes = [self._insert_question(conn, q, time.time(), known) for q in questions]
            if question_outcomes is not None:
                question_outcomes[:] = outcomes
            conn.executemany("UPDATE captures SET journal_batch_id=? WHERE id=?", [(batch_id, i) for i in ids])
            conn.executemany("UPDATE messages SET journaled=1 WHERE capture_id=?", [(i,) for i in ids])
            return batch_id

        return self._write(op)

    def record_failed_batch(self, call: ModelCall, capture_ids: Iterable[int], *, count_attempt: bool = True) -> int:
        """Record a failed call and bump its captures' attempt counters (they stay pending).

        ``count_attempt=False`` (the model could not be reached at all: no
        network yet after logon or a wake) records the call without spending
        one of the captures' attempts.
        """
        ids = list(capture_ids)

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_batch(conn, call, len(ids), 0)
            if count_attempt:
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
        clauses, params = ["j.superseded_at IS NULL"], []   # a re-journal's replaced rows are history only
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
        keys = r.keys()
        nature = r["nature"] if "nature" in keys else None
        return JournalEntry(
            id=r["id"], at=r["at"], thread_id=r["thread_id"], app=_shown(r["app"]), host=r["host"],
            fact=self.cipher.decrypt(r["fact_ciphertext"]), importance=r["importance"],
            batch_id=r["batch_id"], score=score, by_yuki=bool(r["by_yuki"]) if "by_yuki" in keys else False,
            nature=nature if nature in JOURNAL_NATURES else "fact",
            milestone=bool(r["milestone"]) if "milestone" in keys else False,
            source=self._source(r["source_ciphertext"]) if "source_ciphertext" in keys else None,
            tainted=bool(r["tainted"]) if "tainted" in keys else False,
            work_thread_id=r["work_thread_id"] if "work_thread_id" in keys else None,
            attention=(r["attention"] or "front") if "attention" in keys else "front",
            reviewed_at=r["reviewed_at"] if "reviewed_at" in keys else None,
        )

    def _source(self, stored: str | None) -> dict[str, Any] | None:
        """A journal row's provenance JSON (decrypted), ``None`` when absent or unreadable."""
        if not stored:
            return None
        try:
            value = json.loads(self.cipher.decrypt(stored))
        except Exception:
            return None
        return value if isinstance(value, dict) else None

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
            "SELECT j.* FROM journal j WHERE j.thread_id=? AND j.superseded_at IS NULL"
            " ORDER BY j.at DESC, j.id DESC LIMIT ?",
            (thread_id, int(limit)),
        )
        return [self._entry(r) for r in reversed(rows)]

    def journal_without_vectors(self, limit: int = 256) -> list[JournalEntry]:
        """Facts that have no embedding yet (e.g. the embedder failed when they were written)."""
        rows = self._query(
            "SELECT j.* FROM journal j LEFT JOIN journal_vec v ON v.journal_id = j.id"
            " WHERE v.journal_id IS NULL AND j.superseded_at IS NULL ORDER BY j.id LIMIT ?",
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
        sql = "SELECT j.* FROM journal j WHERE j.id > ? AND j.superseded_at IS NULL ORDER BY j.id"
        params: list[Any] = [int(after_id)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._entry(r) for r in self._query(sql, params)]

    # -- re-journal (yuki.memory.rejournal) ---------------------------------

    def journaled_captures(
        self, *, thread_kind: str | None = None, since: TimeArg = None, until: TimeArg = None,
        batch_ids: Iterable[int] | None = None,
    ) -> list[tuple[PendingCapture, int]]:
        """Captures still stored that were journaled, with the batch that journaled each, oldest first.

        Filtered by the thread's kind (``threads.kind``, e.g. ``my_call_side``), the
        capture time window, or the batches given. Captures past their TTL are gone
        (:meth:`prune`) and cannot be re-journaled.
        """
        clauses, params = ["c.journal_batch_id IS NOT NULL"], []
        if thread_kind:
            clauses.append("t.kind = ?")
            params.append(thread_kind)
        if (s := _ts(since)) is not None:
            clauses.append("c.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("c.at < ?")
            params.append(u)
        if batch_ids is not None:
            ids = sorted({int(b) for b in batch_ids})
            if not ids:
                return []
            clauses.append("c.journal_batch_id IN (%s)" % ",".join("?" * len(ids)))
            params.extend(ids)
        rows = self._query(
            "SELECT c.id, c.thread_id, c.at, c.trigger, c.delta_ciphertext, c.chars, c.journal_attempts, c.kind,"
            " c.profile, c.by_yuki, c.yuki_request_ciphertext, c.journal_batch_id FROM captures c"
            " JOIN threads t ON t.id = c.thread_id WHERE " + " AND ".join(clauses) + " ORDER BY c.at, c.id", params)
        return [(PendingCapture(
            id=r["id"], thread_id=r["thread_id"], at=r["at"], trigger=r["trigger"],
            delta=self.cipher.decrypt(r["delta_ciphertext"]), chars=r["chars"], attempts=r["journal_attempts"],
            kind=r["kind"], profile=r["profile"], by_yuki=bool(r["by_yuki"]),
            yuki_request=self._dec(r["yuki_request_ciphertext"]),
        ), int(r["journal_batch_id"])) for r in rows]

    def journal_of_batches(self, batch_ids: Iterable[int], *, include_superseded: bool = False) -> list[JournalEntry]:
        """The journal rows the given batches wrote, oldest first (superseded ones too when asked)."""
        ids = sorted({int(b) for b in batch_ids})
        if not ids:
            return []
        extra = "" if include_superseded else " AND j.superseded_at IS NULL"
        return [self._entry(r) for r in self._query(
            "SELECT j.* FROM journal j WHERE j.batch_id IN (%s)%s ORDER BY j.at, j.id"
            % (",".join("?" * len(ids)), extra), ids)]

    def withdrawn_journal(self, journal_ids: Iterable[int]) -> set[int]:
        """Those of ``journal_ids`` that no longer stand as evidence: marked as text for or from another AI tool or a
        sample page (``tainted``), or superseded by a re-journal that wrote nothing standing in their place (no
        untainted entry of the same capture thread in the batch that replaced them: on 1 Oct the demo page's
        "received a job offer from Google" was re-journaled into nothing). An entry re-journaled into corrected
        entries is not withdrawn (the portrait's audit weighs those), and an id not in the journal is unknown, not
        withdrawn. Ids and flags only (nothing decrypted).

        Review 34 M9: the replacement is looked up only for superseded entries (``CASE``; SQLite would otherwise run
        the subquery for every row) through the ``journal(batch_id, thread_id)`` index (migration 32), and each
        entry's answer is cached on this store: a withdrawn entry stays withdrawn (taint is set once, a supersede is
        never undone), a standing one is asked again after :data:`WITHDRAWN_RECHECK_S` or once this store
        re-journals."""
        ids = sorted({int(i) for i in journal_ids})
        if not ids:
            return set()
        cache: dict[int, float | None] = self.__dict__.setdefault("_withdrawn_cache", {})   # id -> checked at | None
        clock = time.monotonic()
        out = {i for i in ids if i in cache and cache[i] is None}
        ask = [i for i in ids if i not in out and clock - float(cache.get(i) or -1e18) >= self.WITHDRAWN_RECHECK_S]
        for start in range(0, len(ask), 500):
            chunk = ask[start:start + 500]
            rows = self._query(
                "SELECT j.id, coalesce(j.tainted, 0) AS tainted, j.superseded_at, CASE WHEN j.superseded_by IS NULL"
                " THEN 0 ELSE EXISTS (SELECT 1 FROM journal n WHERE n.batch_id = j.superseded_by"
                " AND n.thread_id IS j.thread_id AND n.superseded_at IS NULL AND coalesce(n.tainted, 0) = 0) END"
                " AS replaced FROM journal j WHERE j.id IN (%s)" % ",".join("?" * len(chunk)), chunk)
            gone = {int(r["id"]) for r in rows
                    if r["tainted"] or (r["superseded_at"] is not None and not r["replaced"])}
            out |= gone
            for i in chunk:   # an id not in the journal is unknown, not withdrawn: asked again later like a standing one
                cache[i] = None if i in gone else clock
        if len(cache) > self.WITHDRAWN_CACHE_MAX:
            cache.clear()
        return out

    #: How long a standing entry's answer is kept (seconds), and the most entries cached per store.
    WITHDRAWN_RECHECK_S = 600.0
    WITHDRAWN_CACHE_MAX = 200_000

    def _forget_withdrawn(self) -> None:
        """A re-journal changed what stands: the standing entries' cached answers are asked again."""
        cache = self.__dict__.get("_withdrawn_cache")
        if cache:
            for key in [k for k, v in list(cache.items()) if v is not None]:
                cache.pop(key, None)

    def superseded_journal(self, journal_ids: Iterable[int]) -> dict[int, int]:
        """``{journal id: the batch that replaced it}`` for those of ``journal_ids`` a re-journal superseded."""
        ids = sorted({int(i) for i in journal_ids})
        if not ids:
            return {}
        return {int(r["id"]): int(r["superseded_by"] or 0) for r in self._query(
            "SELECT id, superseded_by FROM journal WHERE superseded_at IS NOT NULL AND id IN (%s)"
            % ",".join("?" * len(ids)), ids)}

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
            aspect=(r["aspect"] if "aspect" in r.keys() and r["aspect"] in PERSONALITY_ASPECTS else None),
            importance=(int(r["importance"]) if "importance" in r.keys() and r["importance"] is not None else None),
            summary=self._dec(r["summary_ciphertext"]) if "summary_ciphertext" in r.keys() else None,
            shown=self._dec(r["shown_ciphertext"]) if "shown_ciphertext" in r.keys() else None,
            pinned=bool(r["pinned"]) if "pinned" in r.keys() else (r["origin"] == "user"),
            reviewed_at=r["reviewed_at"] if "reviewed_at" in r.keys() else None,
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
        episode_ids: Sequence[int] = (), aspect: str | None = None, importance: int | None = None,
        summary: str | None = None, shown: str | None = None, pinned: bool | None = None,
        reviewed_at: float | None = None,
    ) -> int:
        # a fact the user stated (origin user) starts pinned, as before migration 33; the user may unpin it
        pin = (origin == "user" and kind != CORRECTION_KIND) if pinned is None else bool(pinned)
        cur = conn.execute(
            "INSERT INTO portrait_facts(kind, subject_ciphertext, text_ciphertext, valid_from, valid_to,"
            " source_ids, confidence, origin, created_at, run_id, episode_ids, aspect, importance,"
            " summary_ciphertext, shown_ciphertext, pinned, reviewed_at) VALUES (?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                kind, self._enc(subject or ""), self.cipher.encrypt(text), float(valid_from),
                json.dumps([int(i) for i in source_ids]), confidence, origin, now, run_id,
                json.dumps([int(i) for i in episode_ids]) if episode_ids else None,
                aspect if kind == "personality" and aspect in PERSONALITY_ASPECTS else None,
                None if importance is None else min(10, max(1, int(importance))),
                self._enc(summary) if summary else None, self._enc(shown) if shown else None, int(pin),
                reviewed_at,
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

    def add_correction(self, text: str, at: float | None = None, *, apply: bool = True,
                       target: str | None = None) -> int:
        """Store a user-confirmed correction (kind ``correction``, origin ``user``, confidence 1).

        ``apply``: queue the correction-apply pass for it in the same transaction (migration 18,
        :mod:`yuki.memory.corrections`: every place the corrected thing appears is superseded with corrected
        wording within minutes). False for a correction that only closes something (a loop marked done).
        ``target`` (migration 35): the item the user was correcting (``W12``, ``E40``, ``P7``, ``J300``), which the pass
        judges first.
        """
        now = time.time()
        when = float(at) if at is not None else now

        def op(conn: sqlite3.Connection) -> int:
            fact_id = self._insert_fact(
                conn, kind=CORRECTION_KIND, subject="", text=text, valid_from=when, source_ids=(),
                confidence=1.0, origin="user", run_id=None, now=now,
            )
            self._queue_pass(conn, fact_id, now, status="queued" if apply else "skipped")
            if target:
                conn.execute("UPDATE correction_passes SET target=? WHERE correction_id=?", (str(target), fact_id))
            return fact_id

        return self._write(op)

    def correction_target(self, correction_id: int) -> str | None:
        """The item a correction was made on (migration 35), ``W12`` / ``E40`` / ``P7`` / ``J300``, or ``None``."""
        try:
            rows = self._query("SELECT target FROM correction_passes WHERE correction_id=?", (int(correction_id),))
        except sqlite3.OperationalError:
            return None
        return rows[0]["target"] if rows and rows[0]["target"] else None

    def _queue_pass(self, conn: sqlite3.Connection, correction_id: int, now: float, status: str = "queued") -> None:
        conn.execute("INSERT OR IGNORE INTO correction_passes(correction_id, status, queued_at, updated_at)"
                     " VALUES (?,?,?,?)", (int(correction_id), status, now, now))

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
                    importance = ch.importance
                    if importance is None and old is not None and "importance" in old.keys():
                        importance = old["importance"]   # an UPDATE that does not re-rate keeps the rating
                    # a new version keeps the user's pin and review (migration 33): they were about this fact
                    pinned = None if old is None else (bool(old["pinned"]) or ch.origin == "user")
                    reviewed = None if old is None else old["reviewed_at"]
                    new_id = self._insert_fact(
                        conn, kind=ch.kind, subject=ch.subject, text=ch.text, valid_from=at,
                        source_ids=ch.source_ids, confidence=ch.confidence, origin=ch.origin,
                        run_id=run_id, now=now, episode_ids=ch.episode_ids, aspect=ch.aspect,
                        importance=importance, summary=ch.summary, shown=ch.shown, pinned=pinned,
                        reviewed_at=reviewed,
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

    # -- the portrait audit (yuki.memory.portrait, "connect the dots") -------------

    def journal_by_ids(self, ids: Iterable[int]) -> list[JournalEntry]:
        """The current (not superseded) journal rows with these ids, oldest first."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return []
        return [self._entry(r) for r in self._query(
            "SELECT j.* FROM journal j WHERE j.superseded_at IS NULL AND j.id IN (%s) ORDER BY j.at, j.id"
            % ",".join("?" * len(wanted)), wanted)]

    def similar_journal(self, vectors: Sequence[np.ndarray], limit: int = 6) -> list[list[JournalEntry]]:
        """For each query vector, the ``limit`` most similar current journal rows (cosine), best first.

        The journal's vectors are decrypted once for all queries (the audit asks for
        dozens at a time; :meth:`search_journal` would decrypt them once per query).
        """
        if not vectors:
            return []
        rows = [r for r in self._query(
            "SELECT j.*, v.vec_ciphertext, v.dim FROM journal j JOIN journal_vec v ON v.journal_id = j.id"
            " WHERE j.superseded_at IS NULL")]
        dim = int(np.asarray(vectors[0]).ravel().shape[0])
        rows = [r for r in rows if r["dim"] == dim]
        if not rows:
            return [[] for _ in vectors]
        matrix = np.stack([np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
                           for r in rows])
        norms = np.linalg.norm(matrix, axis=1)
        norms[norms == 0] = 1.0
        out: list[list[JournalEntry]] = []
        for vec in vectors:
            q = np.asarray(vec, dtype=np.float32).ravel()
            qn = float(np.linalg.norm(q)) or 1.0
            scores = (matrix @ (q / qn)) / norms
            order = np.argsort(-scores)[: max(0, int(limit))]
            out.append([self._entry(rows[i], float(scores[i])) for i in order])
        return out

    def batch_captures(self, batch_ids: Iterable[int], *, per_batch: int = 3) -> dict[int, list[dict[str, Any]]]:
        """``{batch id: [{id, at, kind, thread_id, text}]}``: the captures still stored that a journal batch
        was read from (at most ``per_batch`` each, oldest first; text decrypted). Captures past their TTL are gone."""
        ids = sorted({int(b) for b in batch_ids})
        if not ids:
            return {}
        out: dict[int, list[dict[str, Any]]] = {}
        for r in self._query(
            "SELECT id, at, kind, thread_id, journal_batch_id, delta_ciphertext FROM captures"
            " WHERE journal_batch_id IN (%s) ORDER BY at, id" % ",".join("?" * len(ids)), ids):
            items = out.setdefault(int(r["journal_batch_id"]), [])
            if len(items) < per_batch:
                items.append({"id": int(r["id"]), "at": float(r["at"]), "kind": r["kind"],
                              "thread_id": r["thread_id"],
                              "text": user_side_text(r["kind"], self._dec(r["delta_ciphertext"])) or ""})
        return out

    def first_capture_text(self, thread_id: int, before: float | None = None) -> tuple[float, str] | None:
        """``(at, text)`` of the oldest capture still stored for a thread (before ``before``), or ``None``:
        the top of a page, where it shows its title, author and date, when later captures are deltas."""
        sql, params = "SELECT at, kind, delta_ciphertext FROM captures WHERE thread_id=?", [int(thread_id)]
        if before is not None:
            sql += " AND at < ?"
            params.append(float(before))
        rows = self._query(sql + " ORDER BY at, id LIMIT 1", params)
        return ((float(rows[0]["at"]), user_side_text(rows[0]["kind"], self._dec(rows[0]["delta_ciphertext"])) or "")
                if rows else None)

    def journal_superseded_since(self, journal_ids: Iterable[int], since: float) -> set[int]:
        """Those of ``journal_ids`` a re-journal replaced after ``since``."""
        ids = sorted({int(i) for i in journal_ids})
        if not ids:
            return set()
        return {int(r["id"]) for r in self._query(
            "SELECT id FROM journal WHERE superseded_at > ? AND id IN (%s)" % ",".join("?" * len(ids)),
            [float(since), *ids])}

    def questions_changed_since(self, since: float) -> list[QuestionRecord]:
        """Questions answered, skipped, refused or expired after ``since`` (their facts may now be settled)."""
        return [self._question_row(r) for r in self._query(
            "SELECT * FROM questions WHERE updated_at > ? AND status IN ('answered', 'skipped', 'never', 'expired')",
            (float(since),))]

    def add_audit_findings(self, run_id: int | None, findings: Sequence[dict[str, Any]],
                           at: float | None = None) -> list[int]:
        """Record an audit run's findings (the reasoning encrypted); returns their ids."""
        when = float(at) if at is not None else time.time()

        def op(conn: sqlite3.Connection) -> list[int]:
            out = []
            for f in findings:
                cur = conn.execute(
                    "INSERT INTO portrait_audits(run_id, at, finding, decision, fact_ids, journal_ids, new_fact_ids,"
                    " question_ids, reasoning_ciphertext) VALUES (?,?,?,?,?,?,?,?,?)",
                    (run_id, when, str(f.get("finding") or "contradiction"), str(f.get("decision") or "kept"),
                     json.dumps([int(i) for i in f.get("fact_ids") or []]),
                     json.dumps([int(i) for i in f.get("journal_ids") or []]),
                     json.dumps([int(i) for i in f.get("new_fact_ids") or []]),
                     json.dumps([int(i) for i in f.get("question_ids") or []]),
                     self._enc(str(f.get("reasoning") or "") or None)),
                )
                out.append(int(cur.lastrowid))
            return out

        return self._write(op) if findings else []

    def audit_findings(self, *, run_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """Recorded audit findings (reasoning decrypted), newest first."""
        sql, params = "SELECT * FROM portrait_audits", []
        if run_id is not None:
            sql += " WHERE run_id=?"
            params.append(int(run_id))
        out = []
        for r in self._query(sql + " ORDER BY id DESC LIMIT ?", [*params, int(limit)]):
            item = dict(r)
            for key in ("fact_ids", "journal_ids", "new_fact_ids", "question_ids"):
                item[key] = json.loads(item[key] or "[]")
            item["reasoning"] = self._dec(item.pop("reasoning_ciphertext"))
            out.append(item)
        return out

    def save_portrait(
        self, text: str, *, run_id: int | None = None, model: str | None = None, fact_count: int = 0,
        at: float | None = None, sections: dict[str, str] | None = None, brief: dict[str, Any] | None = None,
    ) -> int:
        """Store a rendered portrait (encrypted); the newest one is the current portrait. ``sections``: the page's
        fields (migration 27); ``brief``: the personal brief the same render wrote (``yuki.memory.brief``), kept in
        the sections' JSON under :data:`BRIEF_SECTION`."""
        when = float(at) if at is not None else time.time()
        payload: dict[str, Any] = dict(sections or {})
        if brief:
            payload[BRIEF_SECTION] = brief
        sealed = self.cipher.encrypt(json.dumps(payload, ensure_ascii=False)) if payload else None
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO portraits(at, run_id, model, text_ciphertext, chars, fact_count, sections_ciphertext)"
            " VALUES (?,?,?,?,?,?,?)",
            (when, run_id, model, self.cipher.encrypt(text), len(text), int(fact_count), sealed),
        ).lastrowid))

    def latest_portrait(self) -> Portrait | None:
        """The newest rendered portrait, decrypted, or ``None``."""
        rows = self._query("SELECT * FROM portraits ORDER BY at DESC, id DESC LIMIT 1")
        if not rows:
            return None
        r = rows[0]
        sections = None
        brief = None
        if "sections_ciphertext" in r.keys() and r["sections_ciphertext"]:
            try:
                value = json.loads(self.cipher.decrypt(r["sections_ciphertext"]))
                if isinstance(value, dict):
                    raw_brief = value.pop(BRIEF_SECTION, None)
                    brief = raw_brief if isinstance(raw_brief, dict) and raw_brief else None
                    sections = {str(k): str(v) for k, v in value.items()} or None
            except Exception:
                sections = None
        return Portrait(id=r["id"], at=r["at"], text=self.cipher.decrypt(r["text_ciphertext"]),
                        run_id=r["run_id"], model=r["model"], fact_count=r["fact_count"], sections=sections,
                        brief=brief)

    def portrait_at(self, at: float) -> Portrait | None:
        """The portrait that was current at ``at`` (the newest rendered at or before it), or ``None``; the brief a
        reply or card of that moment was written with (the personal-ness judge)."""
        rows = self._query("SELECT id FROM portraits WHERE at <= ? ORDER BY at DESC, id DESC LIMIT 1", (float(at),))
        if not rows:
            return None
        latest = self.latest_portrait()
        if latest is not None and latest.id == rows[0]["id"]:
            return latest
        r = self._query("SELECT * FROM portraits WHERE id=?", (rows[0]["id"],))[0]
        brief = None
        try:
            value = json.loads(self.cipher.decrypt(r["sections_ciphertext"])) if r["sections_ciphertext"] else {}
            raw = value.get(BRIEF_SECTION) if isinstance(value, dict) else None
            brief = raw if isinstance(raw, dict) and raw else None
        except Exception:
            brief = None
        return Portrait(id=r["id"], at=r["at"], text=self.cipher.decrypt(r["text_ciphertext"]), run_id=r["run_id"],
                        model=r["model"], fact_count=r["fact_count"], sections=None, brief=brief)

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
        # What Yuki put on screen for the user (by_yuki) is not the user's routine.
        cwhere = where + (" AND " if where else " WHERE ") + "c.by_yuki = 0"
        for r in self._query(
            "SELECT c.at, t.app, t.process, t.host FROM captures c JOIN threads t ON t.id = c.thread_id" + cwhere,
            params,
        ):
            slot = int(r["at"] // slot_s)
            app = _shown(r["app"] or "", r["process"])
            apps.setdefault(app, set()).add(slot)
            if r["host"]:
                hosts.setdefault((app, r["host"]), set()).add(slot)
        hwhere = where.replace("c.at", "h.at")
        hwhere += (" AND " if hwhere else " WHERE ") + (
            "h.outcome IN ('captured','deduplicated','unchanged','no_new_text','empty') AND coalesce(h.app,'') != ''"
        )
        for r in self._query("SELECT h.at, h.app FROM health h" + hwhere, params):
            apps.setdefault(_shown(r["app"]), set()).add(int(r["at"] // slot_s))
        return {"apps": apps, "hosts": hosts, "slot_s": slot_s}

    # -- know-how ----------------------------------------------------------

    def _knowhow(self, r: sqlite3.Row, score: float | None = None) -> KnowHow:
        return KnowHow(
            id=r["id"], app=r["app"], text=self.cipher.decrypt(r["text_ciphertext"]), valid_from=r["valid_from"],
            valid_to=r["valid_to"], source_request=self._dec(r["source_request_ciphertext"]),
            superseded_by=r["superseded_by"], score=score,
            title=self._dec(r["title_ciphertext"]) if "title_ciphertext" in r.keys() else None,
            summary=self._dec(r["summary_ciphertext"]) if "summary_ciphertext" in r.keys() else None,
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
        origin: str | None = None,
        title: str | None = None,
        summary: str | None = None,
    ) -> int:
        """Write a know-how entry; with ``supersedes``, end that entry (bi-temporal) in the same transaction.

        ``origin``: ``agent`` (the agent's remember_how) or ``conversation`` (the conversation worker's
        post-job extraction after a correction led to success).

        ``app`` is stored as :func:`app_key` (``"Arc.exe"`` -> ``"arc"``); ``None``/empty = general.
        """
        now = time.time()
        when = float(at) if at is not None else now
        key = app_key(app)

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO knowhow(app, task_kind, text_ciphertext, valid_from, valid_to,"
                " source_request_ciphertext, created_at, origin, title_ciphertext, summary_ciphertext)"
                " VALUES (?,NULL,?,?,NULL,?,?,?,?,?)",
                (key, self.cipher.encrypt(text), when, self._enc(source_request), now, origin,
                 self._enc(title) if title else None, self._enc(summary) if summary else None),
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

    def knowhow_duplicate(self, app: str | None, text: str, vector: np.ndarray | None,
                          threshold: float = 0.85) -> int | None:
        """The current entry a new know-how entry restates (same app; identical text, or embedding cosine at
        least ``threshold``), which it then supersedes; ``None`` when it is new."""
        key = app_key(app)
        candidates = self.knowhow_current(key) if key else self.knowhow_current(None, any_app=False)
        for k in candidates:
            if k.text.casefold() == (text or "").casefold():
                return k.id
        if vector is None or not candidates:
            return None
        vec = np.asarray(vector, dtype=np.float32).ravel()
        stored = self.knowhow_vectors([k.id for k in candidates])
        best, best_id = -1.0, None
        for k in candidates:
            other = stored.get(k.id)
            if other is None or other.shape != vec.shape:
                continue
            score = float(np.dot(vec, other) / ((np.linalg.norm(other) * np.linalg.norm(vec)) or 1.0))
            if score >= threshold and score > best:
                best, best_id = score, k.id
        return best_id

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
            title=self._dec(r["title_ciphertext"]) if "title_ciphertext" in r.keys() else None,
        )

    def open_loops(self, status: str | None = "open") -> list[OpenLoop]:
        """Open loops with this status (``None`` = all), oldest first."""
        sql = _LOOPS_WITH_TITLES
        params: list[Any] = []
        if status is not None:
            sql += " WHERE l.status = ?"
            params.append(status)
        return [self._loop(r) for r in self._query(sql + " ORDER BY l.opened_at, l.id", params)]

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
        conversation_cost = self._query(
            "SELECT coalesce(sum(cost_usd), 0) FROM conversation_batches WHERE at >= ?", (s,))[0][0]
        nudge_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM nudge_checkins WHERE at >= ?", (s,))[0][0]
        review_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM weekly_reviews WHERE at >= ?", (s,))[0][0]
        digest_cost = self._query("SELECT coalesce(sum(cost_usd), 0) FROM call_digests WHERE updated_at >= ?",
                                  (s,))[0][0]
        return {
            "captures": int(captures), "facts": int(facts), "last_capture_at": last,
            "journal_cost_usd": float(journal_cost or 0.0), "portrait_cost_usd": float(portrait_cost or 0.0),
            "episode_cost_usd": float(episode_cost or 0.0), "conversation_cost_usd": float(conversation_cost or 0.0),
            "nudge_cost_usd": float(nudge_cost or 0.0), "review_cost_usd": float(review_cost or 0.0),
            "call_digest_cost_usd": float(digest_cost or 0.0),
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
            round(float(row.mic_s or 0.0), 3), int(bool(row.by_yuki)),
            self._enc(row.yuki_request) if row.by_yuki and row.yuki_request else None,
        )

        def op(conn: sqlite3.Connection) -> int:
            if row.id is not None:
                cur = conn.execute(
                    "UPDATE timeline SET started_at=?, ended_at=?, state=?, process=?, app=?, title_ciphertext=?,"
                    " host=?, path_ciphertext=?, page_key=?, active_s=?, passive_s=?, away_s=?, media_s=?,"
                    " media_other=?, withheld=?, segments=?, updated_at=?, fullscreen=?, meeting=?, mic_s=?,"
                    " by_yuki=?, yuki_request_ciphertext=? WHERE id=?",
                    (*values, int(row.id)),
                )
                if cur.rowcount == 1:
                    return int(row.id)
            cur = conn.execute(
                "INSERT INTO timeline(started_at, ended_at, state, process, app, title_ciphertext, host,"
                " path_ciphertext, page_key, active_s, passive_s, away_s, media_s, media_other, withheld,"
                " segments, updated_at, fullscreen, meeting, mic_s, by_yuki, yuki_request_ciphertext)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
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
            app=_shown(r["app"], r["process"]), title=self._dec(r["title_ciphertext"]), host=r["host"],
            path=self._dec(r["path_ciphertext"]),
            page_key=r["page_key"], active_s=r["active_s"], passive_s=r["passive_s"], away_s=r["away_s"],
            media_s=r["media_s"], media_other=other, withheld=r["withheld"], segments=r["segments"],
            fullscreen=bool(r["fullscreen"]), meeting=r["meeting"], mic_s=r["mic_s"] or 0.0,
            by_yuki=bool(r["by_yuki"]), yuki_request=self._dec(r["yuki_request_ciphertext"]),
            source=r["source"],
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
        raw = self._query("SELECT * FROM timeline" + where + " ORDER BY started_at, id", params)
        return self._apply_proof([self._timeline_row(r) for r in raw], [float(r["updated_at"] or 0.0) for r in raw])

    #: A meeting row needs a call of the same app around it, this close in time (seconds): the call worker sees a
    #: call some seconds after the app takes the microphone and lets it linger ~2 minutes after it lets go.
    MEETING_CALL_SLACK_S = 180.0
    #: ``calls.via`` values that mean a screen or a calling site was seen in a call (not only a microphone hold).
    SEEN_CALL_VIA = ("screen", "screen_stale", "meeting")

    def _apply_proof(self, rows: list[TimelineRow], written: Sequence[float] | None = None) -> list[TimelineRow]:
        """The one read-time meeting proof, for every timeline reader (:meth:`timeline_between`,
        :meth:`timeline_of`, :meth:`timeline_states`; phase F, fPrivacy). Decrypt-free: it reads process, app,
        meeting, host and times only, and rows are never edited.

        Rows recorded before the timeline's live call-evidence gate (migration 27) that it marked a meeting only
        because the app in front held the microphone, with no qualifying call of that app around them, read as that
        app's own time (``meeting`` None): a game's voice chat (VALORANT) or a music app recording (FL Studio) is not
        a meeting (revamp A9: 1 h 35 m of "meetings" on 2 Oct were those, and they held back the coach's cards). The
        evidence is measured: the call worker writes a call row (a declined one too) for every hold on a screen
        judged in a call, so a meeting with no call row of its app was never seen as one.

        A qualifying call (decision D7, the same rule as the moments' ``call_join``): a ``calls`` row of kind
        ``call`` whose program is not a game (:func:`yuki.memory.gamegate.judge_program`, machine facts only, never
        what is in front now), and, when it is only a microphone hold (``via='mic'``, from before screen evidence),
        of an app with a seen call on record (``via`` screen / screen_stale / meeting) or one of the user's own
        ``[calls] apps``. Rows from before the first call row ever written stay as they were (nothing to judge them
        by), and so do rows since the gate (judged live) and a site's meeting rows (``host`` set: the site that took
        the device in a browser)."""
        meet = [r for r in rows if r.meeting and r.process and not r.host]   # an app's own meeting, not a site's
        if not meet:
            return rows
        gate = getattr(self, "_meeting_gate_since", None)
        if gate is None:
            try:
                value = self.source_checkpoint(MEETING_GATE_CHECKPOINT)
                gate = float(value) if value else 0.0
            except (sqlite3.OperationalError, ValueError):
                gate = 0.0
            self._meeting_gate_since = gate
        written = list(written) if written is not None else [r.ended_at for r in rows]
        before = {id(r) for r, w in zip(rows, written) if w < gate}   # last written before the live gate
        meet = [r for r in meet if id(r) in before]   # rows written since were judged on a screen already
        if not meet:
            return rows
        seen_marks = ",".join("?" * len(self.SEEN_CALL_VIA))
        try:
            first = self._query("SELECT min(detected_at) FROM calls")[0][0]
            if first is None:
                return rows
            lo = min(r.started_at for r in meet) - self.MEETING_CALL_SLACK_S
            hi = max(r.ended_at for r in meet) + self.MEETING_CALL_SLACK_S
            now = time.time()
            # a game's voice chat (calls.kind "game", migration 34) is never a meeting's proof
            calls = self._query(
                "SELECT process, detected_at, coalesce(ended_at, last_alive_at, ?), app, via FROM calls"
                " WHERE detected_at < ? AND coalesce(ended_at, last_alive_at, ?) > ? AND kind = 'call'",
                (now, hi, now, lo))
            seen = self._query(f"SELECT DISTINCT process, app FROM calls WHERE kind = 'call' AND via IN ({seen_marks})",
                               self.SEEN_CALL_VIA)
        except sqlite3.OperationalError:   # a database before migration 26 (no last_alive_at) or 34 (no kind)
            try:
                first = self._query("SELECT min(detected_at) FROM calls")[0][0]
                now = time.time()
                calls = self._query("SELECT process, detected_at, coalesce(ended_at, ?), app, via FROM calls", (now,))
                seen = self._query(f"SELECT DISTINCT process, app FROM calls WHERE via IN ({seen_marks})",
                                   self.SEEN_CALL_VIA)
            except sqlite3.OperationalError:
                return rows
            if first is None:
                return rows
        slack = self.MEETING_CALL_SLACK_S
        games: dict[str, bool] = {}

        def game(process: str | None) -> bool:
            key = app_key(process) or ""
            if key not in games:
                games[key] = self._program_is_game(key)
            return games[key]

        # the same app: its process key, or its name as Yuki resolves it (a packaged app's call row is keyed by its
        # package, "com.tinyspeck.slackdesktop", where the timeline has the image, "slack")
        def keys_of(process: str | None, app: str | None) -> tuple[str, str]:
            return app_key(process) or "", _shown(app, process).casefold()

        allowed: set[str] = set(self._calls_apps_keys())
        for c in seen:
            if not game(c[0]):
                allowed.update(k for k in keys_of(c[0], c[1]) if k)
        spans = []
        for c in calls:
            if game(c[0]):
                continue
            p, n = keys_of(c[0], c[3])
            if c[4] == "mic" and p not in allowed and n not in allowed:
                continue   # a microphone hold of an app never seen in a call (VALORANT's 27 Sep rows)
            spans.append((p, n, float(c[1]), float(c[2])))
        out: list[TimelineRow] = []
        for r in rows:
            if r.meeting and r.process and not r.host and float(first) <= r.started_at and id(r) in before:
                key, name = app_key(r.process) or "", (r.app or "").casefold()
                if not any((p == key or (name and n == name)) and start < r.ended_at + slack and end > r.started_at - slack
                           for p, n, start, end in spans):
                    r = replace(r, meeting=None)
            out.append(r)
        return out

    #: The name it had before phase F (the docs and older tests refer to it).
    _meetings_proven = _apply_proof

    @staticmethod
    def _program_is_game(key: str) -> bool:
        """Whether a program (its app key) is a game, by machine facts only (:func:`yuki.memory.gamegate.judge_program`:
        the fixed anti-cheat list, the user's additions, learned Direct3D full screen), never by what is in front
        now (a stored row's proof must not change with the screen)."""
        if not key:
            return False
        try:
            from yuki.memory.gamegate import judge_program

            return bool(judge_program(key, d3d_fn=lambda: False)[0])
        except Exception:  # noqa: BLE001 - no game facts: not a game
            return False

    def _calls_apps_keys(self) -> list[str]:
        """The user's own ``[calls] apps`` (app keys) from the privacy file beside this database (read again when it
        changes); ``[]`` when there is none or it cannot be read (a copy of the database carries no privacy file)."""
        path = Path(self.path).parent / "privacy.toml"
        try:
            stamp = path.stat().st_mtime_ns
        except OSError:
            return []
        cached = getattr(self, "_calls_apps_cache", None)
        if cached is not None and cached[0] == stamp:
            return cached[1]
        keys: list[str] = []
        try:
            import tomllib

            with path.open("rb") as fh:
                apps = (tomllib.load(fh).get("calls") or {}).get("apps") or []
            keys = [k for k in (app_key(str(a)) for a in apps if str(a).strip()) if k]
        except Exception:  # noqa: BLE001 - a broken file proves nothing
            keys = []
        self._calls_apps_cache = (stamp, keys)
        return keys

    def timeline_of(self, process: str, since: TimeArg = None) -> list[TimelineRow]:
        """One app's stretches (``process`` = its ``app_key``) ending after ``since``, oldest first (decrypted): only
        its rows are decrypted (v3.13, the voice's recent titles of the window in front). Phase F: through the one
        meeting proof (:meth:`_apply_proof`), like every timeline reader."""
        where, params = " WHERE process = ?", [process]
        if (s := _ts(since)) is not None:
            where += " AND ended_at > ?"
            params.append(s)
        raw = self._query("SELECT * FROM timeline" + where + " ORDER BY started_at, id", params)
        return self._apply_proof([self._timeline_row(r) for r in raw], [float(r["updated_at"] or 0.0) for r in raw])

    def timeline_states(self, since: TimeArg = None, until: TimeArg = None) -> list[TimelineRow]:
        """Like :meth:`timeline_between` but content-free and cheap: nothing is decrypted.

        The rows carry times, state, app, host, counters and flags only (no
        title, path or Yuki request): enough for presence and breaks, read on
        every coach tick over a day and a half. Phase F: through the one meeting
        proof (:meth:`_apply_proof`, decrypt-free), like every timeline reader.
        """
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("ended_at > ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("started_at < ?")
            params.append(u)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        raw = self._query(
            "SELECT id, started_at, ended_at, state, process, app, host, page_key, active_s, passive_s, away_s,"
            " media_s, withheld, fullscreen, meeting, mic_s, by_yuki, source, updated_at FROM timeline" + where
            + " ORDER BY started_at, id", params)
        return self._apply_proof([TimelineRow(
            id=r["id"], started_at=r["started_at"], ended_at=r["ended_at"], state=r["state"], process=r["process"],
            app=_shown(r["app"], r["process"]), host=r["host"], page_key=r["page_key"], active_s=r["active_s"] or 0.0,
            passive_s=r["passive_s"] or 0.0, away_s=r["away_s"] or 0.0, media_s=r["media_s"] or 0.0,
            withheld=r["withheld"], fullscreen=bool(r["fullscreen"]), meeting=r["meeting"], mic_s=r["mic_s"] or 0.0,
            by_yuki=bool(r["by_yuki"]), source=r["source"],
        ) for r in raw], [float(r["updated_at"] or 0.0) for r in raw])

    def timeline_bounds(self) -> tuple[float | None, float | None]:
        """Earliest start and latest end in the timeline."""
        r = self._query("SELECT min(started_at), max(ended_at) FROM timeline")[0]
        return (r[0], r[1])

    # -- the PC's power history (yuki.memory.power) ------------------------------

    def add_power_spans(self, spans: Iterable[tuple[str, float, float, str | None]]) -> int:
        """Record ``(state, start, end, source)`` power spans as timeline rows; returns how many were new.

        ``state`` is one of :data:`POWER_STATES`. A span already recorded
        (same state and end) is left as it is, so the log can be re-read.
        """
        values = []
        now = time.time()
        for state, start, end, source in spans:
            if state not in POWER_STATES or not end > start:
                continue
            values.append((float(start), float(end), state, "", self.cipher.digest("tl-power", state), now, source))

        def op(conn: sqlite3.Connection) -> int:
            added = 0
            for v in values:
                cur = conn.execute(
                    "INSERT OR IGNORE INTO timeline(started_at, ended_at, state, app, page_key, updated_at, source)"
                    " VALUES (?,?,?,?,?,?,?)", v)
                added += cur.rowcount
            return added

        return self._write(op) if values else 0

    def power_spans(self, since: TimeArg = None, until: TimeArg = None) -> list[TimelineRow]:
        """Power rows (off / asleep / unknown) overlapping ``[since, until)``, oldest first."""
        clauses = ["state IN (%s)" % ",".join("?" * len(POWER_STATES))]
        params: list[Any] = list(POWER_STATES)
        if (s := _ts(since)) is not None:
            clauses.append("ended_at > ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("started_at < ?")
            params.append(u)
        return [self._timeline_row(r) for r in self._query(
            "SELECT * FROM timeline WHERE " + " AND ".join(clauses) + " ORDER BY started_at, id", params)]

    def timeline_last_end_before(self, at: float) -> float | None:
        """The latest end of a recorded stretch (present or away) that began before ``at``, or ``None``."""
        r = self._query(
            "SELECT max(ended_at) FROM timeline WHERE started_at < ? AND state IN ('present', 'away')",
            (float(at),))[0]
        return float(r[0]) if r[0] is not None else None

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
            self._captured(ids)
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
                    " aggregates_ciphertext, run_id, created_at, title_ciphertext, summary_ciphertext)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (float(ep.started_at), float(ep.ended_at), float(ep.window_start), float(ep.window_end),
                     int(bool(ep.final)), self.cipher.encrypt(ep.text),
                     self.cipher.encrypt(json.dumps(ep.aggregates, ensure_ascii=False, default=str)),
                     run_id, now, self._enc(ep.title or None), self._enc(ep.summary or None)),
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
            title=self._dec(r["title_ciphertext"]) if "title_ciphertext" in r.keys() else None,
            summary=self._dec(r["summary_ciphertext"]) if "summary_ciphertext" in r.keys() else None,
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

    def episodes_by_ids(self, ids: Iterable[int]) -> list[EpisodeRecord]:
        """Episodes (any version) with these ids, oldest first."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return []
        return [self._episode(r) for r in self._query(
            "SELECT e.* FROM episodes e WHERE e.id IN (%s) ORDER BY e.started_at, e.id" % ",".join("?" * len(wanted)),
            wanted,
        )]

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

    # -- conversations with Yuki (yuki.memory.conversations) -----------------

    #: The journal thread that facts learned in conversations with Yuki are filed under.
    CONVERSATION_APP = "Yuki"
    CONVERSATION_SCOPE = "yuki:conversation"

    def _put_vec(self, conn: sqlite3.Connection, table: str, id_col: str, item_id: int,
                 vector: np.ndarray, model: str | None) -> None:
        vec = np.asarray(vector, dtype=np.float32).ravel()
        conn.execute(
            f"INSERT OR REPLACE INTO {table}({id_col}, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
            (int(item_id), model or "", int(vec.shape[0]), self.cipher.encrypt_bytes(vec.tobytes())),
        )

    def _rank(self, rows: list[sqlite3.Row], query_vec: np.ndarray, limit: int) -> list[tuple[sqlite3.Row, float]]:
        """Rows carrying ``vec_ciphertext``/``dim``, best cosine first."""
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
        return [(keep[i], float(scores[i])) for i in order]

    # turns ------------------------------------------------------------------

    def _next_turn_id(self) -> int:
        """A turn id known before the write: the microsecond clock, strictly increasing per store."""
        with self._id_lock:
            self._last_turn_id = max(time.time_ns() // 1000, self._last_turn_id + 1)
            return self._last_turn_id

    def _exec_turn_insert(self, conn: sqlite3.Connection, row: list[Any]) -> bool:
        """INSERT one turn row on ``conn``; False when the database stayed locked past its busy timeout.

        A primary-key clash (another process took the same microsecond) moves
        the id on by one and retries.
        """
        for _ in range(8):
            try:
                conn.execute(
                    "INSERT INTO conversation_turns(id, session_id, request_id, at, user_ciphertext,"
                    " reply_ciphertext, actions_ciphertext, outcome, created_at, addressed, envelope_ciphertext)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    row,
                )
                return True
            except sqlite3.IntegrityError:
                with self._id_lock:
                    row[0] = int(row[0]) + 1
                    self._last_turn_id = max(self._last_turn_id, row[0])
            except sqlite3.OperationalError as exc:
                if "locked" in str(exc) or "busy" in str(exc):
                    return False
                raise
        raise sqlite3.IntegrityError("could not allocate a conversation turn id")

    def _insert_turn_row(self, row: list[Any], busy_timeout_s: float) -> bool:
        """The fast path: the dedicated turn connection, waiting at most ``busy_timeout_s`` for the lock."""
        with self._turn_lock:
            if self._turn_conn is None:
                conn = sqlite3.connect(str(self.path), timeout=0.05, isolation_level=None, check_same_thread=False)
                conn.execute("PRAGMA synchronous=NORMAL")
                self._turn_conn = conn
            self._turn_conn.execute(f"PRAGMA busy_timeout={max(1, int(busy_timeout_s * 1000))}")
            return self._exec_turn_insert(self._turn_conn, row)

    def _defer_turn(self, row: list[Any]) -> None:
        """Hand a turn to the background writer (the database was busy, or earlier turns still wait)."""
        with self._deferred_lock:
            self._deferred_turns.append(row)
            if self._turn_thread is None or not self._turn_thread.is_alive():
                self._turn_thread = threading.Thread(target=self._drain_turns, name="yuki-memory-turns", daemon=True)
                self._turn_thread.start()

    def _drain_turns(self) -> None:
        """Write deferred turns in order on the writer's own connection (never the fast path's lock)."""
        conn = sqlite3.connect(str(self.path), timeout=10.0, isolation_level=None, check_same_thread=False)
        try:
            while True:
                with self._deferred_lock:
                    if not self._deferred_turns:
                        self._turn_thread = None
                        return
                    row = self._deferred_turns[0]
                try:
                    ok = self._exec_turn_insert(conn, row)
                except Exception as exc:
                    self.turn_write_errors += 1
                    self.last_turn_error = f"{type(exc).__name__}: {exc}"
                    ok = True   # dropped: a row that cannot be written never blocks the ones after it
                if ok:
                    with self._deferred_lock:
                        if self._deferred_turns and self._deferred_turns[0] is row:
                            self._deferred_turns.pop(0)
        finally:
            conn.close()

    def add_turn(
        self, session_id: str, request_id: int | str, at: float, user_text: str, reply_text: str | None = None,
        actions: Sequence[str] = (), outcome: str | None = None, *, busy_timeout_s: float = 0.008,
        addressed: str | None = None, envelope: str | None = None,
    ) -> int:
        """Store one exchange (encrypted) for the conversation worker; returns its id.

        ``actions`` are one-line tool summaries (stored as an encrypted JSON
        list); ``outcome`` is cleartext metadata ("done", "cancelled", ...).
        Written through a dedicated connection that waits at most
        ``busy_timeout_s`` for the write lock: if another writer holds it
        longer, the row goes to a background thread that retries and the id is
        returned at once (ids come from a microsecond clock, so they are known
        before the write lands). Raises only on encryption/argument errors.
        ``addressed``: who a voice turn was said to (:data:`ADDRESSED`, from voice's ``turn_closed``);
        a turn said to others is stored for the record but never extracted (migration 18); an "unanswered"
        call turn is extracted with its addressee left to the extractor's judgement (report 77b).
        ``envelope``: the notes Yuki put after the user's words; ``None``: read off ``user_text`` itself
        (:func:`split_envelope`), so the words and the notes are always stored apart (migration 27).
        """
        now = time.time()
        acts = [" ".join(str(a).split()) for a in (actions or ()) if str(a).strip()]
        if envelope is None:
            user_text, envelope = split_envelope(user_text)
        row: list[Any] = [
            self._next_turn_id(), str(session_id or ""), _request_key(request_id), float(at),
            self.cipher.encrypt(user_text or ""), self._enc(reply_text),
            self.cipher.encrypt(json.dumps(acts, ensure_ascii=False)) if acts else None,
            outcome or None, now, addressed if addressed in ADDRESSED else None,
            self._enc(envelope) if envelope else None,
        ]
        with self._deferred_lock:
            queued = bool(self._deferred_turns)
        if queued or not self._insert_turn_row(row, busy_timeout_s):
            self._defer_turn(row)   # behind the turns already waiting, in order
        return int(row[0])

    def flush_turns(self, timeout_s: float = 5.0) -> int:
        """Write turns still waiting in the background writer now; returns how many are left."""
        with self._deferred_lock:
            thread = self._turn_thread
        if thread is not None:
            thread.join(max(0.0, timeout_s))   # the writer itself drains (no second inserter, no duplicates)
        with self._deferred_lock:
            return len(self._deferred_turns)

    def _turn(self, r: sqlite3.Row, score: float | None = None) -> ConversationTurn:
        try:
            actions = json.loads(self._dec(r["actions_ciphertext"]) or "[]")
        except ValueError:
            actions = []
        keys = r.keys()
        words = self.cipher.decrypt(r["user_ciphertext"])
        envelope = self._dec(r["envelope_ciphertext"]) if "envelope_ciphertext" in keys else None
        if envelope is None:   # a turn stored before migration 27: its words and Yuki's notes are read apart here
            words, envelope = split_envelope(words)
        return ConversationTurn(
            id=r["id"], session_id=r["session_id"], request_id=r["request_id"], at=r["at"],
            user_text=words, reply_text=self._dec(r["reply_ciphertext"]),
            actions=[str(a) for a in actions] if isinstance(actions, list) else [], outcome=r["outcome"],
            extracted=bool(r["extracted"]), attempts=r["extract_attempts"], summary_id=r["summary_id"],
            created_at=r["created_at"], score=score,
            addressed=r["addressed"] if "addressed" in keys else None, envelope=envelope,
        )

    def turns_pending(self, max_attempts: int = 3, limit: int | None = None) -> list[ConversationTurn]:
        """Turns the conversation worker has not extracted yet (nor given up on), oldest first."""
        sql = ("SELECT * FROM conversation_turns WHERE extracted=0 AND extract_attempts < ?"
               " ORDER BY at, id")
        params: list[Any] = [int(max_attempts)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._turn(r) for r in self._query(sql, params)]

    def turns_unsummarized(self, max_attempts: int = 3) -> list[ConversationTurn]:
        """Turns no session summary covers yet (nor given up on), oldest first."""
        return [self._turn(r) for r in self._query(
            "SELECT * FROM conversation_turns WHERE summary_id IS NULL AND summary_attempts < ? ORDER BY at, id",
            (int(max_attempts),),
        )]

    def turns_for_session(
        self, session_id: str, *, before: TimeArg = None, since: TimeArg = None, limit: int | None = None,
    ) -> list[ConversationTurn]:
        """A session's turns in ``[since, before)``; with ``limit`` the newest N. Oldest first."""
        clauses, params = ["session_id = ?"], [str(session_id)]
        if (b := _ts(before)) is not None:
            clauses.append("at < ?")
            params.append(b)
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        sql = "SELECT * FROM conversation_turns WHERE " + " AND ".join(clauses) + " ORDER BY at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._turn(r) for r in reversed(self._query(sql, params))]

    def turns_by_ids(self, ids: Iterable[int]) -> list[ConversationTurn]:
        """Turns with these ids, oldest first."""
        wanted = [int(i) for i in ids]
        if not wanted:
            return []
        rows = self._query(
            "SELECT * FROM conversation_turns WHERE id IN (%s) ORDER BY at, id" % ",".join("?" * len(wanted)), wanted
        )
        return [self._turn(r) for r in rows]

    def session_span(self, session_id: str) -> tuple[float | None, float | None, int]:
        """``(first exchange at, last exchange at, exchanges)`` of one session."""
        r = self._query("SELECT min(at), max(at), count(*) FROM conversation_turns WHERE session_id=?",
                        (str(session_id),))[0]
        return (r[0], r[1], int(r[2] or 0))

    def latest_turn(self) -> ConversationTurn | None:
        """The newest turn of any session, or ``None``."""
        rows = self._query("SELECT * FROM conversation_turns ORDER BY at DESC, id DESC LIMIT 1")
        return self._turn(rows[0]) if rows else None

    @staticmethod
    def turn_text(turn: ConversationTurn) -> str:
        """The exchange as one text (what is embedded and keyword-matched)."""
        parts = [turn.user_text or ""]
        if turn.reply_text:
            parts.append(turn.reply_text)
        if turn.actions:
            parts.append("; ".join(turn.actions))
        return "\n".join(parts)

    def _turn_filter(self, since: TimeArg, until: TimeArg) -> tuple[str, list[Any]]:
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("t.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("t.at < ?")
            params.append(u)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def search_turns(
        self, query_vec: np.ndarray, since: TimeArg = None, until: TimeArg = None, limit: int = 10
    ) -> list[ConversationTurn]:
        """Turns most similar to ``query_vec`` (cosine) in ``[since, until)``."""
        where, params = self._turn_filter(since, until)
        rows = self._query(
            "SELECT t.*, v.vec_ciphertext, v.dim FROM conversation_turns t"
            " JOIN conversation_turn_vec v ON v.turn_id = t.id" + where, params,
        )
        return [self._turn(r, score) for r, score in self._rank(rows, query_vec, limit)]

    def keyword_turns(
        self, text: str, since: TimeArg = None, until: TimeArg = None, limit: int = 50, *, max_scan: int = 5000,
    ) -> list[ConversationTurn]:
        """Turns whose user text, reply or actions contain every term of ``text`` (casefolded), newest first.

        Decrypted in memory (no plaintext index); at most the ``max_scan``
        newest turns of the window are read.
        """
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._turn_filter(since, until)
        hits: list[ConversationTurn] = []
        for r in self._query("SELECT t.* FROM conversation_turns t" + where + " ORDER BY t.at DESC, t.id DESC LIMIT ?",
                             [*params, int(max_scan)]):
            turn = self._turn(r)
            folded = self.turn_text(turn).casefold()
            if all(t in folded for t in terms):
                hits.append(turn)
                if len(hits) >= limit:
                    break
        return hits

    def turns_between(self, since: TimeArg = None, until: TimeArg = None) -> list[ConversationTurn]:
        """Every exchange in ``[since, until)``, oldest first (decrypted)."""
        where, params = self._turn_filter(since, until)
        return [self._turn(r) for r in self._query(
            "SELECT t.* FROM conversation_turns t" + where + " ORDER BY t.at, t.id", params)]

    def turn_times(self, since: TimeArg = None, until: TimeArg = None) -> list[tuple[float, str]]:
        """``(at, session_id)`` of every exchange in ``[since, until)``, oldest first (content-free)."""
        where, params = self._turn_filter(since, until)
        return [(float(r["at"]), str(r["session_id"])) for r in self._query(
            "SELECT t.at, t.session_id FROM conversation_turns t" + where + " ORDER BY t.at, t.id", params
        )]

    def turns_without_vectors(self, limit: int = 256) -> list[ConversationTurn]:
        return [self._turn(r) for r in self._query(
            "SELECT t.* FROM conversation_turns t LEFT JOIN conversation_turn_vec v ON v.turn_id = t.id"
            " WHERE v.turn_id IS NULL ORDER BY t.id LIMIT ?", (int(limit),),
        )]

    # conversation facts (rules, preferences, commitments) ---------------------

    def _cfact(self, r: sqlite3.Row, score: float | None = None) -> ConversationFact:
        try:
            sources = [int(i) for i in json.loads(r["source_turn_ids"] or "[]")]
        except (ValueError, TypeError):
            sources = []
        return ConversationFact(
            id=r["id"], kind=r["kind"], subject=self._dec(r["subject_ciphertext"]) or "",
            text=self.cipher.decrypt(r["text_ciphertext"]), quote=self._dec(r["quote_ciphertext"]),
            status=r["status"], valid_from=r["valid_from"], valid_to=r["valid_to"], due_at=r["due_at"],
            source_turn_ids=sources, origin=r["origin"] or "model", created_at=r["created_at"],
            expired_at=r["expired_at"], superseded_by=r["superseded_by"],
            end_note=self._dec(r["end_note_ciphertext"]), score=score,
            nudge_id=r["nudge_id"] if "nudge_id" in r.keys() else None,
            owner=r["owner"] if "owner" in r.keys() else None,
            title=self._dec(r["title_ciphertext"]) if "title_ciphertext" in r.keys() else None,
            value=self._dec(r["value_ciphertext"]) if "value_ciphertext" in r.keys() else None,
            keyterms=self._keyterms(r),
            reopened_at=r["reopened_at"] if "reopened_at" in r.keys() else None,
        )

    def _keyterms(self, r: sqlite3.Row) -> list[str]:
        if "keyterms_ciphertext" not in r.keys() or not r["keyterms_ciphertext"]:
            return []
        try:
            return [str(t) for t in json.loads(self.cipher.decrypt(r["keyterms_ciphertext"])) if str(t).strip()]
        except (ValueError, TypeError):
            return []

    def conversation_facts(
        self, kinds: Sequence[str] | None = None, statuses: Sequence[str] | None = ("active",),
    ) -> list[ConversationFact]:
        """Conversation facts of these kinds and statuses (``None`` = any), oldest first."""
        clauses, params = [], []
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._cfact(r) for r in self._query(
            "SELECT * FROM conversation_facts" + where + " ORDER BY valid_from, id", params
        )]

    def conversation_facts_ended(self, since: TimeArg, kinds: Sequence[str] | None = None) -> list[ConversationFact]:
        """Facts that ended (done, revoked, superseded) at or after ``since``, oldest first."""
        sql = "SELECT * FROM conversation_facts WHERE status != 'active' AND coalesce(valid_to, expired_at) >= ?"
        params: list[Any] = [_ts(since) or 0.0]
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        return [self._cfact(r) for r in self._query(sql + " ORDER BY valid_to, id", params)]

    def conversation_fact(self, fact_id: int) -> ConversationFact | None:
        rows = self._query("SELECT * FROM conversation_facts WHERE id=?", (int(fact_id),))
        return self._cfact(rows[0]) if rows else None

    def conversation_fact_vectors(self, ids: Sequence[int]) -> dict[int, np.ndarray]:
        """Stored (decrypted) vectors for these conversation-fact ids."""
        if not ids:
            return {}
        rows = self._query(
            "SELECT fact_id, vec_ciphertext FROM conversation_fact_vec WHERE fact_id IN (%s)" % ",".join("?" * len(ids)),
            [int(i) for i in ids],
        )
        return {r["fact_id"]: np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
                for r in rows}

    def conversation_facts_without_vectors(self, limit: int = 256) -> list[ConversationFact]:
        return [self._cfact(r) for r in self._query(
            "SELECT f.* FROM conversation_facts f LEFT JOIN conversation_fact_vec v ON v.fact_id = f.id"
            " WHERE v.fact_id IS NULL ORDER BY f.id LIMIT ?", (int(limit),),
        )]

    def _insert_cfact(self, conn: sqlite3.Connection, ch: ConversationChange, now: float,
                      batch_id: int | None) -> int:
        cur = conn.execute(
            "INSERT INTO conversation_facts(kind, subject_ciphertext, text_ciphertext, quote_ciphertext, status,"
            " valid_from, valid_to, due_at, source_turn_ids, origin, created_at, batch_id, nudge_id, owner,"
            " title_ciphertext, value_ciphertext, keyterms_ciphertext)"
            " VALUES (?,?,?,?,'active',?,NULL,?,?,?,?,?,?,?,?,?,?)",
            (
                ch.kind, self._enc(ch.subject or ""), self.cipher.encrypt(ch.text),
                self._enc(ch.quote) if ch.quote else None, float(ch.at if ch.at is not None else now),
                ch.due_at, json.dumps([int(i) for i in ch.source_turn_ids]), ch.origin or "model", now, batch_id,
                ch.nudge_id, ch.owner if ch.kind == "commitment" and ch.owner in COMMITMENT_OWNERS else None,
                self._enc(ch.title) if ch.title else None,
                self._enc(ch.value) if ch.kind == "resource" and ch.value else None,
                self.cipher.encrypt(json.dumps(list(ch.keyterms), ensure_ascii=False))
                if ch.kind == "resource" and ch.keyterms else None,
            ),
        )
        new_id = int(cur.lastrowid)
        if ch.vector is not None:
            self._put_vec(conn, "conversation_fact_vec", "fact_id", new_id, ch.vector, ch.embed_model)
        return new_id

    def _end_cfact(
        self, conn: sqlite3.Connection, fact_id: int, status: str, valid_to: float, now: float,
        *, superseded_by: int | None = None, note: str | None = None,
    ) -> bool:
        cur = conn.execute(
            "UPDATE conversation_facts SET status=?, valid_to=?, expired_at=?, superseded_by=?,"
            " end_note_ciphertext=? WHERE id=? AND status='active'",
            (status, float(valid_to), now, superseded_by, self._enc(note) if note else None, int(fact_id)),
        )
        return cur.rowcount == 1

    def _apply_cchanges(
        self, conn: sqlite3.Connection, changes: Sequence[ConversationChange], now: float, batch_id: int | None,
    ) -> list[tuple[bool, int | None]]:
        out: list[tuple[bool, int | None]] = []
        for ch in changes:
            at = float(ch.at) if ch.at is not None else now
            if ch.op in ("UPDATE", "INVALIDATE"):
                old = conn.execute(
                    "SELECT id, owner FROM conversation_facts WHERE id=? AND status='active'", (ch.fact_id,)
                ).fetchone()
                if old is None:
                    out.append((False, None))
                    continue
                if ch.op == "UPDATE" and ch.owner is None:
                    ch.owner = old["owner"]   # a new version keeps whose it is unless it says otherwise
            if ch.op == "ADD":
                out.append((True, self._insert_cfact(conn, ch, now, batch_id)))
            elif ch.op == "UPDATE":
                new_id = self._insert_cfact(conn, ch, now, batch_id)
                # a commitment the user put back stays put back in its new version (migration 37)
                conn.execute("UPDATE conversation_facts SET reopened_at=(SELECT reopened_at FROM conversation_facts"
                             " WHERE id=?) WHERE id=?", (int(ch.fact_id), new_id))
                self._end_cfact(conn, int(ch.fact_id), "superseded", at, now, superseded_by=new_id, note=ch.end_note)
                out.append((True, new_id))
            elif ch.op == "INVALIDATE":
                status = ch.end_as if ch.end_as in ("done", "revoked", "dropped", "wrong") else "revoked"
                self._end_cfact(conn, int(ch.fact_id), status, at, now, note=ch.end_note)
                out.append((True, None))
            else:
                out.append((False, None))
        return out

    def add_conversation_fact(
        self, kind: str, text: str, *, quote: str | None = None, subject: str = "", origin: str = "user",
        source_turn_ids: Sequence[int] = (), due_at: float | None = None, at: float | None = None,
        vector: np.ndarray | None = None, embed_model: str | None = None, supersedes: int | None = None,
        owner: str | None = None, end_note: str | None = None, title: str | None = None,
        value: str | None = None, keyterms: Sequence[str] = (),
    ) -> int:
        """Write one active fact now; with ``supersedes``, end that fact (bi-temporal) in the same transaction.

        ``owner`` (a commitment): "yuki" | "user"; an update keeps the old owner when None.
        ``end_note``: why the superseded version ended (e.g. "corrected: the user said ...").
        """
        if kind not in CONVERSATION_FACT_KINDS:
            raise ValueError(f"unknown conversation fact kind {kind!r}")
        now = time.time()
        ch = ConversationChange(
            "UPDATE" if supersedes is not None else "ADD", supersedes, kind, subject, text, quote, due_at,
            list(source_turn_ids), float(at) if at is not None else now, origin=origin, vector=vector,
            embed_model=embed_model, owner=owner, end_note=end_note, title=title, value=value,
            keyterms=list(keyterms),
        )

        def op(conn: sqlite3.Connection) -> int:
            if supersedes is not None:
                ok, new_id = self._apply_cchanges(conn, [ch], now, None)[0]
                if ok and new_id is not None:
                    return new_id
                ch.op = "ADD"   # the old fact ended meanwhile: plain add
            return self._insert_cfact(conn, ch, now, None)

        return self._write(op)

    def end_conversation_fact(self, fact_id: int, status: str, *, at: float | None = None,
                              note: str | None = None) -> bool:
        """End an active fact as ``done``/``revoked``/``superseded``; False if it was not active."""
        if status not in CONVERSATION_FACT_STATUSES or status == "active":
            raise ValueError(f"cannot end a fact as {status!r}")
        now = time.time()
        return self._write(lambda conn: self._end_cfact(
            conn, fact_id, status, float(at) if at is not None else now, now, note=note))

    # the worker's batches -----------------------------------------------------

    def _insert_cbatch(self, conn: sqlite3.Connection, call: ConversationCall) -> int:
        cur = conn.execute(
            "INSERT INTO conversation_batches(at, purpose, session_id, model, turn_count, input_chars, input_tokens,"
            " output_tokens, cache_write_tokens, cache_read_tokens, cost_usd, latency_ms, stop_reason, ops_applied,"
            " ops_noop, ops_rejected, journal_facts, outcome, error) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                call.at, call.purpose, call.session_id, call.model, call.turn_count, call.input_chars,
                call.input_tokens, call.output_tokens, call.cache_write_tokens, call.cache_read_tokens,
                call.cost_usd, call.latency_ms, call.stop_reason, call.ops_applied, call.ops_noop,
                call.ops_rejected, call.journal_facts, call.outcome, call.error,
            ),
        )
        return int(cur.lastrowid)

    def conversation_thread_id(self) -> int:
        """The journal thread for facts learned in conversations with Yuki (created on first use)."""
        return self._write(lambda conn: self._upsert_thread(
            conn, self.CONVERSATION_APP, "Conversations with Yuki", None, scope=self.CONVERSATION_SCOPE,
            kind="yuki_chat"))

    def commit_conversation_batch(
        self, call: ConversationCall, turn_ids: Iterable[int], changes: Sequence[ConversationChange],
        journal_facts: Sequence[NewFact], *, turn_vectors: Sequence[tuple[int, np.ndarray]] = (),
        embed_model: str | None = None, questions: Sequence[NewQuestion] = (),
        answers: Sequence[tuple[int, str, str, float]] = (),
        question_outcomes: list[tuple[int | None, str]] | None = None,
        addressed: dict[int, str] | None = None,
    ) -> tuple[int, list[tuple[bool, int | None]]]:
        """One transaction: the batch row, the fact changes, the journal facts (filed under
        the Yuki conversation thread), the questions for the user found in the exchanges
        (deduped), the answers the user gave to asked questions (``(question id, outcome,
        the user's words, at)``, see :meth:`answer_question`), the turns marked extracted
        and their vectors. ``addressed``: ``{turn id: "yuki" | "others"}``, the extractor's
        judgement of call turns nobody had judged; only a turn still marked "unanswered"
        takes it (report 77b).

        Returns ``(batch_id, [(applied, new fact id) per change])``; a change
        whose target is no longer active is skipped (``(False, None)``).
        ``question_outcomes`` receives ``(id, outcome)`` per question, then per answer.
        """
        ids = [int(i) for i in turn_ids]
        now = time.time()
        known = self.question_known_keys() if questions else set()

        def op(conn: sqlite3.Connection) -> tuple[int, list[tuple[bool, int | None]]]:
            batch_id = self._insert_cbatch(conn, call)
            results = self._apply_cchanges(conn, changes, now, batch_id)
            outcomes = [self._insert_question(conn, q, now, known) for q in questions]
            for qid, outcome, words, at in answers:
                fact = self._answer_question(conn, qid, outcome, words, at, now)
                outcomes.append((qid, f"answer:{outcome}" if fact is not None else "answer:not_open"))
            if question_outcomes is not None:
                question_outcomes[:] = outcomes
            if journal_facts:
                thread_id = self._upsert_thread(conn, self.CONVERSATION_APP, "Conversations with Yuki", None,
                                                scope=self.CONVERSATION_SCOPE, kind="yuki_chat")
                for fact in journal_facts:
                    if fact.thread_id is None:
                        fact.thread_id = thread_id
                    self._insert_journal(conn, fact, None)
            conn.executemany(
                "UPDATE conversation_turns SET extracted=1, extract_batch_id=? WHERE id=?", [(batch_id, i) for i in ids]
            )
            judged = [(v, int(k)) for k, v in (addressed or {}).items() if v in ("yuki", "others")]
            if judged:
                conn.executemany("UPDATE conversation_turns SET addressed=? WHERE id=? AND addressed='unanswered'",
                                 judged)
            for turn_id, vec in turn_vectors:
                self._put_vec(conn, "conversation_turn_vec", "turn_id", turn_id, vec, embed_model)
            return batch_id, results

        return self._write(op)

    def record_failed_conversation_batch(
        self, call: ConversationCall, *, extract_turn_ids: Iterable[int] = (), summary_turn_ids: Iterable[int] = (),
    ) -> int:
        """Record a failed call; bump the attempt counters of the turns it was for (they stay pending)."""
        ext = [(int(i),) for i in extract_turn_ids]
        summ = [(int(i),) for i in summary_turn_ids]

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_cbatch(conn, call)
            conn.executemany("UPDATE conversation_turns SET extract_attempts=extract_attempts+1 WHERE id=?", ext)
            conn.executemany("UPDATE conversation_turns SET summary_attempts=summary_attempts+1 WHERE id=?", summ)
            return batch_id

        return self._write(op)

    def conversation_batches(self, limit: int = 50) -> list[dict[str, Any]]:
        """Latest conversation-worker calls (content-free), newest first."""
        return [dict(r) for r in self._query(
            "SELECT * FROM conversation_batches ORDER BY id DESC LIMIT ?", (int(limit),))]

    # session summaries ----------------------------------------------------------

    def _summary(self, r: sqlite3.Row, score: float | None = None) -> SessionSummary:
        return SessionSummary(
            id=r["id"], session_id=r["session_id"], started_at=r["started_at"], ended_at=r["ended_at"],
            text=self.cipher.decrypt(r["text_ciphertext"]), turn_count=r["turn_count"], created_at=r["created_at"],
            score=score,
        )

    def commit_session_summary(
        self, call: ConversationCall, session_id: str, started_at: float, ended_at: float, text: str,
        turn_ids: Iterable[int], *, vector: np.ndarray | None = None, embed_model: str | None = None,
    ) -> int:
        """One transaction: the batch row, the summary (+vector), its turns linked to it."""
        ids = [int(i) for i in turn_ids]
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            batch_id = self._insert_cbatch(conn, call)
            cur = conn.execute(
                "INSERT INTO session_summaries(session_id, started_at, ended_at, turn_count, text_ciphertext,"
                " batch_id, created_at) VALUES (?,?,?,?,?,?,?)",
                (str(session_id), float(started_at), float(ended_at), len(ids), self.cipher.encrypt(text),
                 batch_id, now),
            )
            summary_id = int(cur.lastrowid)
            conn.executemany("UPDATE conversation_turns SET summary_id=? WHERE id=?", [(summary_id, i) for i in ids])
            if vector is not None:
                self._put_vec(conn, "session_summary_vec", "summary_id", summary_id, vector, embed_model)
            return summary_id

        return self._write(op)

    def _summary_filter(self, since: TimeArg, until: TimeArg, session_id: str | None = None) -> tuple[str, list]:
        clauses, params = [], []
        if session_id is not None:
            clauses.append("s.session_id = ?")
            params.append(str(session_id))
        if (s := _ts(since)) is not None:
            clauses.append("s.ended_at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("s.started_at < ?")
            params.append(u)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def session_summaries(
        self, session_id: str | None = None, since: TimeArg = None, until: TimeArg = None, limit: int | None = None,
    ) -> list[SessionSummary]:
        """Summaries overlapping ``[since, until)`` (of one session if given); with ``limit`` the newest N. Oldest first."""
        where, params = self._summary_filter(since, until, session_id)
        sql = "SELECT s.* FROM session_summaries s" + where + " ORDER BY s.ended_at DESC, s.id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._summary(r) for r in reversed(self._query(sql, params))]

    def search_summaries(
        self, query_vec: np.ndarray, since: TimeArg = None, until: TimeArg = None, limit: int = 10
    ) -> list[SessionSummary]:
        where, params = self._summary_filter(since, until)
        rows = self._query(
            "SELECT s.*, v.vec_ciphertext, v.dim FROM session_summaries s"
            " JOIN session_summary_vec v ON v.summary_id = s.id" + where, params,
        )
        return [self._summary(r, score) for r, score in self._rank(rows, query_vec, limit)]

    def keyword_summaries(
        self, text: str, since: TimeArg = None, until: TimeArg = None, limit: int = 50
    ) -> list[SessionSummary]:
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._summary_filter(since, until)
        hits: list[SessionSummary] = []
        for r in self._query("SELECT s.* FROM session_summaries s" + where + " ORDER BY s.ended_at DESC, s.id DESC",
                             params):
            summary = self._summary(r)
            folded = summary.text.casefold()
            if all(t in folded for t in terms):
                hits.append(summary)
                if len(hits) >= limit:
                    break
        return hits

    def summaries_without_vectors(self, limit: int = 64) -> list[SessionSummary]:
        return [self._summary(r) for r in self._query(
            "SELECT s.* FROM session_summaries s LEFT JOIN session_summary_vec v ON v.summary_id = s.id"
            " WHERE v.summary_id IS NULL ORDER BY s.id LIMIT ?", (int(limit),),
        )]

    def add_conversation_vectors(self, what: str, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        """Store embeddings for existing turns (``what="turn"``), summaries (``"summary"``) or facts (``"fact"``)."""
        table, col = {
            "turn": ("conversation_turn_vec", "turn_id"),
            "summary": ("session_summary_vec", "summary_id"),
            "fact": ("conversation_fact_vec", "fact_id"),
        }[what]

        def op(conn: sqlite3.Connection) -> int:
            for item_id, vec in items:
                self._put_vec(conn, table, col, item_id, vec, model)
            return len(items)

        return self._write(op) if items else 0

    def conversation_changed_since(self, at: TimeArg) -> bool:
        """Whether a rule/preference started or ended, or a session summary was written, after ``at``."""
        t = _ts(at) or 0.0
        row = self._query(
            "SELECT (SELECT count(*) FROM conversation_facts WHERE kind IN ('rule','preference')"
            " AND (created_at > ? OR coalesce(expired_at, 0) > ?))"
            " + (SELECT count(*) FROM session_summaries WHERE created_at > ?)", (t, t, t),
        )[0][0]
        return bool(row)

    # -- the coach: to-dos, nudges, check-ins (yuki.memory.nudges) ------------

    def _todo(self, r: sqlite3.Row) -> TodoItem:
        return TodoItem(
            id=r["id"], text=self.cipher.decrypt(r["text_ciphertext"]), due_at=r["due_at"],
            due_all_day=bool(r["due_all_day"]), status=r["status"], created_at=r["created_at"], done_at=r["done_at"],
            title=self._dec(r["title_ciphertext"]) if "title_ciphertext" in r.keys() else None,
        )

    def add_todo(self, text: str, *, due_at: float | None = None, all_day: bool = False,
                 at: float | None = None, title: str | None = None) -> int:
        """Store a to-do the user added (encrypted); returns its id. ``title``: its short title when a model
        wrote one with it (else the titles pass writes it, :mod:`yuki.memory.titles`)."""
        now = float(at) if at is not None else time.time()
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO todos(text_ciphertext, due_at, due_all_day, status, created_at, updated_at, title_ciphertext)"
            " VALUES (?,?,?,'open',?,?,?)",
            (self.cipher.encrypt(text), float(due_at) if due_at is not None else None, int(bool(all_day)), now, now,
             self._enc(title) if title else None),
        ).lastrowid))

    def todos(self, statuses: Sequence[str] | None = ("open",), *, done_since: TimeArg = None) -> list[TodoItem]:
        """User-added to-dos with these statuses (``None`` = any), oldest first.

        ``done_since``: done ones only when they were done at or after it.
        """
        clauses, params = [], []
        if statuses:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if (s := _ts(done_since)) is not None:
            clauses.append("(status != 'done' OR done_at >= ?)")
            params.append(s)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        return [self._todo(r) for r in self._query("SELECT * FROM todos" + where + " ORDER BY created_at, id", params)]

    def todo(self, todo_id: int) -> TodoItem | None:
        rows = self._query("SELECT * FROM todos WHERE id=?", (int(todo_id),))
        return self._todo(rows[0]) if rows else None

    def complete_todo(self, todo_id: int, *, at: float | None = None) -> bool:
        """Mark an open user to-do done; False if it was not open."""
        return self.end_todo(todo_id, "done", at=at)

    def end_todo(self, todo_id: int, status: str, *, at: float | None = None) -> bool:
        """End an open user to-do as ``done``, ``dropped`` (not needed) or ``wrong`` (migration 18); False if it
        was not open. Only ``open`` to-dos are listed, reminded of or shown to the coach."""
        if status not in TODO_END_STATUSES:
            raise ValueError(f"cannot end a to-do as {status!r}")
        now = float(at) if at is not None else time.time()
        return self._write(lambda conn: conn.execute(
            "UPDATE todos SET status=?, done_at=?, updated_at=? WHERE id=? AND status='open'",
            (status, now, now, int(todo_id)),
        ).rowcount == 1)

    def supersede_todo(self, todo_id: int, text: str, *, at: float | None = None,
                       title: str | None = None) -> int | None:
        """Replace an open user to-do's wording (a correction): the old row ends as ``superseded``, a new open row
        with the same due (and ``title``, the new wording's, when given) takes its place, in one transaction.
        Returns the new id, or None if it was not open."""
        now = float(at) if at is not None else time.time()

        def op(conn: sqlite3.Connection) -> int | None:
            row = conn.execute("SELECT * FROM todos WHERE id=? AND status='open'", (int(todo_id),)).fetchone()
            if row is None:
                return None
            new_id = int(conn.execute(
                "INSERT INTO todos(text_ciphertext, due_at, due_all_day, status, created_at, updated_at,"
                " title_ciphertext) VALUES (?,?,?,'open',?,?,?)",
                (self.cipher.encrypt(text), row["due_at"], row["due_all_day"], row["created_at"], now,
                 self._enc(title) if title else None),
            ).lastrowid)
            conn.execute("UPDATE todos SET status='superseded', done_at=?, updated_at=? WHERE id=?",
                         (now, now, int(todo_id)))
            return new_id

        return self._write(op)

    # -- short labels for the app (migration 21, yuki.memory.titles) ---------------------------------------

    def labels_missing(self, surfaces: Sequence[str] | None = None) -> dict[str, list[int]]:
        """``{surface: ids}`` of current items still without their short label(s) (:data:`LABEL_SURFACES`),
        oldest first. Plumbing on NULL columns: nothing is decrypted."""
        out: dict[str, list[int]] = {}
        for surface, (table, cols, where) in LABEL_SURFACES.items():
            if surfaces is not None and surface not in surfaces:
                continue
            empty = " OR ".join(f"{c} IS NULL" for c in cols.values())
            out[surface] = [int(r[0]) for r in self._query(
                f"SELECT id FROM {table} WHERE {where} AND ({empty}) ORDER BY id")]
        return out

    def set_labels(self, surface: str, labels: dict[int, dict[str, str]], *, overwrite: bool = False) -> int:
        """Write short labels (encrypted): ``labels`` is ``{item id: {"title" | "summary": text}}`` for one
        surface of :data:`LABEL_SURFACES`. Without ``overwrite`` a label another writer stored meanwhile is kept.
        Returns how many labels were written."""
        table, cols, _where = LABEL_SURFACES[surface]

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for item_id, fields in labels.items():
                for name, text in fields.items():
                    col = cols.get(name)
                    text = " ".join(str(text or "").split())
                    if col is None or not text:
                        continue
                    guard = "" if overwrite else f" AND {col} IS NULL"
                    n += conn.execute(f"UPDATE {table} SET {col}=? WHERE id=?{guard}",
                                      (self.cipher.encrypt(text), int(item_id))).rowcount
            return n

        return int(self._write(op))

    # -- what the app shows, said to the user (migration 33, yuki.memory.titles SHOWN_RULES) ------------------

    def shown_missing(self, surfaces: Sequence[str] | None = None) -> dict[str, list[int]]:
        """``{surface: ids}`` of current items still without their shown form (:data:`SHOWN_SURFACES`), oldest
        first. Plumbing on NULL columns: nothing is decrypted."""
        out: dict[str, list[int]] = {}
        for surface, (table, key, col, where) in SHOWN_SURFACES.items():
            if surfaces is not None and surface not in surfaces:
                continue
            out[surface] = [int(r[0]) for r in self._query(
                f"SELECT {key} FROM {table} WHERE {where} AND {col} IS NULL ORDER BY {key}")]
        return out

    def set_shown(self, surface: str, texts: dict[int, str], *, overwrite: bool = False) -> int:
        """Write shown forms (encrypted), ``{item id: text}`` for one surface of :data:`SHOWN_SURFACES`. Without
        ``overwrite`` a form another writer stored meanwhile is kept. Returns how many were written."""
        table, key, col, _where = SHOWN_SURFACES[surface]

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for item_id, text in texts.items():
                text = " ".join(str(text or "").split())
                if not text:
                    continue
                guard = "" if overwrite else f" AND {col} IS NULL"
                n += conn.execute(f"UPDATE {table} SET {col}=? WHERE {key}=?{guard}",
                                  (self.cipher.encrypt(text), int(item_id))).rowcount
            return n

        return int(self._write(op)) if texts else 0

    def call_contexts(self, call_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """Who was on each call and where (the digest's reading of the call screen, migration 25; ``place`` is the
        structured form since migration 33's digests): ``{call_id: {"app", "people": [{"name", "how"}], "where",
        "place"}}``. Only the context column is decrypted; calls without one are left out."""
        ids = sorted({int(i) for i in call_ids})
        if not ids:
            return {}
        out: dict[int, dict[str, Any]] = {}
        for r in self._query(f"SELECT call_id, app, started_at, context_ciphertext FROM call_digests WHERE call_id IN"
                             f" ({','.join('?' * len(ids))})", ids):
            try:
                ctx = json.loads(self._dec(r["context_ciphertext"]) or "{}")
            except (ValueError, TypeError):
                ctx = {}
            if not isinstance(ctx, dict):
                ctx = {}
            people = [p for p in ctx.get("people") or [] if isinstance(p, dict) and p.get("name")]
            place = ctx.get("place") if isinstance(ctx.get("place"), dict) else None
            out[int(r["call_id"])] = {"app": _shown(r["app"]), "people": people, "started_at": r["started_at"],
                                      "where": " ".join(str(ctx.get("where") or "").split()), "place": place}
        return out

    def set_fact_pinned(self, fact_id: int, on: bool, *, now: float | None = None) -> bool:
        """The user's pin on a current portrait fact (migration 33), one UPDATE and a diary line (pass ``pin``). No
        model, no correction. False when the fact is not current."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> bool:
            if conn.execute("UPDATE portrait_facts SET pinned=? WHERE id=? AND valid_to IS NULL AND kind != ?",
                            (int(bool(on)), int(fact_id), CORRECTION_KIND)).rowcount != 1:
                return False
            self._insert_diary(conn, None, now, {"pass": "pin", "action": "pin" if on else "unpin",
                                                 "fact_ids": [int(fact_id)],
                                                 "text": f"The user {'pinned' if on else 'unpinned'} F{int(fact_id)}."})
            return True

        return bool(self._write(op))

    def mark_fact_reviewed(self, fact_id: int, at: float | None, *, pin: bool | None = None,
                           note: str | None = None) -> bool:
        """Set (``at``) or clear (``None``) a current fact's ``reviewed_at``, optionally its pin, with a diary line
        (pass ``review``). False when the fact is not current."""
        now = time.time()

        def op(conn: sqlite3.Connection) -> bool:
            sets, params = ["reviewed_at=?"], [at]
            if pin is not None:
                sets.append("pinned=?")
                params.append(int(bool(pin)))
            if conn.execute(f"UPDATE portrait_facts SET {', '.join(sets)} WHERE id=? AND valid_to IS NULL",
                            (*params, int(fact_id))).rowcount != 1:
                return False
            if note:
                self._insert_diary(conn, None, now, {"pass": "review", "action": "review" if at else "undo",
                                                     "fact_ids": [int(fact_id)], "text": note})
            return True

        return bool(self._write(op))

    def restore_fact(self, fact_id: int) -> bool:
        """Undo the user's own retirement of a fact (the review queue's Wrong, within its Undo window): the version
        becomes current again when nothing superseded it. False otherwise."""
        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT kind FROM portrait_facts WHERE id=? AND valid_to IS NOT NULL AND"
                               " superseded_by IS NULL", (int(fact_id),)).fetchone()
            if row is None or row["kind"] == CORRECTION_KIND:
                return False
            conn.execute("UPDATE portrait_facts SET valid_to=NULL, expired_at=NULL WHERE id=?", (int(fact_id),))
            return True

        return bool(self._write(op))

    def cancel_correction(self, correction_id: int) -> bool:
        """Take back a correction whose pass has not started (the user's Undo): its pass is skipped and the correction
        ends (kept as history). False when the pass already started or finished."""
        now = time.time()

        def op(conn: sqlite3.Connection) -> bool:
            if conn.execute("UPDATE correction_passes SET status='skipped', updated_at=? WHERE correction_id=? AND"
                            " status='queued'", (now, int(correction_id))).rowcount != 1:
                return False
            conn.execute("UPDATE portrait_facts SET valid_to=?, expired_at=? WHERE id=? AND kind=? AND valid_to IS NULL",
                         (now, now, int(correction_id), CORRECTION_KIND))
            return True

        return bool(self._write(op))

    def review_counts(self, since: float) -> int:
        """How many answers the user gave in the "To review" queue since ``since`` (the queue's daily cap). Counted by
        distinct answer time: a later version of an answered fact carries its ``reviewed_at`` over, so one answer
        followed by a same-day update is still one answer (review A4)."""
        return int(self._query("SELECT count(DISTINCT reviewed_at) FROM portrait_facts WHERE reviewed_at >= ?",
                               (float(since),))[0][0])

    def resolve_journal(self, journal_id: int, verdict: str, *, now: float | None = None) -> dict[str, Any] | None:
        """The user's verdict on a "maybe misheard" journal entry (migration 33), deterministic, no model:
        ``right`` - its nature becomes ``fact`` and ``reviewed_at`` is set (in place: a classification the user just
        confirmed, nothing is reworded); ``pending`` - answered (``reviewed_at``) but still uncertain, while the
        correction pass rewrites it (Yuki's guess confirmed); ``wrong`` - it is retired now (kept, superseded by a no-model batch, like a
        correction's retirement); ``undo`` - either is taken back (a retirement only while it is the latest state of
        that entry). A diary line each (pass ``heard``). Returns ``{"id", "verdict", "nature", "text"}`` or ``None``
        when the entry is not there (or already resolved the other way)."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute("SELECT * FROM journal WHERE id=?", (int(journal_id),)).fetchone()
            if row is None:
                return None
            text = self.cipher.decrypt(row["fact_ciphertext"])
            if verdict == "right":
                if row["superseded_at"] is not None:
                    return None
                conn.execute("UPDATE journal SET nature='fact', reviewed_at=? WHERE id=?", (now, int(journal_id)))
                line = "confirmed what it heard"
            elif verdict == "pending":
                # Yuki's guess confirmed (W6): answered, off the list, but still uncertain (never used as a fact)
                # until the correction pass rewrites it - a rejected rewrite leaves no misheard words as a fact
                if row["superseded_at"] is not None:
                    return None
                conn.execute("UPDATE journal SET reviewed_at=? WHERE id=?", (now, int(journal_id)))
                line = "said Yuki's guess was right (the rewrite follows)"
            elif verdict == "wrong":
                if row["superseded_at"] is not None:
                    return None
                batch = self._insert_batch(conn, ModelCall(at=now, thread_id=row["thread_id"], model="user:heard",
                                                           cost_usd=0.0, outcome="ok"), 0, 0)
                conn.execute("UPDATE journal SET superseded_at=?, superseded_by=?, reviewed_at=? WHERE id=?",
                             (now, batch, now, int(journal_id)))
                line = "said it was misheard; retired"
            elif verdict == "undo":
                if row["superseded_at"] is not None:
                    batch = conn.execute("SELECT model FROM journal_batches WHERE id=?",
                                         (row["superseded_by"],)).fetchone()
                    if batch is None or batch["model"] != "user:heard":
                        return None   # superseded by something else since (a correction's rewrite): not ours to undo
                    conn.execute("UPDATE journal SET superseded_at=NULL, superseded_by=NULL, reviewed_at=NULL"
                                 " WHERE id=?", (int(journal_id),))
                else:
                    if row["reviewed_at"] is None:
                        return None   # never answered: nothing to take back (review A7)
                    conn.execute("UPDATE journal SET nature='uncertain', reviewed_at=NULL WHERE id=?",
                                 (int(journal_id),))
                line = "took back the answer"
            else:
                raise ValueError(f"unknown verdict {verdict!r}")
            self._insert_diary(conn, None, now, {"pass": "heard", "action": verdict, "fact_ids": [int(journal_id)],
                                                 "text": f"The user {line} for J{int(journal_id)}."})
            nature = "fact" if verdict == "right" else ("uncertain" if verdict == "undo" else row["nature"])
            # (pending keeps its nature: row["nature"])
            return {"id": int(journal_id), "verdict": verdict, "nature": nature, "text": text,
                    "app": _shown(row["app"]) if row["app"] else ""}

        out = self._write(op)
        self._forget_withdrawn()
        return out

    def uncertain_journal(self, since: float, *, limit: int | None = None) -> tuple[list[JournalEntry], int]:
        """Current journal entries marked uncertain (possibly misheard) since ``since`` that the user has not answered
        (migration 33), newest first, and how many there are in all."""
        where = "superseded_at IS NULL AND nature = 'uncertain' AND reviewed_at IS NULL AND at >= ?"
        total = int(self._query(f"SELECT count(*) FROM journal WHERE {where}", (float(since),))[0][0])
        sql = f"SELECT * FROM journal WHERE {where} ORDER BY at DESC, id DESC"
        params: list[Any] = [float(since)]
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._entry(r) for r in self._query(sql, params)], total

    def set_journal_sources(self, sources: dict[int, dict[str, Any] | None]) -> int:
        """Replace journal rows' provenance (``{journal id: source dict or None}``), in place: metadata about a row (the
        misheard words and Yuki's guess, :mod:`yuki.memory.misheard`), never its words. Returns how many."""

        def op(conn: sqlite3.Connection) -> int:
            return sum(conn.execute("UPDATE journal SET source_ciphertext=? WHERE id=?",
                                    (None if src is None else self.cipher.encrypt(json.dumps(src, ensure_ascii=False)),
                                     int(jid))).rowcount for jid, src in sources.items())

        return int(self._write(op)) if sources else 0

    def work_events_between(self, since: float, until: float, *, limit: int = 200) -> list[dict[str, Any]]:
        """What happened to the threads of work in ``[since, until)``, oldest first: ``{"thread_id", "title", "status",
        "at", "action", "state"}`` (decrypted; merged and dismissed threads left out)."""
        rows = self._query(
            "SELECT e.work_thread_id, e.at, e.action, e.state_ciphertext, t.title_ciphertext, t.status FROM"
            " work_thread_events e JOIN work_threads t ON t.id = e.work_thread_id WHERE e.at >= ? AND e.at < ? AND"
            " t.status NOT IN ('merged', 'dismissed') ORDER BY e.at, e.id LIMIT ?",
            (float(since), float(until), int(limit)))
        return [{"thread_id": int(r["work_thread_id"]), "title": self._dec(r["title_ciphertext"]) or "",
                 "status": r["status"], "at": float(r["at"]), "action": r["action"],
                 "state": self._dec(r["state_ciphertext"])} for r in rows]

    # -- day digests (migration 33, yuki.memory.daydigest) ----------------------------------------------------

    def save_day_digest(self, day: str, text: str, shown: str | None, refs: dict[str, Any] | None, *,
                        model: str | None = None, cost_usd: float | None = None) -> None:
        """Store (or replace) one user day's digest, encrypted."""
        now = time.time()
        self._write(lambda conn: conn.execute(
            "INSERT INTO day_digests(day, text_ciphertext, shown_ciphertext, refs, model, cost_usd, created_at,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(day) DO UPDATE SET text_ciphertext=excluded.text_ciphertext,"
            " shown_ciphertext=excluded.shown_ciphertext, refs=excluded.refs, model=excluded.model,"
            " cost_usd=excluded.cost_usd, updated_at=excluded.updated_at",
            (str(day), self.cipher.encrypt(text), self._enc(shown) if shown else None,
             json.dumps(refs or {}), model, cost_usd, now, now)))

    def day_digests(self, *, days: Sequence[str] | None = None, before: str | None = None,
                    limit: int = 7) -> list[dict[str, Any]]:
        """Day digests, newest day first: ``{"day", "text", "shown", "refs", "created_at"}``; ``days``: these days
        only; ``before``: days strictly before this one."""
        sql, params = "SELECT * FROM day_digests WHERE 1=1", []
        if days is not None:
            if not days:
                return []
            sql += " AND day IN (%s)" % ",".join("?" * len(days))
            params.extend(str(d) for d in days)
        if before is not None:
            sql += " AND day < ?"
            params.append(str(before))
        out = []
        for r in self._query(sql + " ORDER BY day DESC LIMIT ?", [*params, int(limit)]):
            try:
                refs = json.loads(r["refs"] or "{}")
            except ValueError:
                refs = {}
            out.append({"day": r["day"], "text": self.cipher.decrypt(r["text_ciphertext"]),
                        "shown": self._dec(r["shown_ciphertext"]), "refs": refs if isinstance(refs, dict) else {},
                        "created_at": r["created_at"]})
        return out

    def complete_open_loop(self, loop_id: int, *, at: float | None = None) -> bool:
        """The user says an open loop is done: in one transaction the loop row becomes ``done``
        and its current portrait fact is invalidated (``valid_to`` = now, history kept).
        False if the loop was not open."""
        now = float(at) if at is not None else time.time()

        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT portrait_fact_id FROM open_loops WHERE id=? AND status='open'",
                               (int(loop_id),)).fetchone()
            if row is None:
                return False
            conn.execute("UPDATE open_loops SET status='done', resolved_at=?, updated_at=? WHERE id=?",
                         (now, now, int(loop_id)))
            if row["portrait_fact_id"] is not None:
                self._end_fact(conn, int(row["portrait_fact_id"]), now, now, None)
            return True

        return self._write(op)

    def open_loops_done_since(self, since: TimeArg) -> list[OpenLoop]:
        """Loops closed as done or resolved at or after ``since``, oldest first."""
        return [self._loop(r) for r in self._query(
            _LOOPS_WITH_TITLES + " WHERE l.status IN ('done','resolved') AND l.resolved_at >= ?"
            " ORDER BY l.resolved_at, l.id", (_ts(since) or 0.0,),
        )]

    def _nudge(self, r: sqlite3.Row) -> NudgeRecord:
        try:
            inputs = json.loads(self._dec(r["inputs_ciphertext"]) or "{}")
        except (ValueError, TypeError):
            inputs = {}
        return NudgeRecord(
            id=r["id"], at=r["at"], kind=r["kind"], trigger=r["trigger"], text=self.cipher.decrypt(r["text_ciphertext"]),
            reason=self._dec(r["reason_ciphertext"]), inputs=inputs if isinstance(inputs, dict) else {},
            ref=r["ref"], due_at=r["due_at"], checkin_id=r["checkin_id"], shown_at=r["shown_at"],
            reaction=r["reaction"], reacted_at=r["reacted_at"],
            reply=self._dec(r["reply_ciphertext"]) if "reply_ciphertext" in r.keys() else None,
            outcome=r["outcome"] if "outcome" in r.keys() else None,
            outcome_at=r["outcome_at"] if "outcome_at" in r.keys() else None,
        )

    def set_nudge_outcome(self, nudge_id: int, outcome: str, *, at: float | None = None,
                          only_if: Sequence[str | None] | None = None) -> bool:
        """Record what came of a card's typed offer (:data:`NUDGE_OUTCOMES`); False when the card is unknown, or
        its outcome is not one of ``only_if`` (``None`` in it: no outcome yet) - a second click changes nothing."""
        if outcome not in NUDGE_OUTCOMES:
            raise ValueError(f"outcome must be one of {NUDGE_OUTCOMES}, not {outcome!r}")
        now = float(at) if at is not None else time.time()

        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT outcome FROM nudges WHERE id=?", (int(nudge_id),)).fetchone()
            if row is None or (only_if is not None and row["outcome"] not in tuple(only_if)):
                return False
            conn.execute("UPDATE nudges SET outcome=?, outcome_at=? WHERE id=?", (outcome, now, int(nudge_id)))
            return True

        return self._write(op)

    def record_checkin(self, checkin: NudgeCheckin, nudge: NewNudge | None = None, *,
                       question: tuple[int, str] | None = None) -> tuple[int, int | None]:
        """One transaction: the check-in's accounting and, when it said something, the nudge.

        ``question``: ``(question id, status)`` of the question this check-in was
        about - ``asked`` with the nudge written, or ``dropped`` when the phrasing
        call found memory already knows it - set in the same transaction.
        Returns ``(checkin_id, nudge_id or None)``.
        """
        if nudge is not None and nudge.kind not in NUDGE_KINDS:
            raise ValueError(f"unknown nudge kind {nudge.kind!r}")
        return self._write(lambda conn: self._record_checkin(conn, checkin, nudge, question))

    def _record_checkin(self, conn: sqlite3.Connection, checkin: NudgeCheckin, nudge: NewNudge | None,
                        question: tuple[int, str] | None) -> tuple[int, int | None]:
        """:meth:`record_checkin`'s writes, inside the caller's transaction."""
        c = checkin
        dropped = False
        if nudge is not None and nudge.text:
            # Yuki's own internal words never reach a card (report 76; yuki.core.ownwords: the sentences that carry
            # one go). A card with nothing left is not written, and its look is accounted as held back, its question
            # left open to ask again (review F6), never as sent
            from yuki.core.ownwords import strip_internal

            clean = strip_internal(nudge.text)
            if clean != nudge.text:
                nudge = replace(nudge, text=clean) if clean else None
                if nudge is None:
                    dropped = True
                    c.outcome = "suppressed:internal_words"
        cur = conn.execute(
            "INSERT INTO nudge_checkins(at, trigger, outcome, model, input_chars, input_tokens, output_tokens,"
            " cache_write_tokens, cache_read_tokens, cost_usd, latency_ms, stop_reason, error, reason_ciphertext)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (c.at, c.trigger, c.outcome, c.model, c.input_chars, c.input_tokens, c.output_tokens,
             c.cache_write_tokens, c.cache_read_tokens, c.cost_usd, c.latency_ms, c.stop_reason, c.error,
             self._enc(c.reason) if c.reason else None),
        )
        checkin_id = int(cur.lastrowid)
        nudge_id = None
        if nudge is not None:
            cur = conn.execute(
                "INSERT INTO nudges(at, kind, trigger, text_ciphertext, reason_ciphertext, inputs_ciphertext,"
                " ref, due_at, checkin_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (c.at, nudge.kind, nudge.trigger or c.trigger, self.cipher.encrypt(nudge.text),
                 self._enc(nudge.reason), self._enc(json.dumps(nudge.inputs, ensure_ascii=False, default=str)),
                 nudge.ref, nudge.due_at, checkin_id),
            )
            nudge_id = int(cur.lastrowid)
            conn.execute("UPDATE nudge_checkins SET nudge_id=? WHERE id=?", (nudge_id, checkin_id))
            self._surfaced_card(conn, nudge_id, nudge, c.at)
        else:
            _take_card_context()   # a look that ended in none: its noted context goes with it
        if question is not None:
            qid, status = question
            if dropped and status == "asked":
                status = "open"   # never shown: not asked
            conn.execute(
                "UPDATE questions SET status=?, nudge_id=coalesce(?, nudge_id),"
                " asked_at=CASE WHEN ?='asked' THEN ? ELSE asked_at END, updated_at=?"
                " WHERE id=? AND status IN ('open', 'asked')",
                (status, nudge_id, status, c.at, time.time(), int(qid)),
            )
        return checkin_id, nudge_id

    # -- what each reply and card was written from (migration 31, yuki.memory.brief) ---------------------------------

    def _surfaced_card(self, conn: sqlite3.Connection, nudge_id: int, nudge: NewNudge, at: float) -> None:
        """The surfaced row of a card just stored: the brief lines and memory items its writer noted
        (:func:`note_card_context`) plus the ids the card itself cites. Never raises (a missing table: no row)."""
        ctx = _take_card_context() or {}
        inputs = nudge.inputs if isinstance(nudge.inputs, dict) else {}
        cited = [str(i) for key in ("evidence", "mentions") for i in (inputs.get(key) or []) if i]
        facts = list(dict.fromkeys([*ctx.get("fact_ids", []), *cited]))
        try:
            conn.execute("INSERT INTO surfaced(at, kind, ref, session_id, brief_at, brief_ids, fact_ids)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (float(at), "card", f"nudge:{int(nudge_id)}", None, ctx.get("brief_at"),
                          json.dumps(ctx.get("brief_ids") or []), json.dumps(facts)))
        except sqlite3.Error:
            pass

    def add_surfaced(self, kind: str, ref: str, *, at: float | None = None, brief_ids: Sequence[str] = (),
                     fact_ids: Sequence[str] = (), session_id: str | None = None,
                     brief_at: float | None = None) -> int:
        """Record what one reply (``kind`` "reply", ``ref`` "turn:<session>:<request>") or card was written from:
        ids only (revamp 09 P4.1). Returns the row id (0 before migration 31)."""
        try:
            return int(self._write(lambda conn: conn.execute(
                "INSERT INTO surfaced(at, kind, ref, session_id, brief_at, brief_ids, fact_ids) VALUES (?,?,?,?,?,?,?)",
                (float(at if at is not None else time.time()), str(kind), str(ref), session_id, brief_at,
                 json.dumps([str(i) for i in brief_ids if i]), json.dumps([str(i) for i in fact_ids if i]))
            ).lastrowid))
        except sqlite3.Error:
            return 0

    def surfaced(self, since: TimeArg = None, until: TimeArg = None, *, unjudged: bool = False,
                 kinds: Sequence[str] | None = None) -> list[dict[str, Any]]:
        """Surfaced rows in ``[since, until)``, oldest first: ``{"id", "at", "kind", "ref", "session_id", "brief_at",
        "brief_ids", "fact_ids", "judged_at", "verdict", "judge"}``; ``[]`` before migration 31."""
        sql, params = "SELECT * FROM surfaced WHERE 1=1", []
        if (s := _ts(since)) is not None:
            sql += " AND at >= ?"
            params.append(s)
        if (u := _ts(until)) is not None:
            sql += " AND at < ?"
            params.append(u)
        if unjudged:
            sql += " AND judged_at IS NULL"
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
            params.extend(kinds)
        try:
            rows = self._query(sql + " ORDER BY at, id", params)
        except sqlite3.Error:
            return []
        out = []
        for r in rows:
            try:
                judge = json.loads(self._dec(r["judge_ciphertext"]) or "null")
            except (ValueError, TypeError):
                judge = None
            out.append({"id": int(r["id"]), "at": float(r["at"]), "kind": r["kind"], "ref": r["ref"],
                        "session_id": r["session_id"], "brief_at": r["brief_at"],
                        "brief_ids": json.loads(r["brief_ids"] or "[]"), "fact_ids": json.loads(r["fact_ids"] or "[]"),
                        "judged_at": r["judged_at"], "verdict": r["verdict"], "judge": judge})
        return out

    def judge_surfaced(self, row_id: int, verdict: str, judge: dict[str, Any] | None = None,
                       at: float | None = None) -> bool:
        """Keep the nightly judge's verdict on one surfaced row (``personal`` | ``generic`` | ``wrong``)."""
        try:
            return bool(self._write(lambda conn: conn.execute(
                "UPDATE surfaced SET judged_at=?, verdict=?, judge_ciphertext=? WHERE id=?",
                (float(at if at is not None else time.time()), str(verdict),
                 self._enc(json.dumps(judge, ensure_ascii=False)) if judge else None, int(row_id))).rowcount))
        except sqlite3.Error:
            return False

    def nudges(
        self, since: TimeArg = None, until: TimeArg = None, *, kinds: Sequence[str] | None = None,
        pending: bool = False, limit: int | None = None,
    ) -> list[NudgeRecord]:
        """Nudges written in ``[since, until)``, oldest first (with ``limit``: the newest N).

        ``pending``: only those never shown and without a reaction.
        """
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("at < ?")
            params.append(u)
        if kinds:
            clauses.append("kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if pending:
            clauses.append("shown_at IS NULL AND reaction IS NULL")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        sql = "SELECT * FROM nudges" + where + " ORDER BY at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._nudge(r) for r in reversed(self._query(sql, params))]

    def nudge(self, nudge_id: int) -> NudgeRecord | None:
        rows = self._query("SELECT * FROM nudges WHERE id=?", (int(nudge_id),))
        return self._nudge(rows[0]) if rows else None

    def react_to_nudge(self, nudge_id: int, reaction: str, *, at: float | None = None,
                       reply: str | None = None) -> bool:
        """Record what the user did with a nudge; ``shown`` also sets ``shown_at`` (first time only).

        A later reaction replaces an earlier one (shown -> dismissed); ``shown``
        never replaces a real reaction. ``reply``: with ``replied``, what the user
        wrote back (their words, encrypted), so the coach reads the reply itself.
        False if the nudge does not exist.
        """
        if reaction not in NUDGE_REACTIONS:
            raise ValueError(f"reaction must be one of {NUDGE_REACTIONS}, not {reaction!r}")
        now = float(at) if at is not None else time.time()
        if reaction == "replied" and reply and reply.strip():
            words = " ".join(reply.split())[:2000]
            return self._write(lambda conn: conn.execute(
                "UPDATE nudges SET shown_at=coalesce(shown_at, ?), reaction='replied', reacted_at=?,"
                " reply_ciphertext=? WHERE id=?", (now, now, self._enc(words), int(nudge_id))).rowcount == 1)
        if reaction == "shown":
            sql = ("UPDATE nudges SET shown_at=coalesce(shown_at, ?),"
                   " reaction=CASE WHEN reaction IS NULL OR reaction='expired' THEN 'shown' ELSE reaction END,"
                   " reacted_at=CASE WHEN reaction IS NULL OR reaction='expired' THEN ? ELSE reacted_at END WHERE id=?")
            params: tuple[Any, ...] = (now, now, int(nudge_id))
        else:
            sql = "UPDATE nudges SET shown_at=coalesce(shown_at, ?), reaction=?, reacted_at=? WHERE id=?"
            params = (now, reaction, now, int(nudge_id))
        return self._write(lambda conn: conn.execute(sql, params).rowcount == 1)

    def expire_nudges(self, before: float, kinds: Sequence[str]) -> int:
        """Mark never-shown nudges of these kinds written before ``before`` as ``expired``; returns how many."""
        if not kinds:
            return 0
        return self._write(lambda conn: conn.execute(
            "UPDATE nudges SET reaction='expired', reacted_at=? WHERE shown_at IS NULL AND reaction IS NULL"
            " AND at < ? AND kind IN (%s)" % ",".join("?" * len(kinds)),
            (time.time(), float(before), *kinds),
        ).rowcount)

    def nudge_replied_near(self, at: float, within_s: float = 15.0) -> NudgeRecord | None:
        """The card the user pressed Reply on at about ``at`` (its ``replied`` reaction within ``within_s``).

        Plumbing on two measured times: the UI records the reply the moment the
        user sends it, and the agent logs the exchange with the same start time.
        """
        rows = self._query(
            "SELECT * FROM nudges WHERE reaction='replied' AND reacted_at BETWEEN ? AND ?"
            " ORDER BY abs(reacted_at - ?) LIMIT 1", (float(at) - within_s, float(at) + within_s, float(at)))
        return self._nudge(rows[0]) if rows else None

    def turn_near(self, at: float, within_s: float = 15.0) -> ConversationTurn | None:
        """The exchange that started closest to ``at`` within ``within_s`` (the one a card reply became)."""
        rows = self._query(
            "SELECT * FROM conversation_turns WHERE at BETWEEN ? AND ? ORDER BY abs(at - ?) LIMIT 1",
            (float(at) - within_s, float(at) + within_s, float(at)))
        return self._turn(rows[0]) if rows else None

    def milestones_since(self, after_id: int, since: TimeArg, until: TimeArg = None) -> list[tuple[int, float]]:
        """``(journal id, at)`` of milestone facts (not Yuki's own, not superseded) newer than ``after_id``
        and dated at or after ``since`` (and written before ``until``, when given), oldest first: content-free,
        for the coach's moment trigger and the observation cards (yuki.memory.presence)."""
        sql = ("SELECT id, at FROM journal WHERE milestone=1 AND by_yuki=0 AND superseded_at IS NULL"
               " AND id > ? AND at >= ?")
        params: list[Any] = [int(after_id), _ts(since) or 0.0]
        if (u := _ts(until)) is not None:
            sql += " AND at < ? AND created_at <= ?"
            params += [u, u]
        try:
            rows = self._query(sql + " ORDER BY at, id", params)
        except sqlite3.OperationalError:   # a database before migration 16
            return []
        return [(int(r["id"]), float(r["at"])) for r in rows]

    def journal_vectors(self, ids: Iterable[int]) -> dict[int, np.ndarray]:
        """The stored (decrypted) embedding of each of these journal facts that has one: ``{id: vector}``
        (float32, as written; the recurrence signal of yuki.memory.presence compares them - no model)."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        out: dict[int, np.ndarray] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            for r in self._query("SELECT journal_id, vec_ciphertext FROM journal_vec WHERE journal_id IN (%s)"
                                 % ",".join("?" * len(chunk)), chunk):
                try:
                    out[int(r[0])] = np.frombuffer(self.cipher.decrypt_bytes(r[1]), dtype=np.float32)
                except Exception:
                    continue
        return out

    def closest_portrait_facts(self, vector: np.ndarray, *, limit: int = 3,
                               min_cosine: float = 0.0) -> list[tuple[float, PortraitFact]]:
        """The current portrait facts closest to ``vector`` by their stored embeddings, ``[(cosine, fact)]`` best
        first (no model, no words; the observation cards' follow-up reads what the portrait settled on)."""
        q = np.asarray(vector, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(q))
        if qn == 0.0:
            return []
        q = q / qn
        scored: list[tuple[float, int]] = []
        for r in self._query("SELECT f.id, v.vec_ciphertext, v.dim FROM portrait_facts f JOIN portrait_fact_vec v"
                             " ON v.fact_id = f.id WHERE f.valid_to IS NULL"):
            if r["dim"] != q.shape[0]:
                continue
            v = np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
            n = float(np.linalg.norm(v))
            if n:
                scored.append((float(v @ q) / n, int(r["id"])))
        scored = sorted((s for s in scored if s[0] >= min_cosine), reverse=True)[:limit]
        out = []
        for cos, fid in scored:
            f = self.portrait_fact(fid)
            if f is not None:
                out.append((round(cos, 4), f))
        return out

    def journal_newest(self) -> tuple[int, float]:
        """``(the newest journal id, when the newest fact was written)``: content-free, one indexed read (the
        observation cards look again only when memory learned something new)."""
        row = self._query("SELECT coalesce(max(id), 0), coalesce(max(created_at), 0) FROM journal")[0]
        return int(row[0]), float(row[1])

    def journal_written(self, ids: Iterable[int]) -> dict[int, float]:
        """When each of these journal facts was written (``created_at``): content-free. A fact is dated by its
        source, which can be hours before memory learned it (a chat's history, a call transcribed later)."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        out: dict[int, float] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            for r in self._query("SELECT id, created_at FROM journal WHERE id IN (%s)" % ",".join("?" * len(chunk)),
                                 chunk):
                out[int(r[0])] = float(r[1] or 0.0)
        return out

    def fact_card(self, fact: ConversationFact) -> NudgeRecord | None:
        """The card a conversation fact answers: its ``nudge_id``, else the card its source exchange replied to.

        A rule or preference extracted before feedback existed (the 2026-09-25 "stfu")
        is found through its exchange: the card whose Reply was pressed at that
        moment (:meth:`nudge_replied_near`). ``None`` for an item said in plain chat.
        """
        if fact.nudge_id:
            return self.nudge(int(fact.nudge_id))
        if fact.kind not in ("rule", "preference", "feedback") or not fact.source_turn_ids:
            return None
        for turn in self.turns_by_ids(fact.source_turn_ids):
            card = self.nudge_replied_near(turn.at)
            if card is not None:
                return card
        return None

    def checkin_reasons(self, since: TimeArg = None, limit: int = 20) -> list[dict[str, Any]]:
        """The newest check-ins since ``since`` with the coach's why (decrypted), oldest first."""
        params: list[Any] = []
        sql = "SELECT id, at, trigger, outcome, reason_ciphertext FROM nudge_checkins"
        if (s := _ts(since)) is not None:
            sql += " WHERE at >= ?"
            params.append(s)
        sql += " ORDER BY at DESC, id DESC LIMIT ?"
        params.append(int(limit))
        try:
            rows = self._query(sql, params)
        except sqlite3.OperationalError:   # a database before migration 16
            return []
        return [{"id": r["id"], "at": r["at"], "trigger": r["trigger"], "outcome": r["outcome"],
                 "reason": self._dec(r["reason_ciphertext"])} for r in reversed(rows)]

    def reminded(self, ref: str, due_at: float | None) -> bool:
        """Whether a reminder for this item and due time was already written (or folded into another card)."""
        folded = self.nudge_state(f"reminded:{ref}:{int(due_at) if due_at is not None else 'none'}")
        if folded is not None:
            return True
        if due_at is None:
            rows = self._query("SELECT 1 FROM nudges WHERE kind='reminder' AND ref=? AND due_at IS NULL LIMIT 1", (ref,))
        else:
            rows = self._query(
                "SELECT 1 FROM nudges WHERE kind='reminder' AND ref=? AND abs(due_at - ?) < 1 LIMIT 1",
                (ref, float(due_at)),
            )
        return bool(rows)

    def nudge_checkins(self, since: TimeArg = None, limit: int | None = None) -> list[dict[str, Any]]:
        """Check-in accounting rows (content-free) since ``since``, oldest first (with ``limit``: the newest N)."""
        sql = "SELECT * FROM nudge_checkins"
        params: list[Any] = []
        if (s := _ts(since)) is not None:
            sql += " WHERE at >= ?"
            params.append(s)
        sql += " ORDER BY at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in reversed(self._query(sql, params))]

    def nudge_state(self, name: str) -> str | None:
        rows = self._query("SELECT value FROM nudge_state WHERE name=?", (name,))
        return rows[0]["value"] if rows else None

    def set_nudge_state(self, name: str, value: str | None) -> None:
        """Set (or with ``None`` delete) one coach state value."""
        if value is None:
            self._write(lambda conn: conn.execute("DELETE FROM nudge_state WHERE name=?", (name,)))
            return
        self._write(lambda conn: conn.execute(
            "INSERT INTO nudge_state(name, value, updated_at) VALUES (?,?,?)"
            " ON CONFLICT(name) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (name, str(value), time.time()),
        ))

    def captures_between(self, since: TimeArg, until: TimeArg = None, *, limit: int = 4) -> list[dict[str, Any]]:
        """The newest ``limit`` captures in ``[since, until)`` that were the user's own screen (not ``by_yuki``;
        the window in front - not one beside it or a popup, migration 24's attention), oldest first: ``{"at",
        "app", "host", "kind", "text"}`` (text decrypted). Never a call's transcript (not a screen; since 3 Oct it
        holds other people's words too)."""
        clauses, params = ["c.by_yuki = 0", "coalesce(c.trigger, '') != 'popup'",
                           "coalesce(c.trigger, '') NOT LIKE 'visible:%'", "coalesce(c.kind, '') != ?"], [CALL_SIDE_KIND]
        if (s := _ts(since)) is not None:
            clauses.append("c.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("c.at < ?")
            params.append(u)
        params.append(int(limit))
        rows = self._query(
            "SELECT c.at, c.kind, c.delta_ciphertext, t.app, t.process, t.host FROM captures c"
            " JOIN threads t ON t.id = c.thread_id"
            " WHERE " + " AND ".join(clauses) + " ORDER BY c.at DESC, c.id DESC LIMIT ?", params,
        )
        return [{"at": r["at"], "app": _shown(r["app"], r["process"]), "host": r["host"], "kind": r["kind"],
                 "text": self.cipher.decrypt(r["delta_ciphertext"])} for r in reversed(rows)]

    def captures_around(self, since: TimeArg, until: TimeArg, *, limit: int = 800) -> list[dict[str, Any]]:
        """Every screen capture in ``[since, until)`` (any attention, Yuki's own too; never a call's words),
        oldest first, at most ``limit`` (the earliest): ``{"id", "at", "trigger", "kind", "by_yuki", "thread_id",
        "app", "process", "host", "title", "text"}``, decrypted. For the context pack of a call
        (:mod:`yuki.memory.callcontext`)."""
        s, u = _ts(since), _ts(until)
        rows = self._query(
            "SELECT c.id, c.at, c.trigger, c.kind, c.by_yuki, c.thread_id, c.delta_ciphertext, t.app, t.process,"
            " t.host, t.title_ciphertext FROM captures c JOIN threads t ON t.id = c.thread_id"
            " WHERE c.at >= ? AND c.at < ? AND coalesce(c.kind, '') != ? ORDER BY c.at, c.id LIMIT ?",
            (s if s is not None else 0.0, u if u is not None else time.time() + 1.0, CALL_SIDE_KIND, int(limit)))
        out = []
        for r in rows:
            try:
                text = self.cipher.decrypt(r["delta_ciphertext"])
            except Exception:
                continue
            out.append({"id": int(r["id"]), "at": float(r["at"]), "trigger": r["trigger"] or "", "kind": r["kind"],
                        "by_yuki": bool(r["by_yuki"]), "thread_id": int(r["thread_id"]),
                        "app": _shown(r["app"], r["process"]), "process": r["process"], "host": r["host"],
                        "title": self._dec(r["title_ciphertext"]) or "", "text": text or ""})
        return out

    # -- questions for the user (yuki.memory.questions) ----------------------------

    def _question_row(self, r: sqlite3.Row) -> QuestionRecord:
        def ids(raw: Any) -> list[int]:
            try:
                return [int(i) for i in json.loads(raw or "[]")]
            except (ValueError, TypeError):
                return []

        keys = r.keys()
        return QuestionRecord(
            id=r["id"], at=r["at"], created_at=r["created_at"], kind=r["kind"],
            subject=self.cipher.decrypt(r["subject_ciphertext"]), question=self.cipher.decrypt(r["question_ciphertext"]),
            evidence=self._dec(r["evidence_ciphertext"]), reason=self._dec(r["reason_ciphertext"]),
            importance=int(r["importance"]), origin=r["origin"], source_ids=ids(r["source_ids"]),
            fact_ids=ids(r["fact_ids"]), status=r["status"], seen=int(r["seen"] or 1), nudge_id=r["nudge_id"],
            asked_at=r["asked_at"], answered_at=r["answered_at"], answer=self._dec(r["answer_ciphertext"]),
            answer_fact_id=r["answer_fact_id"], updated_at=r["updated_at"],
            moment=r["moment"] if "moment" in keys else None,
            goal_id=r["goal_id"] if "goal_id" in keys else None,
            folded_at=r["folded_at"] if "folded_at" in keys else None,
            reply_tone=r["reply_tone"] if "reply_tone" in keys else None,
        )

    def question_known_keys(self) -> set[str]:
        """Subject keys of what the user already told memory themselves (current user-origin portrait facts)."""
        return {question_subject_key(self.cipher, f.subject) for f in self.portrait_facts()
                if f.origin == "user" and f.kind != CORRECTION_KIND and (f.subject or "").strip()}

    def _insert_question(self, conn: sqlite3.Connection, q: NewQuestion, now: float,
                         known: set[str]) -> tuple[int | None, str]:
        """Store one candidate unless it is already known; returns ``(id, outcome)``.

        Dedupe (plumbing, not judgement - the model that found it was shown what
        memory knows and what is already asked): the same subject (its HMAC key)
        or a "subject: question" embedding within :data:`QUESTION_DUPLICATE_COSINE`
        of an existing question is that question - merged into it while it is
        still open or asked (seen +1, the higher importance, the evidence ids
        joined), reopened if it expired unasked, and otherwise left alone
        (answered, skipped, "never", dropped: ``known``). A subject the user
        already stated a fact about (a user-origin portrait fact) is ``known`` too.
        """
        subject, question = " ".join((q.subject or "").split()), " ".join((q.question or "").split())
        if not subject or not question:
            return None, "rejected:empty"
        kind = q.kind if q.kind in QUESTION_KINDS else "fact"
        key = question_subject_key(self.cipher, subject)
        if key in known:
            return None, "known"
        row = conn.execute("SELECT * FROM questions WHERE subject_key=? ORDER BY id DESC LIMIT 1", (key,)).fetchone()
        if row is None and q.vector is not None:
            vec_rows = conn.execute(
                "SELECT q.*, v.dim, v.vec_ciphertext FROM questions q JOIN question_vec v ON v.question_id = q.id"
            ).fetchall()
            ranked = self._rank(vec_rows, q.vector, 1)
            if ranked and ranked[0][1] >= QUESTION_DUPLICATE_COSINE:
                row = ranked[0][0]
        importance = min(10, max(1, int(q.importance)))
        if row is not None:
            merged_sources = json.dumps(sorted({*(json.loads(row["source_ids"] or "[]")), *map(int, q.source_ids)}))
            merged_facts = json.dumps(sorted({*(json.loads(row["fact_ids"] or "[]")), *map(int, q.fact_ids)}))
            if row["status"] in ("open", "asked", "expired"):
                reopen = row["status"] == "expired"
                conn.execute(
                    "UPDATE questions SET seen=seen+1, importance=max(importance, ?), source_ids=?, fact_ids=?,"
                    " status=CASE WHEN status='expired' THEN 'open' ELSE status END,"
                    " at=CASE WHEN status='expired' THEN ? ELSE at END, updated_at=? WHERE id=?",
                    (importance, merged_sources, merged_facts, float(q.at), now, row["id"]),
                )
                return int(row["id"]), "reopened" if reopen else "merged"
            conn.execute("UPDATE questions SET seen=seen+1, updated_at=? WHERE id=?", (now, row["id"]))
            return int(row["id"]), "known"
        cur = conn.execute(
            "INSERT INTO questions(at, created_at, kind, subject_ciphertext, subject_key, question_ciphertext,"
            " evidence_ciphertext, reason_ciphertext, importance, origin, source_ids, fact_ids, status, seen,"
            " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,'open',1,?)",
            (float(q.at), now, kind, self.cipher.encrypt(subject), key, self.cipher.encrypt(question),
             self._enc(" ".join((q.evidence or "").split()) or None), self._enc(" ".join((q.reason or "").split()) or None),
             importance, q.origin, json.dumps(sorted({int(i) for i in q.source_ids})),
             json.dumps(sorted({int(i) for i in q.fact_ids})), now),
        )
        qid = int(cur.lastrowid)
        if q.vector is not None:
            self._put_vec(conn, "question_vec", "question_id", qid, q.vector, q.embed_model)
        return qid, "added"

    def add_questions(self, questions: Sequence[NewQuestion]) -> list[tuple[int | None, str]]:
        """Store candidate questions (deduped, :meth:`_insert_question`) in one transaction; ``(id, outcome)`` each."""
        if not questions:
            return []
        known = self.question_known_keys()
        now = time.time()
        return self._write(lambda conn: [self._insert_question(conn, q, now, known) for q in questions])

    def questions(self, statuses: Sequence[str] | None = None, *, limit: int | None = None) -> list[QuestionRecord]:
        """Stored questions (with ``statuses``: only those), newest first."""
        sql, params = "SELECT * FROM questions", []
        if statuses:
            sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
            params.extend(statuses)
        sql += " ORDER BY coalesce(answered_at, asked_at, updated_at) DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._question_row(r) for r in self._query(sql, params)]

    def question(self, question_id: int) -> QuestionRecord | None:
        rows = self._query("SELECT * FROM questions WHERE id=?", (int(question_id),))
        return self._question_row(rows[0]) if rows else None

    def next_question(self, *, min_importance: int = 1, since: TimeArg = None) -> QuestionRecord | None:
        """The open question to ask next: the most important, then the newest evidence. Never a check-in (origin
        ``checkin``: asked at its own moment by yuki.memory.checkins, never queued)."""
        sql = "SELECT * FROM questions WHERE status='open' AND origin != 'checkin' AND importance >= ?"
        params: list[Any] = [int(min_importance)]
        if (s := _ts(since)) is not None:
            sql += " AND at >= ?"
            params.append(s)
        rows = self._query(sql + " ORDER BY importance DESC, at DESC, id DESC LIMIT 1", params)
        return self._question_row(rows[0]) if rows else None

    def _answer_question(self, conn: sqlite3.Connection, question_id: int, outcome: str, words: str,
                         at: float, now: float) -> int | None:
        """Record the user's answer (``answered``: their words become a user-origin portrait correction
        that states the question it answers) or their choice (``skipped`` / ``never``).
        Returns the correction id (0 for a skip or never), or ``None`` when the question does not
        exist, is already answered, or an answer came without words."""
        row = conn.execute("SELECT * FROM questions WHERE id=?", (int(question_id),)).fetchone()
        if row is None or row["status"] not in ("open", "asked", "skipped", "expired", "dropped"):
            return None
        words = " ".join((words or "").split())
        if outcome == "answered" and row["origin"] == "checkin":
            # a check-in's answer is about the user's goals and their day, not a portrait correction: it waits for
            # the fold-in (yuki.memory.checkins.fold_answers), which turns it into goals or updates of them
            if not words:
                return None
            words = redact_text(words)   # like add_rule and add_todo: a pasted key never reaches the fold (review 34 L20)
            conn.execute(
                "UPDATE questions SET status='answered', answered_at=?, answer_ciphertext=?, folded_at=NULL,"
                " updated_at=? WHERE id=?", (float(at), self.cipher.encrypt(words), now, row["id"]))
            return 0
        if outcome == "answered":
            if not words:
                return None
            subject = self.cipher.decrypt(row["subject_ciphertext"])
            question = self.cipher.decrypt(row["question_ciphertext"])
            evidence = self._dec(row["evidence_ciphertext"])
            seen = f"; memory had seen: {evidence}" if evidence else ""
            text = (f'Asked "{question}" (about {subject}{seen}), the user answered in their own words: '
                    f'"{words}"')
            fact_id = self._insert_fact(
                conn, kind=CORRECTION_KIND, subject="", text=text, valid_from=float(at),
                source_ids=json.loads(row["source_ids"] or "[]"), confidence=1.0, origin="user", run_id=None, now=now,
            )
            conn.execute(
                "UPDATE questions SET status='answered', answered_at=?, answer_ciphertext=?, answer_fact_id=?,"
                " updated_at=? WHERE id=?",
                (float(at), self.cipher.encrypt(words), fact_id, now, row["id"]),
            )
            self._queue_pass(conn, fact_id, now)   # an answer can fix a name everywhere ("Himanth is Hemanth")
            return fact_id
        if outcome not in ("skipped", "never"):
            raise ValueError(f"outcome must be answered, skipped or never, not {outcome!r}")
        if row["status"] == "skipped" and outcome == "skipped":
            return 0
        conn.execute("UPDATE questions SET status=?, answered_at=?, answer_ciphertext=?, updated_at=? WHERE id=?",
                     (outcome, float(at), self._enc(words or None), now, row["id"]))
        return 0

    def answer_question(self, question_id: int, outcome: str = "answered", words: str = "", *,
                        at: float | None = None) -> int | None:
        """The user's answer to a question, or their skip / "don't ask about this" (see :meth:`_answer_question`)."""
        now = time.time()
        when = float(at) if at is not None else now
        return self._write(lambda conn: self._answer_question(conn, question_id, outcome, words, when, now))

    def reopen_unshown_questions(self) -> int:
        """Questions whose card expired without ever being shown go back to ``open`` (asked at a later calm moment).
        A check-in belongs to its moment: one whose card expired unshown is ``expired``, never reopened."""
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            unshown = "nudge_id IN (SELECT id FROM nudges WHERE shown_at IS NULL AND reaction='expired')"
            conn.execute("UPDATE questions SET status='expired', updated_at=? WHERE status='asked'"
                         " AND origin='checkin' AND " + unshown, (now,))
            return conn.execute(
                "UPDATE questions SET status='open', nudge_id=NULL, asked_at=NULL, updated_at=? WHERE status='asked'"
                " AND origin != 'checkin' AND " + unshown, (now,)).rowcount

        return self._write(op)

    def drop_question(self, question_id: int, fact_id: int | None = None) -> bool:
        """An open or asked question memory turned out to answer already (:func:`yuki.memory.questions.revet`):
        ``dropped``, with the portrait fact that answers it in ``answer_fact_id`` when it is one. Returns whether
        it changed."""
        return bool(self._write(lambda conn: conn.execute(
            "UPDATE questions SET status='dropped', answer_fact_id=coalesce(?, answer_fact_id), updated_at=?"
            " WHERE id=? AND status IN ('open', 'asked')",
            (int(fact_id) if fact_id is not None else None, time.time(), int(question_id)),
        ).rowcount))

    def questions_near(self, vectors: Sequence[np.ndarray], statuses: Sequence[str] = ("open", "asked"), *,
                       limit: int = 12) -> list[tuple[QuestionRecord, float]]:
        """Questions in ``statuses`` best first by their "subject: question" vector's highest cosine to any of
        ``vectors`` (measured; questions without a vector are left out)."""
        if not vectors or not statuses:
            return []
        rows = self._query(
            "SELECT q.*, v.dim, v.vec_ciphertext FROM questions q JOIN question_vec v ON v.question_id = q.id"
            " WHERE q.status IN (%s)" % ",".join("?" * len(statuses)), list(statuses))
        best: dict[int, tuple[sqlite3.Row, float]] = {}
        for vec in vectors:
            for row, score in self._rank(rows, vec, len(rows)):
                if row["id"] not in best or score > best[row["id"]][1]:
                    best[row["id"]] = (row, score)
        ranked = sorted(best.values(), key=lambda x: -x[1])[: max(0, int(limit))]
        return [(self._question_row(r), s) for r, s in ranked]

    def expire_questions(self, before: float) -> int:
        """Open questions whose evidence is older than ``before`` are not asked any more (``expired``)."""
        return self._write(lambda conn: conn.execute(
            "UPDATE questions SET status='expired', updated_at=? WHERE status='open' AND at < ?",
            (time.time(), float(before)),
        ).rowcount)

    # -- the user's goals and Yuki's check-ins (migration 30, yuki.memory.checkins) ------------------

    def _goal(self, r: sqlite3.Row) -> Goal:
        try:
            evidence = [str(x) for x in json.loads(r["evidence"] or "[]")]
        except (ValueError, TypeError):
            evidence = []
        return Goal(
            id=int(r["id"]), scope=r["scope"], day=r["day"], work_thread_id=r["work_thread_id"],
            text=self._dec(r["text_ciphertext"]) or "", words=self._dec(r["words_ciphertext"]), source=r["source"],
            source_ref=r["source_ref"], status=r["status"], evidence=evidence, due_at=r["due_at"],
            next_checkin_at=r["next_checkin_at"], last_checkin_at=r["last_checkin_at"],
            checkins=int(r["checkins"] or 0), created_at=float(r["created_at"]), updated_at=float(r["updated_at"]),
            ended_at=r["ended_at"])

    def goals(self, statuses: Sequence[str] | None = ("open", "unknown"), *, scope: str | None = None,
              day: str | None = None, work_thread_id: int | None = None, ids: Iterable[int] | None = None,
              limit: int | None = None) -> list[Goal]:
        """The user's goals (``statuses`` None: all), most recently updated first; ``[]`` before migration 30."""
        clauses, params = [], []
        if statuses is not None:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if scope is not None:
            clauses.append("scope=?")
            params.append(scope)
        if day is not None:
            clauses.append("day=?")
            params.append(day)
        if work_thread_id is not None:
            clauses.append("work_thread_id=?")
            params.append(int(work_thread_id))
        if ids is not None:
            wanted = sorted({int(i) for i in ids})
            if not wanted:
                return []
            clauses.append("id IN (%s)" % ",".join("?" * len(wanted)))
            params.extend(wanted)
        sql = "SELECT * FROM goals" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY updated_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            return [self._goal(r) for r in self._query(sql, params)]
        except sqlite3.OperationalError:
            return []

    def goal(self, goal_id: int) -> Goal | None:
        rows = self.goals(None, ids=[int(goal_id)])
        return rows[0] if rows else None

    def goal_events(self, goal_id: int) -> list[dict[str, Any]]:
        """Every change to a goal, oldest first: ``{"id", "at", "type", "status", "question_id", "evidence", "note"}``."""
        try:
            rows = self._query("SELECT * FROM goal_events WHERE goal_id=? ORDER BY at, id", (int(goal_id),))
        except sqlite3.OperationalError:
            return []
        out = []
        for r in rows:
            try:
                evidence = [str(x) for x in json.loads(r["evidence"] or "[]")]
            except (ValueError, TypeError):
                evidence = []
            out.append({"id": int(r["id"]), "at": float(r["at"]), "type": r["type"], "status": r["status"],
                        "question_id": r["question_id"], "evidence": evidence, "note": self._dec(r["note_ciphertext"])})
        return out

    def _goal_event(self, conn: sqlite3.Connection, goal_id: int, at: float, type: str, *, status: str | None = None,
                    question_id: int | None = None, evidence: Sequence[str] = (), note: str | None = None) -> None:
        conn.execute("INSERT INTO goal_events(goal_id, at, type, status, question_id, evidence, note_ciphertext)"
                     " VALUES (?,?,?,?,?,?,?)",
                     (int(goal_id), float(at), type, status, int(question_id) if question_id is not None else None,
                      json.dumps(list(dict.fromkeys(str(e) for e in evidence))) if evidence else None,
                      self._enc(note) if note else None))

    def _insert_goal(self, conn: sqlite3.Connection, *, scope: str, text: str, source: str, at: float,
                     day: str | None = None, work_thread_id: int | None = None, words: str | None = None,
                     source_ref: str | None = None, status: str = "unknown", evidence: Sequence[str] = (),
                     due_at: float | None = None, next_checkin_at: float | None = None,
                     question_id: int | None = None, note: str | None = None) -> int:
        text = " ".join((text or "").split())
        if scope not in GOAL_SCOPES or status not in GOAL_STATUSES or source not in GOAL_SOURCES or not text:
            raise ValueError(f"bad goal (scope {scope!r}, status {status!r}, source {source!r}, text {bool(text)})")
        now = time.time()
        words = " ".join((words or "").split()) or None
        ev = list(dict.fromkeys(str(e) for e in evidence if str(e).strip()))
        cur = conn.execute(
            "INSERT INTO goals(scope, day, work_thread_id, text_ciphertext, words_ciphertext, source, source_ref, status,"
            " evidence, due_at, next_checkin_at, created_at, updated_at, ended_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (scope, day, int(work_thread_id) if work_thread_id is not None else None, self.cipher.encrypt(text),
             self._enc(words), source, source_ref, status, json.dumps(ev), due_at, next_checkin_at, float(at), now,
             float(at) if status in ("done", "dropped") else None))
        gid = int(cur.lastrowid)
        self._goal_event(conn, gid, at, "created", status=status, question_id=question_id, evidence=ev, note=note)
        return gid

    def add_goal(self, scope: str, text: str, *, source: str, at: float | None = None, **kw: Any) -> int:
        """Store one goal (``kw``: day, work_thread_id, words, source_ref, status, evidence, due_at,
        next_checkin_at, question_id, note); returns its id. Only the check-in pipeline writes goals."""
        when = float(at) if at is not None else time.time()
        return self._write(lambda conn: self._insert_goal(conn, scope=scope, text=text, source=source, at=when, **kw))

    #: "Leave this column as it is" for :meth:`update_goal` (None is a value there: no next check-in).
    UNSET: Any = object()

    def _update_goal(self, conn: sqlite3.Connection, goal_id: int, *, at: float, type: str, status: str | None = None,
                     text: str | None = None, words: str | None = None, evidence: Sequence[str] = (),
                     due_at: Any = UNSET, next_checkin_at: Any = UNSET, checked_in: bool = False,
                     question_id: int | None = None, note: str | None = None, event: bool = True) -> bool:
        row = conn.execute("SELECT * FROM goals WHERE id=?", (int(goal_id),)).fetchone()
        if row is None:
            return False
        if status is not None and status not in GOAL_STATUSES:
            raise ValueError(f"bad goal status {status!r}")
        sets, params = ["updated_at=?"], [time.time()]
        if status is not None:
            sets += ["status=?", "ended_at=?"]
            params += [status, float(at) if status in ("done", "dropped") else None]
        if text and " ".join(text.split()):
            sets.append("text_ciphertext=?")
            params.append(self.cipher.encrypt(" ".join(text.split())))
        if words and " ".join(words.split()):
            sets.append("words_ciphertext=?")
            params.append(self.cipher.encrypt(" ".join(words.split())))
        new_ev = [str(e) for e in evidence if str(e).strip()]
        if new_ev:
            try:
                old = [str(x) for x in json.loads(row["evidence"] or "[]")]
            except (ValueError, TypeError):
                old = []
            sets.append("evidence=?")
            params.append(json.dumps(list(dict.fromkeys([*old, *new_ev]))[-40:]))
        if due_at is not self.UNSET:
            sets.append("due_at=?")
            params.append(due_at)
        if next_checkin_at is not self.UNSET:
            sets.append("next_checkin_at=?")
            params.append(next_checkin_at)
        if checked_in:
            sets += ["last_checkin_at=?", "checkins=checkins+1"]
            params.append(float(at))
        conn.execute(f"UPDATE goals SET {', '.join(sets)} WHERE id=?", (*params, int(goal_id)))
        if event:
            self._goal_event(conn, goal_id, at, type, status=status, question_id=question_id, evidence=new_ev,
                             note=note)
        return True

    def update_goal(self, goal_id: int, *, type: str = "updated", at: float | None = None, **kw: Any) -> bool:
        """Change one goal (``kw``: status, text, words, evidence (added), due_at, next_checkin_at, checked_in,
        question_id, note), with its event; False when it does not exist. Only the check-in pipeline calls it."""
        when = float(at) if at is not None else time.time()
        return bool(self._write(lambda conn: self._update_goal(conn, goal_id, at=when, type=type, **kw)))

    def record_checkin_question(self, checkin: NudgeCheckin, nudge: NewNudge, question: NewQuestion, *,
                                moment: str, goal: dict[str, Any] | None = None
                                ) -> tuple[int, int | None, int | None, int | None]:
        """One transaction for a check-in Yuki asks: its goal (``goal``: ``{"id": n}`` for one it is about, or
        ``{"new": {scope, text, source, day, work_thread_id, source_ref, evidence, words}}`` for one memory shows
        that has no row yet - stored ``unknown`` -, with ``next_checkin_at``), the question (origin ``checkin``,
        status ``asked``, never deduped: a goal is checked on again), the card (kind ``question``, ``ref`` its
        question) and the check-in's accounting. Returns ``(checkin_id, nudge_id, question_id, goal_id)``. A card that
        carries nothing but Yuki's internal words (yuki.core.ownwords) is never shown, so nothing is asked: only the
        accounting is written, as held back (review F6), and ``(checkin_id, None, None, None)`` comes back."""
        if nudge.kind != "question":
            raise ValueError("a check-in's card is a question card")
        from yuki.core.ownwords import strip_internal

        if nudge.text and not strip_internal(nudge.text).strip():
            return (*self._write(lambda conn: self._record_checkin(conn, checkin, nudge, None)), None, None)

        def op(conn: sqlite3.Connection) -> tuple[int, int | None, int, int | None]:
            at = float(checkin.at)
            goal_id: int | None = None
            nxt = (goal or {}).get("next_checkin_at", self.UNSET)
            if goal and goal.get("id") is not None:
                if self._update_goal(conn, int(goal["id"]), at=at, type="checked_in", checked_in=True,
                                     next_checkin_at=nxt, evidence=goal.get("evidence") or (), event=False):
                    goal_id = int(goal["id"])
            elif goal and goal.get("new"):
                new = dict(goal["new"])
                goal_id = self._insert_goal(
                    conn, scope=new.get("scope") or "thread", text=new.get("text") or "",
                    source=new.get("source") or "thread", at=at, day=new.get("day"),
                    work_thread_id=new.get("work_thread_id"), words=new.get("words"),
                    source_ref=new.get("source_ref"), status="unknown", evidence=new.get("evidence") or (),
                    due_at=new.get("due_at"), next_checkin_at=None if nxt is self.UNSET else nxt)
                conn.execute("UPDATE goals SET last_checkin_at=?, checkins=1 WHERE id=?", (at, goal_id))
            subject, text = " ".join((question.subject or "").split()), " ".join((question.question or "").split())
            if not subject or not text:
                raise ValueError("a check-in question needs a subject and a question")
            cur = conn.execute(
                "INSERT INTO questions(at, created_at, kind, subject_ciphertext, subject_key, question_ciphertext,"
                " evidence_ciphertext, reason_ciphertext, importance, origin, source_ids, fact_ids, status, seen,"
                " updated_at, moment, goal_id) VALUES (?,?,?,?,?,?,?,?,?,'checkin',?,?,'asked',1,?,?,?)",
                (at, at, question.kind if question.kind in QUESTION_KINDS else "fact", self.cipher.encrypt(subject),
                 question_subject_key(self.cipher, f"checkin:{subject}"), self.cipher.encrypt(text),
                 self._enc(" ".join((question.evidence or "").split()) or None),
                 self._enc(" ".join((question.reason or "").split()) or None),
                 min(10, max(1, int(question.importance))), json.dumps(sorted({int(i) for i in question.source_ids})),
                 json.dumps(sorted({int(i) for i in question.fact_ids})), time.time(), moment, goal_id))
            qid = int(cur.lastrowid)
            if question.vector is not None:
                self._put_vec(conn, "question_vec", "question_id", qid, question.vector, question.embed_model)
            card = replace(nudge, ref=f"question:{qid}", inputs={**(nudge.inputs or {}), "question_id": qid,
                                                                 "goal_id": goal_id, "moment": moment})
            checkin_id, nudge_id = self._record_checkin(conn, checkin, card, (qid, "asked"))
            if goal_id is not None:
                self._goal_event(conn, goal_id, at, "checked_in", question_id=qid,
                                 evidence=(goal or {}).get("evidence") or ())
            return checkin_id, nudge_id, qid, goal_id

        return self._write(op)

    def checkin_questions(self, since: TimeArg = None, until: TimeArg = None) -> list[QuestionRecord]:
        """Check-ins asked in ``[since, until)`` (origin ``checkin``), oldest first."""
        sql, params = "SELECT * FROM questions WHERE origin='checkin'", []
        if (s := _ts(since)) is not None:
            sql += " AND created_at >= ?"
            params.append(s)
        if (u := _ts(until)) is not None:
            sql += " AND created_at < ?"
            params.append(u)
        return [self._question_row(r) for r in self._query(sql + " ORDER BY created_at, id", params)]

    def checkin_answers_pending(self, limit: int = 20) -> list[QuestionRecord]:
        """Check-ins the user answered, skipped or said not to ask about whose answer is not folded into the goals
        yet, oldest answer first."""
        rows = self._query("SELECT * FROM questions WHERE origin='checkin' AND status IN ('answered', 'skipped', 'never')"
                           " AND folded_at IS NULL ORDER BY coalesce(answered_at, updated_at), id LIMIT ?",
                           (int(limit),))
        return [self._question_row(r) for r in rows]

    def mark_checkin_folded(self, question_id: int, tone: str | None, at: float | None = None) -> bool:
        """The check-in's answer was folded into the goals; ``tone``: how the user took being asked."""
        when = float(at) if at is not None else time.time()
        return bool(self._write(lambda conn: conn.execute(
            "UPDATE questions SET folded_at=?, reply_tone=coalesce(?, reply_tone), updated_at=? WHERE id=?"
            " AND origin='checkin'", (when, tone, time.time(), int(question_id))).rowcount))

    def expire_checkins(self, before: float) -> int:
        """Check-ins asked before ``before`` and still unanswered are ``expired`` (answering is always optional)."""
        return self._write(lambda conn: conn.execute(
            "UPDATE questions SET status='expired', updated_at=? WHERE origin='checkin' AND status='asked'"
            " AND asked_at < ?", (time.time(), float(before))).rowcount)

    # -- the weekly review (yuki.memory.review) --------------------------------

    def _review(self, r: sqlite3.Row, score: float | None = None) -> WeeklyReview:
        def load(name: str, default: Any) -> Any:
            try:
                value = json.loads(self._dec(r[name]) or "null")
            except (ValueError, TypeError):
                return default
            return value if isinstance(value, type(default)) else default

        return WeeklyReview(
            id=r["id"], at=r["at"], finished_at=r["finished_at"], week=r["week"], period_start=r["period_start"],
            period_end=r["period_end"], trigger=r["trigger"], model=r["model"], text=self._dec(r["text_ciphertext"]),
            sections=load("sections_ciphertext", {}), teaser=self._dec(r["teaser_ciphertext"]),
            numbers=load("numbers_ciphertext", {}), candidates=load("candidates_ciphertext", []),
            candidates_run_id=r["candidates_run_id"], nudge_id=r["nudge_id"], outcome=r["outcome"],
            error=r["error"], cost_usd=r["cost_usd"], input_tokens=r["input_tokens"] or 0,
            output_tokens=r["output_tokens"] or 0, score=score,
        )

    def revise_weekly_review(
        self, review: WeeklyReview, *, text: str, sections: dict[str, Any], teaser: str | None, model: str | None,
        stats: ReviewStats | None = None, vector: np.ndarray | None = None, embed_model: str | None = None,
    ) -> int:
        """Store a corrected version of a finished review (``python -m yuki.memory.recheck``): a new ``ok`` row for the
        same week and period, dated like the original (so the review schedule and the teaser card see no new run:
        its ``nudge_id`` is carried over), and the original marked ``superseded`` (kept as history, out of every
        ``ok`` read: the latest review, recall, the window). Returns the new id."""
        now = time.time()
        stats = stats or ReviewStats()

        def seal(value: Any) -> str | None:
            return None if value is None else self.cipher.encrypt(json.dumps(value, ensure_ascii=False, default=str))

        def op(conn: sqlite3.Connection) -> int:
            cur = conn.execute(
                "INSERT INTO weekly_reviews(at, finished_at, week, period_start, period_end, trigger, model,"
                " text_ciphertext, sections_ciphertext, teaser_ciphertext, numbers_ciphertext, candidates_ciphertext,"
                " candidates_run_id, nudge_id, calls, input_tokens, output_tokens, cache_write_tokens,"
                " cache_read_tokens, cost_usd, latency_ms, stop_reason, outcome)"
                " VALUES (?,?,?,?,?,'recheck',?,?,?,?,?,NULL,?,?,?,?,?,?,?,?,?,?,'ok')",
                (float(review.at), now, review.week, float(review.period_start), float(review.period_end), model,
                 self._enc(text), seal(sections), self._enc(teaser), seal(review.numbers), review.candidates_run_id,
                 review.nudge_id, stats.calls, stats.input_tokens, stats.output_tokens, stats.cache_write_tokens,
                 stats.cache_read_tokens, stats.cost_usd, stats.latency_ms, stats.stop_reason))
            new_id = int(cur.lastrowid)
            conn.execute("UPDATE weekly_reviews SET outcome='superseded' WHERE id=?", (int(review.id),))
            if vector is not None:
                self._put_vec(conn, "weekly_review_vec", "review_id", new_id, vector, embed_model)
            return new_id

        return self._write(op)

    def start_weekly_review(
        self, week: str, period_start: float, period_end: float, trigger: str, model: str | None,
        *, at: float | None = None,
    ) -> int:
        """Insert a ``running`` review row and return its id."""
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO weekly_reviews(at, week, period_start, period_end, trigger, model, outcome)"
            " VALUES (?,?,?,?,?,?,'running')",
            (float(at) if at is not None else time.time(), str(week), float(period_start), float(period_end),
             str(trigger), model),
        ).lastrowid))

    def finish_weekly_review(
        self, review_id: int, *, outcome: str, stats: ReviewStats, text: str | None = None,
        sections: dict[str, Any] | None = None, teaser: str | None = None, numbers: dict[str, Any] | None = None,
        candidates: Sequence[dict[str, Any]] | None = None, error: str | None = None,
        vector: np.ndarray | None = None, embed_model: str | None = None,
    ) -> None:
        """Record a review's outcome, content (encrypted) and accounting in one transaction (plus its vector)."""

        def seal(value: Any) -> str | None:
            return None if value is None else self.cipher.encrypt(json.dumps(value, ensure_ascii=False, default=str))

        def op(conn: sqlite3.Connection) -> None:
            conn.execute(
                "UPDATE weekly_reviews SET finished_at=?, text_ciphertext=?, sections_ciphertext=?, teaser_ciphertext=?,"
                " numbers_ciphertext=?, candidates_ciphertext=?, calls=?, input_tokens=?, output_tokens=?,"
                " cache_write_tokens=?, cache_read_tokens=?, cost_usd=?, latency_ms=?, stop_reason=?, outcome=?,"
                " error=? WHERE id=?",
                (time.time(), self._enc(text), seal(sections), self._enc(teaser), seal(numbers),
                 seal(list(candidates)) if candidates is not None else None, stats.calls, stats.input_tokens,
                 stats.output_tokens, stats.cache_write_tokens, stats.cache_read_tokens, stats.cost_usd,
                 stats.latency_ms, stats.stop_reason, outcome, error, int(review_id)),
            )
            if vector is not None:
                self._put_vec(conn, "weekly_review_vec", "review_id", int(review_id), vector, embed_model)

        self._write(op)

    def weekly_review(self, review_id: int) -> WeeklyReview | None:
        rows = self._query("SELECT * FROM weekly_reviews WHERE id=?", (int(review_id),))
        return self._review(rows[0]) if rows else None

    def weekly_reviews(
        self, *, week: str | None = None, outcomes: Sequence[str] | None = ("ok",), limit: int | None = None,
    ) -> list[WeeklyReview]:
        """Review runs (of one ISO week if given, with these outcomes; ``None`` = any), newest first."""
        clauses, params = [], []
        if week:
            clauses.append("week = ?")
            params.append(str(week))
        if outcomes:
            clauses.append("outcome IN (%s)" % ",".join("?" * len(outcomes)))
            params.extend(outcomes)
        sql = "SELECT * FROM weekly_reviews" + ((" WHERE " + " AND ".join(clauses)) if clauses else "")
        sql += " ORDER BY at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._review(r) for r in self._query(sql, params)]

    def set_review_nudge(self, review_id: int, nudge_id: int) -> None:
        self._write(lambda conn: conn.execute(
            "UPDATE weekly_reviews SET nudge_id=? WHERE id=?", (int(nudge_id), int(review_id))))

    def review_candidates_pending(self, since: TimeArg = None) -> list[WeeklyReview]:
        """Finished reviews whose behaviour candidates no portrait run has been given yet, oldest first."""
        sql = ("SELECT * FROM weekly_reviews WHERE outcome='ok' AND candidates_run_id IS NULL"
               " AND candidates_ciphertext IS NOT NULL")
        params: list[Any] = []
        if (s := _ts(since)) is not None:
            sql += " AND at >= ?"
            params.append(s)
        return [rv for rv in (self._review(r) for r in self._query(sql + " ORDER BY at, id", params)) if rv.candidates]

    def mark_review_candidates(self, review_ids: Iterable[int], run_id: int) -> None:
        """The portrait run ``run_id`` was given these reviews' candidates."""
        ids = [int(i) for i in review_ids]
        if not ids:
            return
        self._write(lambda conn: conn.executemany(
            "UPDATE weekly_reviews SET candidates_run_id=? WHERE id=? AND candidates_run_id IS NULL",
            [(int(run_id), i) for i in ids]))

    def _review_filter(self, since: TimeArg, until: TimeArg) -> tuple[str, list[Any]]:
        clauses, params = ["w.outcome = 'ok'"], []
        if (s := _ts(since)) is not None:
            clauses.append("w.period_end >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("w.period_start < ?")
            params.append(u)
        return " WHERE " + " AND ".join(clauses), params

    def search_reviews(
        self, query_vec: np.ndarray, since: TimeArg = None, until: TimeArg = None, limit: int = 10
    ) -> list[WeeklyReview]:
        """Finished reviews most similar to ``query_vec`` (cosine) whose period overlaps ``[since, until)``."""
        where, params = self._review_filter(since, until)
        rows = self._query(
            "SELECT w.*, v.vec_ciphertext, v.dim FROM weekly_reviews w JOIN weekly_review_vec v ON v.review_id = w.id"
            + where, params,
        )
        return [self._review(r, score) for r, score in self._rank(rows, query_vec, limit)]

    def keyword_reviews(
        self, text: str, since: TimeArg = None, until: TimeArg = None, limit: int = 50
    ) -> list[WeeklyReview]:
        """Finished reviews whose text contains every term of ``text`` (casefolded), newest first."""
        terms = [t.casefold() for t in (text or "").split()]
        if not terms:
            return []
        where, params = self._review_filter(since, until)
        hits: list[WeeklyReview] = []
        for r in self._query("SELECT w.* FROM weekly_reviews w" + where + " ORDER BY w.at DESC, w.id DESC", params):
            review = self._review(r)
            if all(t in (review.text or "").casefold() for t in terms):
                hits.append(review)
                if len(hits) >= limit:
                    break
        return hits

    def reviews_without_vectors(self, limit: int = 16) -> list[WeeklyReview]:
        return [self._review(r) for r in self._query(
            "SELECT w.* FROM weekly_reviews w LEFT JOIN weekly_review_vec v ON v.review_id = w.id"
            " WHERE v.review_id IS NULL AND w.outcome='ok' AND w.text_ciphertext IS NOT NULL ORDER BY w.id LIMIT ?",
            (int(limit),),
        )]

    def add_review_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            for review_id, vec in items:
                self._put_vec(conn, "weekly_review_vec", "review_id", review_id, vec, model)
            return len(items)

        return self._write(op) if items else 0

    # -- calls and game voice chats, every track (yuki.memory.calls) -------------

    def _call(self, r: sqlite3.Row) -> CallRecord:
        return CallRecord(
            id=r["id"], app=_shown(r["app"], r["process"]), process=r["process"], service=r["service"], via=r["via"],
            detected_at=r["detected_at"], ended_at=r["ended_at"], status=r["status"], consent=r["consent"],
            consent_by=r["consent_by"], consent_at=r["consent_at"], output_form_factor=r["output_form_factor"],
            output_device=r["output_device"], headphones=None if r["headphones"] is None else bool(r["headphones"]),
            recording_started_at=r["recording_started_at"], recording_stopped_at=r["recording_stopped_at"],
            stop_requested=bool(r["stop_requested"]), stop_reason=r["stop_reason"], thread_id=r["thread_id"],
            chunks=r["chunks"], chunks_silent=r["chunks_silent"], speech_s=r["speech_s"], audio_s=r["audio_s"],
            utterances=r["utterances"], transcribe_ms=r["transcribe_ms"], stt_model=r["stt_model"],
            stt_device=r["stt_device"], captures=r["captures"], updated_at=r["updated_at"],
            hold_since=r["hold_since"] if "hold_since" in r.keys() else None,
            last_alive_at=r["last_alive_at"] if "last_alive_at" in r.keys() else None,
            kind=(r["kind"] if "kind" in r.keys() else None) or "call",
        )

    def start_call(
        self, *, app: str, process: str | None, via: str, service: str | None = None, at: float | None = None,
        status: str = "asking", consent: str | None = None, consent_by: str | None = None,
        output_form_factor: str | None = None, output_device: str | None = None, headphones: bool | None = None,
        hold_since: float | None = None, kind: str = "call",
    ) -> int:
        """A call (or, ``kind`` "game", a game's voice chat) was detected: one content-free ``calls`` row; returns
        its id. ``hold_since``: the start of the microphone hold it is in (the ConsentStore's record); the row is
        alive as of ``at``."""
        if kind not in CALL_KINDS:
            raise ValueError(f"unknown call kind {kind!r}")
        now = time.time()
        when = float(at) if at is not None else now
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO calls(app, process, service, via, detected_at, status, consent, consent_by, consent_at,"
            " output_form_factor, output_device, headphones, updated_at, hold_since, last_alive_at, kind)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (app or "", app_key(process), service, via, when, status, consent, consent_by,
             when if consent else None, output_form_factor, output_device,
             None if headphones is None else int(bool(headphones)), now,
             None if hold_since is None else float(hold_since), when, kind),
        ).lastrowid))

    def set_call_status_if(self, call_id: int, status: str, *, when_status: str) -> bool:
        """Move a call from ``when_status`` to ``status`` only while it has no answer (a game's card going up or
        coming down must never undo an answer that arrived meanwhile); False when it did not move."""
        def op(conn: sqlite3.Connection) -> bool:
            cur = conn.execute("UPDATE calls SET status=?, updated_at=? WHERE id=? AND status=? AND consent IS NULL",
                               (status, time.time(), int(call_id), when_status))
            return cur.rowcount > 0

        return bool(self._write(op))

    def call_max_seq(self, call_id: int) -> int:
        """The highest spool chunk number written for a call (0 when none): a call adopted after a restart goes on
        numbering its chunks from here (:meth:`write_spool` names files by call and number)."""
        rows = self._query("SELECT max(seq) FROM call_chunks WHERE call_id=?", (int(call_id),))
        return int(rows[0][0] or 0) if rows else 0

    def update_call(self, call_id: int, **fields: Any) -> None:
        """Set columns of one call (only the plumbing columns in ``_CALL_COLUMNS``)."""
        bad = set(fields) - _CALL_COLUMNS
        if bad:
            raise ValueError(f"not call columns: {sorted(bad)}")
        if not fields:
            return
        values = [int(v) if isinstance(v, bool) else v for v in fields.values()]
        sets = ", ".join(f"{k}=?" for k in fields)
        self._write(lambda conn: conn.execute(
            f"UPDATE calls SET {sets}, updated_at=? WHERE id=?", (*values, time.time(), int(call_id))))

    def call_record(self, call_id: int) -> CallRecord | None:
        rows = self._query("SELECT * FROM calls WHERE id=?", (int(call_id),))
        return self._call(rows[0]) if rows else None

    def calls(self, *, statuses: Sequence[str] | None = None, since: TimeArg = None,
              limit: int | None = None) -> list[CallRecord]:
        """Calls, newest first; ``statuses`` filters (e.g. :data:`CALL_OPEN_STATUSES`)."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' * len(statuses))})")
            params += list(statuses)
        if (s := _ts(since)) is not None:
            clauses.append("detected_at >= ?")
            params.append(s)
        sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY detected_at DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [self._call(r) for r in self._query(sql, params)]

    def calls_overlapping(self, since: TimeArg = None, until: TimeArg = None) -> list[CallRecord]:
        """Calls overlapping ``[since, until)``, oldest first: detected before ``until`` and not
        ended by ``since`` (a row with no ``ended_at`` is still going). Content-free; see
        :mod:`yuki.memory.callspans` for calls merged across service restarts."""
        clauses, params = [], []
        if (u := _ts(until)) is not None:
            clauses.append("detected_at < ?")
            params.append(u)
        if (s := _ts(since)) is not None:
            clauses.append("(ended_at IS NULL OR ended_at > ?)")
            params.append(s)
        sql = "SELECT * FROM calls" + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY detected_at, id"
        return [self._call(r) for r in self._query(sql, params)]

    def answer_call(self, call_id: int, answer: str, *, by: str = "card", at: float | None = None) -> bool:
        """Record the user's answer to a call's consent card; False when the call is no longer asking.

        ``yes``/``always`` leave the call to the worker (it starts, or waits for
        headphones); ``no``/``never`` decline it now; ``none`` (the card went
        away unanswered) marks it ``unanswered``. ``always``/``never`` are also
        kept for the call's app (:meth:`call_pref`), in the same transaction.
        """
        if answer not in CALL_ANSWERS:
            raise ValueError(f"unknown answer {answer!r}")
        now = time.time()
        when = float(at) if at is not None else now

        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT * FROM calls WHERE id=?", (int(call_id),)).fetchone()
            if row is None or row["status"] != "asking" or row["consent"] is not None:
                return False
            status = {"no": "declined", "never": "declined", "none": "unanswered"}.get(answer, "asking")
            conn.execute("UPDATE calls SET consent=?, consent_by=?, consent_at=?, status=?, updated_at=? WHERE id=?",
                         (answer, by, when, status, now, int(call_id)))
            # A call on a site inside a browser keeps the standing answer for that site's service (the row's
            # service), never for the whole browser (2026-09-28: "Always" for a Google Meet call made every
            # later microphone use of the Zen browser a call); an app's own call keeps it for the app.
            target = app_key(row["service"]) if row["service"] else app_key(row["process"])
            if answer in ("always", "never") and target:
                self._set_call_pref(conn, target, answer, row["app"], now)
            return True

        return self._write(op)

    def request_call_stop(self, call_id: int | None = None) -> int | None:
        """Ask the worker to stop remembering (``call_id`` None: every voice session recording now - a call and a
        game's voice chat at once stop together, every track of each); the newest such id, or None."""
        def op(conn: sqlite3.Connection) -> int | None:
            if call_id is None:
                rows = conn.execute(
                    "SELECT id FROM calls WHERE ended_at IS NULL AND (status IN ('recording','waiting_headphones')"
                    " OR (status='asking' AND consent IN ('yes','always'))) ORDER BY detected_at DESC, id DESC"
                ).fetchall()
            else:
                rows = conn.execute("SELECT id FROM calls WHERE id=?", (int(call_id),)).fetchall()
            if not rows:
                return None
            conn.executemany("UPDATE calls SET stop_requested=1, updated_at=? WHERE id=?",
                             [(time.time(), r["id"]) for r in rows])
            return int(rows[0]["id"])

        return self._write(op)

    @staticmethod
    def _set_call_pref(conn: sqlite3.Connection, app: str, decision: str | None, name: str | None,
                       now: float) -> None:
        if decision is None:
            conn.execute("DELETE FROM call_prefs WHERE app=?", (app,))
            return
        conn.execute(
            "INSERT INTO call_prefs(app, decision, name, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(app) DO UPDATE SET decision=excluded.decision, name=coalesce(excluded.name, name),"
            " updated_at=excluded.updated_at", (app, decision, name, now))

    def sites_shown(self, process: str | None, since: TimeArg = None) -> int:
        """How many different page hosts the timeline saw ``process`` show since ``since`` (content-free).

        A browser shows many sites (measured 2026-09-28: chrome 36, zen 8, arc 6); an app built on a web view
        shows its own one (discord, whatsapp, slack, claude: 1).
        """
        key = app_key(process)
        if not key:
            return 0
        s = _ts(since)
        rows = self._query("SELECT COUNT(DISTINCT host) AS n FROM timeline WHERE process=? AND host IS NOT NULL"
                           + (" AND started_at >= ?" if s is not None else ""), (key, s) if s is not None else (key,))
        return int(rows[0]["n"] or 0) if rows else 0

    def call_pref(self, app: str | None) -> str | None:
        """``always`` / ``never`` for a call app (any spelling of its process name), or None."""
        key = app_key(app)
        if not key:
            return None
        rows = self._query("SELECT decision FROM call_prefs WHERE app=?", (key,))
        return rows[0]["decision"] if rows else None

    def set_call_pref(self, app: str, decision: str | None, *, name: str | None = None) -> None:
        """Keep (``always``/``never``) or forget (``None``) the user's standing answer for a call app."""
        if decision not in (None, "always", "never"):
            raise ValueError(f"unknown call preference {decision!r}")
        key = app_key(app)
        if not key:
            raise ValueError("no app")
        self._write(lambda conn: self._set_call_pref(conn, key, decision, name, time.time()))

    def call_prefs(self) -> list[dict[str, Any]]:
        return [{**dict(r), "name": _shown(r["name"], r["app"]) if r["name"] else r["name"]}
                for r in self._query("SELECT app, decision, name, updated_at FROM call_prefs ORDER BY app")]

    def add_call_side(
        self, call_id: int, *, app: str, process: str | None, started_at: float, ended_at: float,
        utterances: Sequence[dict[str, Any]], final: bool = False, lines: Sequence[dict[str, Any]] | None = None,
        kind: str = "call",
    ) -> int | None:
        """Store a stretch of a call's transcript, for the journal; the capture id (None if empty).

        One thread per call (``kind`` :data:`CALL_SIDE_KIND`, scope ``call:<id>``);
        each capture's delta is ``{"call_id", "app", "kind", "start", "end", "final",
        "utterances": [the user's lines], "lines": [every line, time-ordered]}`` as JSON,
        encrypted like every capture (capture TTL). A line is ``{"at", "end", "text", "lang", "who", "app",
        "track_kind", ...}`` (:meth:`call_lines`). ``utterances`` keeps only the user's own lines, so every reader
        written when only the user's microphone was recorded stays true; ``lines`` (None: the utterances) carries
        the other people too. The call row's counters move in the same transaction. Text only (the audio stays
        in the spool until it is transcribed, then is deleted).
        """
        every = [dict(u) for u in (lines if lines is not None else utterances) if str(u.get("text") or "").strip()]
        if not every:
            return None
        capture_id = self._write(lambda conn: self._add_call_side(conn, call_id, app, process, started_at, ended_at,
                                                                  every, final, kind=kind))
        self._captured([capture_id])
        return capture_id

    @staticmethod
    def call_lines(data: dict[str, Any] | None) -> list[dict[str, Any]]:
        """Every line of a call capture's JSON (``call_side_captures``' ``data``), time-ordered, each with ``who``
        (``user`` | ``others``), ``app`` and ``track_kind`` (``call`` | ``game``): its ``lines`` when it has them,
        else its ``utterances`` - a capture written before 3 Oct 2026 holds only the user's own words, read as
        ``who`` "user" on the capture's app."""
        data = data if isinstance(data, dict) else {}
        raw = data.get("lines")
        if not isinstance(raw, list):
            raw = data.get("utterances") or []
        app = str(data.get("app") or "")
        kind = str(data.get("kind") or "call")
        out = []
        for u in raw:
            if not isinstance(u, dict) or not str(u.get("text") or "").strip():
                continue
            line = dict(u)
            line["who"] = "others" if line.get("who") == "others" else "user"
            line["app"] = str(line.get("app") or app)
            line["track_kind"] = str(line.get("track_kind") or kind)
            out.append(line)
        out.sort(key=lambda u: (float(u.get("at") or 0.0), float(u.get("end") or 0.0)))
        return out

    def _add_call_side(self, conn: sqlite3.Connection, call_id: int, app: str, process: str | None,
                       started_at: float, ended_at: float, items: list[dict[str, Any]], final: bool,
                       kind: str = "call") -> int:
        """``items``: every line (the user's and the others'); the user's alone also go in as ``utterances``."""
        redact_utterances(items)   # the transcriber already did; a second pass costs microseconds and holds for any writer
        for u in items:
            u["who"] = "others" if u.get("who") == "others" else "user"
            u.setdefault("app", app)
            u.setdefault("track_kind", kind)
        items.sort(key=lambda u: (float(u.get("at") or started_at), float(u.get("end") or 0.0)))
        mine = [u for u in items if u["who"] == "user"]
        delta = json.dumps({"call_id": int(call_id), "app": app, "kind": kind, "start": float(started_at),
                            "end": float(ended_at), "final": bool(final), "utterances": mine, "lines": items},
                           ensure_ascii=False)
        title = f"{app} game voice" if kind == "game" else f"{app} call"
        thread_id = self._upsert_thread(conn, app or "", title, None, process=process,
                                        scope=f"call:{int(call_id)}", kind=CALL_SIDE_KIND)
        first = min(float(u.get("at") or started_at) for u in items)
        cur = conn.execute(
            "INSERT INTO captures(thread_id, at, trigger, delta_ciphertext, chars, hash, kind, profile)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (thread_id, first, "call", self.cipher.encrypt(delta), len(delta),
             self.cipher.digest(CALL_SIDE_KIND, str(call_id), str(first), str(len(items))), CALL_SIDE_KIND,
             "callside"),
        )
        conn.execute("UPDATE threads SET last_seen=max(last_seen, ?) WHERE id=?", (float(ended_at), thread_id))
        conn.execute("UPDATE calls SET thread_id=?, captures=captures+1, updated_at=? WHERE id=?",
                     (thread_id, time.time(), int(call_id)))
        return int(cur.lastrowid)

    # -- the call spool: every track's speech, encrypted, until transcribed ------

    @property
    def spool_dir(self) -> Path:
        """``<database folder>\\spool``: encrypted call audio (every track), waiting for transcription."""
        return self.path.parent / "spool"

    @staticmethod
    def spool_track_label(side: str, process: str | None) -> str:
        """The track part of a spool file's name: ``mic`` for the user's microphone, ``app-<process>`` for an
        app's own audio (letters, digits and ``_`` only)."""
        if side != "others":
            return "mic"
        safe = "".join(ch if ch.isascii() and (ch.isalnum() or ch == "_") else "_" for ch in (process or "app"))
        return f"app-{safe[:40] or 'app'}"

    def write_spool(self, call_id: int, seq: int, *, start_at: float, audio_s: float, speech_s: float,
                    payload: bytes, codec: str, side: str = "user", track_app: str | None = None,
                    track_process: str | None = None, track_kind: str | None = None,
                    track_call_id: int | None = None) -> int:
        """Encrypt ``payload`` (compressed audio) into a spool file and record it; the chunk id.

        ``side``: ``user`` (the microphone) or ``others`` (an app's own audio, ``track_*`` naming the app); the
        file's name carries the track (``call12-mic-00003-....yspool``, ``call12-app-discord-00004-...``).
        The file holds only ciphertext (AES-256-GCM with the store's
        DPAPI-protected key): it is written to a temporary name and renamed,
        and the row is written after it, so a crash leaves at most an orphan
        file (removed by :meth:`sweep_spool`), never a row without its audio.
        """
        if side not in CALL_SIDES:
            raise ValueError(f"unknown side {side!r}")
        folder = self.spool_dir
        folder.mkdir(parents=True, exist_ok=True)
        track = self.spool_track_label(side, app_key(track_process))
        name = f"call{int(call_id)}-{track}-{int(seq):05d}-{os.urandom(4).hex()}.yspool"
        blob = b"YSP1" + self.cipher.encrypt_bytes(payload)
        tmp = folder / (name + ".part")
        tmp.write_bytes(blob)
        os.replace(tmp, folder / name)
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO call_chunks(call_id, seq, start_at, audio_s, speech_s, file, bytes, codec, status, created_at,"
            " side, track_app, track_process, track_kind, track_call_id)"
            " VALUES (?,?,?,?,?,?,?,?, 'spooled', ?,?,?,?,?,?)",
            (int(call_id), int(seq), float(start_at), float(audio_s), float(speech_s), name, len(blob), codec,
             time.time(), side, track_app, app_key(track_process), track_kind,
             None if track_call_id is None else int(track_call_id)),
        ).lastrowid))

    def read_spool(self, name: str) -> bytes:
        """The decrypted payload of one spool file (raises when it is missing or was tampered with)."""
        blob = (self.spool_dir / name).read_bytes()
        if not blob.startswith(b"YSP1"):
            raise ValueError("not a Yuki spool file")
        return self.cipher.decrypt_bytes(blob[4:])

    def _unlink_spool(self, name: str | None) -> None:
        if not name:
            return
        try:
            (self.spool_dir / name).unlink()
        except FileNotFoundError:
            pass

    def call_chunks(self, *, statuses: Sequence[str] = ("spooled",), call_id: int | None = None,
                    limit: int | None = None) -> list[dict[str, Any]]:
        """Chunk rows (cleartext metadata), oldest first, with their call's status, app and kind (``call_kind``).
        ``side`` is ``user`` (the microphone; every row before migration 34) or ``others``."""
        sql = ("SELECT k.*, c.status AS call_status, c.app AS app, c.process AS process, c.kind AS call_kind"
               " FROM call_chunks k"
               " LEFT JOIN calls c ON c.id = k.call_id"
               f" WHERE k.status IN ({','.join('?' * len(statuses))})")
        params: list[Any] = list(statuses)
        if call_id is not None:
            sql += " AND k.call_id=?"
            params.append(int(call_id))
        sql += " ORDER BY k.start_at, k.id"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        return [dict(r) for r in self._query(sql, params)]

    def finish_call_chunk(self, chunk_id: int, utterances: Sequence[dict[str, Any]], *, lang: str | None,
                          ms: float, model: str | None, device: str | None) -> bool:
        """The chunk's words are stored (encrypted) and its audio deleted: in that order, so audio is only ever
        deleted once its text is safe. False when the chunk was erased or expired meanwhile."""
        rows = self._query("SELECT file, status FROM call_chunks WHERE id=?", (int(chunk_id),))
        if not rows or rows[0]["status"] != "spooled":
            return False
        text = json.dumps(list(utterances), ensure_ascii=False)
        self._write(lambda conn: conn.execute(
            "UPDATE call_chunks SET status='done', text_ciphertext=?, lang=?, transcribe_ms=?, stt_model=?,"
            " stt_device=?, file=NULL, done_at=?, removed_at=? WHERE id=? AND status='spooled'",
            (self.cipher.encrypt(text), lang, float(ms), model, device, time.time(), time.time(), int(chunk_id))))
        self._unlink_spool(rows[0]["file"])
        return True

    def fail_call_chunk(self, chunk_id: int, error: str, *, max_attempts: int = 3) -> None:
        """One failed attempt; after ``max_attempts`` the chunk is given up and its audio deleted."""
        rows = self._query("SELECT file, attempts FROM call_chunks WHERE id=?", (int(chunk_id),))
        if not rows:
            return
        give_up = rows[0]["attempts"] + 1 >= max_attempts
        self._write(lambda conn: conn.execute(
            "UPDATE call_chunks SET attempts=attempts+1, error=?, status=?, file=?, removed_at=? WHERE id=?",
            (error[:300], "failed" if give_up else "spooled", None if give_up else rows[0]["file"],
             time.time() if give_up else None, int(chunk_id))))
        if give_up:
            self._unlink_spool(rows[0]["file"])

    def remove_call_chunks(self, chunk_ids: Iterable[int], reason: str) -> tuple[int, int]:
        """Delete these chunks' audio now (``expired`` | ``capped`` | ``erased`` | ``lost``); ``(chunks, bytes)``."""
        ids = [int(i) for i in chunk_ids]
        if not ids:
            return 0, 0
        rows = self._query(f"SELECT id, file, bytes FROM call_chunks WHERE status='spooled' AND id IN"
                           f" ({','.join('?' * len(ids))})", ids)
        for r in rows:
            self._unlink_spool(r["file"])
        now = time.time()
        self._write(lambda conn: conn.executemany(
            "UPDATE call_chunks SET status=?, file=NULL, removed_at=? WHERE id=? AND status='spooled'",
            [(reason, now, r["id"]) for r in rows]))
        return len(rows), sum(int(r["bytes"] or 0) for r in rows)

    def sweep_spool(self, *, max_age_s: float, max_bytes: int, now: float | None = None) -> list[tuple[str, int, int]]:
        """Enforce the spool's caps: audio older than ``max_age_s`` is deleted (``expired``), then the oldest
        until the total is under ``max_bytes`` (``capped``); rows whose file vanished become ``lost`` and files
        without a row (a crash between writing and recording) are deleted. Returns ``[(reason, chunks, bytes)]``."""
        now = time.time() if now is None else float(now)
        out: list[tuple[str, int, int]] = []
        rows = self.call_chunks()
        known = {r["file"] for r in rows if r["file"]}
        folder = self.spool_dir
        if folder.exists():
            for f in folder.iterdir():
                if f.is_file() and f.name not in known and now - f.stat().st_mtime > 120:
                    size = f.stat().st_size
                    f.unlink(missing_ok=True)
                    out.append(("orphan", 1, size))
        lost = [r["id"] for r in rows if not (folder / r["file"]).exists()] if rows else []
        if lost:
            out.append(("lost", *self.remove_call_chunks(lost, "lost")))
        rows = [r for r in rows if r["id"] not in set(lost)]
        old = [r["id"] for r in rows if now - float(r["created_at"]) > max_age_s]
        if old:
            out.append(("expired", *self.remove_call_chunks(old, "expired")))
        rows = [r for r in rows if r["id"] not in set(old)]
        total = sum(int(r["bytes"] or 0) for r in rows)
        drop = []
        for r in rows:                      # oldest first
            if total <= max_bytes:
                break
            drop.append(r["id"])
            total -= int(r["bytes"] or 0)
        if drop:
            out.append(("capped", *self.remove_call_chunks(drop, "capped")))
        # who took the microphone when: only needed until the audio it describes is transcribed (or gone)
        cut = now - max_age_s - 86400.0
        gone = self._write(lambda conn: conn.execute(
            "DELETE FROM mic_spans WHERE coalesce(ended_at, started_at) < ?", (cut,)).rowcount)
        if gone:
            out.append(("mic_spans", int(gone), 0))
        return out

    # -- who took the microphone when (migration 34): content-free spans -----------------------

    def open_mic_span(self, *, kind: str, at: float, process: str | None = None, app: str | None = None,
                      pid: int | None = None) -> int:
        """A span begins: ``kind`` "capture" (an app's capture session went active: it is taking the microphone)
        or "watch" (the watcher is running, so absence of a capture span means nobody took it). Its id."""
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO mic_spans(kind, process, app, pid, started_at) VALUES (?,?,?,?,?)",
            (kind, app_key(process), app, None if pid is None else int(pid), float(at))).lastrowid))

    def close_mic_span(self, span_id: int, at: float) -> None:
        self._write(lambda conn: conn.execute("UPDATE mic_spans SET ended_at=? WHERE id=? AND ended_at IS NULL",
                                              (float(at), int(span_id))))

    def close_open_mic_spans(self, at: float) -> int:
        """Spans a previous run left open end now (nothing was recorded while it was down)."""
        return int(self._write(lambda conn: conn.execute(
            "UPDATE mic_spans SET ended_at=max(started_at, ?) WHERE ended_at IS NULL", (float(at),)).rowcount))

    def mic_spans_between(self, since: float, until: float) -> list[dict[str, Any]]:
        """Spans overlapping ``[since, until)``, oldest first: ``{"kind", "process", "app", "pid", "start",
        "end"}`` (``end`` None: still open)."""
        rows = self._query("SELECT kind, process, app, pid, started_at, ended_at FROM mic_spans"
                           " WHERE started_at < ? AND (ended_at IS NULL OR ended_at > ?) ORDER BY started_at, id",
                           (float(until), float(since)))
        return [{"kind": r["kind"], "process": r["process"], "app": r["app"], "pid": r["pid"],
                 "start": float(r["started_at"]), "end": None if r["ended_at"] is None else float(r["ended_at"])}
                for r in rows]

    def track_speech(self, call_ids: Iterable[int]) -> dict[tuple[str, str], float]:
        """Seconds of speech kept per track of these calls (every chunk, transcribed or not):
        ``{(side, track process or ""): speech_s}``. Content-free."""
        ids = sorted({int(i) for i in call_ids})
        if not ids:
            return {}
        rows = self._query(
            f"SELECT side, coalesce(track_process, '') AS process, coalesce(sum(speech_s), 0) AS s FROM call_chunks"
            f" WHERE call_id IN ({','.join('?' * len(ids))}) GROUP BY side, coalesce(track_process, '')", ids)
        return {(r["side"] or "user", r["process"] or ""): float(r["s"] or 0.0) for r in rows}

    def spool_summary(self) -> dict[str, Any]:
        """``{"calls", "chunks", "audio_s", "speech_s", "bytes", "oldest_at"}`` of the audio waiting."""
        r = self._query("SELECT count(DISTINCT call_id) AS calls, count(*) AS chunks, coalesce(sum(audio_s),0) AS audio_s,"
                        " coalesce(sum(speech_s),0) AS speech_s, coalesce(sum(bytes),0) AS bytes,"
                        " min(created_at) AS oldest_at FROM call_chunks WHERE status='spooled'")[0]
        return {k: r[k] for k in r.keys()}

    def journal_call_chunks(self, call_id: int) -> int | None:
        """A call whose chunks are all transcribed: its words become one ``my_call_side`` capture for the
        journal, and the chunk rows drop their text (one transaction). Every track of the call goes into the one
        capture, merged in time order (:meth:`call_lines`); the user's own lines are also its ``utterances``. A
        line stored before migration 34 carries no ``who``: it is the user's (its chunk's ``side``). The capture
        id, or None (nothing said, or chunks still waiting)."""
        def op(conn: sqlite3.Connection) -> int | None:
            if conn.execute("SELECT 1 FROM call_chunks WHERE call_id=? AND status='spooled' LIMIT 1",
                            (int(call_id),)).fetchone():
                return None
            call = conn.execute("SELECT app, process, kind FROM calls WHERE id=?", (int(call_id),)).fetchone()
            rows = conn.execute("SELECT id, text_ciphertext, side, track_app, track_kind FROM call_chunks"
                                " WHERE call_id=? AND status='done' ORDER BY start_at, id", (int(call_id),)).fetchall()
            if not rows:
                return None
            app = (call["app"] if call is not None else "") or ""
            kind = (call["kind"] if call is not None else None) or "call"
            items: list[dict[str, Any]] = []
            for r in rows:
                side = "others" if r["side"] == "others" else "user"
                for u in json.loads(self.cipher.decrypt(r["text_ciphertext"]) or "[]"):
                    if not isinstance(u, dict) or not str(u.get("text") or "").strip():
                        continue
                    u.setdefault("who", side)
                    u.setdefault("app", r["track_app"] or app)
                    u.setdefault("track_kind", r["track_kind"] or kind)
                    items.append(u)
            items.sort(key=lambda u: (float(u.get("at") or 0.0), float(u.get("end") or 0.0)))
            capture_id = None
            if items and call is not None:
                capture_id = self._add_call_side(conn, call_id, app, call["process"], items[0]["at"],
                                                 max(float(u.get("end") or u["at"]) for u in items), items, True,
                                                 kind=kind)
            conn.executemany("UPDATE call_chunks SET status='journaled', text_ciphertext=NULL WHERE id=?",
                             [(r["id"],) for r in rows])
            conn.execute("UPDATE calls SET utterances=utterances+?, updated_at=? WHERE id=?",
                         (sum(1 for u in items if u.get("who") != "others"), time.time(), int(call_id)))
            return capture_id

        capture_id = self._write(op)
        if capture_id is not None:
            self._captured([capture_id])
        return capture_id

    def commit_call_batch(
        self, call: ModelCall, capture_ids: Iterable[int], facts: Sequence[NewFact],
        commitments: Sequence[ConversationChange] = (), *, supersedes: Sequence[int] = (),
        questions: Sequence[NewQuestion] = (), question_outcomes: list[tuple[int | None, str]] | None = None,
        work: Sequence[WorkOp] = (),
    ) -> tuple[int, list[int]]:
        """:meth:`commit_batch` for a call's side, plus the user's commitments from it, in one transaction.

        Commitments are conversation facts of kind ``commitment``, origin
        ``call`` (so they reach the to-do list and reminders like any other),
        each already checked to quote the user's own words. Returns the batch
        id and the new commitment ids. ``supersedes``, ``questions``,
        ``question_outcomes`` and ``work``: as for :meth:`commit_batch` (a call's questions are
        about names the user said).
        """
        ids = list(capture_ids)
        now = time.time()
        known = self.question_known_keys() if questions else set()

        def op(conn: sqlite3.Connection) -> tuple[int, list[int]]:
            batch_id = self._insert_batch(conn, call, len(ids), len(facts))
            prior = self._line_snapshot(conn, supersedes)   # read before the re-journal supersedes them
            self._supersede_batches(conn, batch_id, supersedes)
            journal_ids = [self._insert_journal(conn, fact, batch_id) for fact in facts]
            self._hold_journal(conn, facts, journal_ids, call.thread_id, prior)
            if work:
                self._apply_work(conn, work, batch_id=batch_id, journal_ids=journal_ids)
            for q in questions:
                q.source_ids = sorted({*q.source_ids, *(journal_ids[i] for i in q.fact_indexes
                                                        if 0 <= i < len(journal_ids))})
            outcomes = [self._insert_question(conn, q, time.time(), known) for q in questions]
            if question_outcomes is not None:
                question_outcomes[:] = outcomes
            new_ids = [self._insert_cfact(conn, ch, now, None) for ch in commitments]
            conn.executemany("UPDATE captures SET journal_batch_id=? WHERE id=?", [(batch_id, i) for i in ids])
            return batch_id, new_ids

        return self._write(op)

    # -- call timeline digests (migration 19, yuki.memory.calldigest) --------------

    def call_side_captures(self, call_id: int) -> list[dict[str, Any]]:
        """The ``my_call_side`` captures of one call, oldest first: ``{"id", "at", "journaled", "data"}``, ``data``
        being the decrypted capture JSON (``{"call_id", "app", "kind", "start", "end", "final", "utterances": [the
        user's lines], "lines": [every line]}``; read the lines through :meth:`call_lines`). Empty once the
        captures are past the 30-day TTL."""
        rows = self._query(
            "SELECT c.id, c.at, c.journal_batch_id, c.delta_ciphertext FROM captures c"
            " JOIN calls k ON k.thread_id = c.thread_id WHERE k.id=? AND c.kind=? ORDER BY c.at, c.id",
            (int(call_id), CALL_SIDE_KIND))
        out = []
        for r in rows:
            try:
                data = json.loads(self.cipher.decrypt(r["delta_ciphertext"]) or "{}")
            except (ValueError, TypeError):
                data = {}
            out.append({"id": int(r["id"]), "at": r["at"], "journaled": r["journal_batch_id"] is not None,
                        "data": data if isinstance(data, dict) else {}})
        return out

    def call_digest_candidates(self, *, max_attempts: int = 3, stale_s: float = 900.0, now: float | None = None,
                               journaled_only: bool = True, include_done: bool = False) -> list[dict[str, Any]]:
        """Ended calls whose words landed and whose digest is missing, failed (under ``max_attempts``) or written
        from other captures than the call has now; oldest first: ``{"call_id", "app", "capture_ids"}``.
        ``journaled_only``: wait until the journal has read every capture (the journal goes first).
        ``include_done``: every ended call that still has its words (a forced re-write). Content-free."""
        now = time.time() if now is None else float(now)
        marks = ",".join("?" * len(CALL_OPEN_STATUSES))
        rows = self._query(
            "SELECT k.id AS call_id, k.app, group_concat(c.id) AS ids,"
            " sum(CASE WHEN c.journal_batch_id IS NULL THEN 1 ELSE 0 END) AS unjournaled,"
            " d.status, d.capture_ids AS digested, d.attempts, d.claimed_at"
            " FROM calls k JOIN captures c ON c.thread_id = k.thread_id AND c.kind = ?"
            " LEFT JOIN call_digests d ON d.call_id = k.id"
            f" WHERE k.status NOT IN ({marks}) GROUP BY k.id ORDER BY k.detected_at, k.id",
            (CALL_SIDE_KIND, *CALL_OPEN_STATUSES))
        out = []
        for r in rows:
            ids = sorted(int(x) for x in str(r["ids"] or "").split(",") if x)
            if not ids or (journaled_only and r["unjournaled"]):
                continue
            try:
                done = sorted(int(x) for x in json.loads(r["digested"] or "[]"))
            except (ValueError, TypeError):
                done = []
            same = done == ids and not include_done
            if r["status"] in ("ok", "empty") and same:
                continue
            if r["status"] == "failed" and same and int(r["attempts"] or 0) >= max_attempts:
                continue
            if r["status"] == "running" and (r["claimed_at"] or 0.0) > now - stale_s:
                continue
            out.append({"call_id": int(r["call_id"]), "app": _shown(r["app"] or ""), "capture_ids": ids})
        return out

    def claim_call_digest(self, call_id: int, *, app: str, capture_ids: Sequence[int], owner: str,
                          stale_s: float = 900.0, now: float | None = None) -> bool:
        """Mark one call's digest as being written by ``owner`` (a process tag); False when another writer holds
        a fresh claim. A digest already there keeps its blocks until the new one is saved; new captures reset
        the attempts."""
        now = time.time() if now is None else float(now)
        ids = json.dumps(sorted(int(i) for i in capture_ids))

        def op(conn: sqlite3.Connection) -> bool:
            row = conn.execute("SELECT status, claimed_by, claimed_at, capture_ids FROM call_digests WHERE call_id=?",
                               (int(call_id),)).fetchone()
            if row is None:
                conn.execute("INSERT INTO call_digests(call_id, app, capture_ids, status, claimed_by, claimed_at,"
                             " created_at, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                             (int(call_id), app or "", ids, "running", owner, now, now, now))
                return True
            if (row["status"] == "running" and row["claimed_by"] != owner
                    and (row["claimed_at"] or 0.0) > now - stale_s):
                return False
            reset = row["capture_ids"] != ids
            conn.execute("UPDATE call_digests SET status='running', claimed_by=?, claimed_at=?, updated_at=?,"
                         " attempts=CASE WHEN ? THEN 0 ELSE attempts END WHERE call_id=?",
                         (owner, now, now, int(reset), int(call_id)))
            return True

        return bool(self._write(op))

    def save_call_digest(
        self, call_id: int, *, app: str, capture_ids: Sequence[int], status: str, gist: str | None,
        blocks: Sequence[dict[str, Any]], started_at: float | None, ended_at: float | None, speech_s: float,
        utterances: int, kept: int, model: str | None, usage: dict[str, int] | None = None, requests: int = 0,
        cost_usd: float | None = None, latency_ms: float = 0.0, context: dict[str, Any] | None = None,
        gist_shown: str | None = None,
    ) -> None:
        """Store a call's digest (``status`` ok, or empty when the call held nothing worth a line), encrypted;
        the cost is added to what earlier attempts cost. ``context``: who was on the call and where, as the
        screen around it showed (:mod:`yuki.memory.callcontext`; migration 25). ``gist_shown``: the gist said to
        the user (migration 33); ``None`` leaves it for the titles pass."""
        usage = usage or {}
        now = time.time()
        ids = json.dumps(sorted(int(i) for i in capture_ids))
        values = (app or "", started_at, ended_at, float(speech_s or 0.0), int(utterances), int(kept), ids, status,
                  self._enc(gist) if gist else None, self.cipher.encrypt(json.dumps(list(blocks), ensure_ascii=False)),
                  model, int(requests), int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0),
                  int(usage.get("cache_write_tokens") or 0), int(usage.get("cache_read_tokens") or 0),
                  float(cost_usd or 0.0), float(latency_ms or 0.0),
                  self.cipher.encrypt(json.dumps(context, ensure_ascii=False)) if context else None)

        def op(conn: sqlite3.Connection) -> None:
            if conn.execute("SELECT 1 FROM call_digests WHERE call_id=?", (int(call_id),)).fetchone() is None:
                conn.execute("INSERT INTO call_digests(call_id, app, capture_ids, status, created_at, updated_at)"
                             " VALUES (?,?,?,?,?,?)", (int(call_id), app or "", ids, status, now, now))
            conn.execute(
                "UPDATE call_digests SET app=?, started_at=?, ended_at=?, speech_s=?, utterances=?, kept=?,"
                " capture_ids=?, status=?, gist_ciphertext=?, blocks_ciphertext=?, model=?, requests=?,"
                " input_tokens=?, output_tokens=?, cache_write_tokens=?, cache_read_tokens=?,"
                " cost_usd=cost_usd+?, latency_ms=?, context_ciphertext=?, error=NULL, claimed_by=NULL,"
                " claimed_at=NULL, updated_at=?, gist_shown_ciphertext=? WHERE call_id=?",
                (*values, now, self._enc(" ".join(gist_shown.split())) if gist_shown and gist_shown.strip() else None,
                 int(call_id)))
            try:   # the digest's words changed: its vector (whole-memory ranking, migration 23) is made again
                conn.execute("DELETE FROM call_digest_vec WHERE call_id=?", (int(call_id),))
            except sqlite3.OperationalError:
                pass

        self._write(op)

    def fail_call_digest(self, call_id: int, *, capture_ids: Sequence[int], error: str, model: str | None = None,
                         requests: int = 0, cost_usd: float | None = None) -> None:
        """A digest attempt failed: count it (a call is given up after its attempts), keep any older blocks."""
        now = time.time()
        ids = json.dumps(sorted(int(i) for i in capture_ids))
        self._write(lambda conn: conn.execute(
            "UPDATE call_digests SET status='failed', attempts=attempts+1, error=?, capture_ids=?, model=?,"
            " requests=?, cost_usd=cost_usd+?, claimed_by=NULL, claimed_at=NULL, updated_at=? WHERE call_id=?",
            (str(error)[:500], ids, model, int(requests), float(cost_usd or 0.0), now, int(call_id))))

    def call_digests(self, call_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
        """The digests of these call rows, decrypted: ``{call_id: {"call_id", "app", "status", "gist", "blocks",
        "started_at", "ended_at", "speech_s", "utterances", "kept", "capture_ids", "model", "cost_usd",
        "updated_at"}}``; rows without one are left out. ``blocks`` is ``[]`` until a first digest is written."""
        ids = sorted({int(i) for i in call_ids})
        if not ids:
            return {}
        rows = self._query(f"SELECT * FROM call_digests WHERE call_id IN ({','.join('?' * len(ids))})", ids)
        out = {}
        for r in rows:
            try:
                blocks = json.loads(self._dec(r["blocks_ciphertext"]) or "[]")
            except (ValueError, TypeError):
                blocks = []
            try:
                captures = json.loads(r["capture_ids"] or "[]")
            except (ValueError, TypeError):
                captures = []
            try:   # migration 25: who was on the call and where, as the screen around it showed
                context = json.loads(self._dec(r["context_ciphertext"]) or "{}") \
                    if "context_ciphertext" in r.keys() else {}
            except (ValueError, TypeError):
                context = {}
            out[int(r["call_id"])] = {
                "context": context if isinstance(context, dict) else {},
                "call_id": int(r["call_id"]), "app": _shown(r["app"]), "status": r["status"],
                "gist": self._dec(r["gist_ciphertext"]),
                "gist_shown": self._dec(r["gist_shown_ciphertext"]) if "gist_shown_ciphertext" in r.keys() else None,
                "blocks": blocks if isinstance(blocks, list) else [], "started_at": r["started_at"],
                "ended_at": r["ended_at"], "speech_s": r["speech_s"], "utterances": r["utterances"], "kept": r["kept"],
                "capture_ids": captures, "model": r["model"], "cost_usd": r["cost_usd"], "attempts": r["attempts"],
                "error": r["error"], "updated_at": r["updated_at"],
            }
        return out

    def call_digest(self, call_id: int) -> dict[str, Any] | None:
        return self.call_digests([call_id]).get(int(call_id))

    def call_digests_ready(self, since: TimeArg = None, until: TimeArg = None) -> dict[int, float]:
        """``{call_id: when its digest was written}`` for digests written (status ``ok``, blocks stored) in
        ``[since, until]``: content-free, for the call-recap card (yuki.memory.presence)."""
        clauses, params = ["status = 'ok'", "blocks_ciphertext IS NOT NULL"], []
        if (s := _ts(since)) is not None:
            clauses.append("updated_at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("updated_at <= ?")
            params.append(u)
        try:
            rows = self._query("SELECT call_id, updated_at FROM call_digests WHERE " + " AND ".join(clauses), params)
        except sqlite3.OperationalError:   # a database before migration 19
            return {}
        return {int(r[0]): float(r[1] or 0.0) for r in rows}

    def digested_call_threads(self) -> dict[int, int]:
        """``{thread_id: call_id}`` of the calls that have a written digest (to point recall hits at it)."""
        rows = self._query("SELECT k.thread_id, k.id FROM calls k JOIN call_digests d ON d.call_id = k.id"
                           " WHERE k.thread_id IS NOT NULL AND d.blocks_ciphertext IS NOT NULL")
        return {int(r[0]): int(r[1]) for r in rows}

    def voice_turns(self, since: TimeArg, until: TimeArg) -> list[dict[str, Any]]:
        """Spoken exchanges with Yuki in ``[since, until)`` (outcome ``voice``), oldest first, content-free:
        ``{"at", "request_id", "addressed"}`` (``at`` is when the turn closed; ``addressed`` yuki | others |
        unanswered | None)."""
        rows = self._query("SELECT at, request_id, addressed FROM conversation_turns WHERE outcome = 'voice'"
                           " AND at >= ? AND at < ? ORDER BY at", (_ts(since) or 0.0, _ts(until) or time.time()))
        return [{"at": r["at"], "request_id": str(r["request_id"]), "addressed": r["addressed"]} for r in rows]

    # -- maintenance -------------------------------------------------------

    # -- fast recall index (migration 17) ------------------------------------

    #: What :meth:`recall_vectors` reads per kind: the metadata columns and the join to the vectors.
    _RECALL_SQL = {
        "journal": ("SELECT j.id, j.at, j.importance, j.nature, j.tainted, j.by_yuki, j.attention, v.vec_ciphertext, v.dim"
                    " FROM journal j JOIN journal_vec v ON v.journal_id = j.id"
                    " WHERE j.superseded_at IS NULL AND j.id > ?"),
        "portrait": ("SELECT f.id, f.valid_from AS at, f.importance, f.kind, f.origin, f.confidence,"
                     " v.vec_ciphertext, v.dim FROM portrait_facts f JOIN portrait_fact_vec v ON v.fact_id = f.id"
                     " WHERE f.valid_to IS NULL AND f.id > ?"),
        "episode": ("SELECT e.id, e.started_at AS at, e.ended_at, v.vec_ciphertext, v.dim"
                    " FROM episodes e JOIN episode_vec v ON v.episode_id = e.id"
                    " WHERE e.expired_at IS NULL AND e.id > ?"),
        "commitment": ("SELECT c.id, c.valid_from AS at, c.status, c.kind, v.vec_ciphertext, v.dim"
                       " FROM conversation_facts c JOIN conversation_fact_vec v ON v.fact_id = c.id"
                       " WHERE c.kind = 'commitment' AND c.status IN ('active', 'done') AND c.id > ?"),
        "resource": ("SELECT c.id, c.valid_from AS at, c.status, c.kind, v.vec_ciphertext, v.dim"
                     " FROM conversation_facts c JOIN conversation_fact_vec v ON v.fact_id = c.id"
                     " WHERE c.kind = 'resource' AND c.status = 'active' AND c.id > ?"),
        # whole-memory ranking (migration 23, yuki.memory.related): session summaries, exchanges said to Yuki,
        # know-how, call digests, threads of work and links seen
        "session": ("SELECT s.id, s.ended_at AS at, v.vec_ciphertext, v.dim FROM session_summaries s"
                    " JOIN session_summary_vec v ON v.summary_id = s.id WHERE s.id > ?"),
        "chat": ("SELECT t.id, t.at, t.addressed, v.vec_ciphertext, v.dim FROM conversation_turns t"
                 " JOIN conversation_turn_vec v ON v.turn_id = t.id WHERE t.id > ?"),
        "knowhow": ("SELECT k.id, k.valid_from AS at, v.vec_ciphertext, v.dim FROM knowhow k"
                    " JOIN knowhow_vec v ON v.knowhow_id = k.id WHERE k.valid_to IS NULL AND k.id > ?"),
        "call": ("SELECT c.id, coalesce(d.started_at, c.detected_at) AS at, v.vec_ciphertext, v.dim FROM calls c"
                 " JOIN call_digest_vec v ON v.call_id = c.id JOIN call_digests d ON d.call_id = c.id"
                 " WHERE c.id > ?"),
        "thread": ("SELECT w.id, w.last_seen AS at, w.importance, w.status, v.vec_ciphertext, v.dim FROM work_threads w"
                   " JOIN work_thread_vec v ON v.work_thread_id = w.id WHERE w.status NOT IN ('merged', 'dismissed')"
                   " AND w.id > ?"),
        "link": ("SELECT l.id, l.last_seen AS at, l.by_yuki, l.attention, v.vec_ciphertext, v.dim FROM links_seen l"
                 " JOIN link_vec v ON v.link_id = l.id WHERE l.id > ?"),
    }

    def recall_vectors(self, kind: str, after_id: int = 0) -> list[tuple[sqlite3.Row, np.ndarray]]:
        """Rows of one recall-index kind with id above ``after_id``, each with its decrypted vector.

        Kinds: ``journal`` (current facts), ``portrait`` (current facts), ``episode`` (current),
        ``commitment`` (open or done). Nothing but the vectors is decrypted: the index
        (:mod:`yuki.memory.relevant`) decrypts the text of the few it picks.
        """
        try:
            rows = self._query(self._RECALL_SQL[kind], (int(after_id),))
        except sqlite3.OperationalError:   # a table a newer kind needs is not there (a database not yet migrated)
            return []
        out: list[tuple[sqlite3.Row, np.ndarray]] = []
        for r in rows:
            try:
                out.append((r, np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)))
            except Exception:
                continue
        return out

    def recall_live_ids(self) -> dict[str, set[int]]:
        """Which indexed rows are still live, per kind (cheap id-only queries; nothing decrypted).

        A journal fact can be superseded by a re-journal, a portrait fact or an episode replaced,
        a commitment superseded or revoked after the index loaded it; the index drops those.
        """
        live = {
            "journal_dead": {r[0] for r in self._query("SELECT id FROM journal WHERE superseded_at IS NOT NULL")},
            "portrait": {r[0] for r in self._query("SELECT id FROM portrait_facts WHERE valid_to IS NULL")},
            "episode": {r[0] for r in self._query("SELECT id FROM episodes WHERE expired_at IS NULL")},
            "commitment": {r[0] for r in self._query(
                "SELECT id FROM conversation_facts WHERE kind='commitment' AND status IN ('active','done')")},
            "resource": {r[0] for r in self._query(
                "SELECT id FROM conversation_facts WHERE kind='resource' AND status='active'")},
        }
        # the whole-memory kinds (migration 23): summaries, exchanges and links are never ended; know-how and
        # threads are (a superseded entry, a merged thread)
        for kind, sql in (("knowhow", "SELECT id FROM knowhow WHERE valid_to IS NULL"),
                          ("thread", "SELECT id FROM work_threads WHERE status NOT IN ('merged', 'dismissed')")):
            try:
                live[kind] = {r[0] for r in self._query(sql)}
            except sqlite3.OperationalError:
                live[kind] = set()
        return live

    def episode_importance(self, spans: Sequence[tuple[int, float, float]]) -> dict[int, int]:
        """The highest journal importance inside each episode's span (a measured signal the journal's model
        wrote); ``{episode id: importance}``, episodes with no journal fact left out."""
        out: dict[int, int] = {}
        for eid, start, end in spans:
            row = self._query("SELECT max(importance) FROM journal WHERE superseded_at IS NULL AND at >= ? AND at < ?",
                              (float(start), float(end)))
            if row and row[0][0] is not None:
                out[int(eid)] = int(row[0][0])
        return out

    def journal_entries(self, ids: Iterable[int]) -> dict[int, JournalEntry]:
        """Journal rows by id, superseded ones included (provenance of old citations)."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        rows = self._query("SELECT * FROM journal WHERE id IN (%s)" % ",".join("?" * len(wanted)), wanted)
        return {r["id"]: self._entry(r) for r in rows}

    def portrait_facts_without_vectors(self, limit: int = 256) -> list[PortraitFact]:
        """Current portrait facts that have no vector yet (new versions, or written before migration 17)."""
        return [self._fact(r) for r in self._query(
            "SELECT f.* FROM portrait_facts f LEFT JOIN portrait_fact_vec v ON v.fact_id = f.id"
            " WHERE f.valid_to IS NULL AND v.fact_id IS NULL ORDER BY f.id LIMIT ?", (int(limit),))]

    def add_portrait_fact_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            for fact_id, vec in items:
                self._put_vec(conn, "portrait_fact_vec", "fact_id", fact_id, vec, model)
            return len(items)
        return self._write(op) if items else 0

    def point_superseded(self, fact_ids: Sequence[int], by: int) -> int:
        """Point ended fact versions at the version that replaced them (a dreaming merge: each merged-away
        fact is superseded by the merged one, so its history leads there)."""
        ids = [int(i) for i in fact_ids]
        if not ids:
            return 0
        return self._write(lambda conn: conn.execute(
            "UPDATE portrait_facts SET superseded_by=? WHERE id IN (%s) AND valid_to IS NOT NULL AND superseded_by IS NULL"
            % ",".join("?" * len(ids)), (int(by), *ids)).rowcount)

    def portrait_fact_vectors(self, ids: Sequence[int]) -> dict[int, np.ndarray]:
        """Stored (decrypted) vectors of these portrait fact versions (current or not)."""
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        rows = self._query("SELECT fact_id, vec_ciphertext FROM portrait_fact_vec WHERE fact_id IN (%s)"
                           % ",".join("?" * len(wanted)), wanted)
        return {r["fact_id"]: np.frombuffer(self.cipher.decrypt_bytes(r["vec_ciphertext"]), dtype=np.float32)
                for r in rows}

    def fact_predecessors(self, ids: Sequence[int]) -> dict[int, int]:
        """``{new version id: the version it superseded}`` for these fact ids (those that replaced one)."""
        wanted = [int(i) for i in ids]
        if not wanted:
            return {}
        rows = self._query("SELECT id, superseded_by FROM portrait_facts WHERE superseded_by IN (%s)"
                           % ",".join("?" * len(wanted)), wanted)
        return {int(r["superseded_by"]): int(r["id"]) for r in rows}

    def set_fact_importance(self, fact_id: int, importance: int) -> bool:
        """Re-rate a current portrait fact in place (the dreaming pass's promote / demote; logged in its diary)."""
        return self._write(lambda conn: conn.execute(
            "UPDATE portrait_facts SET importance=? WHERE id=? AND valid_to IS NULL",
            (min(10, max(1, int(importance))), int(fact_id))).rowcount == 1)

    # -- provenance backfill (yuki.memory.provenance) ----------------------------

    def journal_without_source(self, limit: int = 5000) -> list[JournalEntry]:
        """Current journal rows with no provenance yet (written before migration 17), oldest first."""
        return [self._entry(r) for r in self._query(
            "SELECT * FROM journal WHERE source_ciphertext IS NULL AND superseded_at IS NULL ORDER BY id LIMIT ?",
            (int(limit),))]

    def set_journal_sources(self, items: Sequence[tuple[int, dict[str, Any]]]) -> int:
        """Write provenance onto journal rows that have none (never overwrites one)."""
        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for jid, source in items:
                n += conn.execute(
                    "UPDATE journal SET source_ciphertext=? WHERE id=? AND source_ciphertext IS NULL",
                    (self.cipher.encrypt(json.dumps(source, ensure_ascii=False)), int(jid))).rowcount
            return n
        return self._write(op) if items else 0

    def batch_capture_rows(self, batch_id: int) -> list[PendingCapture]:
        """Every capture a journal batch read that is still stored (30-day TTL), oldest first."""
        rows = self._query(
            "SELECT id, thread_id, at, trigger, delta_ciphertext, chars, journal_attempts, kind, profile, by_yuki,"
            " yuki_request_ciphertext FROM captures WHERE journal_batch_id = ? ORDER BY at, id", (int(batch_id),))
        return [
            PendingCapture(
                id=r["id"], thread_id=r["thread_id"], at=r["at"], trigger=r["trigger"],
                delta=self.cipher.decrypt(r["delta_ciphertext"]), chars=r["chars"],
                attempts=r["journal_attempts"], kind=r["kind"], profile=r["profile"],
                by_yuki=bool(r["by_yuki"]), yuki_request=self._dec(r["yuki_request_ciphertext"]),
            )
            for r in rows
        ]

    # -- standing requests (migration 17) -------------------------------------

    def _request_row(self, r: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": r["id"], "created_at": r["created_at"], "condition": self.cipher.decrypt(r["condition_ciphertext"]),
            "quote": self.cipher.decrypt(r["quote_ciphertext"]),
            "request": self._dec(r["source_request_ciphertext"]), "status": r["status"],
            "max_fires": r["max_fires"], "fires": r["fires"], "cooldown_s": r["cooldown_s"],
            "expires_at": r["expires_at"], "last_fired_at": r["last_fired_at"], "last_nudge_id": r["last_nudge_id"],
            "ended_at": r["ended_at"],
        }

    def add_standing_request(self, condition: str, quote: str, *, request: str | None = None, max_fires: int = 1,
                             cooldown_s: float = 1800.0, expires_at: float | None = None,
                             at: float | None = None) -> int:
        now = time.time() if at is None else float(at)
        return self._write(lambda conn: int(conn.execute(
            "INSERT INTO standing_requests(created_at, condition_ciphertext, quote_ciphertext,"
            " source_request_ciphertext, status, max_fires, fires, cooldown_s, expires_at, updated_at)"
            " VALUES (?,?,?,?,'open',?,0,?,?,?)",
            (now, self.cipher.encrypt(condition), self.cipher.encrypt(quote), self._enc(request),
             max(1, int(max_fires)), float(cooldown_s), expires_at, now)).lastrowid))

    def standing_requests(self, statuses: Sequence[str] | None = ("open",), *, now: float | None = None
                          ) -> list[dict[str, Any]]:
        """Standing requests by status, oldest first; open ones past their expiry are marked expired first."""
        now = time.time() if now is None else float(now)
        expired = self._query("SELECT count(*) FROM standing_requests WHERE status='open'"
                              " AND expires_at IS NOT NULL AND expires_at <= ?", (now,))[0][0]
        if expired:
            self._write(lambda conn: conn.execute(
                "UPDATE standing_requests SET status='expired', ended_at=?, updated_at=? WHERE status='open'"
                " AND expires_at IS NOT NULL AND expires_at <= ?", (now, now, now)))
        sql = "SELECT * FROM standing_requests"
        params: list[Any] = []
        if statuses:
            sql += " WHERE status IN (%s)" % ",".join("?" * len(statuses))
            params.extend(statuses)
        return [self._request_row(r) for r in self._query(sql + " ORDER BY created_at, id", params)]

    def standing_request(self, request_id: int) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM standing_requests WHERE id=?", (int(request_id),))
        return self._request_row(rows[0]) if rows else None

    def end_standing_request(self, request_id: int, status: str = "cancelled", *, at: float | None = None) -> bool:
        now = time.time() if at is None else float(at)
        return self._write(lambda conn: conn.execute(
            "UPDATE standing_requests SET status=?, ended_at=?, updated_at=? WHERE id=? AND status='open'",
            (status, now, now, int(request_id))).rowcount == 1)

    def fire_standing_request(self, request_id: int, checkin: NudgeCheckin, nudge: NewNudge, *,
                              now: float | None = None, daily_budget: int = 6,
                              day_start: float | None = None) -> tuple[int | None, str]:
        """Write the card for one match, in one transaction with the request's count (plumbing gates in code).

        Refused (``(None, why)``) when the request is not open, is still in its cooldown since
        its last card, already fired its own budget, or ``daily_budget`` request cards were
        already written since ``day_start``. The last allowed fire closes it (``fired``).
        """
        now = time.time() if now is None else float(now)
        if nudge.kind not in NUDGE_KINDS:
            raise ValueError(f"unknown nudge kind {nudge.kind!r}")

        def op(conn: sqlite3.Connection) -> tuple[int | None, str]:
            r = conn.execute("SELECT * FROM standing_requests WHERE id=?", (int(request_id),)).fetchone()
            if r is None or r["status"] != "open":
                return None, "not open"
            if r["expires_at"] is not None and r["expires_at"] <= now:
                conn.execute("UPDATE standing_requests SET status='expired', ended_at=?, updated_at=? WHERE id=?",
                             (now, now, r["id"]))
                return None, "expired"
            if r["last_fired_at"] is not None and now - r["last_fired_at"] < r["cooldown_s"]:
                return None, "cooldown"
            if r["fires"] >= r["max_fires"]:
                return None, "budget"
            if day_start is not None:
                today = conn.execute("SELECT count(*) FROM nudges WHERE ref LIKE 'request:%' AND at >= ?",
                                     (float(day_start),)).fetchone()[0]
                if today >= daily_budget:
                    return None, "daily budget"
            _checkin_id, nudge_id = self._record_checkin(conn, checkin, nudge, None)
            fires = r["fires"] + 1
            status = "fired" if fires >= r["max_fires"] else "open"
            conn.execute(
                "UPDATE standing_requests SET fires=?, last_fired_at=?, last_nudge_id=?, status=?, ended_at=?,"
                " updated_at=? WHERE id=?",
                (fires, now, nudge_id, status, now if status != "open" else None, now, r["id"]))
            return nudge_id, "fired"

        return self._write(op)

    # -- Yuki's persona and the changelog (migration 17) ------------------------

    def persona_items(self, *, include_ended: bool = False) -> list[dict[str, Any]]:
        """Yuki's own persona items (current ones, or every version), oldest first."""
        sql = "SELECT * FROM yuki_persona" + ("" if include_ended else " WHERE valid_to IS NULL") + " ORDER BY id"
        out = []
        for r in self._query(sql):
            try:
                evidence = json.loads(self._dec(r["evidence_ciphertext"]) or "{}")
            except ValueError:
                evidence = {}
            out.append({"id": r["id"], "text": self.cipher.decrypt(r["text_ciphertext"]), "evidence": evidence,
                        "valid_from": r["valid_from"], "valid_to": r["valid_to"],
                        "superseded_by": r["superseded_by"], "run_id": r["run_id"]})
        return out

    def commit_persona(self, run_id: int | None, changes: Sequence[dict[str, Any]],
                       diary: Sequence[dict[str, Any]], *, now: float | None = None) -> list[int | None]:
        """Apply validated persona changes (``{op: add|update|remove, id, text, evidence}``) and their diary lines
        in one transaction; returns the new row id per change (``None`` for a remove or a skipped one)."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> list[int | None]:
            out: list[int | None] = []
            for ch in changes:
                new_id = None
                if ch["op"] in ("update", "remove"):
                    live = conn.execute("SELECT id FROM yuki_persona WHERE id=? AND valid_to IS NULL",
                                        (int(ch.get("id") or 0),)).fetchone()
                    if live is None:
                        out.append(None)
                        continue
                if ch["op"] in ("add", "update"):
                    new_id = int(conn.execute(
                        "INSERT INTO yuki_persona(text_ciphertext, evidence_ciphertext, valid_from, run_id, created_at)"
                        " VALUES (?,?,?,?,?)",
                        (self.cipher.encrypt(ch["text"]),
                         self.cipher.encrypt(json.dumps(ch.get("evidence") or {}, ensure_ascii=False)),
                         now, run_id, now)).lastrowid)
                if ch["op"] in ("update", "remove"):
                    conn.execute("UPDATE yuki_persona SET valid_to=?, superseded_by=? WHERE id=? AND valid_to IS NULL",
                                 (now, new_id, int(ch["id"])))
                out.append(new_id)
            for line in diary:
                self._insert_diary(conn, run_id, now, line)
            return out

        return self._write(op)

    def _insert_diary(self, conn: sqlite3.Connection, run_id: int | None, now: float, line: dict[str, Any]) -> int:
        return int(conn.execute(
            "INSERT INTO memory_changelog(at, run_id, pass, action, fact_ids, new_fact_ids, cites, text_ciphertext)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now, run_id, line["pass"], line["action"], json.dumps(line.get("fact_ids") or []),
             json.dumps(line.get("new_fact_ids") or []), json.dumps(line.get("cites") or []),
             self.cipher.encrypt(line["text"]))).lastrowid)

    def add_diary(self, run_id: int | None, lines: Sequence[dict[str, Any]], *, now: float | None = None) -> int:
        """Append diary lines (``{pass, action, text, fact_ids, new_fact_ids, cites}``) to the memory changelog."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> int:
            for line in lines:
                self._insert_diary(conn, run_id, now, line)
            return len(lines)

        return self._write(op) if lines else 0

    def changelog(self, *, since: TimeArg = None, pass_: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        """The memory diary, newest first: what dreaming and the persona pass changed, one line each."""
        sql, params = "SELECT * FROM memory_changelog WHERE 1=1", []
        if (s := _ts(since)) is not None:
            sql += " AND at >= ?"
            params.append(s)
        if pass_:
            sql += " AND pass = ?"
            params.append(pass_)
        rows = self._query(sql + " ORDER BY at DESC, id DESC LIMIT ?", [*params, int(limit)])
        return [{"id": r["id"], "at": r["at"], "run_id": r["run_id"], "pass": r["pass"], "action": r["action"],
                 "fact_ids": json.loads(r["fact_ids"] or "[]"), "new_fact_ids": json.loads(r["new_fact_ids"] or "[]"),
                 "cites": json.loads(r["cites"] or "[]"), "text": self.cipher.decrypt(r["text_ciphertext"])}
                for r in rows]

    # -- corrections applied everywhere, name aliases (migration 18; yuki.memory.corrections) -------------

    def queue_correction_pass(self, correction_id: int, *, at: float | None = None) -> None:
        """Queue the correction-apply pass for one user correction (idempotent: a queued, running or finished
        pass is left alone; a failed one is queued again)."""
        now = float(at) if at is not None else time.time()
        self._write(lambda conn: conn.execute(
            "INSERT INTO correction_passes(correction_id, status, queued_at, updated_at) VALUES (?,?,?,?)"
            " ON CONFLICT(correction_id) DO UPDATE SET status='queued', updated_at=excluded.updated_at"
            " WHERE correction_passes.status='failed'",
            (int(correction_id), "queued", now, now)))

    def correction_pass(self, correction_id: int) -> dict[str, Any] | None:
        """The pass of one correction: ``{correction_id, status, queued_at, started_at, finished_at, attempts,
        changes: [...], cost_usd, error}``, or None when none was queued."""
        rows = self._query("SELECT * FROM correction_passes WHERE correction_id=?", (int(correction_id),))
        if not rows:
            return None
        r = rows[0]
        try:
            changes = json.loads(self._dec(r["changes_ciphertext"]) or "[]")
        except ValueError:
            changes = []
        return {"correction_id": r["correction_id"], "status": r["status"], "queued_at": r["queued_at"],
                "started_at": r["started_at"], "finished_at": r["finished_at"], "attempts": r["attempts"],
                "changes": changes if isinstance(changes, list) else [], "cost_usd": r["cost_usd"] or 0.0,
                "error": r["error"]}

    def corrections_to_apply(self, *, max_attempts: int = 3, since: TimeArg = None,
                             retry_after_s: float = 600.0) -> list[int]:
        """Correction ids whose pass is queued, oldest first; also one cut short (``applying`` for longer than
        ``retry_after_s``: its run died) or failed under ``max_attempts`` at least ``retry_after_s`` ago.
        ``since``: also user corrections written at or after it that were never queued (a correction written
        by a process running older code)."""
        stale = time.time() - float(retry_after_s)
        ids = [r["correction_id"] for r in self._query(
            "SELECT correction_id FROM correction_passes WHERE status='queued'"
            " OR (status='applying' AND started_at < ?) OR (status='failed' AND attempts < ? AND updated_at < ?)"
            " ORDER BY queued_at", (stale, int(max_attempts), stale))]
        if (s := _ts(since)) is not None:
            ids += [r["id"] for r in self._query(
                "SELECT f.id FROM portrait_facts f LEFT JOIN correction_passes c ON c.correction_id=f.id"
                " WHERE f.kind=? AND f.origin='user' AND f.created_at >= ? AND c.correction_id IS NULL ORDER BY f.id",
                (CORRECTION_KIND, s))]
        return list(dict.fromkeys(ids))

    def start_correction_pass(self, correction_id: int, *, at: float | None = None) -> None:
        now = float(at) if at is not None else time.time()
        self._write(lambda conn: conn.execute(
            "INSERT INTO correction_passes(correction_id, status, queued_at, started_at, attempts, updated_at)"
            " VALUES (?,'applying',?,?,1,?) ON CONFLICT(correction_id) DO UPDATE SET status='applying',"
            " started_at=excluded.started_at, attempts=correction_passes.attempts+1, updated_at=excluded.updated_at",
            (int(correction_id), now, now, now)))

    def finish_correction_pass(self, correction_id: int, status: str, *, changes: Sequence[dict[str, Any]] = (),
                               cost_usd: float = 0.0, error: str | None = None, at: float | None = None) -> None:
        """Record how a pass ended (``done`` | ``failed``), what it changed and what it cost."""
        now = float(at) if at is not None else time.time()
        self._write(lambda conn: conn.execute(
            "UPDATE correction_passes SET status=?, finished_at=?, changes_ciphertext=?, cost_usd=cost_usd+?,"
            " error=?, updated_at=? WHERE correction_id=?",
            (status, now, self.cipher.encrypt(json.dumps(list(changes), ensure_ascii=False, default=str)),
             float(cost_usd or 0.0), error, now, int(correction_id))))

    def alias_key(self, heard: str) -> str:
        return self.cipher.digest("alias", " ".join((heard or "").casefold().split()))

    def add_alias(self, heard: str, name: str, *, why: str | None = None, correction_id: int | None = None,
                  at: float | None = None) -> int | None:
        """Record that ``heard`` (a misheard or wrong form) is really ``name``; an earlier alias of the same heard
        form ends (the newest correction wins). None when the two are the same or one is empty."""
        heard, name = " ".join((heard or "").split()), " ".join((name or "").split())
        if not heard or not name or heard.casefold() == name.casefold():
            return None
        now = float(at) if at is not None else time.time()
        key = self.alias_key(heard)

        def op(conn: sqlite3.Connection) -> int:
            conn.execute("UPDATE name_aliases SET ended_at=? WHERE heard_key=? AND ended_at IS NULL", (now, key))
            return int(conn.execute(
                "INSERT INTO name_aliases(heard_key, heard_ciphertext, name_ciphertext, why_ciphertext, correction_id,"
                " created_at) VALUES (?,?,?,?,?,?)",
                (key, self.cipher.encrypt(heard), self.cipher.encrypt(name), self._enc(why) if why else None,
                 correction_id, now)).lastrowid)

        return self._write(op)

    def end_aliases(self, alias_ids: Sequence[int], *, at: float | None = None) -> int:
        """End live aliases by id (an Undo of the answer that taught them); returns how many ended."""
        ids = [int(i) for i in alias_ids if i is not None]
        if not ids:
            return 0
        now = float(at) if at is not None else time.time()
        marks = ",".join("?" * len(ids))
        return self._write(lambda conn: conn.execute(
            f"UPDATE name_aliases SET ended_at=? WHERE ended_at IS NULL AND id IN ({marks})", (now, *ids)).rowcount)

    def reopen_aliases(self, alias_ids: Sequence[int]) -> int:
        """Bring back ended aliases by id (an Undo of the alias that replaced them); one whose heard form has a live
        alias again stays ended. Returns how many came back."""
        ids = [int(i) for i in alias_ids if i is not None]

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for i in ids:
                n += conn.execute(
                    "UPDATE name_aliases SET ended_at=NULL WHERE id=? AND ended_at IS NOT NULL AND NOT EXISTS"
                    " (SELECT 1 FROM name_aliases b WHERE b.heard_key=name_aliases.heard_key AND b.ended_at IS NULL)",
                    (i,)).rowcount
            return n

        return self._write(op) if ids else 0

    def aliases(self, *, include_ended: bool = False, name_forms: bool = False) -> list[dict[str, Any]]:
        """Name aliases, oldest first: ``{id, heard, name, why, correction_id, at, ended_at}``. The heard forms of
        Yuki's own name (:func:`is_name_form`: the ``why`` mark, or Yuki's name as the alias's name) only with
        ``name_forms`` (the wake word's and the fast path's readers)."""
        sql = "SELECT * FROM name_aliases" + ("" if include_ended else " WHERE ended_at IS NULL") + " ORDER BY id"
        rows = [{"id": r["id"], "heard": self.cipher.decrypt(r["heard_ciphertext"]),
                 "name": self.cipher.decrypt(r["name_ciphertext"]), "why": self._dec(r["why_ciphertext"]),
                 "correction_id": r["correction_id"], "at": r["created_at"], "ended_at": r["ended_at"]}
                for r in self._query(sql)]
        return rows if name_forms else [r for r in rows if not is_name_form(r)]

    def supersede_journal_facts(self, call: ModelCall, items: Sequence[tuple[int, str | None, np.ndarray | None]],
                                *, embed_model: str | None = None) -> list[tuple[int, int | None]]:
        """Corrected versions of journal facts: each ``(journal id, new wording, vector)`` becomes a new row with
        the old one's time, thread, app, importance, nature, provenance and taint, and the old row is
        superseded by this pass's batch (kept; every journal reader leaves it out). One transaction with the
        batch row. Returns ``(old id, new id | None when the old one was already superseded)``."""

        def op(conn: sqlite3.Connection) -> list[tuple[int, int | None]]:
            batch_id = self._insert_batch(conn, call, 0, len(items))
            out: list[tuple[int, int | None]] = []
            now = time.time()
            for old_id, text, vec in items:
                row = conn.execute("SELECT * FROM journal WHERE id=? AND superseded_at IS NULL", (int(old_id),)).fetchone()
                if row is None:
                    out.append((int(old_id), None))
                    continue
                if text is None:   # retired: wrong, with no corrected version (kept, superseded by this batch)
                    conn.execute("UPDATE journal SET superseded_at=?, superseded_by=? WHERE id=?",
                                 (now, batch_id, int(old_id)))
                    out.append((int(old_id), None))
                    continue
                cur = conn.execute(
                    "INSERT INTO journal(at, thread_id, app, host, fact_ciphertext, importance, batch_id, created_at,"
                    " by_yuki, nature, milestone, source_ciphertext, tainted) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (row["at"], row["thread_id"], row["app"], row["host"], self.cipher.encrypt(text), row["importance"],
                     batch_id, now, row["by_yuki"], row["nature"], row["milestone"], row["source_ciphertext"],
                     row["tainted"]))
                new_id = int(cur.lastrowid)
                if vec is not None:
                    self._put_vec(conn, "journal_vec", "journal_id", new_id, vec, embed_model)
                conn.execute("UPDATE journal SET superseded_at=?, superseded_by=? WHERE id=?", (now, batch_id, int(old_id)))
                out.append((int(old_id), new_id))
            return out

        try:
            return self._write(op)
        finally:
            self._forget_withdrawn()

    def supersede_episode(self, episode_id: int, text: str, *, vector: np.ndarray | None = None,
                          embed_model: str | None = None, title: str | None = None,
                          summary: str | None = None) -> int | None:
        """A corrected version of one current episode (same span, window, aggregates and run); the old one
        expires, superseded by it. ``title`` / ``summary`` (migration 35): the corrected session title and summary;
        each left out keeps the old one. Returns the new id, or None when it was no longer current."""

        def op(conn: sqlite3.Connection) -> int | None:
            row = conn.execute("SELECT * FROM episodes WHERE id=? AND expired_at IS NULL", (int(episode_id),)).fetchone()
            if row is None:
                return None
            now = time.time()
            new_id = int(conn.execute(
                "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext,"
                " aggregates_ciphertext, run_id, created_at, title_ciphertext, summary_ciphertext)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (row["started_at"], row["ended_at"], row["window_start"], row["window_end"], row["final"],
                 self.cipher.encrypt(text), row["aggregates_ciphertext"], row["run_id"], now,
                 self._enc(title) if title else row["title_ciphertext"],
                 self._enc(summary) if summary else row["summary_ciphertext"])).lastrowid)
            if vector is not None:
                self._put_vec(conn, "episode_vec", "episode_id", new_id, vector, embed_model)
            conn.execute("UPDATE episodes SET expired_at=?, superseded_by=? WHERE id=?", (now, new_id, int(episode_id)))
            return new_id

        return self._write(op)

    def set_episode_labels(self, rows: dict[int, tuple[str | None, str | None]], *, overwrite: bool = False) -> int:
        """Write sessions' titles and summaries (migration 35), ``{episode id: (title, summary)}``, current episodes
        only; without ``overwrite`` an episode that has a title already is left. Returns how many were written."""

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for eid, (title, summary) in rows.items():
                title, summary = " ".join(str(title or "").split()), " ".join(str(summary or "").split())
                if not title or not summary:
                    continue
                guard = "" if overwrite else " AND title_ciphertext IS NULL"
                n += conn.execute(f"UPDATE episodes SET title_ciphertext=?, summary_ciphertext=? WHERE id=? AND"
                                  f" expired_at IS NULL{guard}",
                                  (self.cipher.encrypt(title), self.cipher.encrypt(summary), int(eid))).rowcount
            return n

        return int(self._write(op)) if rows else 0

    def episodes_without_labels(self, since: TimeArg = None) -> list[EpisodeRecord]:
        """Current episodes without a session title (migration 35), oldest first."""
        where, params = self._episode_filter(since, None)
        where += (" AND " if where else " WHERE ") + "e.title_ciphertext IS NULL"
        return [self._episode(r) for r in self._query("SELECT e.* FROM episodes e" + where
                                                      + " ORDER BY e.started_at, e.id", params)]

    def retire_episodes(self, pairs: Sequence[tuple[int, int]]) -> int:
        """Expire episodes that retell a stretch another one tells (``(retold id, kept id)``): kept as history,
        pointing at the one kept (:func:`yuki.memory.episodes.repair_overlaps`). Returns how many."""
        now = time.time()

        def op(conn: sqlite3.Connection) -> int:
            return sum(conn.execute("UPDATE episodes SET expired_at=?, superseded_by=? WHERE id=? AND expired_at IS NULL",
                                    (now, int(kept), int(retold))).rowcount for retold, kept in pairs)

        return self._write(op) if pairs else 0

    def supersede_question(self, question_id: int, subject: str, question: str, evidence: str | None, *,
                           vector: np.ndarray | None = None, embed_model: str | None = None) -> int | None:
        """A corrected version of an open or asked question (everything else kept: status, card, sources); the
        old one is dropped. Returns the new id, or None when it was not open or asked."""

        def op(conn: sqlite3.Connection) -> int | None:
            row = conn.execute("SELECT * FROM questions WHERE id=? AND status IN ('open','asked')",
                               (int(question_id),)).fetchone()
            if row is None:
                return None
            now = time.time()
            if not question:   # retired: the question itself rests on something wrong
                conn.execute("UPDATE questions SET status='dropped', updated_at=? WHERE id=?", (now, int(question_id)))
                return None
            new_id = int(conn.execute(
                "INSERT INTO questions(at, created_at, kind, subject_ciphertext, subject_key, question_ciphertext,"
                " evidence_ciphertext, reason_ciphertext, importance, origin, source_ids, fact_ids, status, seen,"
                " nudge_id, asked_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (row["at"], now, row["kind"], self.cipher.encrypt(subject), question_subject_key(self.cipher, subject),
                 self.cipher.encrypt(question), self._enc(evidence) if evidence else row["evidence_ciphertext"],
                 row["reason_ciphertext"], row["importance"], row["origin"], row["source_ids"], row["fact_ids"],
                 row["status"], row["seen"], row["nudge_id"], row["asked_at"], now)).lastrowid)
            if vector is not None:
                self._put_vec(conn, "question_vec", "question_id", new_id, vector, embed_model)
            conn.execute("UPDATE questions SET status='dropped', updated_at=? WHERE id=?", (now, int(question_id)))
            return new_id

        return self._write(op)

    def set_commitment_owners(self, owners: Sequence[tuple[int, str]]) -> int:
        """Record whose each commitment is (``(id, "yuki" | "user")``), on rows that have no owner yet."""
        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for fid, owner in owners:
                if owner in COMMITMENT_OWNERS:
                    n += conn.execute("UPDATE conversation_facts SET owner=? WHERE id=? AND kind='commitment'"
                                      " AND owner IS NULL", (owner, int(fid))).rowcount
            return n

        return self._write(op) if owners else 0

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
            # run traces (migration 29) share the captures' TTL: a 30-day buffer for the learners, not an archive
            # (what they learned stays in the playbooks and their ledger)
            conn.execute("DELETE FROM runs WHERE at < ?", (cutoff,))
            # what each reply and card was written from (migration 31): a measure for the nightly judge, not an archive
            conn.execute("DELETE FROM surfaced WHERE at < ?", (cutoff,))
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
            "by_app": [{**dict(r), "app": _shown(r["app"])} for r in by_app],
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

    # ==================================================================================================
    # Threads of work, links seen, the freshest screens (migration 23; yuki.memory.workthreads, links,
    # related, screenrecall). Plumbing only: what a thread is, and what a batch did to one, is the
    # journal model's judgement; nothing here reads words.
    # ==================================================================================================

    #: Most entities a thread keeps (apps, sites, repos, files, people), newest first.
    WORK_ENTITIES_MAX = 12
    #: Most open items a thread keeps.
    WORK_OPEN_MAX = 5

    def _json_list(self, stored: str | None) -> list[str]:
        if not stored:
            return []
        try:
            value = json.loads(self.cipher.decrypt(stored))
        except Exception:
            return []
        return [str(v) for v in value if str(v).strip()] if isinstance(value, list) else []

    def _work_thread(self, r: sqlite3.Row, score: float | None = None) -> WorkThread:
        return WorkThread(
            id=int(r["id"]), title=self._dec(r["title_ciphertext"]) or "", kind=r["kind"], status=r["status"],
            state=self._dec(r["state_ciphertext"]) or "", open_items=self._json_list(r["open_ciphertext"]),
            entities=self._json_list(r["entities_ciphertext"]), first_seen=float(r["first_seen"]),
            last_seen=float(r["last_seen"]), importance=r["importance"], origin=r["origin"],
            merged_into=r["merged_into"], events=int(r["events"] or 0), created_at=float(r["created_at"]),
            updated_at=float(r["updated_at"]), score=score,
            shown=self._dec(r["shown_ciphertext"]) if "shown_ciphertext" in r.keys() else None,
            next=self._dec(r["next_ciphertext"]) if "next_ciphertext" in r.keys() else None,
            closed_by=r["closed_by"] if "closed_by" in r.keys() else None,
            closed_at=r["closed_at"] if "closed_at" in r.keys() else None)

    def work_threads(self, statuses: Sequence[str] | None = ("active", "paused"), *, since: TimeArg = None,
                     limit: int | None = None, ids: Iterable[int] | None = None) -> list[WorkThread]:
        """Threads of work, most recently touched first. ``statuses`` None: every status (merged ones too)."""
        clauses, params = [], []
        if statuses is not None:
            clauses.append("status IN (%s)" % ",".join("?" * len(statuses)))
            params.extend(statuses)
        if (s := _ts(since)) is not None:
            clauses.append("last_seen >= ?")
            params.append(s)
        if ids is not None:
            wanted = sorted({int(i) for i in ids})
            if not wanted:
                return []
            clauses.append("id IN (%s)" % ",".join("?" * len(wanted)))
            params.extend(wanted)
        sql = "SELECT * FROM work_threads" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        sql += " ORDER BY last_seen DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))
        try:
            return [self._work_thread(r) for r in self._query(sql, params)]
        except sqlite3.OperationalError:   # an older schema: no threads
            return []

    def work_thread(self, thread_id: int) -> WorkThread | None:
        rows = self.work_threads(None, ids=[int(thread_id)])
        return rows[0] if rows else None

    def resolve_work_thread(self, thread_id: int | None) -> int | None:
        """The thread a (possibly merged) thread lives on in: follows ``merged_into`` (at most 16 hops)."""
        if thread_id is None:
            return None
        current = int(thread_id)
        for _ in range(16):
            rows = self._query("SELECT id, merged_into FROM work_threads WHERE id=?", (current,))
            if not rows:
                return None
            if rows[0]["merged_into"] is None:
                return current
            current = int(rows[0]["merged_into"])
        return current

    def work_thread_vectors(self, ids: Iterable[int] | None = None) -> dict[int, np.ndarray]:
        """Stored (decrypted) vectors of threads (all non-merged ones when ``ids`` is None)."""
        if ids is None:
            sql, params = ("SELECT v.work_thread_id, v.vec_ciphertext FROM work_thread_vec v JOIN work_threads w"
                           " ON w.id = v.work_thread_id WHERE w.status NOT IN ('merged', 'dismissed')"), []
        else:
            wanted = sorted({int(i) for i in ids})
            if not wanted:
                return {}
            sql = "SELECT work_thread_id, vec_ciphertext FROM work_thread_vec WHERE work_thread_id IN (%s)" % (
                ",".join("?" * len(wanted)))
            params = wanted
        try:
            rows = self._query(sql, params)
        except sqlite3.OperationalError:
            return {}
        out: dict[int, np.ndarray] = {}
        for r in rows:
            try:
                out[int(r[0])] = np.frombuffer(self.cipher.decrypt_bytes(r[1]), dtype=np.float32)
            except Exception:
                continue
        return out

    def work_threads_of_capture_threads(self, capture_thread_ids: Iterable[int], *, limit: int = 3,
                                        since: TimeArg = None) -> list[int]:
        """The threads of work these windows, pages, conversations or calls were last part of, newest first
        (merged ones resolved to the thread they went into)."""
        wanted = sorted({int(i) for i in capture_thread_ids if i is not None})
        if not wanted:
            return []
        params: list[Any] = list(wanted)
        extra = ""
        if (s := _ts(since)) is not None:
            extra = " AND at >= ?"
            params.append(s)
        try:
            rows = self._query("SELECT work_thread_id, max(at) a FROM work_thread_events WHERE thread_id IN (%s)%s"
                               " GROUP BY work_thread_id ORDER BY a DESC LIMIT ?" % (",".join("?" * len(wanted)), extra),
                               (*params, int(limit) * 3))
        except sqlite3.OperationalError:
            return []
        out: list[int] = []
        for r in rows:
            tid = self.resolve_work_thread(int(r[0]))
            if tid is not None and tid not in out:
                out.append(tid)
        return out[: int(limit)]

    def work_threads_touched(self, since: TimeArg, until: TimeArg = None, *, limit: int = 8
                             ) -> list[tuple[int, int, float]]:
        """``(thread id, events, last event at)`` of the threads of work touched in ``[since, until)``, most events
        first (merged threads resolved to the thread they went into): content-free, for the ``activity`` header."""
        sql, params = "SELECT work_thread_id, count(*) AS n, max(at) AS a FROM work_thread_events WHERE at >= ?", \
            [_ts(since) or 0.0]
        if (u := _ts(until)) is not None:
            sql += " AND at < ?"
            params.append(u)
        try:
            rows = self._query(sql + " GROUP BY work_thread_id ORDER BY n DESC, a DESC LIMIT ?",
                               (*params, int(limit) * 2))
        except sqlite3.OperationalError:
            return []
        out: dict[int, list[float]] = {}
        for r in rows:
            tid = self.resolve_work_thread(int(r[0]))
            if tid is None:
                continue
            got = out.setdefault(tid, [0, 0.0])
            got[0] += int(r[1])
            got[1] = max(got[1], float(r[2]))
        ranked = sorted(out.items(), key=lambda kv: (-kv[1][0], -kv[1][1]))[: int(limit)]
        return [(tid, int(n), float(a)) for tid, (n, a) in ranked]

    def work_thread_events(self, work_thread_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """A thread's events, oldest first: ``{"id", "at", "action", "batch_id", "thread_id", "app", "host",
        "state", "origin"}`` (the events of threads merged into it were moved onto it at the merge)."""
        try:
            rows = self._query("SELECT * FROM work_thread_events WHERE work_thread_id=? ORDER BY at DESC, id DESC LIMIT ?",
                               (int(work_thread_id), int(limit)))
        except sqlite3.OperationalError:
            return []
        return [{"id": r["id"], "at": r["at"], "action": r["action"], "batch_id": r["batch_id"],
                 "thread_id": r["thread_id"], "app": _shown(r["app"]) if r["app"] else r["app"], "host": r["host"],
                 "state": self._dec(r["state_ciphertext"]), "origin": r["origin"]} for r in reversed(rows)]

    def work_event_spans(self, work_thread_ids: Iterable[int]) -> dict[int, list[tuple[float, int | None]]]:
        """``{thread: [(at, capture thread), ...]}`` oldest first, content-free (the thread-affinity plumbing)."""
        wanted = sorted({int(i) for i in work_thread_ids if i is not None})
        if not wanted:
            return {}
        try:
            rows = self._query("SELECT work_thread_id, at, thread_id FROM work_thread_events WHERE work_thread_id IN (%s)"
                               " ORDER BY at" % ",".join("?" * len(wanted)), wanted)
        except sqlite3.OperationalError:
            return {}
        out: dict[int, list[tuple[float, int | None]]] = {}
        for r in rows:
            out.setdefault(int(r[0]), []).append((float(r[1]), r[2]))
        return out

    #: The events that are the user's own touch of a thread (not the nightly tidy's merges, closes and renames,
    #: nor a thread only glimpsed beside the window in front or in a popup).
    WORK_TOUCHES: tuple[str, ...] = ("starts", "continues", "pauses", "ends", "reopens")

    def latest_work_events(self, *, since: TimeArg = None, until: TimeArg = None,
                           limit: int = 20) -> list[dict[str, Any]]:
        """The newest events in ``[since, until)`` where the user touched a thread (:data:`WORK_TOUCHES`),
        newest first, content-free: ``{"work_thread_id", "at", "action", "thread_id", "app", "host"}``."""
        clauses = ["action IN (%s)" % ",".join("?" * len(self.WORK_TOUCHES))]
        params: list[Any] = list(self.WORK_TOUCHES)
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("at < ?")
            params.append(u)
        try:
            rows = self._query("SELECT work_thread_id, at, action, thread_id, app, host FROM work_thread_events WHERE "
                               + " AND ".join(clauses) + " ORDER BY at DESC, id DESC LIMIT ?", (*params, int(limit)))
        except sqlite3.OperationalError:
            return []
        return [dict(r) for r in rows]

    def journal_for_work_thread(self, work_thread_id: int, *, limit: int = 40) -> list[JournalEntry]:
        """The newest ``limit`` current journal facts of a thread, oldest first."""
        try:
            rows = self._query("SELECT j.* FROM journal j WHERE j.work_thread_id=? AND j.superseded_at IS NULL"
                               " ORDER BY j.at DESC, j.id DESC LIMIT ?", (int(work_thread_id), int(limit)))
        except sqlite3.OperationalError:
            return []
        return [self._entry(r) for r in reversed(rows)]

    def journal_ids_of_work_threads(self, work_thread_ids: Iterable[int]) -> set[int]:
        """Ids of the current journal facts of these threads (the related ranking's thread affinity)."""
        wanted = sorted({int(i) for i in work_thread_ids if i is not None})
        if not wanted:
            return set()
        try:
            return {int(r[0]) for r in self._query(
                "SELECT id FROM journal WHERE superseded_at IS NULL AND work_thread_id IN (%s)"
                % ",".join("?" * len(wanted)), wanted)}
        except sqlite3.OperationalError:
            return set()

    def work_affinity(self, work_thread_ids: Iterable[int], *, pad_s: float = 600.0,
                      call_pad_s: float = 3600.0) -> dict[str, set[int]]:
        """Ids of what is linked to these threads (the related ranking's thread affinity), id-only queries with
        nothing decrypted: ``{"thread", "journal", "episode", "call", "link"}`` - their current facts and links,
        the episodes within ``pad_s`` of one of their events, the calls whose capture thread one of their events
        came from (within ``call_pad_s``)."""
        wanted = sorted({int(i) for i in work_thread_ids if i is not None})
        out: dict[str, set[int]] = {"thread": set(wanted)}
        if not wanted:
            return out
        marks = ",".join("?" * len(wanted))
        out["journal"] = self.journal_ids_of_work_threads(wanted)
        try:
            out["link"] = {int(r[0]) for r in self._query(
                f"SELECT id FROM links_seen WHERE work_thread_id IN ({marks})", wanted)}
        except sqlite3.OperationalError:
            out["link"] = set()
        spans = self.work_event_spans(wanted)
        times = sorted(a for evs in spans.values() for a, _t in evs)
        caps = {t for evs in spans.values() for _a, t in evs if t is not None}
        out["episode"], out["call"] = set(), set()
        if not times:
            return out
        rows = self._query("SELECT id, started_at, ended_at FROM episodes WHERE expired_at IS NULL AND ended_at >= ?"
                           " AND started_at <= ?", (times[0] - pad_s, times[-1] + pad_s))
        for r in rows:
            start, end = float(r[1]) - pad_s, float(r[2]) + pad_s
            i = bisect.bisect_left(times, start)
            if i < len(times) and times[i] <= end:
                out["episode"].add(int(r[0]))
        if caps:
            cmarks = ",".join("?" * len(caps))
            out["call"] = {int(r[0]) for r in self._query(
                f"SELECT id FROM calls WHERE thread_id IN ({cmarks}) AND detected_at < ?"
                " AND (ended_at IS NULL OR ended_at > ?)", (*sorted(caps), times[-1] + call_pad_s,
                                                           times[0] - call_pad_s))}
        return out

    def _put_work_vec(self, conn: sqlite3.Connection, thread_id: int, vec: np.ndarray | None, model: str | None) -> None:
        if vec is None:
            return
        v = np.asarray(vec, dtype=np.float32).ravel()
        conn.execute("INSERT OR REPLACE INTO work_thread_vec(work_thread_id, model, dim, vec_ciphertext) VALUES (?,?,?,?)",
                     (int(thread_id), model or "", int(v.shape[0]), self.cipher.encrypt_bytes(v.tobytes())))

    def _merge_list(self, old: list[str], new: Sequence[str], cap: int) -> list[str]:
        """``new`` first, then the ``old`` items not in it (case-insensitive identity: plumbing on strings)."""
        out: list[str] = []
        seen: set[str] = set()
        for item in [*new, *old]:
            text = " ".join(str(item or "").split())
            key = text.casefold()
            if text and key not in seen:
                seen.add(key)
                out.append(text)
        return out[:cap]

    def _apply_work(self, conn: sqlite3.Connection, ops: Sequence[WorkOp], *, batch_id: int | None,
                    journal_ids: Sequence[int] = ()) -> None:
        """Write what a batch did to threads of work, inside the caller's transaction; sets each op's
        ``result_id`` and ``outcome``. A thread named that is gone (merged away) is followed to where it went."""
        now = time.time()
        floor = self.memory_started_at()
        for op in ops:
            seen = float(op.seen_at) if op.seen_at else None
            # never after it was seen, never in the future; a date before memory existed is the content's own (a
            # file listed with its 2024 date), so the thread is dated when memory saw it (revamp A9)
            at = min(float(op.at or now), (seen if seen is not None else now) + 60.0, now + 60.0)
            first = at if floor is None or at >= floor - 86400.0 else (seen if seen is not None else now)
            content_at = at   # the content's own date (an old one never moves where the thread stands)
            at = max(at, first)   # its event and its touch are dated when memory saw it, never in 2024
            state = " ".join((op.state or "").split())
            if op.action == "starts":
                cur = conn.execute(
                    "INSERT INTO work_threads(title_ciphertext, kind, status, state_ciphertext, open_ciphertext,"
                    " entities_ciphertext, first_seen, last_seen, importance, origin, merged_into, events, created_at,"
                    " updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,1,?,?)",
                    (self.cipher.encrypt(" ".join(op.title.split())), op.kind, "active", self._enc(state or None),
                     self.cipher.encrypt(json.dumps(self._merge_list([], op.open_items, self.WORK_OPEN_MAX))),
                     self.cipher.encrypt(json.dumps(self._merge_list([], op.entities, self.WORK_ENTITIES_MAX))),
                     first, max(at, first), op.importance, op.origin, now, now))
                tid, event, op.outcome = int(cur.lastrowid), "starts", "started"
            else:
                tid = None
                current = op.work_thread_id
                for _ in range(16):   # a thread merged away since the batch was offered it: follow it
                    row = conn.execute("SELECT * FROM work_threads WHERE id=?", (current,)).fetchone()
                    if row is None:
                        break
                    if row["merged_into"] is None:
                        tid = int(row["id"])
                        break
                    current = int(row["merged_into"])
                if tid is None:
                    op.outcome = "rejected:no such thread"
                    continue
                if row["status"] == "dismissed":   # the nightly tidy judged it no piece of work
                    op.outcome = "rejected:dismissed thread"
                    continue
                status = {"continues": "active", "pauses": "paused", "ends": "done"}.get(op.action)
                if status is None:
                    op.outcome = f"rejected:unknown action {op.action}"
                    continue
                entities = self._merge_list(self._json_list(row["entities_ciphertext"]), op.entities,
                                            self.WORK_ENTITIES_MAX)
                open_items = (self._merge_list([], op.open_items, self.WORK_OPEN_MAX) if (op.open_items or state)
                              else self._json_list(row["open_ciphertext"]))
                rated = [x for x in (row["importance"], op.importance) if x is not None]
                older = content_at < float(row["last_seen"]) - 60.0
                if older:
                    # an older source (a history message read now, an old page): the thread's newer state and
                    # status stand; the event still records that the thread came up there
                    state, open_items, status = "", self._json_list(row["open_ciphertext"]), row["status"]
                glimpsed = op.attention in ("visible", "popup")
                touched = at
                event = op.action
                if glimpsed:
                    # only beside the window in front, or a popup (migration 24): what it said may move where the
                    # thread stands, but it is no touch of the user's - status and last seen stay
                    status, touched, event = row["status"], float(row["last_seen"]), "seen"
                if op.action == "continues" and row["status"] in ("paused", "done") and not older and not glimpsed:
                    event = "reopens"
                # a new state makes the shown one and its next step stale (migrations 33, 35): cleared, the titles
                # pass writes them again; a thread back at work is no longer closed by anyone
                conn.execute(
                    "UPDATE work_threads SET status=?, state_ciphertext=coalesce(?, state_ciphertext), open_ciphertext=?,"
                    " entities_ciphertext=?, first_seen=min(first_seen, ?), last_seen=max(last_seen, ?),"
                    " importance=?, events=events+1, updated_at=?,"
                    " shown_ciphertext=CASE WHEN ? THEN NULL ELSE shown_ciphertext END,"
                    " next_ciphertext=CASE WHEN ? THEN NULL ELSE next_ciphertext END,"
                    " closed_by=CASE WHEN ?='active' THEN NULL ELSE closed_by END,"
                    " closed_at=CASE WHEN ?='active' THEN NULL ELSE closed_at END WHERE id=?",
                    (status, self._enc(state or None), self.cipher.encrypt(json.dumps(open_items)),
                     self.cipher.encrypt(json.dumps(entities)), min(first, touched), touched,
                     max(rated) if rated else None, now, int(bool(state)), int(bool(state)), status, status, tid))
                op.outcome = {"continues": "continued", "pauses": "paused", "ends": "ended"}[op.action]
                if event == "reopens":
                    op.outcome = "reopened"
                elif glimpsed:
                    op.outcome = "seen"
            self._put_work_vec(conn, tid, op.vector, op.embed_model)
            conn.execute(
                "INSERT INTO work_thread_events(work_thread_id, at, action, batch_id, thread_id, app, host,"
                " state_ciphertext, origin, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (tid, at, event, batch_id, op.capture_thread_id, op.app, op.host, self._enc(state or None),
                 op.origin, now))
            linked = [journal_ids[i] for i in op.fact_indexes if 0 <= i < len(journal_ids)] + list(op.journal_ids)
            if linked:
                conn.executemany("UPDATE journal SET work_thread_id=? WHERE id=?", [(tid, int(j)) for j in linked])
            op.result_id = tid

    def memory_started_at(self) -> float | None:
        """When memory began capturing, or ``None`` before anything was captured: nothing it holds can have been seen
        earlier. Taken from capture-time stamps that are never pruned (review 34 H8: captures go at 30 days, so their
        oldest is "about a month ago", not when memory began): the first capture thread's ``first_seen``, the first
        journal batch, and the oldest capture still kept. Never ``journal.at``, which is the content's own date (a
        2021 message read back). Cached once known."""
        cached = getattr(self, "_memory_started", None)
        if cached is not None:
            return cached
        found: list[float] = []
        for sql in ("SELECT min(first_seen) FROM threads", "SELECT min(at) FROM journal_batches",
                    "SELECT min(at) FROM captures"):
            try:
                row = self._query(sql)[0][0]
            except sqlite3.OperationalError:   # a table an old or test database does not have
                continue
            if row is not None:
                found.append(float(row))
        if not found:
            return None
        self._memory_started = min(found)
        return self._memory_started

    def repair_work_thread_dates(self, *, now: float | None = None, dry_run: bool = False) -> list[dict[str, Any]]:
        """Threads of work dated where memory cannot have seen them, set right from their own events (measured
        bounds, revamp A9): a first seen before memory began (a thread "since Fri 2024-11-08", 11 months before
        memory existed) becomes its first event dated after memory began; a last seen after now (a calendar line
        "08 Nov" read as when it was touched) becomes its last event up to now. Only those impossible or future
        dates move: a true start inside memory's life stays, however old (review 34 H8: the floor is when memory
        began, :meth:`memory_started_at`, never the oldest capture the 30-day prune left). Returns what changed
        (``dry_run``: what would change; nothing is written)."""
        now = time.time() if now is None else float(now)
        floor = self.memory_started_at()
        rows = self._query("SELECT id, first_seen, last_seen, created_at FROM work_threads WHERE status != 'merged'")
        changed: list[dict[str, Any]] = []
        for r in rows:
            first, last = float(r["first_seen"]), float(r["last_seen"])
            new_first, new_last = first, last
            events = self._query("SELECT at, created_at FROM work_thread_events WHERE work_thread_id=?",
                                 (int(r["id"]),))
            ats = [float(e[0]) for e in events]
            written = [min(float(e[1]), now) for e in events] or [min(float(r["created_at"]), now)]

            def within(a: float) -> bool:
                return a <= now + 60.0 and (floor is None or a >= floor - 86400.0)

            if last > now + 60.0:
                past = [a for a in ats if within(a)]
                new_last = max(past) if past else max(written)   # else when memory wrote its events down
            if floor is not None and first < floor - 86400.0:
                good = [a for a in ats if within(a)]
                new_first = min(good) if good else min(written)
            if new_first > new_last:
                new_first = new_last
            if (new_first, new_last) != (first, last):
                changed.append({"id": int(r["id"]), "first_seen": [first, new_first], "last_seen": [last, new_last]})
        if changed and not dry_run:
            self._write(lambda conn: conn.executemany(
                "UPDATE work_threads SET first_seen=?, last_seen=?, updated_at=? WHERE id=?",
                [(c["first_seen"][1], c["last_seen"][1], now, c["id"]) for c in changed]))
        return changed

    def apply_work(self, ops: Sequence[WorkOp], *, batch_id: int | None = None) -> list[WorkOp]:
        """:meth:`_apply_work` in a transaction of its own (the backfill: facts already stored, ``journal_ids``)."""
        if ops:
            self._write(lambda conn: self._apply_work(conn, ops, batch_id=batch_id))
        return list(ops)

    def _shown_args(self, ch: dict[str, Any]) -> tuple[Any, Any, int]:
        """The three parameters of ``shown_ciphertext=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN NULL ELSE
        shown_ciphertext END`` for one nightly change: its shown form, else cleared when the state changed."""
        shown = " ".join(str(ch.get("shown") or "").split())
        enc = self.cipher.encrypt(shown) if shown else None
        return enc, enc, int(bool(ch.get("state")))

    def _next_args(self, ch: dict[str, Any]) -> tuple[Any, Any, int]:
        """The same three parameters for ``next_ciphertext`` (migration 35): the change's next step, else cleared when
        the state changed."""
        nxt = " ".join(str(ch.get("next") or "").split())
        enc = self.cipher.encrypt(nxt) if nxt else None
        return enc, enc, int(bool(ch.get("state")))

    #: work_threads columns a before-image keeps (migration 35): everything a change can touch.
    _WORK_SNAPSHOT_COLUMNS = ("title_ciphertext", "kind", "status", "state_ciphertext", "open_ciphertext",
                              "entities_ciphertext", "first_seen", "last_seen", "importance", "merged_into", "events",
                              "shown_ciphertext", "next_ciphertext", "closed_by", "closed_at")

    def _snapshot_work_thread(self, conn: sqlite3.Connection, tid: int, *, run_id: int | None, action: str, by: str,
                              now: float) -> None:
        """Keep a thread's before-image (migration 35's ``undo_images``): its row, the journal facts, events and
        links that point at it, and its vector, encrypted; :meth:`undo_work_threads` and :meth:`reopen_work_thread`
        put it back."""
        import base64

        row = conn.execute("SELECT * FROM work_threads WHERE id=?", (int(tid),)).fetchone()
        if row is None:
            return
        vec = conn.execute("SELECT model, dim, vec_ciphertext FROM work_thread_vec WHERE work_thread_id=?",
                           (int(tid),)).fetchone()
        before = {
            "row": {c: row[c] for c in self._WORK_SNAPSHOT_COLUMNS if c in row.keys()},
            "journal": [int(r[0]) for r in conn.execute("SELECT id FROM journal WHERE work_thread_id=?", (int(tid),))],
            "events": [int(r[0]) for r in conn.execute("SELECT id FROM work_thread_events WHERE work_thread_id=?",
                                                       (int(tid),))],
            "links": [int(r[0]) for r in conn.execute("SELECT id FROM links_seen WHERE work_thread_id=?", (int(tid),))],
            "vec": None if vec is None else {"model": vec["model"], "dim": vec["dim"],
                                             "blob": base64.b64encode(bytes(vec["vec_ciphertext"])).decode("ascii")},
        }
        conn.execute("INSERT INTO undo_images(run_id, surface, item_id, action, by, at, before_ciphertext)"
                     " VALUES (?,?,?,?,?,?,?)", (run_id, "thread", int(tid), str(action), str(by), now,
                                                 self.cipher.encrypt(json.dumps(before, default=str))))

    def _restore_work_thread(self, conn: sqlite3.Connection, snap: sqlite3.Row, now: float) -> None:
        """Put one before-image back (inside the caller's transaction) and mark it undone."""
        import base64

        tid = int(snap["item_id"])
        before = json.loads(self.cipher.decrypt(snap["before_ciphertext"]))
        cols = [c for c in self._WORK_SNAPSHOT_COLUMNS if c in before["row"]]
        conn.execute(f"UPDATE work_threads SET {', '.join(f'{c}=?' for c in cols)}, updated_at=? WHERE id=?",
                     (*(before["row"][c] for c in cols), now, tid))
        for table, key in (("journal", "journal"), ("work_thread_events", "events"), ("links_seen", "links")):
            ids = [int(i) for i in before.get(key) or []]
            for start in range(0, len(ids), 500):
                part = ids[start:start + 500]
                conn.execute(f"UPDATE {table} SET work_thread_id=? WHERE id IN ({','.join('?' * len(part))})",
                             (tid, *part))
        vec = before.get("vec")
        if vec:
            conn.execute("INSERT OR REPLACE INTO work_thread_vec(work_thread_id, model, dim, vec_ciphertext)"
                         " VALUES (?,?,?,?)", (tid, vec["model"], vec["dim"], base64.b64decode(vec["blob"])))
        conn.execute("UPDATE undo_images SET undone_at=? WHERE id=?", (now, int(snap["id"])))

    def _touched_since(self, conn: sqlite3.Connection, tid: int, at: float) -> bool:
        """The user's own work reached the thread after ``at`` (a journal or backfill event, never a pass's own)."""
        return conn.execute("SELECT 1 FROM work_thread_events WHERE work_thread_id=? AND at > ? AND created_at > ?"
                            " AND origin IN ('journal', 'backfill') AND action != 'seen' LIMIT 1",
                            (int(tid), float(at), float(at))).fetchone() is not None

    #: What a thread shows (its after-image, compared plaintext): a later change to any of these since an image was
    #: kept means the image is no longer the thread's latest state, and putting it back would undo that change.
    _WORK_FACE_COLUMNS = ("title_ciphertext", "kind", "status", "state_ciphertext", "open_ciphertext",
                          "shown_ciphertext", "next_ciphertext", "merged_into", "closed_by")

    def _thread_face(self, conn: sqlite3.Connection, tid: int) -> dict[str, Any] | None:
        row = conn.execute(f"SELECT {', '.join(self._WORK_FACE_COLUMNS)} FROM work_threads WHERE id=?",
                           (int(tid),)).fetchone()
        if row is None:
            return None
        return {c: (self._dec(row[c]) if c.endswith("_ciphertext") else row[c]) for c in self._WORK_FACE_COLUMNS}

    def _seal_thread_after(self, conn: sqlite3.Connection, mark: int) -> None:
        """Keep the after-image (:data:`_WORK_FACE_COLUMNS`) on each thread image taken since ``mark``."""
        for snap in conn.execute("SELECT id, item_id FROM undo_images WHERE id > ? AND surface='thread'"
                                 " AND after_ciphertext IS NULL", (int(mark),)).fetchall():
            face = self._thread_face(conn, int(snap["item_id"]))
            if face is not None:
                conn.execute("UPDATE undo_images SET after_ciphertext=? WHERE id=?",
                             (self.cipher.encrypt(json.dumps({"face": face}, default=str)), int(snap["id"])))

    def _thread_diverged(self, conn: sqlite3.Connection, snap: sqlite3.Row) -> bool:
        """Whether something changed a thread after this image was kept, so putting it back would undo that: the user
        worked on it (a journal event), a later image of it still stands (another run's card, rename or close, the
        user's own close), or what it shows differs from the image's after-image (the user's Reopen, a correction)."""
        tid = int(snap["item_id"])
        if self._touched_since(conn, tid, float(snap["at"])):
            return True
        if conn.execute("SELECT 1 FROM undo_images WHERE surface IN ('thread', 'thread_card', 'thread_title')"
                        " AND item_id=? AND id > ?"
                        " AND undone_at IS NULL LIMIT 1", (tid, int(snap["id"]))).fetchone() is not None:
            return True
        if snap["after_ciphertext"]:
            after = json.loads(self.cipher.decrypt(snap["after_ciphertext"])).get("face")
            if after is not None and self._thread_face(conn, tid) != after:
                return True
        return False

    def prune_undo_images(self, *, older_than_days: float = 30.0, now: float | None = None) -> int:
        """Drop pass images older than ``older_than_days`` (and images already taken back): a pass run is undone
        within days or not at all, and the nightly cards would otherwise grow the table without end. The user's own
        closes (no run) are kept, so an old close can still be reopened with its facts. Returns how many."""
        now = time.time() if now is None else float(now)
        cut = now - float(older_than_days) * 86400.0
        return int(self._write(lambda conn: conn.execute(
            "DELETE FROM undo_images WHERE at < ? AND (run_id IS NOT NULL OR undone_at IS NOT NULL)",
            (cut,)).rowcount))

    def undo_work_threads(self, run_id: int, *, now: float | None = None) -> dict[str, Any]:
        """Take back what one pass run did to the threads of work (migration 35): :meth:`undo_run` for threads only.
        Returns ``{"restored", "diverged": [thread ids], "threads": [thread ids]}``."""
        got = self.undo_run(run_id, surfaces=("thread", "thread_card"), now=now)

        def both(d: dict[str, list[int]]) -> list[int]:
            return sorted({*d.get("thread", []), *d.get("thread_card", [])})

        return {"restored": got["restored"], "diverged": both(got["diverged"]), "threads": both(got["items"])}

    #: The simple surfaces an image keeps (migration 35): ``surface -> (table, key column, columns)``.
    _IMAGE_SURFACES: dict[str, tuple[str, str, tuple[str, ...]]] = {
        "fact": ("portrait_facts", "id", ("shown_ciphertext",)),
        "gist": ("call_digests", "call_id", ("gist_shown_ciphertext",)),
        "episode": ("episodes", "id", ("title_ciphertext", "summary_ciphertext")),
        "journal": ("journal", "id", ("source_ciphertext",)),
        "thread_card": ("work_threads", "id", ("shown_ciphertext", "next_ciphertext")),
        "thread_title": ("work_threads", "id", ("title_ciphertext",)),
        "fact_label": ("portrait_facts", "id", ("summary_ciphertext",)),
        "todo_title": ("conversation_facts", "id", ("title_ciphertext",)),
    }

    def write_with_images(self, surface: str, rows: dict[int, dict[str, Any]], *, run_id: int | None, action: str,
                          by: str = "model", now: float | None = None, only_if: dict[int, dict[str, Any]] | None = None
                          ) -> int:
        """Write plaintext values into one simple surface's columns (:data:`_IMAGE_SURFACES`), ``{item id: {column:
        text or None}}``, keeping each item's before- and after-image under ``run_id`` (:meth:`undo_run` puts the old
        values back while the new ones still stand). ``only_if``: ``{item id: {column: plaintext}}`` the item must
        still hold (a value changed since a dry run is left). Returns how many items were written."""
        table, key, columns = self._IMAGE_SURFACES[surface]
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for item_id, values in rows.items():
                cols = [c for c in values if c in columns]
                if not cols:
                    continue
                row = conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {key}=?", (item_id,)).fetchone()
                if row is None:
                    continue
                want = (only_if or {}).get(item_id)
                if want and any(self._dec(row[c]) != want.get(c) for c in want if c in columns):
                    continue
                enc = {c: (self.cipher.encrypt(str(values[c])) if values[c] not in (None, "") else None) for c in cols}
                conn.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in cols)} WHERE {key}=?",
                             (*(enc[c] for c in cols), item_id))
                before = {c: row[c] for c in cols}
                conn.execute("INSERT INTO undo_images(run_id, surface, item_id, action, by, at, before_ciphertext,"
                             " after_ciphertext) VALUES (?,?,?,?,?,?,?,?)",
                             (run_id, surface, int(item_id) if str(item_id).isdigit() else 0, action, by, now,
                              self.cipher.encrypt(json.dumps(before)), self.cipher.encrypt(json.dumps(enc))))
                n += 1
            return n

        return int(self._write(op)) if rows else 0

    def undo_run(self, run_id: int, *, surfaces: Sequence[str] | None = None, now: float | None = None
                 ) -> dict[str, Any]:
        """Take back everything one pass run changed (migration 35's images), newest change first: a thread gets its
        before-image back unless anything changed it since (:meth:`_thread_diverged`: the user's work, their close or
        Reopen, a later run's change); a simple surface
        gets its old values back only while the run's values still stand (a later change is never undone). Returns
        ``{"restored", "items": {surface: [ids]}, "diverged": {surface: [ids]}}``."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> dict[str, Any]:
            snaps = conn.execute("SELECT * FROM undo_images WHERE run_id=? AND undone_at IS NULL ORDER BY id DESC",
                                 (int(run_id),)).fetchall()
            restored = 0
            items: dict[str, list[int]] = {}
            diverged: dict[str, list[int]] = {}
            for snap in snaps:
                surface, item = snap["surface"], int(snap["item_id"])
                if surfaces is not None and surface not in surfaces:
                    continue
                if surface == "thread":
                    if self._thread_diverged(conn, snap):
                        diverged.setdefault(surface, []).append(item)
                        continue
                    self._restore_work_thread(conn, snap, now)
                else:
                    table, key, columns = self._IMAGE_SURFACES[surface]
                    before = json.loads(self.cipher.decrypt(snap["before_ciphertext"]))
                    after = json.loads(self.cipher.decrypt(snap["after_ciphertext"])) if snap["after_ciphertext"] else {}
                    row = conn.execute(f"SELECT {', '.join(columns)} FROM {table} WHERE {key}=?", (item,)).fetchone()
                    # the run's value still stands (ciphertexts are random: compare what they say)
                    if row is None or any(self._dec(row[c]) != (self._dec(after[c]) if after.get(c) else None)
                                          for c in before):
                        diverged.setdefault(surface, []).append(item)
                        continue
                    cols = list(before)
                    conn.execute(f"UPDATE {table} SET {', '.join(f'{c}=?' for c in cols)} WHERE {key}=?",
                                 (*(before[c] for c in cols), item))
                    conn.execute("UPDATE undo_images SET undone_at=? WHERE id=?", (now, int(snap["id"])))
                restored += 1
                if item not in items.setdefault(surface, []):
                    items[surface].append(item)
            # logged as "undone", never a touch (WORK_TOUCHES): taking a pass back is not the user working on the thread
            for tid in items.get("thread", []):
                conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, state_ciphertext, origin,"
                             " created_at) VALUES (?,?,?,?,?,?)", (tid, now, "undone", None, "undo", now))
            return {"restored": restored, "items": items,
                    "diverged": {k: sorted(set(v)) for k, v in diverged.items()}}

        return self._write(op)

    def reopen_work_thread(self, thread_id: int, *, now: float | None = None) -> dict[str, Any] | None:
        """The user's Reopen (or the Undo of their close or dismiss, migration 35): the thread is open again as it was
        before it was closed - its before-image back when one was kept and nothing changed the thread since (a
        dismissed thread gets its facts, links and vector back); else only the close is taken back (status and who
        closed it), so a later rename or card stays. Returns ``{"id", "status"}`` or ``None`` when it is not closed (or gone,
        or merged into another)."""
        now = time.time() if now is None else float(now)

        def op(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute("SELECT status FROM work_threads WHERE id=?", (int(thread_id),)).fetchone()
            if row is None or row["status"] not in ("done", "paused", "dismissed"):
                return None
            snap = conn.execute("SELECT * FROM undo_images WHERE surface='thread' AND item_id=? AND undone_at IS NULL"
                                " AND action IN ('close', 'dismiss') ORDER BY id DESC LIMIT 1",
                                (int(thread_id),)).fetchone()
            if snap is not None:
                if not self._thread_diverged(conn, snap):
                    self._restore_work_thread(conn, snap, now)
                else:   # changed since the close (a rename, a card): only the close is taken back, never those
                    conn.execute("UPDATE undo_images SET undone_at=? WHERE id=?", (now, int(snap["id"])))
            status = conn.execute("SELECT status FROM work_threads WHERE id=?", (int(thread_id),)).fetchone()["status"]
            if status not in ("active", "paused"):
                status = "active"
            conn.execute("UPDATE work_threads SET status=?, closed_by=NULL, closed_at=NULL, updated_at=? WHERE id=?",
                         (status, now, int(thread_id)))
            conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, state_ciphertext, origin,"
                         " created_at) VALUES (?,?,?,?,?,?)", (int(thread_id), now, "reopens", None, "user", now))
            return {"id": int(thread_id), "status": status}

        return self._write(op)

    def set_thread_next(self, texts: dict[int, str], *, overwrite: bool = False) -> int:
        """Write threads' next steps (migration 35), ``{thread id: text}``; without ``overwrite`` a next step stored
        meanwhile is kept. Returns how many were written."""

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for tid, text in texts.items():
                text = " ".join(str(text or "").split())
                if not text:
                    continue
                guard = "" if overwrite else " AND next_ciphertext IS NULL"
                n += conn.execute(f"UPDATE work_threads SET next_ciphertext=? WHERE id=?{guard}",
                                  (self.cipher.encrypt(text), int(tid))).rowcount
            return n

        return int(self._write(op)) if texts else 0

    def change_work_threads(self, changes: Sequence[dict[str, Any]], *, now: float | None = None,
                            origin: str = "consolidate", run_id: int | None = None,
                            by: str | None = None) -> list[dict[str, Any]]:
        """The nightly pass's validated decisions (or the user's own close), in one transaction; returns what happened
        to each.

        Each change: ``{"action": "merge", "ids": [...], "keep": id, "title", "state", "open", "vector",
        "embed_model"}`` (the others end ``merged`` into ``keep``; their facts, events and links move to it),
        ``{"action": "close", "id", "status": "paused" | "done"}``, ``{"action": "dismiss", "id"}``,
        ``{"action": "rename", "id", "title", "vector", "embed_model"}``, ``{"action": "update", "id", "state",
        "open", "vector", "embed_model"}``, ``{"action": "present", "id", "shown", "next"}`` (migration 35: where it
        stands and the next step said to the user, the state as it is).
        A merge or update may carry ``shown`` (the state said to the user, migration 33) and ``next`` (migration 35);
        a new state without them clears the old ones (the titles pass writes them again).
        ``by`` (``user`` | ``model`` | ``code``; default ``user`` for origin ``user``, else ``model``): who closed or
        dismissed it. Every change keeps the thread's before-image (``undo_images``, with ``run_id``), so
        :meth:`undo_work_threads` (a run) and :meth:`reopen_work_thread` (one thread) take it back.
        """
        now = time.time() if now is None else float(now)
        out: list[dict[str, Any]] = []
        who = by or ("user" if origin == "user" else "model")

        def op(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            first = int(conn.execute("SELECT coalesce(max(id), 0) FROM undo_images").fetchone()[0])
            body(conn)
            self._seal_thread_after(conn, first)   # what each changed thread shows now: a later change is never undone
            return out

        def body(conn: sqlite3.Connection) -> None:
            for ch in changes:
                action = ch.get("action")
                touched = ([int(i) for i in ch.get("ids") or []] if action == "merge"
                           else [int(ch["id"])] if ch.get("id") is not None else [])
                mark = int(conn.execute("SELECT coalesce(max(id), 0) FROM undo_images").fetchone()[0])

                def failed(why: str, ch: dict[str, Any] = ch, mark: int = mark) -> None:
                    conn.execute("DELETE FROM undo_images WHERE id > ?", (mark,))   # nothing changed: no undo
                    out.append({**{k: v for k, v in ch.items() if k not in ("vector",)}, "ok": False, "why": why})

                for tid in touched if action != "present" else ():   # a card keeps a small image of its own
                    if conn.execute("SELECT 1 FROM work_threads WHERE id=? AND status NOT IN ('merged', 'dismissed')",
                                    (tid,)).fetchone() is not None:
                        self._snapshot_work_thread(conn, tid, run_id=run_id, action=str(action), by=who, now=now)
                if action == "merge":
                    keep = int(ch["keep"])
                    others = [int(i) for i in ch["ids"] if int(i) != keep]
                    rows = {int(r["id"]): r for r in conn.execute(
                        "SELECT * FROM work_threads WHERE status NOT IN ('merged', 'dismissed') AND id IN (%s)"
                        % ",".join("?" * (len(others) + 1)), (keep, *others)).fetchall()}
                    if keep not in rows or not all(o in rows for o in others):
                        failed("not all current")
                        continue
                    group = [rows[keep], *(rows[o] for o in others)]
                    entities = self._json_list(rows[keep]["entities_ciphertext"])
                    for r in group[1:]:
                        entities = self._merge_list(entities, self._json_list(r["entities_ciphertext"]),
                                                    self.WORK_ENTITIES_MAX)
                    status = "active" if any(r["status"] == "active" for r in group) else rows[keep]["status"]
                    marks = ",".join("?" * len(others))
                    conn.execute(
                        "UPDATE work_threads SET title_ciphertext=?, state_ciphertext=coalesce(?, state_ciphertext),"
                        " open_ciphertext=coalesce(?, open_ciphertext), entities_ciphertext=?, status=?,"
                        " first_seen=?, last_seen=?, events=?, importance=coalesce(?, importance), updated_at=?,"
                        " shown_ciphertext=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN NULL ELSE shown_ciphertext END,"
                        " next_ciphertext=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN NULL ELSE next_ciphertext END"
                        " WHERE id=?",
                        (self.cipher.encrypt(str(ch.get("title") or self._dec(rows[keep]["title_ciphertext"]) or "")),
                         self._enc(ch.get("state") or None),
                         self.cipher.encrypt(json.dumps(list(ch["open"])[: self.WORK_OPEN_MAX])) if ch.get("open")
                         is not None else None,
                         self.cipher.encrypt(json.dumps(entities)), status,
                         min(r["first_seen"] for r in group), max(r["last_seen"] for r in group),
                         sum(int(r["events"] or 0) for r in group), ch.get("importance"), now,
                         *self._shown_args(ch), *self._next_args(ch), keep))
                    conn.execute(f"UPDATE work_threads SET status='merged', merged_into=?, updated_at=? WHERE id IN ({marks})",
                                 (keep, now, *others))
                    conn.execute(f"UPDATE journal SET work_thread_id=? WHERE work_thread_id IN ({marks})", (keep, *others))
                    conn.execute(f"UPDATE work_thread_events SET work_thread_id=? WHERE work_thread_id IN ({marks})",
                                 (keep, *others))
                    conn.execute(f"UPDATE links_seen SET work_thread_id=? WHERE work_thread_id IN ({marks})", (keep, *others))
                    conn.execute(f"DELETE FROM work_thread_vec WHERE work_thread_id IN ({marks})", others)
                    self._put_work_vec(conn, keep, ch.get("vector"), ch.get("embed_model"))
                    conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, state_ciphertext, origin,"
                                 " created_at) VALUES (?,?,?,?,?,?)", (keep, now, "merged", self._enc(ch.get("state") or None),
                                                                       origin, now))
                    out.append({"action": "merge", "keep": keep, "ids": [keep, *others], "ok": True})
                elif action == "dismiss":
                    tid = int(ch["id"])
                    if conn.execute("UPDATE work_threads SET status='dismissed', closed_by=?, closed_at=?, updated_at=?"
                                    " WHERE id=? AND status NOT IN ('merged', 'dismissed')",
                                    (who, now, now, tid)).rowcount != 1:
                        failed("not current")
                        continue
                    conn.execute("UPDATE journal SET work_thread_id=NULL WHERE work_thread_id=?", (tid,))
                    conn.execute("UPDATE links_seen SET work_thread_id=NULL WHERE work_thread_id=?", (tid,))
                    conn.execute("DELETE FROM work_thread_vec WHERE work_thread_id=?", (tid,))
                    conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, state_ciphertext, origin,"
                                 " created_at) VALUES (?,?,?,?,?,?)", (tid, now, "closed", self._enc(ch.get("reason") or None),
                                                                       origin, now))
                    out.append({"action": action, "id": tid, "ok": True})
                elif action in ("close", "rename", "update", "present"):
                    tid = int(ch["id"])
                    row = conn.execute("SELECT * FROM work_threads WHERE id=? AND status NOT IN ('merged', 'dismissed')", (tid,)).fetchone()
                    if row is None:
                        failed("not current")
                        continue
                    if action == "present":
                        # where it stands and the next step, said to the user; the state and its date stay
                        # (only the two card columns' before- and after-image are kept: the whole thread's every
                        # night would grow undo_images without end)
                        shown = " ".join(str(ch.get("shown") or "").split())
                        nxt = " ".join(str(ch.get("next") or "").split())
                        after = {"shown_ciphertext": self._enc(shown or None) or row["shown_ciphertext"],
                                 "next_ciphertext": self._enc(nxt or None)}
                        conn.execute("UPDATE work_threads SET shown_ciphertext=?, next_ciphertext=?, updated_at=?"
                                     " WHERE id=?", (after["shown_ciphertext"], after["next_ciphertext"], now, tid))
                        before = {c: row[c] for c in ("shown_ciphertext", "next_ciphertext")}
                        conn.execute("INSERT INTO undo_images(run_id, surface, item_id, action, by, at,"
                                     " before_ciphertext, after_ciphertext) VALUES (?,?,?,?,?,?,?,?)",
                                     (run_id, "thread_card", tid, "present", who, now,
                                      self.cipher.encrypt(json.dumps(before)), self.cipher.encrypt(json.dumps(after))))
                        out.append({"action": action, "id": tid, "ok": True})
                        continue
                    if action == "close":
                        status = ch.get("status") if ch.get("status") in ("paused", "done") else "paused"
                        conn.execute("UPDATE work_threads SET status=?, closed_by=?, closed_at=?, updated_at=? WHERE id=?",
                                     (status, who, now, now, tid))
                        event = "closed"
                    elif action == "rename":
                        conn.execute("UPDATE work_threads SET title_ciphertext=?, updated_at=? WHERE id=?",
                                     (self.cipher.encrypt(str(ch["title"])), now, tid))
                        event = "renamed"
                    else:
                        conn.execute(
                            "UPDATE work_threads SET state_ciphertext=coalesce(?, state_ciphertext),"
                            " open_ciphertext=coalesce(?, open_ciphertext), updated_at=?,"
                            " title_ciphertext=coalesce(?, title_ciphertext),"
                            " shown_ciphertext=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN NULL ELSE shown_ciphertext END,"
                            " next_ciphertext=CASE WHEN ? IS NOT NULL THEN ? WHEN ? THEN NULL ELSE next_ciphertext END"
                            " WHERE id=?",
                            (self._enc(ch.get("state") or None),
                             self.cipher.encrypt(json.dumps(list(ch["open"])[: self.WORK_OPEN_MAX]))
                             if ch.get("open") is not None else None, now, self._enc(ch.get("title") or None),
                             *self._shown_args(ch), *self._next_args(ch), tid))
                        event = "renamed" if ch.get("title") else "continues"
                    self._put_work_vec(conn, tid, ch.get("vector"), ch.get("embed_model"))
                    conn.execute("INSERT INTO work_thread_events(work_thread_id, at, action, state_ciphertext, origin,"
                                 " created_at) VALUES (?,?,?,?,?,?)",
                                 (tid, now, event, self._enc(ch.get("state") or ch.get("reason") or None), origin, now))
                    out.append({"action": action, "id": tid, "ok": True})

        return self._write(op) if changes else []

    # -- links seen (migration 23, yuki.memory.links) ------------------------------------

    def _link(self, r: sqlite3.Row, score: float | None = None) -> LinkSeen:
        return LinkSeen(
            id=int(r["id"]), url=self._dec(r["url_ciphertext"]) or "", title=self._dec(r["title_ciphertext"]),
            host=r["host"], app=_shown(r["app"]) if r["app"] else r["app"], how=r["how"],
            sender=self._dec(r["sender_ciphertext"]), where=self._dec(r["where_ciphertext"]), page_key=r["page_key"],
            capture_thread_id=r["capture_thread_id"], work_thread_id=r["work_thread_id"], by_yuki=bool(r["by_yuki"]),
            first_seen=float(r["first_seen"]), last_seen=float(r["last_seen"]), visits=int(r["visits"] or 0),
            score=score, attention=r["attention"] if "attention" in r.keys() else None)

    def upsert_links(self, items: Sequence[dict[str, Any]]) -> list[int]:
        """Record pages seen and links shared, one transaction; one row per (how, URL). Each item: ``url``,
        ``how`` (viewed | shared), ``at`` (first seen this time), ``until`` (last seen), and optionally ``title``,
        ``host``, ``app``, ``sender``, ``where``, ``page_key``, ``capture_thread_id``, ``work_thread_id``,
        ``by_yuki``, ``visit`` (count one visit), ``attention`` (``None``: in front; ``visible``: only beside the
        window in front - once seen in front, a link stays in front). Returns the row ids."""
        now = time.time()

        def op(conn: sqlite3.Connection) -> list[int]:
            ids: list[int] = []
            for it in items:
                url = str(it.get("url") or "").strip()
                how = str(it.get("how") or "viewed")
                if not url:
                    continue
                key = self.cipher.digest("link", how, url)
                first = float(it.get("at") or now)
                last = float(it.get("until") or first)
                row = conn.execute("SELECT id FROM links_seen WHERE url_key=?", (key,)).fetchone()
                if row is None:
                    cur = conn.execute(
                        "INSERT INTO links_seen(url_key, url_ciphertext, title_ciphertext, host, app, how, sender_ciphertext,"
                        " where_ciphertext, page_key, capture_thread_id, work_thread_id, by_yuki, first_seen, last_seen,"
                        " visits, updated_at, attention) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (key, self.cipher.encrypt(url), self._enc(it.get("title") or None), it.get("host"), it.get("app"),
                         how, self._enc(it.get("sender") or None), self._enc(it.get("where") or None), it.get("page_key"),
                         it.get("capture_thread_id"), it.get("work_thread_id"), int(bool(it.get("by_yuki"))), first, last,
                         1 if it.get("visit", True) else 0, now, it.get("attention") or None))
                    ids.append(int(cur.lastrowid))
                    continue
                conn.execute(
                    "UPDATE links_seen SET title_ciphertext=coalesce(?, title_ciphertext), host=coalesce(?, host),"
                    " app=coalesce(?, app), sender_ciphertext=coalesce(?, sender_ciphertext),"
                    " where_ciphertext=coalesce(?, where_ciphertext), page_key=coalesce(?, page_key),"
                    " capture_thread_id=coalesce(?, capture_thread_id), work_thread_id=coalesce(?, work_thread_id),"
                    " by_yuki=min(by_yuki, ?), first_seen=min(first_seen, ?), last_seen=max(last_seen, ?),"
                    " visits=visits+?, updated_at=?, attention=CASE WHEN ? IS NULL THEN NULL ELSE attention END"
                    " WHERE id=?",
                    (self._enc(it.get("title") or None), it.get("host"), it.get("app"), self._enc(it.get("sender") or None),
                     self._enc(it.get("where") or None), it.get("page_key"), it.get("capture_thread_id"),
                     it.get("work_thread_id"), int(bool(it.get("by_yuki"))), first, last,
                     1 if it.get("visit", True) else 0, now, it.get("attention") or None, int(row["id"])))
                ids.append(int(row["id"]))
            return ids

        return self._write(op) if items else []

    def links(self, *, since: TimeArg = None, until: TimeArg = None, how: str | None = None, limit: int = 200,
              ids: Iterable[int] | None = None) -> list[LinkSeen]:
        """Links seen, most recently seen first (last seen in ``[since, until)``)."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("last_seen >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("first_seen < ?")
            params.append(u)
        if how:
            clauses.append("how = ?")
            params.append(how)
        if ids is not None:
            wanted = sorted({int(i) for i in ids})
            if not wanted:
                return []
            clauses.append("id IN (%s)" % ",".join("?" * len(wanted)))
            params.extend(wanted)
        sql = "SELECT * FROM links_seen" + (" WHERE " + " AND ".join(clauses) if clauses else "")
        try:
            return [self._link(r) for r in self._query(sql + " ORDER BY last_seen DESC, id DESC LIMIT ?",
                                                       (*params, int(limit)))]
        except sqlite3.OperationalError:
            return []

    def links_without_vectors(self, limit: int = 128) -> list[LinkSeen]:
        try:
            return [self._link(r) for r in self._query(
                "SELECT l.* FROM links_seen l LEFT JOIN link_vec v ON v.link_id = l.id WHERE v.link_id IS NULL"
                " ORDER BY l.id LIMIT ?", (int(limit),))]
        except sqlite3.OperationalError:
            return []

    def add_link_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            for link_id, vec in items:
                self._put_vec(conn, "link_vec", "link_id", int(link_id), vec, model)
            return len(items)
        return self._write(op) if items else 0

    def link_vectors(self, ids: Iterable[int]) -> dict[int, np.ndarray]:
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return {}
        rows = self._query("SELECT link_id, vec_ciphertext FROM link_vec WHERE link_id IN (%s)" % ",".join("?" * len(wanted)),
                           wanted)
        return {int(r[0]): np.frombuffer(self.cipher.decrypt_bytes(r[1]), dtype=np.float32) for r in rows}

    def page_seconds(self, page_keys: Iterable[str]) -> dict[str, tuple[float, int]]:
        """``{page_key: (seconds in front, stretches)}`` from the timeline (content-free)."""
        wanted = sorted({str(k) for k in page_keys if k})
        if not wanted:
            return {}
        rows = self._query("SELECT page_key, sum(coalesce(active_s,0)+coalesce(passive_s,0)), count(*) FROM timeline"
                           " WHERE state='present' AND page_key IN (%s) GROUP BY page_key" % ",".join("?" * len(wanted)),
                           wanted)
        return {str(r[0]): (float(r[1] or 0.0), int(r[2] or 0)) for r in rows}

    def timeline_pages_since(self, updated_after: float, *, limit: int = 2000) -> list[dict[str, Any]]:
        """Timeline stretches of web pages written or changed after ``updated_after`` (the links sync), oldest
        change first: ``{"id", "started_at", "ended_at", "state", "process", "app", "host", "path", "title",
        "page_key", "by_yuki", "updated_at"}``; privacy-withheld and full-screen rows are left out."""
        rows = self._query(
            "SELECT id, started_at, ended_at, state, process, app, host, path_ciphertext, title_ciphertext, page_key,"
            " by_yuki, updated_at FROM timeline WHERE updated_at > ? AND host IS NOT NULL AND withheld IS NULL"
            " AND fullscreen = 0 AND meeting IS NULL AND state = 'present' ORDER BY updated_at, id LIMIT ?",
            (float(updated_after), int(limit)))
        return [{"id": r["id"], "started_at": r["started_at"], "ended_at": r["ended_at"], "state": r["state"],
                 "process": r["process"], "app": _shown(r["app"], r["process"]), "host": r["host"],
                 "path": self._dec(r["path_ciphertext"]), "title": self._dec(r["title_ciphertext"]),
                 "page_key": r["page_key"], "by_yuki": bool(r["by_yuki"]), "updated_at": r["updated_at"]} for r in rows]

    def url_threads_since(self, after: float, *, limit: int = 500) -> list[dict[str, Any]]:
        """Capture threads with a web address seen after ``after`` (the links sync): ``{"id", "app", "url",
        "title", "host", "first_seen", "last_seen", "kind", "front"}``, oldest first. ``front``: some capture of
        it was taken with the window in front (not only beside it, migration 24)."""
        rows = self._query(
            "SELECT t.*, EXISTS(SELECT 1 FROM captures c WHERE c.thread_id = t.id AND c.trigger != 'popup'"
            " AND c.trigger NOT LIKE 'visible:%') AS front FROM threads t WHERE t.url_ciphertext IS NOT NULL"
            " AND t.last_seen > ? ORDER BY t.last_seen LIMIT ?", (float(after), int(limit)))
        return [{"id": r["id"], "app": _shown(r["app"], r["process"]), "url": self._dec(r["url_ciphertext"]),
                 "title": self._dec(r["title_ciphertext"]), "host": r["host"], "first_seen": r["first_seen"],
                 "last_seen": r["last_seen"], "kind": r["kind"], "front": bool(r["front"])} for r in rows]

    def message_captures_after(self, after_id: int, *, limit: int = 200) -> list[dict[str, Any]]:
        """Conversation and mail captures with ``id > after_id`` (the links sync reads the links in them),
        oldest first: ``{"id", "at", "thread_id", "app", "title", "scope", "text", "by_yuki"}``."""
        rows = self._query(
            "SELECT c.id, c.at, c.thread_id, c.delta_ciphertext, c.by_yuki, t.app, t.process, t.title_ciphertext,"
            " t.scope_ciphertext FROM captures c JOIN threads t ON t.id = c.thread_id WHERE c.id > ?"
            " AND c.kind IN ('conversation', 'email') ORDER BY c.id LIMIT ?", (int(after_id), int(limit)))
        return [{"id": r["id"], "at": r["at"], "thread_id": r["thread_id"], "app": _shown(r["app"], r["process"]),
                 "title": self._dec(r["title_ciphertext"]), "scope": self._dec(r["scope_ciphertext"]),
                 "text": self.cipher.decrypt(r["delta_ciphertext"]), "by_yuki": bool(r["by_yuki"])} for r in rows]

    def max_capture_id(self) -> int:
        return int(self._query("SELECT coalesce(max(id), 0) FROM captures")[0][0])

    # -- call digests as vectors (migration 23) -------------------------------------------

    def call_digests_without_vectors(self, limit: int = 32) -> list[dict[str, Any]]:
        """Written digests (status ok) that have no vector yet: ``{"call_id", "app", "started_at", "gist",
        "blocks"}``."""
        try:
            rows = self._query("SELECT d.call_id FROM call_digests d LEFT JOIN call_digest_vec v ON v.call_id = d.call_id"
                               " WHERE v.call_id IS NULL AND d.status = 'ok' AND d.blocks_ciphertext IS NOT NULL"
                               " ORDER BY d.call_id LIMIT ?", (int(limit),))
        except sqlite3.OperationalError:
            return []
        digests = self.call_digests([int(r[0]) for r in rows])
        return [{"call_id": cid, **d} for cid, d in digests.items()]

    def add_call_digest_vectors(self, items: Sequence[tuple[int, np.ndarray]], model: str) -> int:
        def op(conn: sqlite3.Connection) -> int:
            for call_id, vec in items:
                self._put_vec(conn, "call_digest_vec", "call_id", int(call_id), vec, model)
            return len(items)
        return self._write(op) if items else 0

    # -- the freshest layer: recent screens (yuki.memory.related.recent) ------------------

    def recent_captures(self, since: TimeArg, until: TimeArg = None, *, limit: int = 40,
                        kinds: Sequence[str] | None = None, skip_kinds: Sequence[str] = ("my_call_side",)
                        ) -> list[dict[str, Any]]:
        """The newest ``limit`` captures in ``[since, until)``, oldest first, with their window: ``{"id", "at",
        "kind", "text", "thread_id", "app", "process", "title", "scope", "url", "host", "by_yuki", "trigger",
        "attention"}`` (text decrypted; ``attention``: :func:`capture_attention`)."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("c.at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("c.at < ?")
            params.append(u)
        if kinds:
            clauses.append("c.kind IN (%s)" % ",".join("?" * len(kinds)))
            params.extend(kinds)
        if skip_kinds:
            clauses.append("coalesce(c.kind, '') NOT IN (%s)" % ",".join("?" * len(skip_kinds)))
            params.extend(skip_kinds)
        params.append(int(limit))
        rows = self._query(
            "SELECT c.id, c.at, c.kind, c.delta_ciphertext, c.thread_id, c.by_yuki, c.trigger, t.app, t.process, t.host,"
            " t.title_ciphertext, t.scope_ciphertext, t.url_ciphertext FROM captures c JOIN threads t ON t.id = c.thread_id"
            + (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY c.at DESC, c.id DESC LIMIT ?", params)
        return [{"id": r["id"], "at": r["at"], "kind": r["kind"],
                 "text": user_side_text(r["kind"], self.cipher.decrypt(r["delta_ciphertext"])),
                 "thread_id": r["thread_id"], "app": _shown(r["app"], r["process"]), "process": r["process"],
                 "title": self._dec(r["title_ciphertext"]), "scope": self._dec(r["scope_ciphertext"]),
                 "url": self._dec(r["url_ciphertext"]), "host": r["host"], "by_yuki": bool(r["by_yuki"]),
                 "trigger": r["trigger"], "attention": capture_attention(r["trigger"])}
                for r in reversed(rows)]

    def batch_times(self, batch_ids: Iterable[int]) -> dict[int, float]:
        """``{journal batch: when it was made}`` (the order the journal wrote its facts in)."""
        wanted = sorted({int(i) for i in batch_ids})
        out: dict[int, float] = {}
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            out.update({int(r[0]): float(r[1]) for r in self._query(
                "SELECT id, at FROM journal_batches WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk)})
        return out

    def summaries_by_ids(self, ids: Iterable[int]) -> list[SessionSummary]:
        """Session summaries by id (the whole-memory ranking's picks), oldest first."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return []
        return [self._summary(r) for r in self._query(
            "SELECT * FROM session_summaries WHERE id IN (%s) ORDER BY started_at" % ",".join("?" * len(wanted)), wanted)]

    def captures_by_ids(self, ids: Iterable[int]) -> list[dict[str, Any]]:
        """Captures by id with their window (as :meth:`recent_captures`), oldest first."""
        wanted = sorted({int(i) for i in ids if i is not None})
        if not wanted:
            return []
        rows = self._query(
            "SELECT c.id, c.at, c.kind, c.delta_ciphertext, c.thread_id, c.by_yuki, c.trigger, t.app, t.process, t.host,"
            " t.title_ciphertext, t.scope_ciphertext, t.url_ciphertext FROM captures c JOIN threads t ON t.id = c.thread_id"
            " WHERE c.id IN (%s) ORDER BY c.at, c.id" % ",".join("?" * len(wanted)), wanted)
        return [{"id": r["id"], "at": r["at"], "kind": r["kind"],
                 "text": user_side_text(r["kind"], self.cipher.decrypt(r["delta_ciphertext"])),
                 "thread_id": r["thread_id"], "app": _shown(r["app"], r["process"]), "process": r["process"],
                 "title": self._dec(r["title_ciphertext"]), "scope": self._dec(r["scope_ciphertext"]),
                 "url": self._dec(r["url_ciphertext"]), "host": r["host"], "by_yuki": bool(r["by_yuki"]),
                 "trigger": r["trigger"], "attention": capture_attention(r["trigger"])} for r in rows]

    # -- moments: one table of state changes (migration 40, yuki.memory.moments) -----------------------------------

    #: The columns of a moment row, in the order :meth:`save_moments` writes them.
    MOMENT_COLUMNS: tuple[str, ...] = (
        "at", "ended_at", "seen_at", "kind", "app", "service", "entity_digest", "entity_ciphertext", "value",
        "value_from", "means", "by", "request_id", "source", "source_id", "evidence", "facts",
    )

    def save_moments(self, rows: Sequence[dict[str, Any]]) -> int:
        """Write moments, one transaction; returns how many rows were written or changed.

        Each row is a dict of :data:`MOMENT_COLUMNS` (``facts`` a dict or None; ``entity_ciphertext`` already
        encrypted by the caller). A row with the same ``(source, source_id, kind)`` is replaced in place (its ``id``
        and first ``seen_at`` stay). A row with ``partial`` set only updates ``ended_at`` / ``value`` / ``facts`` of
        that row when it exists (a live span that ended: the media stopped, the Meet tab left the front)."""
        if not rows:
            return 0
        now = time.time()
        full, partial = [], []
        for r in rows:
            facts = r.get("facts")
            facts_json = json.dumps(facts, sort_keys=True, separators=(",", ":")) if facts else None
            if r.get("partial"):
                partial.append((r.get("ended_at"), r.get("value"), facts_json, r["source"], r.get("source_id"),
                                r["kind"]))
                continue
            values = dict(r, seen_at=r.get("seen_at") or now, facts=facts_json, by=r.get("by") or "user")
            full.append(tuple(values.get(c) for c in self.MOMENT_COLUMNS))
        cols = ", ".join(self.MOMENT_COLUMNS)
        sets = ", ".join(f"{c}=excluded.{c}" for c in self.MOMENT_COLUMNS if c not in ("seen_at", "source",
                                                                                      "source_id", "kind"))

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for v in full:
                cur = conn.execute(
                    f"INSERT INTO moments({cols}) VALUES ({','.join('?' * len(self.MOMENT_COLUMNS))})"
                    f" ON CONFLICT(source, source_id, kind) DO UPDATE SET {sets}", v)
                n += cur.rowcount
            for ended_at, value, facts_json, source, source_id, kind in partial:
                cur = conn.execute(
                    "UPDATE moments SET ended_at=coalesce(?, ended_at), value=coalesce(?, value),"
                    " facts=coalesce(?, facts) WHERE source=? AND source_id IS ? AND kind=?",
                    (ended_at, value, facts_json, source, source_id, kind))
                n += cur.rowcount
            return n

        return self._write(op)

    def delete_moments(self, ids: Iterable[int]) -> int:
        """Delete moments by id (a derived moment its sources no longer give); returns how many."""
        wanted = sorted({int(i) for i in ids})
        if not wanted:
            return 0

        def op(conn: sqlite3.Connection) -> int:
            n = 0
            for i in range(0, len(wanted), 500):
                chunk = wanted[i:i + 500]
                n += conn.execute("DELETE FROM moments WHERE id IN (%s)" % ",".join("?" * len(chunk)), chunk).rowcount
            return n

        return self._write(op)

    def moments_between(self, since: TimeArg = None, until: TimeArg = None, kinds: Iterable[str] | None = None, *,
                        app: str | None = None, sources: Iterable[str] | None = None) -> list[dict[str, Any]]:
        """Moments with ``at`` in ``[since, until)``, oldest first, as dicts (``facts`` parsed; the entity stays
        encrypted: ``entity_ciphertext``). Filters: ``kinds``, one ``app`` key, ``sources``."""
        clauses, params = [], []
        if (s := _ts(since)) is not None:
            clauses.append("at >= ?")
            params.append(s)
        if (u := _ts(until)) is not None:
            clauses.append("at < ?")
            params.append(u)
        for column, values in (("kind", kinds), ("source", sources)):
            if values is not None:
                values = list(values)
                clauses.append(f"{column} IN ({','.join('?' * len(values))})" if values else "0")
                params.extend(values)
        if app is not None:
            clauses.append("app = ?")
            params.append(app)
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        out = []
        for r in self._query("SELECT * FROM moments" + where + " ORDER BY at, id", params):
            d = dict(r)
            try:
                d["facts"] = json.loads(d["facts"]) if d.get("facts") else {}
            except ValueError:
                d["facts"] = {}
            out.append(d)
        return out

    def moment_entity(self, row: dict[str, Any]) -> str | None:
        """A moment's entity in clear (in-process only; never logged), or None when only its digest was kept."""
        return self._dec(row.get("entity_ciphertext")) if row.get("entity_ciphertext") else None

    def moment_cursor(self, name: str) -> str | None:
        """The moments extractor's cursor ``name`` (``calls``, ``timeline``, ``messages``, ``rule``)."""
        return self.source_checkpoint("moments." + name)

    def set_moment_cursor(self, name: str, value: str) -> None:
        self.set_source_checkpoint("moments." + name, value)

    def clear_moments(self, *, sources: Iterable[str], kinds: Iterable[str] | None = None,
                      since: float | None = None) -> int:
        """Delete the moments of these sources (and kinds; with ``at >= since``): a rebuild starts from nothing."""
        sources = list(sources)
        clauses, params = [f"source IN ({','.join('?' * len(sources))})"], list(sources)
        if kinds is not None:
            kinds = list(kinds)
            clauses.append(f"kind IN ({','.join('?' * len(kinds))})")
            params.extend(kinds)
        if since is not None:
            clauses.append("at >= ?")
            params.append(float(since))
        return self._write(lambda conn: conn.execute("DELETE FROM moments WHERE " + " AND ".join(clauses),
                                                     params).rowcount)

    def end_open_moments(self, until: float, *, sources: Iterable[str], kinds: Iterable[str], reason: str) -> int:
        """End the still-open live spans of these sources and kinds (a run that never stopped cleanly) at ``until``
        (never before their own ``at``), with ``facts.ended = reason`` (a code word); returns how many."""
        sources, kinds = list(sources), list(kinds)
        if not sources or not kinds:
            return 0
        sql = ("UPDATE moments SET ended_at = max(at, ?), facts = json_set(coalesce(facts, '{}'), '$.ended', ?)"
               f" WHERE ended_at IS NULL AND source IN ({','.join('?' * len(sources))})"
               f" AND kind IN ({','.join('?' * len(kinds))})")
        return self._write(lambda conn: conn.execute(sql, (float(until), str(reason), *sources, *kinds)).rowcount)

    def prune_moments(self, before: float) -> int:
        """Delete moments that ended (or, still open, began) before ``before`` (``[moments] keep_days``)."""
        return self._write(lambda conn: conn.execute(
            "DELETE FROM moments WHERE coalesce(ended_at, at) < ?", (float(before),)).rowcount)

    def moments_counts(self) -> dict[str, Any]:
        """Content-free counts: rows per kind and per ``by``, the oldest and newest ``at``."""
        kinds = {r[0]: r[1] for r in self._query("SELECT kind, count(*) FROM moments GROUP BY kind")}
        by = {r[0]: r[1] for r in self._query("SELECT by, count(*) FROM moments GROUP BY by")}
        lo, hi = self._query("SELECT min(at), max(at) FROM moments")[0]
        return {"rows": sum(kinds.values()), "kinds": kinds, "by": by, "first_at": lo, "last_at": hi}


def _split_sql(script: str) -> list[str]:
    """Split a migration script into statements (the scripts contain no ';' in literals)."""
    lines = [line.split("--", 1)[0] for line in script.splitlines()]
    return [s.strip() for s in "\n".join(lines).split(";") if s.strip()]
