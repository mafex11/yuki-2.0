"""Live-test isolation: the one predicate every learner uses to leave the live test harness's sessions out.

The live test harness (17 Oct 2026, live test 1) drives the real voice pipeline on the real desktop with scripted
speech. Its conversations are logged like the user's own, tagged with session ids that start with
:data:`TEST_SESSION_PREFIX` (``livetest-<timestamp>``, ``livetest-<timestamp>-<scenario>``); the requests and runs
they start carry the same tag inside their ids (``voice-livetest-<timestamp>-<scenario>-1-t2``). On 17 Oct the memory
service distilled those test conversations into facts as if the user had done them ("Arc is not properly
installed", "the user tried to request 'Lumin'..."), and the second pass answered questions from the first pass's
test answers.

**The rule.** Every distill and learning job skips what this module calls a test item: the conversation worker's
extraction and session summaries (and so the journal facts, rules, know-how and misheard names it draws from them),
the misheard-names pass, the persona, the portrait's inputs, the commitments check, the day digest, the weekly review,
the work threads, the brief's judge, the know-how backfill, the call vocabulary, the practice learner, the skill
learner's asked occasions, statements and learned-skill score, the routine miner's "done today", and the offer bars
(``registry.offer_ready`` and the routine cards' own D14 read, the playbook's "worked N times"). :func:`report`
(``yuki-cli -m yuki.memory.testtag``) lists what a test left, read-only; :func:`purge` (audit 17 Oct, decision 3: the
user said purge) deletes it, idempotent: once by migration 45 and again with ``--purge`` after a later live test.

**What the purge takes** (counts only in its result): by the tag, the test sessions' turns, batches and summaries,
the journal facts conversation memory drew from their exchanges (and ends the portrait facts resting on those alone),
the conversation facts (rules, preferences, commitments) resting on test exchanges only, the unanswered questions
resting on test material only, Yuki's run traces and their playbook events, skill runs (their ``uses`` taken back,
``last_used_at`` back to the latest real run), surfaced cards, moments and asked occasions of test requests.
By the test sessions' own span (the harness drove the desktop while the user was away; these rows carry no tag): the
know-how, the "said" taste pairings, the misheard-name aliases, the episodes, Yuki's own journal facts, the moments
and occasions (with the preference observations fed from them) learned inside it, and the corrections said in it
(ended, with the journal and portrait changes their pass made undone). The standing rules a test session made are
conversation facts resting on its exchanges only, so they go with them. Kept: what the screen showed in the
span (screen journal facts, links seen, the hour counts of "observed" tastes), which may be real incoming content.

**Live lookups still see test runs** (review r1 M2): they say how a route or a control behaves on this PC, whoever
asked, so a test measures the same Yuki the user runs and real evidence (a verified rung B, a false done) is never
dropped: the rung ladder and the menu (``registry.trust`` without ``real_only``), the finish gate's control record
(``practice.control_record``), the resolver's "again" (``registry.last_runs``).

A test item is identified by the exact tag the harness writes, never by reading words: a session id that starts with
:data:`TEST_SESSION_PREFIX`, or a request id / run key that holds it right after its own prefix and separator. What was
*derived* from test conversations is identified by its own provenance: a journal fact whose source names a test
turn (conversation memory files each fact with the exchange it came from), a conversation fact whose cited exchanges
are all test turns.

Yuki's own runs stay visible where they *exclude* something from the user's data (the moments a run made, the
acting windows the skill learner leaves out): a test run is still Yuki's run, never the user's.

Public API::

    TEST_SESSION_PREFIX
    is_test_session(session_id) -> bool;  is_test_request(request_id) -> bool;  is_test_item(item) -> bool
    keep_real(items) -> list                                  # items whose session / request / run key is not a test's
    sql_real_session(column) -> str;  sql_real_request(column) -> str   # SQL conditions, no parameters
    sql_real_run(alias="") -> str                             # practice memory's runs: session id and run key
    sql_real_skill_run(alias="") -> str;  sql_real_route_try(alias="") -> str
    test_turn_ids(store) -> frozenset[int]
    is_test_journal(entry, turn_ids) -> bool;  is_test_fact(fact, turn_ids) -> bool
    report(db_path=None, *, show=False) -> dict               # read-only; the database is opened ?mode=ro
    purge(conn, dec, *, now) -> dict[str, int]                # inside the caller's write transaction; counts

Usage::

    yuki-cli -m yuki.memory.testtag [--db PATH] [--json] [--show] [--purge]
    python -m yuki.memory.testtag [--db PATH] [--json] [--show] [--purge]

Counts, ids and times only unless ``--show`` (then the derived facts' own text, on this terminal only).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

#: The session id prefix the live test harness tags its conversations with (``livetest-<timestamp>[-<scenario>]``).
TEST_SESSION_PREFIX = "livetest-"
#: The same tag inside a request id or run key, after the id's own prefix and separator: ``voice-livetest-...``
#: (requests), ``agent:livetest-...:worker`` and ``hand:livetest-...:voice`` (practice memory's run keys).
_SEPARATORS = ("-", ":")


def is_test_session(session_id: Any) -> bool:
    """Whether a session id is the live test harness's (``livetest-...``). ``None`` / empty: no."""
    return str(session_id or "").startswith(TEST_SESSION_PREFIX)


