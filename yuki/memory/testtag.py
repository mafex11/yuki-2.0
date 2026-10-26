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
``last_used_at`` back to the latest real run), surfaced cards, moments and asked occasions of test requests;
skill19: the preference events those runs and exchanges wrote (an "in Arc" said, a correction, a usual's answer; the
preference rows restated by what remains) and a false-done mark a test's repeat left on a real run
(:func:`_purge_run_effects`).
By the test sessions' own span (the harness drove the desktop while the user was away; these rows carry no tag): the
"said" taste pairings, the misheard-name aliases, the episodes, Yuki's own journal facts, the taste events
(Yuki's picks, skips and offers, and the outcomes of those offers dated after the span) and the videos' watched /
offered stamps, the moments
and occasions (with the preference observations fed from them) learned inside it, and the corrections said in it
(ended, with the journal and portrait changes their pass made undone). mem22: by the spans of this purge and of earlier
ones (:data:`KEPT_SPANS_STATE`), the know-how dated by a test exchange or inside a span at no real exchange's time
(:func:`_purge_knowhow`; a real row it had superseded current again), the pages a test had in front and nobody saw
outside its span (:func:`_purge_links`), the ways told to Yuki inside a span and never used outside one (integ22
MEM-3, :func:`_purge_told_ways`), and the YouTube offers' live switch when the test fed it
(:func:`_reset_offers_off`, by the feed's own state name). **Except what real use confirmed** (phase 3,
3.4; dots audit 18 Oct: the purge took "lumen = chill" and the Japanese playlist's meaning, learned in test sessions):
a "said" taste row (a pairing of the user's words with a playlist, or a pick they named) or a name alias still in
force is kept when a real exchange (no test session) holds its words - a pairing's words or an alias's heard form said
after it was learned - and that exchange's own skill run finished done or already (:func:`real_uses`: joined by the
request id, else a run within :data:`CONFIRM_RUN_S` after it that no other exchange claims).
Still in force is the "no correction afterwards": a correction drops a pairing (``taste.unsay``) or ends an alias.
A pick row that a kept pairing (or a pairing outside the span) points at is kept too. Only test-only rows go.
The standing rules a test session made are conversation facts resting on its exchanges only, so they go with them.
final-H2 lane D1 (contract K-P): a rule kept through ``remember`` cites no exchange (the exchange is logged after it
ends), so it carries its session instead (``MemoryClient.remember_rule``'s provenance row) and goes by that; one written
before that stamp goes when it was written inside a test's span with no real exchange there (:func:`test_rules`), and a
rule of the user's it superseded stands again. The planner's "Keep this as your usual?" answers carry their session in
their source (``brain:<session>``) and go with the test's other preference events.
r31 (L4): **what a test ended is put back**: a standing rule of the user's that a test's ``forget_rule`` revoked stands
again (:func:`test_revoked`: the end note a live test's session writes, :data:`TEST_REVOKE_NOTE`; for a revocation
before that note, one made inside a test's span with no real exchange before it). A rule the user revoked themselves
(outside every test span, or with their own exchange right before it) is never reopened.
Kept: what the screen showed in the span (screen journal facts, links shared in messages and pages only beside the window
in front, the hour counts of "observed" tastes), which may be real incoming content.

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
    store_test_spans(store) -> list[(start, end)];  sql_outside_spans(column, spans) -> (SQL, params)
    is_test_journal(entry, turn_ids) -> bool;  is_test_fact(fact, turn_ids) -> bool
    known_spans(conn) -> list[(start, end)];  test_rules(conn, known=None) -> {fact id: "session" | "span"}
    revoke_note(session_id) -> str | None;  test_revoked(conn, dec, known=None) -> {fact id: "note" | "span"}
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
import unicodedata
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
#: Integration review MH1: the spans a purge took the turns of are kept here (``nudge_state``, JSON ``[[from, to],
#: ...]``: times only), so the browser measure leaves out a purged block's own rows too (r24 purges before every block),
#: for as long as that measure looks back (``web_route.BAND_DAYS``).
KEPT_SPANS_STATE = "testtag:spans"
KEPT_SPANS_DAYS = 28.0
#: Real use that confirms a "said" pairing or a name alias (phase 3, 3.4): a real exchange holding its words whose own
#: skill run (joined by request id; else one within this long after it, s) finished done or already, read back this
#: many days.
CONFIRM_RUN_S = 180.0
CONFIRM_DAYS = 60
#: The kind suffix of the taste rows that keep what the user called an entity (``yuki.skills.taste.SAID_SUFFIX``).
SAID_SUFFIX = "@said"
#: The skills rows of the ways learned or told (``yuki.skills.learn.ways.ORIGIN``).
WAY_ORIGIN = "learned_way"


def _fold(value: Any) -> str:
    """Case, spacing and punctuation folded, padded: identity of the user's own words, never meaning. A combining mark
    (a Devanagari vowel sign, category M*) stays inside its word: one Hindi word stays one word."""
    text = "".join(ch if ch.isalnum() or unicodedata.category(ch)[:1] == "M" else " "
                   for ch in str(value or "").casefold())
    return " " + " ".join(text.split()) + " "


def _request_base(request_id: Any) -> str:
    """A request id as its exchange and its runs share it: a turn's ``voice-`` prefix and a run's ``-t<n>`` step
    suffix off (``voice-20261017-122119-c060-2`` and ``20261017-122119-c060-2``; the id's own format, no words)."""
    rid = str(request_id or "")
    if rid.startswith("voice-"):
        rid = rid[len("voice-"):]
    head, sep, tail = rid.rpartition("-t")
    return head if sep and tail.isdigit() else rid


def real_uses(conn: sqlite3.Connection, dec: Callable[[Any], str | None], *, now: float
              ) -> list[tuple[float, str]]:
    """``(at, folded words)`` of the user's real exchanges of the last :data:`CONFIRM_DAYS` (no test session) whose
    own skill run finished done or already: the uses that confirm a pairing or an alias whose words they hold. The run
    is the exchange's by its request id (``conversation_turns.request_id`` = ``voice-`` + ``skill_runs.request_id``);
    an exchange with no run of its own id falls back to a real run within :data:`CONFIRM_RUN_S` after it that no other
    exchange's id claims. Decrypted in RAM only; ``[]`` when the tables or the key are missing."""
    if not (_has(conn, "conversation_turns", "user_ciphertext") and _has(conn, "skill_runs", "outcome")):
        return []
    since = now - CONFIRM_DAYS * 86400.0
    lo, hi = _session_range()
    turns = conn.execute("SELECT at, user_ciphertext, request_id FROM conversation_turns WHERE at >= ? AND NOT"
                         " (session_id >= ? AND session_id < ?) AND user_ciphertext IS NOT NULL",
                         (since, lo, hi)).fetchall()
    if not turns:
        return []
    claimed = {_request_base(r[2]) for r in turns if r[2]}
    by_request: dict[str, bool] = {}   # an exchange's own runs: whether any finished done or already
    loose: list[float] = []            # done runs no exchange's id claims (older rows), for the time window
    for r in conn.execute(f"SELECT request_id, coalesce(t0, at), outcome FROM skill_runs WHERE coalesce(t0, at) >= ?"
                          f" AND {sql_real_skill_run()}", (since,)).fetchall():
        ok = str(r[2] or "") in ("done", "already")
        base = _request_base(r[0])
        if r[0] and base in claimed:
            by_request[base] = by_request.get(base, False) or ok
        elif ok and r[1] is not None:
            loose.append(float(r[1]))
    loose.sort()
    import bisect

    out: list[tuple[float, str]] = []
    for r in turns:
        at = float(r[0])
        base = _request_base(r[2]) if r[2] else ""
        if base and base in by_request:
            if not by_request[base]:
                continue   # its own run did not get there: no confirmation
        else:
            i = bisect.bisect_left(loose, at - 5.0)
            if i >= len(loose) or loose[i] > at + CONFIRM_RUN_S:
                continue
        try:
            words = dec(r[1])
        except Exception:  # noqa: BLE001 - unreadable: no evidence
            continue
        if words:
            out.append((at, _fold(words)))
    return out


def _confirmed(words: str | None, uses: Sequence[tuple[float, str]], after: float | None = None) -> bool:
    """Whether a real use (:func:`real_uses`) holds ``words`` as whole words (``after``: only a use after it)."""
    folded = _fold(words)
    if not folded.strip():
        return False
    return any(folded in said and (after is None or at > after) for at, said in uses)


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


#: The live test sessions' first and last turn, one row per session (the session index's range).
_SPANS_SQL = ("SELECT min(at), max(at) FROM conversation_turns WHERE session_id >= ? AND session_id < ?"
              " GROUP BY session_id ORDER BY 1")


def test_spans(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """The live test's sessions as time spans (first to last turn of each, sessions closer than
    :data:`SPAN_JOIN_S` joined, each padded by :data:`SPAN_PAD_S`), oldest first. ``[]`` with no test turns."""
    rows = conn.execute(_SPANS_SQL, _session_range()).fetchall() if _has(
        conn, "conversation_turns", "session_id") else []
    return _merge_spans(rows)


def store_test_spans(store: Any) -> list[tuple[float, float]]:
    """:func:`test_spans` through a store's own read (``store._query``), for a reader that holds a store, not a
    connection (skill19 review L5: the measured browsers leave the test's own tabs out), with the spans earlier purges
    kept (integration review MH1: :data:`KEPT_SPANS_STATE`; a purge deletes the turns they came from). Never raises
    (``[]``)."""
    try:
        spans = _merge_spans(store._query(_SPANS_SQL, _session_range()))
    except Exception:  # noqa: BLE001 - no conversation table (an old or a test database): no spans
        spans = []
    try:
        rows = store._query("SELECT value FROM nudge_state WHERE name = ?", (KEPT_SPANS_STATE,))
        kept = _kept_spans(rows[0][0] if rows else None)
    except Exception:  # noqa: BLE001 - no state table: the turns' spans alone
        kept = []
    return _union_spans(spans, kept)


def _kept_spans(raw: Any) -> list[tuple[float, float]]:
    """The spans a purge kept (:data:`KEPT_SPANS_STATE`'s JSON), ``[]`` when unreadable."""
    try:
        got = json.loads(raw) if raw else []
        return [(float(a), float(b)) for a, b in got if float(a) <= float(b)]
    except (TypeError, ValueError):
        return []


def _union_spans(*groups: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Spans already padded, overlapping ones joined, oldest first."""
    out: list[list[float]] = []
    for a, b in sorted((float(a), float(b)) for g in groups for a, b in g):
        if out and a <= out[-1][1]:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b) for a, b in out]


def _state_value(conn: sqlite3.Connection, name: str) -> Any:
    """One ``nudge_state`` row's value through ``conn``, ``None`` when there is none (or no such table)."""
    if not _has(conn, "nudge_state", "value"):
        return None
    row = conn.execute("SELECT value FROM nudge_state WHERE name = ?", (name,)).fetchone()
    return row[0] if row else None


def _keep_spans(conn: sqlite3.Connection, spans: Sequence[tuple[float, float]], now: float) -> None:
    """Integration review MH1: before a purge deletes the test's turns, their spans (with those kept before, the last
    :data:`KEPT_SPANS_DAYS`) go to :data:`KEPT_SPANS_STATE`. Times only. Idempotent."""
    if not _has(conn, "nudge_state", "value"):
        return
    raw = _state_value(conn, KEPT_SPANS_STATE)
    since = float(now) - KEPT_SPANS_DAYS * 86400.0
    kept = [s for s in _union_spans(_kept_spans(raw), spans) if s[1] >= since]
    if not kept and raw is None:
        return
    conn.execute("INSERT INTO nudge_state(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE SET"
                 " value = excluded.value, updated_at = excluded.updated_at",
                 (KEPT_SPANS_STATE, json.dumps([[round(a, 3), round(b, 3)] for a, b in kept]), float(now)))


def sql_outside_spans(column: str, spans: Sequence[tuple[float, float]]) -> tuple[str, list[float]]:
    """A SQL condition (and its parameters) true for a row whose ``column`` (code's own name) lies in none of
    ``spans``; ``("1", [])`` with none."""
    if not spans:
        return "1", []
    cond, params = _in_spans(column, spans)
    return f"NOT {cond}", params


def _merge_spans(rows: Iterable[Any]) -> list[tuple[float, float]]:
    """Session ``(first, last)`` rows, oldest first, as joined and padded spans (:func:`test_spans`)."""
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


#: final-H2 lane D1 (contract K-P): a rule the user gave Yuki through ``remember`` (``MemoryClient.remember_rule``):
#: kind ``rule``, origin ``user``.
REMEMBERED_KIND = "rule"
REMEMBERED_ORIGIN = "user"
#: The purpose of the provenance row ``remember_rule`` writes a rule with (``yuki.memory.api.REMEMBER_BATCH_PURPOSE``).
REMEMBER_PURPOSE = "remember"
#: A remember is written within this long (s) after the exchange that asked for it: a span's real-exchange check reaches
#: this far before the span, so a real rule said just before a test began is never taken for the test's.
REMEMBER_AFTER_S = 600.0
#: The span rule (:func:`test_rules` ``span``) is for the rules written before ``remember`` stamped its session (final-H2,
#: 22 Oct 2026; installed by this date at the latest). After it every ``remember`` carries its session, and a rule with
#: none is one the user typed in the Yuki window (it has no exchange): never taken by time.
SPAN_RULE_BEFORE = datetime(2026, 11, 1).timestamp()
#: The preference-event sources that carry the session they were said in after ``:`` (``yuki.brain.tools.USUAL_SOURCE``,
#: the planner's "Keep this as your usual?" answers: ``brain:<session>``).
SESSION_SOURCES = ("brain",)
#: r31 (L4, final-I m21_reversal_checkback under one: a test's ``forget_rule`` ended a standing rule the user had given
#: days before, and the purge only took what a test wrote): the end note a rule revoked in a live test's session carries
#: (``MemoryAccess._forget_rule`` through :func:`revoke_note`), followed by that session's id. Code's own format, read
#: back by identity (:func:`test_revoked`), never words of anyone's.
TEST_REVOKE_NOTE = "revoked in a live test session: "
#: The end note ``MemoryClient.revoke_rule`` writes when its caller gives none (``yuki.memory.api``: the user withdrew
#: it). Only a revocation with exactly this note is judged by time (:func:`test_revoked` ``span``): a rule code ended
#: for another reason (moved into Yuki's defaults, a correction) carries its own note and is never reopened.
DEFAULT_REVOKE_NOTE = "the user withdrew it"
#: The kinds ``MemoryClient.revoke_rule`` ends.
REVOKED_KINDS = ("rule", "preference", "feedback")
#: The span rule of :func:`test_revoked` is for revocations made before the note existed (release 31, 27 Oct 2026; this
#: date at the latest, as :data:`SPAN_RULE_BEFORE`): after it every revoke in a live test's session carries the note,
#: and one without it is never taken by time.
REVOKE_SPAN_BEFORE = datetime(2026, 11, 1).timestamp()


def revoke_note(session_id: Any) -> str | None:
    """r31 (L4): the end note for a rule revoked in session ``session_id``: :data:`TEST_REVOKE_NOTE` and the session's
    id for a live test's session (:func:`is_test_session`), ``None`` for any other (``revoke_rule``'s own default
    then says the user withdrew it). Pure."""
    if not is_test_session(session_id):
        return None
    return TEST_REVOKE_NOTE + " ".join(str(session_id).split())[:120]


def test_revoked(conn: sqlite3.Connection, dec: Callable[[Any], str | None],
                 known: Sequence[tuple[float, float]] | None = None, *, skip: Iterable[int] = ()) -> dict[int, str]:
    """r31 (L4): ``{conversation fact id: why}`` of the standing rules (rules, preferences, feedback) a live test ended
    with ``forget_rule``, which :func:`purge` reopens: the user never took them back. Two measured rules, never a
    reading of words:

    * ``note``: its end note is :data:`TEST_REVOKE_NOTE` followed by a live test's session id (``dec`` decrypts it):
      ``MemoryAccess._forget_rule`` revoked it in that session.
    * ``span``: a revocation made before that note existed (``valid_to`` before :data:`REVOKE_SPAN_BEFORE`) with
      ``revoke_rule``'s own default note (:data:`DEFAULT_REVOKE_NOTE`), made inside a live test's span (``known``:
      this database's test sessions and the spans earlier purges kept; :func:`known_spans` when ``None``; judged from
      the span's first test turn on, as :func:`test_rules` does), of a rule written before that span began, with no
      exchange of a real session from :data:`REMEMBER_AFTER_S` before the revocation to it: nothing the user said to
      Yuki could have asked for it (the harness had the desktop). A revocation outside every test span, or with a real
      exchange in that stretch, is the user's own: never taken.

    Either way not when a standing rule of the same kind with the very same text (folded, identity only) is in force
    (the user said it again since: reopening would make it twice), nor for ids in ``skip``. Read-only; content-free;
    ``{}`` on an older database. ``dec`` may return ``None`` (no key): then neither note can be read and nothing is
    taken."""
    out: dict[int, str] = {}
    if not _has(conn, "conversation_facts", "end_note_ciphertext"):
        return out
    marks = ",".join("?" * len(REVOKED_KINDS))
    rows = conn.execute(f"SELECT id, kind, valid_to, created_at, end_note_ciphertext, text_ciphertext FROM"
                        f" conversation_facts WHERE status = 'revoked' AND kind IN ({marks})",
                        REVOKED_KINDS).fetchall()
    skipped = {int(i) for i in skip}
    rows = [r for r in rows if int(r[0]) not in skipped]
    if not rows:
        return out
    spans = [(a + SPAN_PAD_S, b) for a, b in (known_spans(conn) if known is None else known) if a + SPAN_PAD_S <= b]
    lo, hi = _session_range()
    turns = _has(conn, "conversation_turns", "session_id")

    def text_of(raw: Any) -> str | None:
        try:
            got = dec(raw) if raw is not None else None
        except Exception:  # noqa: BLE001 - unreadable: not provably a test's
            return None
        return got if isinstance(got, str) else None

    found: dict[int, tuple[str, str, Any]] = {}
    for fid, kind, vto, created, note_raw, text_raw in rows:
        note = text_of(note_raw)
        if note is None:
            continue
        if note.startswith(TEST_REVOKE_NOTE) and is_test_session(note[len(TEST_REVOKE_NOTE):].strip()):
            found[int(fid)] = ("note", str(kind or ""), text_raw)
            continue
        if note.strip() != DEFAULT_REVOKE_NOTE or vto is None or created is None or not turns:
            continue
        ended = float(vto)
        if ended >= REVOKE_SPAN_BEFORE:
            continue
        span = next(((a, b) for a, b in spans if a <= ended <= b), None)
        if span is None or float(created) >= span[0]:
            continue   # outside every test span, or a rule written inside one (the test's own: test_rules)
        real = conn.execute("SELECT 1 FROM conversation_turns WHERE NOT (session_id >= ? AND session_id < ?) AND"
                            " at >= ? AND at <= ? LIMIT 1", (lo, hi, ended - REMEMBER_AFTER_S, ended)).fetchone()
        if real is None:
            found[int(fid)] = ("span", str(kind or ""), text_raw)
    if not found:
        return out
    # identity only: a rule of the same kind and the very same text in force now (said again since) stays the one
    marks_kinds = ",".join("?" * len(REVOKED_KINDS))
    standing: set[tuple[str, str]] = set()
    for kind, text_raw in conn.execute(f"SELECT kind, text_ciphertext FROM conversation_facts WHERE status ="
                                       f" 'active' AND kind IN ({marks_kinds})", REVOKED_KINDS).fetchall():
        text = text_of(text_raw)
        if text is not None:
            standing.add((str(kind or ""), _fold(text)))
    for fid, (why, kind, text_raw) in sorted(found.items()):
        text = text_of(text_raw)
        if text is not None:
            if (kind, _fold(text)) in standing:
                continue
            standing.add((kind, _fold(text)))   # two revoked copies of one text: the first only
        out[fid] = why
    return out


def known_spans(conn: sqlite3.Connection) -> list[tuple[float, float]]:
    """The live tests' spans this database knows: its test sessions' (:func:`test_spans`) and those earlier purges
    kept (:data:`KEPT_SPANS_STATE`; their turns are gone). Read-only."""
    return _union_spans(test_spans(conn), _kept_spans(_state_value(conn, KEPT_SPANS_STATE)))


def test_rules(conn: sqlite3.Connection, known: Sequence[tuple[float, float]] | None = None) -> dict[int, str]:
    """final-H2 lane D1 (contract K-P; live 22 Oct: ``rule:172`` "Don't use Twinkle Tray ..." was written by
    m21_reversal_checkback and every purge said "Rules ... 0"): ``{conversation fact id: why}`` of the rules a live
    test's session kept through ``remember``, which :func:`is_test_fact` cannot see (``remember_rule`` cites no
    exchange: the exchange is logged only after it ends). Two measured rules, never a reading of words:

    * ``session``: its provenance row (the fact's ``batch_id``, a ``conversation_batches`` row of purpose
      :data:`REMEMBER_PURPOSE`) names a live test's session: ``MemoryClient.remember_rule``'s stamp.
    * ``span``: a rule written before the stamp existed (kind ``rule``, origin ``user``, no provenance row, no exchange
      cited) whose writing (``valid_from`` and ``created_at``) lies inside a live test's span (``known``: this
      database's test sessions and the spans earlier purges kept; :func:`known_spans` when ``None``) in which, from
      :data:`REMEMBER_AFTER_S` before it to its end, no exchange of a real session took place: the harness had the
      desktop and the user said nothing to Yuki. Conservative: a real exchange anywhere in that stretch keeps every
      rule written in the span, a rule a correction rewrote keeps its first ``valid_from`` (outside the span), and
      only rules written before :data:`SPAN_RULE_BEFORE` are judged so (after it, a rule with no stamp is one typed in
      the Yuki window, never a test's). integ30 (S5): the span is judged from its first test turn on (the stored span
      less its :data:`SPAN_PAD_S` lead: a test's remember is written after the turn that asked for it, which is dated
      at its opening), so a rule the user typed in the Yuki window just before a test began is never taken for it.

    Read-only; content-free; ``{}`` on an older database."""
    out: dict[int, str] = {}
    if not _has(conn, "conversation_facts", "batch_id"):
        return out
    lo, hi = _session_range()
    if _has(conn, "conversation_batches", "session_id"):
        for (fid,) in conn.execute(
                "SELECT f.id FROM conversation_facts f JOIN conversation_batches b ON b.id = f.batch_id"
                " WHERE f.kind = ? AND f.origin = ? AND b.purpose = ? AND b.session_id >= ? AND b.session_id < ?",
                (REMEMBERED_KIND, REMEMBERED_ORIGIN, REMEMBER_PURPOSE, lo, hi)).fetchall():
            out[int(fid)] = "session"
    # integ30 (S5): from each span's first test turn (its padded lead off), to its padded end
    spans = [(a + SPAN_PAD_S, b) for a, b in (known_spans(conn) if known is None else known) if a + SPAN_PAD_S <= b]
    if not spans or not _has(conn, "conversation_turns", "session_id"):
        return out
    cond, params = _in_spans("valid_from", spans)
    rows = conn.execute(
        f"SELECT id, valid_from, created_at, source_turn_ids FROM conversation_facts WHERE kind = ? AND origin = ? AND"
        f" batch_id IS NULL AND {cond}", (REMEMBERED_KIND, REMEMBERED_ORIGIN, *params)).fetchall()
    quiet: dict[tuple[float, float], bool] = {}
    for fid, vfrom, created, cited in rows:
        if int(fid) in out:
            continue
        try:
            if json.loads(cited or "[]"):
                continue   # it cites an exchange: is_test_fact's to judge
        except (TypeError, ValueError):
            continue
        span = next(((a, b) for a, b in spans if a <= float(vfrom) <= b), None)
        if span is None or created is None or not span[0] <= float(created) <= span[1]:
            continue
        if float(created) >= SPAN_RULE_BEFORE:
            continue   # written after remember stamped its session: one with none came from the Yuki window
        if span not in quiet:
            quiet[span] = conn.execute(
                "SELECT 1 FROM conversation_turns WHERE NOT (session_id >= ? AND session_id < ?) AND at >= ? AND"
                " at <= ? LIMIT 1", (lo, hi, span[0] - REMEMBER_AFTER_S, span[1])).fetchone() is None
        if quiet[span]:
            out[int(fid)] = "span"
    return out


def _restore_before_rules(conn: sqlite3.Connection, gone: set[int]) -> dict[str, int]:
    """final-H2 lane D1: before the test's rules (``gone``) are deleted, a fact one of them superseded (``remember_rule``
    supersedes a near-duplicate: a test's "don't use X" can end the user's own "use X") stands again when the test's
    rules are all that came after it and the last of them is still in force; when a fact that stays carries its line
    on, it points at that one. A line the test itself ended (revoked or done) leaves it ended. Counts."""
    rows = conn.execute("SELECT id, status, superseded_by FROM conversation_facts").fetchall()
    after = {int(r[0]): (int(r[2]) if r[2] is not None else None) for r in rows}
    status = {int(r[0]): str(r[1] or "") for r in rows}
    restore: list[int] = []
    repoint: list[tuple[int, int]] = []
    for fid, sup in after.items():
        if fid in gone or sup is None or sup not in gone or status.get(fid) != "superseded":
            continue
        j: int | None = sup
        last, seen = None, set()
        while j is not None and j in gone and j not in seen:
            seen.add(j)
            last, j = j, after.get(j)
        if j is None and last is not None and status.get(last) == "active":
            restore.append(fid)
        elif j is not None and j not in gone:
            repoint.append((fid, j))
    out: dict[str, int] = {}
    for fid in restore:
        n = int(conn.execute("UPDATE conversation_facts SET status = 'active', valid_to = NULL, expired_at = NULL,"
                             " superseded_by = NULL, end_note_ciphertext = NULL WHERE id = ? AND status = 'superseded'",
                             (fid,)).rowcount or 0)
        if n:
            out["rules_restored"] = out.get("rules_restored", 0) + n
    for fid, j in repoint:
        conn.execute("UPDATE conversation_facts SET superseded_by = ? WHERE id = ?", (j, fid))
    return out


def _session_sources(conn: sqlite3.Connection) -> list[str]:
    """final-H2 lane D1: the preference-event sources stamped with a live test's session (:data:`SESSION_SOURCES`
    ``:livetest-...``), by the session index's range. Read-only."""
    if not _has(conn, "pref_events", "source"):
        return []
    out: list[str] = []
    for prefix in SESSION_SOURCES:
        lo = f"{prefix}:{TEST_SESSION_PREFIX}"
        hi = lo[:-1] + chr(ord(lo[-1]) + 1)
        out += [str(r[0]) for r in conn.execute("SELECT DISTINCT source FROM pref_events WHERE source >= ? AND"
                                                 " source < ?", (lo, hi)).fetchall()]
    return out


def _refresh_prefs(conn: sqlite3.Connection, gone: Sequence[tuple[int, str]]) -> dict[str, int]:
    """After preference events were deleted (``gone``: their ``(pref_id, type)``), each preference row they touched
    stands as its remaining events say (``skills.prefs.record``'s own bookkeeping, redone): no event left -> the row
    goes; a ``confirmed``, ``said`` or ``declined`` gone -> with no confirmed one left, ``proposed`` while an
    unanswered ``said`` remains, else ``seen``, and its quote goes when no said remains; the said / corrected /
    rejected counters and ``last_at`` recounted. Then (review L3) each slot and context with such a row has its one
    usual elected again as ``record`` keeps it: the row with the newest remaining ``confirmed`` event (a test's confirm
    had stepped the user's own usual down to ``seen``), every other usual of that slot and context ``seen``
    (``record`` lets no other event step a usual down). Content-free counts."""
    out: dict[str, int] = {}
    by_pref: dict[int, set[str]] = {}
    for pid, typ in gone:
        by_pref.setdefault(int(pid), set()).add(str(typ or ""))
    groups: set[tuple[str, str]] = set()
    for pid, types in by_pref.items():
        if types & {"confirmed", "said", "declined"}:
            row = conn.execute("SELECT slot, context FROM prefs WHERE id = ?", (pid,)).fetchone()
            if row is not None:
                groups.add((str(row[0]), str(row[1] or "")))
    for pid, types in by_pref.items():
        rows = conn.execute("SELECT type, at FROM pref_events WHERE pref_id = ? ORDER BY at", (pid,)).fetchall()
        if not rows:
            n = int(conn.execute("DELETE FROM prefs WHERE id = ?", (pid,)).rowcount or 0)
            out["prefs_removed"] = out.get("prefs_removed", 0) + n
            continue
        kinds = [str(r[0]) for r in rows]
        last_at = max(float(r[1]) for r in rows)
        conn.execute("UPDATE prefs SET said = ?, corrected = ?, rejected = ?, last_at = ? WHERE id = ?",
                     (kinds.count("said"), kinds.count("corrected"), kinds.count("rejected"), last_at, pid))
        if types & {"confirmed", "said", "declined"}:
            confirmed = [float(r[1]) for r in rows if str(r[0]) == "confirmed"]
            said_at = max((float(r[1]) for r in rows if str(r[0]) == "said"), default=None)
            declined_at = max((float(r[1]) for r in rows if str(r[0]) == "declined"), default=None)
            if confirmed:
                pass        # review L3: which row of its slot and context is the usual is elected below
            else:
                state = ("proposed" if said_at is not None and (declined_at is None or declined_at < said_at)
                         else "seen")
                conn.execute("UPDATE prefs SET state = ?, confirmed_at = NULL WHERE id = ? AND ended_at IS NULL",
                             (state, pid))
            if said_at is None:
                conn.execute("UPDATE prefs SET quote_ciphertext = NULL WHERE id = ?", (pid,))
        out["prefs_restated"] = out.get("prefs_restated", 0) + 1
    for slot, context in sorted(groups):
        # review L3: one usual per slot and context, the newest remaining confirm's (record's own rule, replayed)
        best = conn.execute(
            "SELECT p.id, max(e.at) AS at FROM prefs p JOIN pref_events e ON e.pref_id = p.id AND e.type = 'confirmed' "
            "WHERE p.slot = ? AND p.context = ? AND p.ended_at IS NULL GROUP BY p.id ORDER BY at DESC, p.id DESC "
            "LIMIT 1", (slot, context)).fetchone()
        if best is None:
            continue        # no confirm left: the rows stand as restated above (a usual with no event is left alone)
        keep, at = int(best[0]), float(best[1])
        n = int(conn.execute("UPDATE prefs SET state = 'seen' WHERE slot = ? AND context = ? AND state = 'usual' AND "
                             "ended_at IS NULL AND id != ?", (slot, context, keep)).rowcount or 0)
        n += int(conn.execute("UPDATE prefs SET state = 'usual', confirmed_at = ? WHERE id = ? AND (state != 'usual' "
                              "OR confirmed_at IS NOT ?)", (at, keep, at)).rowcount or 0)
        if n:
            out["prefs_usual_reelected"] = out.get("prefs_usual_reelected", 0) + 1
    return out


def _reset_offers_off(conn: sqlite3.Connection, gone_events: Sequence[tuple[int, str, float]],
                      known: Sequence[tuple[float, float]]) -> dict[str, int]:
    """mem22 (A2c, contract C15): the YouTube offers' live switch (``yuki.skills.youtube_feed.OFF_STATE``, by its
    name: ``off:<epoch>``) is reset when the switch rested on the test: this purge took an offer or an outcome the live
    check read when it switched them off (within ``youtube_feed.LIVE_DAYS`` before), or it was switched off inside a
    live test's span (``known``: this purge's spans and the kept ones). The live check then decides again, on the
    user's own offers only, at its next settle. Its own ``offers_off`` note goes with it. Idempotent: a second purge
    takes no event and finds the switch on. Content-free counts."""
    try:
        import yuki.skills.youtube_feed as feed

        off_state = str(feed.OFF_STATE)   # contract C15: the switch by its own name
    except Exception:  # noqa: BLE001 - a build without the feed: no switch to reset
        return {}
    live_days = float(getattr(feed, "LIVE_DAYS", 30.0))   # the live check's window (the feed's own)
    raw = _state_value(conn, off_state)
    if not raw or not str(raw).startswith("off"):
        return {}
    try:
        epoch = float(str(raw).split(":", 1)[1])
    except (IndexError, ValueError):
        return {}
    fed = any(kind in ("offered", "outcome") and epoch - live_days * 86400.0 <= at <= epoch + 1.0
              for _i, kind, at in gone_events)
    inside = any(a <= epoch <= b for a, b in known)
    if not (fed or inside):
        return {}
    out = {"offers_off_reset": int(conn.execute("DELETE FROM nudge_state WHERE name = ?", (off_state,)).rowcount or 0)}
    if _has(conn, "taste_events", "kind"):
        n = int(conn.execute("DELETE FROM taste_events WHERE kind = 'offers_off' AND at >= ? AND at < ?",
                             (epoch, epoch + 1.0)).rowcount or 0)
        if n:
            out["offers_off_notes"] = n
    return {k: v for k, v in out.items() if v}


def _purge_run_effects(conn: sqlite3.Connection, turn_ids: set[int] | frozenset[int], questions: Sequence[int],
                       spans: Sequence[tuple[float, float]]) -> dict[str, int]:
    """skill19: what the live test's runs and exchanges left that changes later behaviour, by the test's own ids
    (content-free, before the test's skill runs are deleted):

    * the preference events a test run or a test exchange wrote (``pref_events.source``: ``skill_run:<a test run's
      key>`` from the engine's "said" and the voice's corrections and "Keep this as your usual?" answers,
      ``turn:<a test turn id>`` from conversation memory, ``question:<a question this purge deleted>``), the
      preference rows they touched restated by what remains (:func:`_refresh_prefs`): an "in Arc" said in a test is no
      basis for a later real choice;
    * a false-done mark a test request's repeat left on a real run (``skill_runs.recheck = 0`` on the user's own done
      run that a test run of the same skill and values followed within ``registry.FALSE_DONE_S``, with no real run of
      that skill in between): back to unknown (NULL), so the rung trust (computed from the ledger) never counts a
      test's repeat against the user's rung. A rung's disable is never stored apart (``registry.trust`` computes it
      from ``skill_runs`` / ``skill_route_tries``; ``skill_routes.disabled_at`` is only ever cleared), so the test's
      own rows going takes their part of it too;
    * taste events inside the test's span (``taste_events``: Yuki's picks, skips, offers and their outcomes there: the
      recommender's skip weights and the offers' live check read them), and a video's ``watched_at`` / ``offered_at``
      stamped inside it (``yt_videos``: a video the test played is no video the user watched).

    Counts per kind."""
    out: dict[str, int] = {}

    def count(kind: str, n: int) -> None:
        if n:
            out[kind] = out.get(kind, 0) + int(n)

    test_keys: list[str] = []
    if _has(conn, "skill_runs", "run_key"):
        test_keys = [str(r[0]) for r in conn.execute(
            f"SELECT run_key FROM skill_runs WHERE run_key IS NOT NULL AND NOT ({sql_real_skill_run()})").fetchall()]
    if _has(conn, "pref_events", "source"):
        # final-H2 lane D1 (K-P): and the answers stamped with a test session (the planner's "keep this as your usual")
        sources = ([f"skill_run:{k}" for k in test_keys] + [f"turn:{int(i)}" for i in sorted(turn_ids)]
                   + [f"question:{int(q)}" for q in questions] + _session_sources(conn))
        gone: list[tuple[int, str]] = []
        for i in range(0, len(sources), 400):
            part = sources[i:i + 400]
            marks = ",".join("?" * len(part))
            gone += [(int(r[0]), str(r[1])) for r in conn.execute(
                f"SELECT pref_id, type FROM pref_events WHERE source IN ({marks})", part).fetchall()]
            count("pref_events", conn.execute(f"DELETE FROM pref_events WHERE source IN ({marks})", part).rowcount)
        # integration review MH2: a run's event whose run row is gone (an earlier purge took the row before purges
        # kept its events; a run whose row never landed) is a test's only inside a test's span (this purge's, or one
        # an earlier purge kept): outside one it is not provably a test's (the engine runs on with its own key when
        # the ledger write fails), so it stays. A keyless run's bare "skill_run:" is never matched.
        known = _union_spans(spans, _kept_spans(_state_value(conn, KEPT_SPANS_STATE)))
        if known and _has(conn, "skill_runs", "run_key"):
            cond, params = _in_spans("at", known)
            orphans = conn.execute(
                "SELECT id, pref_id, type FROM pref_events WHERE substr(source, 1, 10) = 'skill_run:' AND"
                f" length(source) > 10 AND {cond} AND NOT EXISTS (SELECT 1 FROM skill_runs WHERE"
                " skill_runs.run_key = substr(pref_events.source, 11))", params).fetchall()
            gone += [(int(r[1]), str(r[2])) for r in orphans]
            count("pref_events_orphaned", _delete_ids(conn, "pref_events", "id", [int(r[0]) for r in orphans]))
        if gone and _has(conn, "prefs", "state"):
            for kind, n in _refresh_prefs(conn, gone).items():
                count(kind, n)
    if test_keys and _has(conn, "skill_runs", "recheck") and _has(conn, "skill_runs", "slots_digest"):
        from yuki.skills.registry import FALSE_DONE_S

        marked = conn.execute(
            "SELECT run_key, skill, slots_digest, coalesce(t_done, t0) FROM skill_runs WHERE recheck = 0 AND outcome ="
            f" 'done' AND slots_digest IS NOT NULL AND {sql_real_skill_run()}").fetchall()
        for run_key, skill, digest, done_at in marked:
            if done_at is None:
                continue
            after = conn.execute(
                "SELECT min(t0) FROM skill_runs WHERE skill = ? AND slots_digest = ? AND t0 > ? AND t0 <= ? AND NOT"
                f" ({sql_real_skill_run()})", (skill, digest, float(done_at), float(done_at) + FALSE_DONE_S)
            ).fetchone()[0]
            if after is None:
                continue
            between = conn.execute(
                f"SELECT 1 FROM skill_runs WHERE skill = ? AND t0 > ? AND t0 < ? AND {sql_real_skill_run()} LIMIT 1",
                (skill, float(done_at), float(after))).fetchone()
            if between is None:
                count("false_done_marks_cleared", conn.execute(
                    "UPDATE skill_runs SET recheck = NULL WHERE run_key = ?", (run_key,)).rowcount)
    gone_events: list[tuple[int, str, float]] = []
    if spans and _has(conn, "taste_events", "at"):
        cond, params = _in_spans("at", spans)
        gone_events = [(int(r[0]), str(r[1] or ""), float(r[2])) for r in conn.execute(
            f"SELECT id, kind, at FROM taste_events WHERE {cond}", params).fetchall()]
        count("taste_events", conn.execute(f"DELETE FROM taste_events WHERE {cond}", params).rowcount)
        # mem22 (A2c): an offer's outcome is dated OUTCOME_S after it, often past the span's end: it goes with its
        # offer (by the offer's id in its content-free detail)
        offers = {i for i, kind, _at in gone_events if kind == "offered"}
        if offers:
            outcomes = []
            for r in conn.execute("SELECT id, at, detail FROM taste_events WHERE kind = 'outcome'").fetchall():
                try:
                    offer = int((json.loads(r[2]) if r[2] else {}).get("offer") or 0)
                except (TypeError, ValueError, AttributeError):
                    continue
                if offer in offers:
                    outcomes.append((int(r[0]), float(r[1])))
            gone_events += [(i, "outcome", at) for i, at in outcomes]
            count("taste_outcomes_of_test_offers",
                  _delete_ids(conn, "taste_events", "id", [i for i, _at in outcomes]))
    for kind, n in _reset_offers_off(conn, gone_events, _union_spans(
            spans, _kept_spans(_state_value(conn, KEPT_SPANS_STATE)))).items():
        count(kind, n)
    if spans and _has(conn, "yt_videos", "watched_at"):
        for column in ("watched_at", "offered_at"):
            cond, params = _in_spans(column, spans)
            count(f"yt_videos_{column.split('_')[0]}_reset", conn.execute(
                f"UPDATE yt_videos SET {column} = NULL WHERE {column} IS NOT NULL AND {cond}", params).rowcount)
    return out


def _purge_knowhow(conn: sqlite3.Connection, known: Sequence[tuple[float, float]],
                   test_ats: set[float] | frozenset[float],
                   dec: Callable[[Any], str | None] | None = None) -> dict[str, int]:
    """mem22 (A2b): the know-how learned from a live test. The table carries no session, but each row is dated by
    what it was learned from (``valid_from``): conversation memory dates the know-how it draws from exchanges
    (``conversation`` / ``taught`` / ``failed``) at its newest cited exchange's own time, exactly; Yuki's own
    ``remember_how`` at the moment it was written, during its run. So a row goes when its date is a test exchange's
    time (``test_ats``, the turns this purge reads), or lies inside a test's span (``known``: this purge's and the ones
    earlier purges kept, whose turns are gone) and is no real exchange's time there. The old rule (written inside a
    span: ``created_at``) took a real session's know-how when the worker drew it while a test ran, and missed the
    test's own when the worker drew it after the span. A real row a test row had superseded (a restatement: its
    ``valid_to`` and ``superseded_by``) is current again when no row that stays carries its line on, and so is one
    whose successor an earlier purge already took (its ``superseded_by`` names no row, or a row that did not start the
    moment it ended: the id was given again). Never one a current row that stays restates word for word (same app,
    the same text through ``dec``: the identity part of ``Store.knowhow_duplicate``; an older purge may have taken the
    test row between it and a real successor that carries it on): it stays ended. Idempotent. Content-free counts."""
    if not (known or test_ats) or not _has(conn, "knowhow", "valid_from"):
        return {}
    real_ats: set[float] = set()
    if known and _has(conn, "conversation_turns", "at"):
        lo, hi = _session_range()
        cond, params = _in_spans("at", known)
        real_ats = {float(r[0]) for r in conn.execute(
            f"SELECT at FROM conversation_turns WHERE NOT (session_id >= ? AND session_id < ?) AND {cond}",
            (lo, hi, *params)).fetchall()}
    chained = _has(conn, "knowhow", "superseded_by")
    rows = [(int(r[0]), r[1], r[2], int(r[3]) if chained and r[3] is not None else None) for r in conn.execute(
        f"SELECT id, valid_from, valid_to, {'superseded_by' if chained else 'NULL'} FROM knowhow").fetchall()]
    gone: set[int] = set()
    for kid, vfrom, _vto, _sup in rows:
        if vfrom is None:
            continue
        at = float(vfrom)
        if at in test_ats or (at not in real_ats and any(a <= at <= b for a, b in known)):
            gone.add(kid)
    after = {kid: sup for kid, _v, _t, sup in rows}
    since = {kid: (float(v) if v is not None else None) for kid, v, _t, _s in rows}
    current = {kid for kid, _v, vto, _s in rows if vto is None}
    restore: list[tuple[int, int]] = []
    unlink: list[tuple[int, int]] = []
    for kid, _v, vto, sup in rows:
        if kid in gone or sup is None or vto is None:
            continue
        if sup in gone:
            j, last, seen = sup, None, set()
            while j is not None and j in gone and j not in seen:   # the test's rows after it on its line
                seen.add(j)
                last, j = j, after.get(j)
            if j is None and last in current:   # its line ended in a current test row: nothing of theirs carries it on
                restore.append((kid, sup))
            else:   # a row that stays carries it on (or it was ended for good): ended, its pointer to the test's cleared
                unlink.append((kid, sup))
        elif sup not in since or since[sup] != float(vto):
            # its successor is gone: only a purge deletes know-how (an earlier one took a test's restatement and
            # left this row ended; the id may since be another row's - a successor starts the moment it ended)
            restore.append((kid, sup))
    if restore and dec is not None:
        restore = _restore_unless_restated(conn, dec, restore, current - gone, gone, unlink)
    if not gone and not restore:
        return {}
    out: dict[str, int] = {}
    expired = ", expired_at = NULL" if _has(conn, "knowhow", "expired_at") else ""
    for kid, sup in restore:
        n = int(conn.execute(f"UPDATE knowhow SET valid_to = NULL{expired}, superseded_by = NULL WHERE id = ? AND"
                             " superseded_by = ?", (kid, sup)).rowcount or 0)
        if n:
            out["knowhow_restored"] = out.get("knowhow_restored", 0) + n
    for kid, sup in unlink:
        conn.execute("UPDATE knowhow SET superseded_by = NULL WHERE id = ? AND superseded_by = ?", (kid, sup))
    _delete_ids(conn, "knowhow_vec", "knowhow_id", gone)
    n = _delete_ids(conn, "knowhow", "id", gone)
    if n:
        out["knowhow"] = n
    return out


def _restore_unless_restated(conn: sqlite3.Connection, dec: Callable[[Any], str | None],
                             restore: list[tuple[int, int]], stays: set[int], gone: set[int],
                             unlink: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """The rows of ``restore`` no current row that stays (``stays``) restates word for word on the same app; one that
    is restated stays ended (its pointer to a deleted test row goes to ``unlink``). Only the candidates' apps are
    decrypted. A read that fails restores none (nothing is lost: the next purge decides again)."""
    try:
        apps = {int(r[0]): str(r[1] or "").casefold() for r in conn.execute("SELECT id, app FROM knowhow")}
        want = {apps.get(kid) for kid, _sup in restore}
        ids = {kid for kid, _sup in restore} | {kid for kid in stays if apps.get(kid) in want}
        texts: dict[int, str] = {}
        for i in range(0, len(ids), 400):
            part = sorted(ids)[i:i + 400]
            for r in conn.execute(f"SELECT id, text_ciphertext FROM knowhow WHERE id IN ({','.join('?' * len(part))})",
                                  part):
                texts[int(r[0])] = str((dec(r[1]) if r[1] else None) or "").strip().casefold()
    except Exception:  # noqa: BLE001
        return []
    kept: list[tuple[int, int]] = []
    for kid, sup in restore:
        text = texts.get(kid)
        if text and any(o != kid and apps.get(o) == apps.get(kid) and texts.get(o) == text for o in stays):
            if sup in gone:
                unlink.append((kid, sup))
            continue
        kept.append((kid, sup))
    return kept


def _purge_links(conn: sqlite3.Connection, known: Sequence[tuple[float, float]]) -> dict[str, int]:
    """mem22 (A2a): the pages a live test had in front, never the user's: a ``viewed`` link row whose every sighting
    lies inside one test span (``known``: this purge's and the kept ones), opened by Yuki there (``by_yuki``) or in
    front while the harness had the desktop. A test's episode page then never reads as the user's progress. Kept: a
    link shared in a message and a page only beside the window in front (both may be real incoming content), and every
    row also seen outside a span (its later sightings are the user's). Idempotent. Content-free counts."""
    if not known or not _has(conn, "links_seen", "first_seen"):
        return {}
    front = "attention IS NULL" if _has(conn, "links_seen", "attention") else "1"
    yukis = "by_yuki = 1" if _has(conn, "links_seen", "by_yuki") else "0"
    whole = " OR ".join("(first_seen >= ? AND last_seen <= ?)" for _ in known)
    ids = _ids(conn.execute(f"SELECT id FROM links_seen WHERE how = 'viewed' AND ({yukis} OR {front}) AND ({whole})",
                            [x for s in known for x in s]))
    if not ids:
        return {}
    _delete_ids(conn, "link_vec", "link_id", ids)
    n = _delete_ids(conn, "links_seen", "id", ids)
    return {"links_seen": n} if n else {}


def _purge_told_ways(conn: sqlite3.Connection, known: Sequence[tuple[float, float]]) -> dict[str, int]:
    """integ22 (MEM-3): a way TOLD to Yuki ("remember how I do this": ``ways.keep_told``, provenance ``told``) kept
    inside a live test's span (``known``: this purge's and the kept ones) and never used outside one: the test's,
    never the user's (the row carries no session or request; the harness drove the desktop in the span). A told way
    used after its span (a real replay stamps ``last_used_at``) stays, and so does one a route row points at.
    Idempotent. Content-free counts."""
    if not known or not _has(conn, "skills", "provenance"):
        return {}
    cond, params = _in_spans("created_at", known)
    gone: list[int] = []
    for r in conn.execute(f"SELECT id, provenance, last_used_at FROM skills WHERE origin = ? AND {cond}",
                          (WAY_ORIGIN, *params)).fetchall():
        try:
            prov = json.loads(r[1] or "{}")
        except (TypeError, ValueError):
            continue
        if not (isinstance(prov, dict) and prov.get("told")):
            continue
        if r[2] is not None and not any(a <= float(r[2]) <= b for a, b in known):
            continue   # used outside every span: a real run of it
        gone.append(int(r[0]))
    if gone and _has(conn, "skill_routes", "skill_id"):
        routed = set()
        for i in range(0, len(gone), 400):
            part = gone[i:i + 400]
            routed |= set(_ids(conn.execute(f"SELECT DISTINCT skill_id FROM skill_routes WHERE skill_id IN"
                                            f" ({','.join('?' * len(part))})", part)))
        gone = [i for i in gone if i not in routed]
    n = _delete_ids(conn, "skills", "id", gone)
    return {"told_ways": n} if n else {}


def purge(conn: sqlite3.Connection, dec: Callable[[Any], str | None], *, now: float) -> dict[str, int]:
    """Delete what the live test left and what was learned from it (module docstring), inside the caller's write
    transaction (``conn``; ``dec`` decrypts a stored field). Idempotent: a second run finds nothing. Counts per kind,
    only the kinds where something went; content-free. What goes is found by the tag, or the test sessions' own span
    for rows that carry none; the one place words are read (in RAM, by identity) is what keeps a "said" pairing or a
    name alias of that span: a real exchange holding its words, confirmed by its own run (:func:`real_uses`)."""
    out: dict[str, int] = {}

    def count(kind: str, n: int) -> None:
        if n:
            out[kind] = out.get(kind, 0) + int(n)

    spans = test_spans(conn)
    lo, hi = _session_range()
    turn_ids: set[int] = set()
    test_ats: set[float] = set()   # the test exchanges' own times (know-how is dated by the exchange it came from)
    if _has(conn, "conversation_turns", "session_id"):
        rows = conn.execute("SELECT id, at FROM conversation_turns WHERE session_id >= ? AND session_id < ?",
                            (lo, hi)).fetchall()
        turn_ids = {int(r[0]) for r in rows}
        test_ats = {float(r[1]) for r in rows if r[1] is not None}
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
    gone_questions: list[int] = []
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
        gone_questions.extend(qs)
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
    # final-H2 lane D1 (contract K-P): the rules a test session kept through remember (its stamp, or for one written
    # before the stamp, a test's span with no real exchange in it), before the test's batches (the stamps) go; a fact
    # of the user's they superseded stands again (:func:`_restore_before_rules`)
    rules = test_rules(conn, _union_spans(spans, _kept_spans(_state_value(conn, KEPT_SPANS_STATE))))
    if rules:
        for kind, n in _restore_before_rules(conn, set(rules)).items():
            count(kind, n)
        _delete_ids(conn, "conversation_fact_vec", "fact_id", rules)
        count("remembered_rules", _delete_ids(conn, "conversation_facts", "id", rules))
    # r31 (L4): a standing rule of the user's a test ended (forget_rule in a live test's session: its end note, or for a
    # revocation before that note, a test's span with no real exchange before it) stands again, as it was; after the
    # test's own facts and rules are gone, so only what remains is judged (:func:`test_revoked`)
    reopen = test_revoked(conn, dec, _union_spans(spans, _kept_spans(_state_value(conn, KEPT_SPANS_STATE))))
    for fid in sorted(reopen):
        count("rules_reopened", conn.execute(
            "UPDATE conversation_facts SET status = 'active', valid_to = NULL, expired_at = NULL,"
            " end_note_ciphertext = NULL WHERE id = ? AND status = 'revoked'", (int(fid),)).rowcount or 0)
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
    if spans:
        _keep_spans(conn, spans, now)   # integration review MH1: the spans outlive the turns they came from
    _delete_ids(conn, "conversation_turn_vec", "turn_id", turn_ids)
    count("conversation_turns", _delete_ids(conn, "conversation_turns", "id", turn_ids))
    if _has(conn, "runs", "run_key"):
        count("run_rows", conn.execute(f"DELETE FROM runs WHERE NOT ({sql_real_run()})").rowcount)
    if _has(conn, "playbook_events", "run_key"):
        count("playbook_events", conn.execute(
            f"DELETE FROM playbook_events WHERE NOT {sql_real_request('run_key')}").rowcount)
    # skill19 (live 19 Oct: one "in Arc" said in a test run made Arc the browser of later real asks; a test's repeat
    # marked runs false done): what test runs left that changes later behaviour, before their rows go
    for kind, n in _purge_run_effects(conn, turn_ids, gone_questions, spans).items():
        count(kind, n)
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
    # mem22 (A2a, A2b): the test's spans with the ones earlier purges kept (their turns are gone, their rows may not be)
    known = _union_spans(spans, _kept_spans(_state_value(conn, KEPT_SPANS_STATE)))
    for kind, n in _purge_knowhow(conn, known, test_ats, dec).items():
        count(kind, n)
    for kind, n in _purge_links(conn, known).items():
        count(kind, n)
    for kind, n in _purge_told_ways(conn, known).items():
        count(kind, n)
    if spans:
        # learned inside the test's span; none of these rows carries a session
        uses: list[tuple[float, str]] | None = None
        if _has(conn, "tastes", "source"):
            # phase 3 (3.4): a pairing or a named pick the user's real use confirmed is kept; only test-only rows go
            cond, params = _in_spans("last_at", spans)
            rows = conn.execute(f"SELECT id, kind, name_ciphertext, ref_ciphertext, last_at FROM tastes WHERE"
                                f" source = 'said' AND {cond}", params).fetchall()
            if rows:
                uses = real_uses(conn, dec, now=now)
            gone_rows: list[int] = []
            kept_targets: set[tuple[str, str]] = set()
            picks: list[tuple[int, str, str | None]] = []
            for r in rows:
                kind = str(r[1] or "")
                try:
                    name = dec(r[2]) if r[2] else None
                    target = dec(r[3]) if r[3] else None
                except Exception:  # noqa: BLE001 - unreadable: not provably the user's
                    name = target = None
                if not kind.endswith(SAID_SUFFIX):
                    picks.append((int(r[0]), kind, name))
                # a later use: one through the pairing (taste.said_target) leaves its row alone, so last_at is when
                # it was learned
                elif _confirmed(name, uses or [], after=float(r[4] or 0.0)):
                    kept_targets.add((kind[: -len(SAID_SUFFIX)], _fold(target)))
                    count("taste_said_kept", 1)
                else:
                    gone_rows.append(int(r[0]))
            # the pairings that stay (outside the span, or confirmed) point at real picks too
            going = set(gone_rows)
            if picks:
                for r in conn.execute("SELECT id, kind, ref_ciphertext FROM tastes WHERE kind LIKE ? AND"
                                      " ref_ciphertext IS NOT NULL", (f"%{SAID_SUFFIX}",)).fetchall():
                    if int(r[0]) in going:
                        continue
                    try:
                        kept_targets.add((str(r[1])[: -len(SAID_SUFFIX)], _fold(dec(r[2]))))
                    except Exception:  # noqa: BLE001
                        continue
            for rid, kind, name in picks:
                if (kind, _fold(name)) in kept_targets or _confirmed(name, uses or []):
                    count("taste_said_kept", 1)
                else:
                    gone_rows.append(rid)
            count("taste_said_pairings", _delete_ids(conn, "tastes", "id", gone_rows))
        if _has(conn, "name_aliases", "created_at"):
            cond, params = _in_spans("created_at", spans)
            gone_aliases: list[int] = []
            rows = conn.execute(f"SELECT id, heard_ciphertext, created_at, ended_at FROM name_aliases WHERE {cond}",
                                params).fetchall()
            if rows and uses is None:
                uses = real_uses(conn, dec, now=now)
            for r in rows:
                try:
                    heard = dec(r[1]) if r[1] else None
                except Exception:  # noqa: BLE001
                    heard = None
                if r[3] is None and _confirmed(heard, uses or [], after=float(r[2] or 0.0)):
                    count("name_aliases_kept", 1)
                else:
                    gone_aliases.append(int(r[0]))
            count("name_aliases", _delete_ids(conn, "name_aliases", "id", gone_aliases))
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
    ("moments", "request_id"), ("ui_actions", "run_key"), ("pref_events", "source"),
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
      commitments) that rest on test exchanges only, each with its id, kind and status (``show``: its text too);
      r31: the user's standing rules a test ended, which ``--purge`` reopens (:func:`test_revoked`).

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
        # final-H2 lane D1 (K-P): what --purge takes of the rules kept through remember (why: "session" by its stamp,
        # "span" written in a test's span with no real exchange there)
        rules = test_rules(conn)
        if "batch_id" in _columns(conn, "conversation_facts"):
            items = []
            ids = sorted(rules)
            for i in range(0, len(ids), 400):
                part = ids[i:i + 400]
                for r in conn.execute(f"SELECT id, kind, status, valid_from, text_ciphertext FROM conversation_facts"
                                      f" WHERE id IN ({','.join('?' * len(part))}) ORDER BY id", part):
                    items.append({"id": int(r["id"]), "kind": r["kind"], "status": r["status"],
                                  "at": _when(r["valid_from"]), "why": rules[int(r["id"])],
                                  **({"text": dec(r["text_ciphertext"])} if show else {})})
            derived["remembered_rules"] = items
        # r31 (L4): what --purge reopens of the user's standing rules a test ended (why: "note" = its live-test end
        # note; "span" = revoked in a test's span with no real exchange before it); the notes need the key to read
        if "end_note_ciphertext" in _columns(conn, "conversation_facts"):
            key = cipher or _cipher(db)

            def key_dec(text: Any) -> str | None:
                if key is None or text is None:
                    return None
                try:
                    return str(key.decrypt(text))
                except Exception:  # noqa: BLE001
                    return None

            gone = set(rules) | {int(f["id"]) for f in derived.get("conversation_facts") or ()}
            reopen = test_revoked(conn, key_dec, skip=gone)
            items = []
            for fid in sorted(reopen):
                r = conn.execute("SELECT id, kind, valid_to, text_ciphertext FROM conversation_facts WHERE id = ?",
                                 (fid,)).fetchone()
                if r is not None:
                    items.append({"id": int(r["id"]), "kind": r["kind"], "ended": _when(r["valid_to"]),
                                  "why": reopen[fid], **({"text": dec(r["text_ciphertext"])} if show else {})})
            derived["revoked_rules"] = items
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
              "conversation_facts": "Rules, preferences and commitments resting on test exchanges only",
              "remembered_rules": "Rules kept through remember in a live test's session (why: session = its "
                                  "stamp; span = written in a test's span with no real exchange there)",
              "revoked_rules": "Standing rules of the user's a test ended, which --purge puts back (why: note = its "
                               "live-test end note; span = revoked in a test's span with no real exchange before it)"}
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

        try:
            store = Store.open(args.db)
            try:
                got = store._write(lambda conn: purge(conn, store._dec, now=time.time()))
            finally:
                store.close()
        except Exception as exc:  # noqa: BLE001 - integ30 (lh merge note 4): a failed purge exits non-zero, content-free
            print(json.dumps({"purged": None, "error": type(exc).__name__}, indent=2))
            return 2
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
           "test_turn_ids", "is_test_journal", "is_test_fact", "test_spans", "store_test_spans",
           "sql_outside_spans", "purge", "report", "format_report",
           "main", "real_uses", "CONFIRM_RUN_S", "CONFIRM_DAYS", "known_spans", "test_rules", "REMEMBER_AFTER_S",
           "SPAN_RULE_BEFORE",
           "SESSION_SOURCES", "TEST_REVOKE_NOTE", "DEFAULT_REVOKE_NOTE", "REVOKE_SPAN_BEFORE", "revoke_note",
           "test_revoked"]


if __name__ == "__main__":
    raise SystemExit(main())
