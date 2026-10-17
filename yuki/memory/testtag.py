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
learner's asked occasions and statements, the routine miner's "done today", and the skills' trust ledger (a rung's
trust and disable streak, the offer bars). Nothing is deleted: the rows stay where they are, and :func:`report`
(``yuki-cli -m yuki.memory.testtag``) lists them, read-only, so the user decides whether to purge them. Answers
(memory search, the recall index, the standing rules) are not filtered here: what a test left shows there until the
user purges it.

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
    sql_real_skill_run(alias="") -> str;  sql_real_route_try(alias="") -> str
    test_turn_ids(store) -> frozenset[int]
    is_test_journal(entry, turn_ids) -> bool;  is_test_fact(fact, turn_ids) -> bool
    report(db_path=None, *, show=False) -> dict               # read-only; the database is opened ?mode=ro

Usage::

    yuki-cli -m yuki.memory.testtag [--db PATH] [--json] [--show]
    python -m yuki.memory.testtag [--db PATH] [--json] [--show]

Counts, ids and times only unless ``--show`` (then the derived facts' own text, on this terminal only).
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterable, Mapping
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
    """The store's field cipher from the key next to the database (read only; ``None`` without a key)."""
    from yuki.memory.store import KEY_FILENAME

    key = db.parent / KEY_FILENAME
    if not key.exists():
        return None
    from yuki.memory.crypto import FieldCipher, load_or_create_key

    return FieldCipher(load_or_create_key(key))


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
    lines.append("Every learner now skips these. Purging them is your call; this command never deletes anything.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.testtag",
                                     description="List what the live test harness left in memory (read-only).")
    parser.add_argument("--db", help="the memory database (default: the live one, opened read-only)")
    parser.add_argument("--json", action="store_true", help="print JSON")
    parser.add_argument("--show", action="store_true", help="also print the derived facts' text (this terminal only)")
    args = parser.parse_args(argv)
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
           "sql_real_session", "sql_real_request", "sql_real_skill_run", "sql_real_route_try", "test_turn_ids", "is_test_journal", "is_test_fact", "report",
           "format_report", "main"]


if __name__ == "__main__":
    raise SystemExit(main())