def is_test_request(request_id: Any) -> bool:
    """Whether a request id or run key carries the live test tag: it starts with it, or holds it right after the id's
    own prefix and separator (``voice-livetest-...``, ``agent:livetest-...``)."""
    text = str(request_id or "")
    return text.startswith(TEST_SESSION_PREFIX) or any(sep + TEST_SESSION_PREFIX in text for sep in _SEPARATORS)


def _field(item: Any, name: str) -> Any:
    if isinstance(item, Mapping):
        return item.get(name)
    try:
        return item[name] if isinstance(item, sqlite3.Row) and name in item.keys() else getattr(item, name, None)
    except (IndexError, KeyError):
        return None


def is_test_item(item: Any) -> bool:
    """Whether a row, a dict or a record (a turn, a summary, a run head, a skill run) belongs to a live test: its
    ``session_id`` is a test session, or its ``request_id`` / ``run_key`` carries the tag."""
    if item is None:
        return False
    return (is_test_session(_field(item, "session_id")) or is_test_request(_field(item, "request_id"))
            or is_test_request(_field(item, "run_key")))


def keep_real(items: Iterable[Any] | None) -> list[Any]:
    """``items`` without the live test's (:func:`is_test_item`), order kept."""
    return [x for x in (items or ()) if not is_test_item(x)]


def sql_real_session(column: str) -> str:
    """A SQL condition (no parameters) true for a row whose session column is not a live test's. ``column`` is code's
    own column name, never user input."""
    return f"coalesce({column}, '') NOT LIKE '{TEST_SESSION_PREFIX}%'"


def sql_real_request(column: str) -> str:
    """A SQL condition (no parameters) true for a row whose request id / run key column carries no live test tag
    (:func:`is_test_request`'s rule)."""
    parts = [f"coalesce({column}, '') NOT LIKE '{TEST_SESSION_PREFIX}%'"]
    parts += [f"coalesce({column}, '') NOT LIKE '%{sep}{TEST_SESSION_PREFIX}%'" for sep in _SEPARATORS]
    return "(" + " AND ".join(parts) + ")"


def sql_real_run(alias: str = "") -> str:
    """A SQL condition (no parameters) true for a practice memory ``runs`` row (a run head or a step) that is not a
    live test's: neither its ``session_id`` nor the tag in its ``run_key``. ``alias``: the table's alias in the query,
    or none. Every query over ``runs`` that leaves test runs out uses this one condition."""
    a = f"{alias}." if alias else ""
    return f"{sql_real_session(a + 'session_id')} AND {sql_real_request(a + 'run_key')}"


def sql_real_skill_run(alias: str = "") -> str:
    """A SQL condition (no parameters) true for a ``skill_runs`` row that is not a live test's (its ``session_id``, or
    the tag in its ``request_id``). ``alias``: the table's alias in the query (``"u"``), or none."""
    a = f"{alias}." if alias else ""
    return f"{sql_real_session(a + 'session_id')} AND {sql_real_request(a + 'request_id')}"


def sql_real_route_try(alias: str = "") -> str:
    """A SQL condition (no parameters) true for a ``skill_route_tries`` row whose run (``run_key``, an opaque key shared
    with its ``skill_runs`` row) is not a live test's."""
    a = f"{alias}." if alias else ""
    return (f"coalesce({a}run_key, '') NOT IN (SELECT run_key FROM skill_runs WHERE run_key IS NOT NULL AND NOT "
            f"({sql_real_skill_run()}))")


def test_turn_ids(store: Any) -> frozenset[int]:
    """The ids of the live test's conversation turns (an indexed read; content-free). Empty when it can't be read."""
    try:
        # the prefix as a range on the session index (LIKE ignores case, so it would read every turn): exactly the ids
        # starting with the prefix, since "." follows "-"
        rows = store._query("SELECT id FROM conversation_turns WHERE session_id >= ? AND session_id < ?",
                            (TEST_SESSION_PREFIX, TEST_SESSION_PREFIX[:-1] + chr(ord(TEST_SESSION_PREFIX[-1]) + 1)))
    except Exception:  # noqa: BLE001 - an older database, or none: nothing is a test turn
        return frozenset()
    return frozenset(int(r[0]) for r in rows)


def is_test_journal(entry: Any, turn_ids: frozenset[int] | set[int]) -> bool:
    """Whether a journal fact was drawn from a live test exchange: conversation memory files each fact it takes from
    an exchange with that exchange's id in its provenance (``source.kind == "chat"``, ``source.turn``)."""
    if not turn_ids:
        return False
    source = _field(entry, "source")
    if not isinstance(source, Mapping) or source.get("kind") != "chat":
        return False
    try:
        return int(source.get("turn")) in turn_ids
    except (TypeError, ValueError):
        return False


def is_test_fact(fact: Any, turn_ids: frozenset[int] | set[int]) -> bool:
    """Whether a conversation fact (a rule, preference, commitment) rests on live test exchanges only: every exchange
    it cites is a test turn. One that also cites a real exchange is the user's."""
    if not turn_ids:
        return False
    cited = _field(fact, "source_turn_ids") or []
    try:
        ids = [int(i) for i in cited]
    except (TypeError, ValueError):
        return False
    return bool(ids) and all(i in turn_ids for i in ids)


# ---------------------------------------------------------------------------------------------------------------------
# The purge
# ---------------------------------------------------------------------------------------------------------------------

#: Test sessions closer than this (s) are one span; a span is padded by :data:`SPAN_PAD_S` on each side.
SPAN_JOIN_S = 300.0
SPAN_PAD_S = 120.0


def _has(conn: sqlite3.Connection, table: str, column: str | None = None) -> bool:
    cols = _columns(conn, table)
    return bool(cols) and (column is None or column in cols)


def _ids(rows: Iterable[Any]) -> list[int]:
    return sorted({int(r[0]) for r in rows})


def _delete_ids(conn: sqlite3.Connection, table: str, column: str, ids: Iterable[int]) -> int:
    ids = sorted({int(i) for i in ids})
    if not ids or not _has(conn, table, column):
        return 0
    n = 0
    for i in range(0, len(ids), 400):
        part = ids[i:i + 400]
        n += max(0, int(conn.execute(f"DELETE FROM {table} WHERE {column} IN ({','.join('?' * len(part))})",
                                     part).rowcount or 0))
    return n


def test_spans(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """The live test's sessions as time spans (first to last turn of each, sessions closer than
    :data:`SPAN_JOIN_S` joined, each padded by :data:`SPAN_PAD_S`), oldest first. ``[]`` with no test turns."""
    rows = conn.execute("SELECT min(at), max(at) FROM conversation_turns WHERE session_id >= ? AND session_id < ?"
                        " GROUP BY session_id ORDER BY 1", _session_range()).fetchall() if _has(
        conn, "conversation_turns", "session_id") else []
    merged: list[list[float]] = []
    for a, b in rows:
        if a is None:
            continue
        if merged and float(a) <= merged[-1][1] + SPAN_JOIN_S:
            merged[-1][1] = max(merged[-1][1], float(b))
        else:
            merged.append([float(a), float(b)])
    return [(a - SPAN_PAD_S, b + SPAN_PAD_S) for a, b in merged]


def _session_range() -> tuple[str, str]:
    """The test session ids as a range on the session index (LIKE ignores case and reads every row)."""
    return TEST_SESSION_PREFIX, TEST_SESSION_PREFIX[:-1] + chr(ord(TEST_SESSION_PREFIX[-1]) + 1)


def _in_spans(column: str, spans: Sequence[tuple[float, float]]) -> tuple[str, list[float]]:
    """A SQL condition (and its parameters) true for a row whose ``column`` (code's own name) lies in one of
    ``spans``."""
    if not spans:
        return "0", []
    return "(" + " OR ".join(f"({column} >= ? AND {column} <= ?)" for _ in spans) + ")", [x for s in spans for x in s]


def _purge_corrections(conn: sqlite3.Connection, dec: Callable[[Any], str | None],
                       spans: Sequence[tuple[float, float]], *, now: float) -> dict[str, int]:
    """The corrections made inside the test's span (``portrait_facts`` of kind ``correction``, origin ``user``: the
    harness's scripted "that's wrong" through the voice tools; a correction carries no session) are ended, not deleted
    (as a correction the user takes back), and their correction pass is undone where it can be exactly: the journal
    facts it rewrote or retired (its own batch, ``journal_batches.model = 'correction:<id>'``: the new versions go and
    the old rows are current again) and the portrait facts it rewrote or retired (the pass's own change record: old and
    new ids). A pass not yet run is skipped. Changes on other surfaces (to-dos, episodes, threads, questions, the
    persona, conversation facts) are kept and counted. Idempotent: an ended correction is never read again. Counts."""
    out: dict[str, int] = {}
    if not spans or not _has(conn, "portrait_facts", "kind"):
        return out
    cond, params = _in_spans("valid_from", spans)
    rows = conn.execute(f"SELECT id FROM portrait_facts WHERE kind = 'correction' AND origin = 'user' AND valid_to IS"
                        f" NULL AND {cond}", params).fetchall()
    for (cid,) in [tuple(r) for r in rows]:
        cid = int(cid)
        conn.execute("UPDATE portrait_facts SET valid_to = ?, expired_at = ? WHERE id = ?", (now, now, cid))
        out["corrections_ended"] = out.get("corrections_ended", 0) + 1
        run = conn.execute("SELECT status, started_at, changes_ciphertext FROM correction_passes WHERE"
                           " correction_id = ?", (cid,)).fetchone() if _has(conn, "correction_passes") else None
        if run is None:
            continue
        if run[0] in ("queued", "applying", "failed"):
            conn.execute("UPDATE correction_passes SET status = 'skipped', updated_at = ? WHERE correction_id = ?",
                         (now, cid))
            continue
        # the journal: the pass's own batch
        batches = _ids(conn.execute("SELECT id FROM journal_batches WHERE model = ?", (f"correction:{cid}",))) if (
            _has(conn, "journal_batches", "model")) else []
        if batches:
            marks = ",".join("?" * len(batches))
            new = _ids(conn.execute(f"SELECT id FROM journal WHERE batch_id IN ({marks})", batches))
            later = _ids(conn.execute(f"SELECT id FROM journal WHERE batch_id IN ({marks}) AND superseded_at IS NOT"
                                      f" NULL", batches))
            if later:   # a version the pass wrote was itself corrected since: the chain is kept as it is
                out["correction_changes_kept"] = out.get("correction_changes_kept", 0) + len(new)
            else:
                _delete_ids(conn, "journal_vec", "journal_id", new)
                n_new = _delete_ids(conn, "journal", "id", new)
                back = conn.execute(f"UPDATE journal SET superseded_at = NULL, superseded_by = NULL WHERE superseded_by"
                                    f" IN ({marks})", batches).rowcount
                out["correction_journal_undone"] = out.get("correction_journal_undone", 0) + max(
                    n_new, int(back or 0))
        # the portrait and the other surfaces: the pass's own change record (old and new ids; decrypted in RAM only)
        try:
            changes = json.loads(dec(run[2]) or "[]") if run[2] else []
        except Exception:  # noqa: BLE001 - an unreadable record: nothing more is undone
            changes = []
        started = float(run[1] or 0.0)
        for ch in changes if isinstance(changes, list) else []:
            if not isinstance(ch, dict) or not ch.get("ok") or ch.get("surface") == "journal":
                continue
            old, new_id = ch.get("old_id"), ch.get("new_id")
            if ch.get("surface") != "portrait" or old is None:
                out["correction_changes_kept"] = out.get("correction_changes_kept", 0) + 1
                continue
            if new_id is not None:
                cur = conn.execute("SELECT 1 FROM portrait_facts WHERE id = ? AND valid_to IS NULL",
                                   (int(new_id),)).fetchone()
                if cur is None:   # the rewritten fact changed again since: kept
                    out["correction_changes_kept"] = out.get("correction_changes_kept", 0) + 1
                    continue
                _delete_ids(conn, "portrait_fact_vec", "fact_id", [int(new_id)])
                conn.execute("DELETE FROM portrait_facts WHERE id = ?", (int(new_id),))
                n = conn.execute("UPDATE portrait_facts SET valid_to = NULL, expired_at = NULL, superseded_by = NULL"
                                 " WHERE id = ? AND superseded_by = ?", (int(old), int(new_id))).rowcount
            else:   # retired by the pass (ended at or after its start, superseded by nothing)
                n = conn.execute("UPDATE portrait_facts SET valid_to = NULL, expired_at = NULL WHERE id = ? AND"
                                 " superseded_by IS NULL AND valid_to >= ?", (int(old), started)).rowcount
            out["correction_portrait_undone"] = out.get("correction_portrait_undone", 0) + int(n or 0)
    return out


def purge(conn: sqlite3.Connection, dec: Callable[[Any], str | None], *, now: float) -> dict[str, int]:
    """Delete what the live test left and what was learned from it (module docstring), inside the caller's write
    transaction (``conn``; ``dec`` decrypts a stored field). Idempotent: a second run finds nothing. Counts per kind,
    only the kinds where something went; content-free. Never reads words to decide: the tag, or the test sessions'
    own span for rows that carry none."""
    out: dict[str, int] = {}

    def count(kind: str, n: int) -> None:
        if n:
            out[kind] = out.get(kind, 0) + int(n)

    spans = test_spans(conn)
    lo, hi = _session_range()
    turn_ids: set[int] = set()
    if _has(conn, "conversation_turns", "session_id"):
        turn_ids = set(_ids(conn.execute("SELECT id FROM conversation_turns WHERE session_id >= ? AND session_id < ?",
                                         (lo, hi))))
    # journal facts conversation memory drew from test exchanges (their provenance names the exchange), and Yuki's
    # own facts written inside the test's span
    gone_journal: set[int] = set()
    if turn_ids and _has(conn, "journal", "source_ciphertext") and spans:
        from yuki.memory.store import Store

        for r in conn.execute("SELECT id, source_ciphertext FROM journal WHERE app = ? AND at >= ? AND at <= ?",
                              (Store.CONVERSATION_APP, spans[0][0], spans[-1][1])).fetchall():
            try:
                source = json.loads(dec(r["source_ciphertext"]) or "null") if r["source_ciphertext"] else None
            except Exception:  # noqa: BLE001 - unreadable provenance: not provably a test's
                continue
            if is_test_journal({"source": source}, turn_ids):
                gone_journal.add(int(r["id"]))
    if spans and _has(conn, "journal", "by_yuki"):
        cond, params = _in_spans("at", spans)
        gone_journal |= set(_ids(conn.execute(f"SELECT id FROM journal WHERE by_yuki = 1 AND {cond}", params)))
    if gone_journal and _has(conn, "portrait_facts", "source_ids"):
        ended = 0
        for r in conn.execute("SELECT id, source_ids FROM portrait_facts WHERE valid_to IS NULL AND source_ids IS NOT"
                              " NULL").fetchall():
            try:
                ids = {int(i) for i in json.loads(r["source_ids"] or "[]")}
            except (TypeError, ValueError):
                continue
            if ids and ids <= gone_journal:
                conn.execute("UPDATE portrait_facts SET valid_to = ? WHERE id = ?", (now, int(r["id"])))
                ended += 1
        count("portrait_facts_ended", ended)
    if (gone_journal or turn_ids) and _has(conn, "questions", "source_ids"):
        # unanswered questions resting only on test material: a conversation question's sources are exchange (turn)
        # ids, any other's are journal ids. An answered one keeps: its answer is the user's own
        qs = []
        for r in conn.execute("SELECT id, origin, source_ids FROM questions WHERE source_ids IS NOT NULL AND"
                              " answered_at IS NULL AND coalesce(status, '') != 'answered'").fetchall():
            try:
                ids = {int(i) for i in json.loads(r["source_ids"] or "[]")}
            except (TypeError, ValueError):
                continue
            if ids and ids <= (turn_ids if r["origin"] == "conversation" else gone_journal):
                qs.append(int(r["id"]))
        _delete_ids(conn, "question_vec", "question_id", qs)
        count("questions", _delete_ids(conn, "questions", "id", qs))
    _delete_ids(conn, "journal_vec", "journal_id", gone_journal)
    count("journal_facts", _delete_ids(conn, "journal", "id", gone_journal))
    # conversation facts (rules, preferences, commitments) resting on test exchanges only
    if turn_ids and _has(conn, "conversation_facts", "source_turn_ids"):
        facts = []
        for r in conn.execute("SELECT id, source_turn_ids FROM conversation_facts WHERE source_turn_ids IS NOT NULL"):
            try:
                cited = json.loads(r["source_turn_ids"] or "[]")
            except (TypeError, ValueError):
                continue
            if is_test_fact({"source_turn_ids": cited}, turn_ids):
                facts.append(int(r["id"]))
        _delete_ids(conn, "conversation_fact_vec", "fact_id", facts)
        count("conversation_facts", _delete_ids(conn, "conversation_facts", "id", facts))
    # the user's corrections said inside the test's span (a correction carries no session) and what their pass changed
    for kind, n in _purge_corrections(conn, dec, spans, now=now).items():
        count(kind, n)
    # the test sessions' own records
    if _has(conn, "session_summaries", "session_id"):
        sums = _ids(conn.execute("SELECT id FROM session_summaries WHERE session_id >= ? AND session_id < ?", (lo, hi)))
        _delete_ids(conn, "session_summary_vec", "summary_id", sums)
        count("session_summaries", _delete_ids(conn, "session_summaries", "id", sums))
    if _has(conn, "conversation_batches", "session_id"):
        count("conversation_batches", conn.execute("DELETE FROM conversation_batches WHERE session_id >= ? AND"
                                                   " session_id < ?", (lo, hi)).rowcount)
    _delete_ids(conn, "conversation_turn_vec", "turn_id", turn_ids)
    count("conversation_turns", _delete_ids(conn, "conversation_turns", "id", turn_ids))
    if _has(conn, "runs", "run_key"):
        count("run_rows", conn.execute(f"DELETE FROM runs WHERE NOT ({sql_real_run()})").rowcount)
    if _has(conn, "playbook_events", "run_key"):
        count("playbook_events", conn.execute(
            f"DELETE FROM playbook_events WHERE NOT {sql_real_request('run_key')}").rowcount)
    if _has(conn, "skill_runs", "request_id"):
        # the uses a test run added to its skill (registry.finish counts a done or already run once, and stamps
        # last_used_at)
        used: list[int] = []
        for r in conn.execute(f"SELECT skill_id, count(*) AS n FROM skill_runs WHERE NOT ({sql_real_skill_run()}) AND"
                              " outcome IN ('done', 'already') AND skill_id IS NOT NULL GROUP BY skill_id").fetchall():
            conn.execute("UPDATE skills SET uses = max(0, uses - ?) WHERE id = ?", (int(r["n"]), int(r["skill_id"])))
            count("skill_uses_taken_back", int(r["n"]))
            used.append(int(r["skill_id"]))
        if _has(conn, "skill_route_tries", "run_key"):
            count("skill_route_tries", conn.execute(
                "DELETE FROM skill_route_tries WHERE run_key IN (SELECT run_key FROM skill_runs WHERE run_key IS NOT"
                f" NULL AND NOT ({sql_real_skill_run()}))").rowcount)
        count("skill_runs", conn.execute(f"DELETE FROM skill_runs WHERE NOT ({sql_real_skill_run()})").rowcount)
        # last_used_at back to the skill's latest real done run (none left: unset), only where a test run stamped it
        # later than that (a real run's own stamp is a moment after its t_done)
        for sid in used:
            real = conn.execute("SELECT max(coalesce(t_done, at)) FROM skill_runs WHERE skill_id = ? AND outcome IN"
                                " ('done', 'already')", (sid,)).fetchone()[0]
            count("skill_last_used_reset", conn.execute(
                "UPDATE skills SET last_used_at = ? WHERE id = ? AND last_used_at > ?",
                (real, sid, float(real or 0.0) + 5.0)).rowcount)
    if _has(conn, "surfaced", "session_id"):
        count("surfaced", conn.execute("DELETE FROM surfaced WHERE session_id >= ? AND session_id < ?",
                                       (lo, hi)).rowcount)
    # moments of test requests, and every moment and occasion inside the test's span (the harness drove the desktop),
    # with the preference observations fed from them (skills.prefs: source "occasion:<moment id>")
    moment_ids: set[int] = set()
    if _has(conn, "moments", "request_id"):
        moment_ids |= set(_ids(conn.execute(f"SELECT id FROM moments WHERE NOT {sql_real_request('request_id')}")))
        if spans:
            cond, params = _in_spans("at", spans)
            moment_ids |= set(_ids(conn.execute(f"SELECT id FROM moments WHERE {cond}", params)))
    occ: set[int] = set()
    if _has(conn, "occasions", "asked_run"):
        occ |= set(_ids(conn.execute(f"SELECT id FROM occasions WHERE NOT {sql_real_request('asked_run')}")))
        if moment_ids:
            mids = sorted(moment_ids)
            for i in range(0, len(mids), 400):
                part = mids[i:i + 400]
                occ |= set(_ids(conn.execute(f"SELECT id FROM occasions WHERE end_moment_id IN"
                                             f" ({','.join('?' * len(part))})", part)))
        if spans:
            cond, params = _in_spans("at", spans)
            occ |= set(_ids(conn.execute(f"SELECT id FROM occasions WHERE {cond}", params)))
    if moment_ids and _has(conn, "pref_events", "source"):
        mids = sorted(moment_ids)
        for i in range(0, len(mids), 400):
            part = [f"occasion:{m}" for m in mids[i:i + 400]]
            count("pref_observations", conn.execute(
                f"DELETE FROM pref_events WHERE type = 'observed' AND source IN ({','.join('?' * len(part))})",
                part).rowcount)
    count("occasions", _delete_ids(conn, "occasions", "id", occ))
    count("moments", _delete_ids(conn, "moments", "id", moment_ids))
    if spans:
        # learned inside the test's span; none of these rows carries a session
        if _has(conn, "knowhow", "created_at"):
            cond, params = _in_spans("created_at", spans)
            kh = _ids(conn.execute(f"SELECT id FROM knowhow WHERE {cond}", params))
            _delete_ids(conn, "knowhow_vec", "knowhow_id", kh)
            count("knowhow", _delete_ids(conn, "knowhow", "id", kh))
        if _has(conn, "tastes", "source"):
            cond, params = _in_spans("last_at", spans)
            count("taste_said_pairings", conn.execute(f"DELETE FROM tastes WHERE source = 'said' AND {cond}",
                                                      params).rowcount)
        if _has(conn, "name_aliases", "created_at"):
            cond, params = _in_spans("created_at", spans)
            count("name_aliases", conn.execute(f"DELETE FROM name_aliases WHERE {cond}", params).rowcount)
        if _has(conn, "episodes", "started_at"):
            # an episode wholly inside the span narrates the harness's desktop
            ep = _ids(conn.execute("SELECT id FROM episodes WHERE " + " OR ".join(
                "(started_at >= ? AND ended_at <= ?)" for _ in spans), [x for s in spans for x in s]))
            _delete_ids(conn, "episode_vec", "episode_id", ep)
            count("episodes", _delete_ids(conn, "episodes", "id", ep))
    return out


# ---------------------------------------------------------------------------------------------------------------------
# The read-only report
# ---------------------------------------------------------------------------------------------------------------------

#: Tables whose rows carry a session id, request id or run key (Yuki's own records), read for the report.
_TAGGED = (
    ("conversation_turns", "session_id"), ("conversation_batches", "session_id"), ("session_summaries", "session_id"),
    ("runs", "session_id"), ("runs", "run_key"), ("skill_runs", "session_id"), ("skill_runs", "request_id"),
    ("skill_route_tries", "run_key"), ("playbook_events", "run_key"), ("surfaced", "session_id"),
    ("moments", "request_id"), ("ui_actions", "run_key"),
)


def _when(at: Any) -> str:
    try:
        return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError, OSError):
        return ""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")}
    except sqlite3.Error:
        return set()


def _cipher(db: Path) -> Any:
    """The store's field cipher from the key next to the database (read only; ``None`` without a key, or when the key
    can't be read or used: the report then shows no text and finds no journal facts). Never raises."""
    try:
        from yuki.memory.store import KEY_FILENAME

        key = db.parent / KEY_FILENAME
        if not key.exists():
            return None
        from yuki.memory.crypto import FieldCipher, load_or_create_key

        return FieldCipher(load_or_create_key(key))
    except Exception:  # noqa: BLE001 - a key that can't be read or used: no text, never a crash
        return None


def report(db_path: str | Path | None = None, *, show: bool = False) -> dict[str, Any]:
    """What the live test left in memory, read-only (the database is opened ``?mode=ro``; nothing is written):

    * ``sessions``: each test session's turns, first and last time, how many were distilled (extracted) and summarised;
    * ``tagged``: per table and column, how many rows carry the tag (turns, batches, summaries, runs, skill runs,
      route tries, playbook events, surfaced cards, moments, trace actions);
    * ``derived``: what was built from them: the session summaries, the journal facts conversation memory drew from
      test exchanges (and the portrait facts that cite any of them), the conversation facts (rules, preferences,
      commitments) that rest on test exchanges only, each with its id, kind and status (``show``: its text too).

    Never raises for a missing table (an older database): that part is just empty."""
    from yuki.memory.store import default_db_path

    db = Path(db_path) if db_path is not None else Path(default_db_path())
    out: dict[str, Any] = {"db": str(db), "prefix": TEST_SESSION_PREFIX, "sessions": [], "tagged": {}, "derived": {}}
    if not db.exists():
        out["error"] = "no memory database"
        return out
    conn = sqlite3.connect(f"file:{db.resolve().as_posix()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    cipher = _cipher(db) if show else None

    def dec(text: Any) -> str | None:
        if cipher is None or text is None:
            return None
        try:
            return str(cipher.decrypt(text))
        except Exception:  # noqa: BLE001
            return None

    like = TEST_SESSION_PREFIX + "%"
    try:
        for table, column in _TAGGED:
            if column not in _columns(conn, table):
                continue
            n = conn.execute(f"SELECT count(*) FROM {table} WHERE NOT {sql_real_request(column)}").fetchone()[0]
            if n:
                out["tagged"][f"{table}.{column}"] = int(n)
        if "session_id" in _columns(conn, "conversation_turns"):
            for r in conn.execute(
                    "SELECT session_id, count(*) AS n, min(at) AS a, max(at) AS b, sum(extracted) AS x,"
                    " sum(summary_id IS NOT NULL) AS s FROM conversation_turns WHERE session_id LIKE ?"
                    " GROUP BY session_id ORDER BY a", (like,)):
                out["sessions"].append({"session": r["session_id"], "turns": int(r["n"]), "from": _when(r["a"]),
                                        "to": _when(r["b"]), "distilled": int(r["x"] or 0),
                                        "summarised": int(r["s"] or 0)})
        turn_ids = {int(r[0]) for r in conn.execute("SELECT id FROM conversation_turns WHERE session_id LIKE ?",
                                                    (like,))} if out["sessions"] else set()
        derived: dict[str, Any] = {}
        if "session_id" in _columns(conn, "session_summaries"):
            derived["session_summaries"] = [
                {"id": int(r["id"]), "session": r["session_id"], "turns": int(r["turn_count"] or 0),
                 "at": _when(r["ended_at"]), **({"text": dec(r["text_ciphertext"])} if show else {})}
                for r in conn.execute("SELECT id, session_id, turn_count, ended_at, text_ciphertext FROM"
                                      " session_summaries WHERE session_id LIKE ? ORDER BY id", (like,))]
        journal_ids: list[int] = []
        if turn_ids and "source_ciphertext" in _columns(conn, "journal"):
            # conversation memory's facts are filed under the "Yuki" app, dated by their exchange, with the exchange's
            # id in their provenance: the candidates by app and time, the turn id read from the provenance
            from yuki.memory.store import Store

            ats = sorted({float(r[0]) for r in conn.execute("SELECT at FROM conversation_turns WHERE session_id"
                                                             " LIKE ?", (like,))})
            key_cipher = cipher or _cipher(db)
            rows = conn.execute("SELECT id, at, fact_ciphertext, source_ciphertext, superseded_at FROM journal WHERE"
                                " app = ? AND at >= ? AND at <= ? ORDER BY id",
                                (Store.CONVERSATION_APP, ats[0] - 1.0, ats[-1] + 1.0)).fetchall() if ats else []
            facts = []
            for r in rows:
                source = None
                if key_cipher is not None and r["source_ciphertext"]:
                    try:
                        source = json.loads(key_cipher.decrypt(r["source_ciphertext"]) or "null")
                    except Exception:  # noqa: BLE001
                        source = None
                if not is_test_journal({"source": source}, turn_ids):
                    continue
                journal_ids.append(int(r["id"]))
                facts.append({"id": int(r["id"]), "at": _when(r["at"]),
                              "superseded": r["superseded_at"] is not None,
                              **({"text": dec(r["fact_ciphertext"])} if show else {})})
            derived["journal_facts"] = facts
        if journal_ids and "source_ids" in _columns(conn, "portrait_facts"):
            wanted = set(journal_ids)
            cites = []
            for r in conn.execute("SELECT id, kind, valid_to, source_ids, text_ciphertext FROM portrait_facts"):
                try:
                    ids = {int(i) for i in json.loads(r["source_ids"] or "[]")}
                except (TypeError, ValueError):
                    continue
                if ids & wanted:
                    cites.append({"id": int(r["id"]), "kind": r["kind"], "current": r["valid_to"] is None,
                                  "only_test": ids <= wanted,
                                  **({"text": dec(r["text_ciphertext"])} if show else {})})
            derived["portrait_facts_citing"] = cites
        if turn_ids and "source_turn_ids" in _columns(conn, "conversation_facts"):
            cfacts = []
            for r in conn.execute("SELECT id, kind, status, source_turn_ids, text_ciphertext FROM conversation_facts"
                                  " WHERE source_turn_ids IS NOT NULL ORDER BY id"):
                try:
                    cited = json.loads(r["source_turn_ids"] or "[]")
                except (TypeError, ValueError):
                    continue
                if is_test_fact({"source_turn_ids": cited}, turn_ids):
                    cfacts.append({"id": int(r["id"]), "kind": r["kind"], "status": r["status"],
                                   **({"text": dec(r["text_ciphertext"])} if show else {})})
            derived["conversation_facts"] = cfacts
        out["derived"] = derived
    except sqlite3.Error as exc:
        out["error"] = type(exc).__name__
    finally:
        conn.close()
    return out


def format_report(rep: Mapping[str, Any]) -> str:
    """The report as plain lines for a terminal."""
    lines = [f"Live-test records in {rep.get('db')} (sessions starting {rep.get('prefix')!r}). Read only: nothing was "
             f"changed or deleted."]
    if rep.get("error"):
        lines.append(f"Could not read everything: {rep['error']}")
    sessions = rep.get("sessions") or []
    if not sessions:
        lines.append("No live-test conversations.")
    for s in sessions:
        lines.append(f"  {s['session']}: {s['turns']} turns, {s['from']} to {s['to']}; distilled {s['distilled']}, "
                     f"summarised {s['summarised']}")
    tagged = rep.get("tagged") or {}
    if tagged:
        lines.append("Rows carrying the tag:")
        lines += [f"  {k}: {v}" for k, v in tagged.items()]
    derived = rep.get("derived") or {}
    labels = {"session_summaries": "Session summaries of test sessions",
              "journal_facts": "Journal facts drawn from test exchanges",
              "portrait_facts_citing": "Portrait facts citing one of those journal facts",
              "conversation_facts": "Rules, preferences and commitments resting on test exchanges only"}
    for key, label in labels.items():
        items = derived.get(key)
        if items is None:
            continue
        lines.append(f"{label}: {len(items)}")
        for it in items:
            meta = ", ".join(f"{k} {v}" for k, v in it.items() if k not in ("text",))
            lines.append(f"  - {meta}" + (f": {it['text']}" if it.get("text") else ""))
    lines.append("Every learner skips these. --purge deletes them; without it this command never deletes anything.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.testtag",
                                     description="List what the live test harness left in memory (read-only).")
    parser.add_argument("--db", help="the memory database (default: the live one, opened read-only)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--show", action="store_true", help="also print the derived facts' text (this terminal only)")
    parser.add_argument("--purge", action="store_true",
                        help="delete what the live test left and what was learned from it (counts printed)")
    args = parser.parse_args(argv)
    if args.purge:
        from yuki.memory.store import Store

        store = Store.open(args.db)
        try:
            got = store._write(lambda conn: purge(conn, store._dec, now=time.time()))
        finally:
            store.close()
        print(json.dumps({"purged": got}, indent=2))
        return 0
    rep = report(args.db, show=args.show)
    if args.json:
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        try:
            sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        print(format_report(rep))
    return 1 if rep.get("error") else 0


__all__ = ["TEST_SESSION_PREFIX", "is_test_session", "is_test_request", "is_test_item", "keep_real",
           "sql_real_session", "sql_real_request", "sql_real_run", "sql_real_skill_run", "sql_real_route_try",
           "test_turn_ids", "is_test_journal", "is_test_fact", "test_spans", "purge", "report", "format_report",
           "main"]


if __name__ == "__main__":
    raise SystemExit(main())
