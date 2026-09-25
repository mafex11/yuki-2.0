"""Hide the secrets already stored in the memory database (maintenance command).

Contract: ``docs/MEMORY.md`` -> "Secret filter".  Every entry point now hides
secrets before storing (:mod:`yuki.memory.secret_filter`); this command runs the
same detector over what was stored before, and replaces each secret with its
typed placeholder in place, re-encrypted with the store's key.

    python -m yuki.memory.scrub_secrets [--db PATH] [--dry-run] [--samples N]

What it reads: every encrypted text column of every table (``*_ciphertext``,
found from the schema, so a table added later is covered too): captures (page
deltas, conversation renderings, call and terminal JSON), the thread's latest
text, messages (each read with the message before it in its thread as
context), thread titles and addresses, journal facts, portrait facts and pages,
open loops, conversation turns, facts and summaries, call chunks, episodes,
questions, audits, reviews, nudges, know-how, timeline titles and paths.  JSON
values are redacted string by string, so they stay valid JSON.  Embedding
vectors are not text and are left alone.

``--dry-run`` writes nothing.  The report gives counts by kind and by column,
and ``--samples`` snippets of the *redacted* text around a placeholder - never
a secret.  Writes are one short transaction per batch, each row updated only
if it did not change since it was read (the memory service may be running);
``secure_delete`` is on, so SQLite zeroes the pages the old values leave.
Capture and thread-text hashes and lengths are recomputed for the new text.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuki.memory import secret_filter
from yuki.memory.store import Store, default_db_path

#: Capture kinds whose ``hash`` is a digest of the delta text (the others hash fingerprints or ids).
_TEXT_HASH_KINDS = ("page", "video", "document", "list", "generic")
#: Rows written per transaction.
BATCH = 200
#: Characters of redacted context shown around a placeholder in a sample.
SNIPPET = 70


@dataclass
class Report:
    rows_read: int = 0
    values_read: int = 0
    rows_changed: int = 0
    by_kind: Counter = field(default_factory=Counter)
    by_column: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    rows_by_column: Counter = field(default_factory=Counter)
    undecryptable: Counter = field(default_factory=Counter)
    conflicts: int = 0
    samples: list[tuple[str, str]] = field(default_factory=list)
    ms: float = 0.0


def text_columns(conn) -> list[tuple[str, list[str]]]:
    """(table, encrypted text columns) for every table that has any."""
    out = []
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                                         " ORDER BY name")]
    for table in tables:
        cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")
                if r[1].endswith("_ciphertext") and r[1] != "vec_ciphertext" and (r[2] or "").upper() != "BLOB"]
        if cols:
            out.append((table, cols))
    return out


def _redact_value(plain: str, context: str) -> tuple[str, Counter]:
    """The value with its secrets hidden: JSON string by string, else as text."""
    head = plain.lstrip()[:1]
    if head in ("{", "["):
        try:
            data = json.loads(plain)
        except ValueError:
            data = None
        if isinstance(data, (dict, list)):
            hidden, counts = secret_filter.redact_json(data)
            return (json.dumps(hidden, ensure_ascii=False) if counts else plain), counts
    r = secret_filter.redact(plain, context=context)
    return r.text, r.counts


def _snippet(text: str) -> str:
    """The redacted text around its first placeholder, on one line."""
    m = secret_filter._PLACEHOLDER_RE.search(text)
    if not m:
        return ""
    s, e = max(0, m.start() - SNIPPET), min(len(text), m.end() + SNIPPET)
    return ("…" if s else "") + " ".join(text[s:e].split()) + ("…" if e < len(text) else "")


def scrub(store: Store, *, dry_run: bool = True, samples: int = 8, log=print) -> Report:
    """Run the detector over every stored encrypted text; redact in place unless ``dry_run``."""
    t0 = time.perf_counter()
    rep = Report()
    conn = store._conn
    if not dry_run:
        conn.execute("PRAGMA secure_delete=ON")
    sampled_columns: set[str] = set()
    for table, cols in text_columns(conn):
        is_messages = table == "messages"
        order = "thread_id, coalesce(at, 0), id" if is_messages else "rowid"
        select = ", ".join(cols) + (", thread_id" if is_messages else "") + (", kind" if table == "captures" else "")
        rows = conn.execute(f"SELECT rowid AS _rowid, {select} FROM {table} ORDER BY {order}").fetchall()
        pending: list[tuple[int, dict[str, tuple[str, str]], dict[str, Any]]] = []
        previous: tuple[Any, str] = (None, "")   # messages: (thread, text of the one before)
        for row in rows:
            rep.rows_read += 1
            changes: dict[str, tuple[str, str]] = {}
            extra: dict[str, Any] = {}
            for col in cols:
                stored = row[col]
                if stored is None:
                    continue
                rep.values_read += 1
                try:
                    plain = store.cipher.decrypt(stored)
                except Exception:
                    rep.undecryptable[f"{table}.{col}"] += 1
                    continue
                context = ""
                if is_messages and col == "text_ciphertext":
                    context = previous[1] if previous[0] == row["thread_id"] else ""
                    previous = (row["thread_id"], plain or "")
                if not plain:
                    continue
                new, counts = _redact_value(plain, context)
                if not counts:
                    continue
                name = f"{table}.{col}"
                rep.by_kind.update(counts)
                rep.by_column[name].update(counts)
                rep.rows_by_column[name] += 1
                if len(rep.samples) < samples and (name not in sampled_columns or len(rep.samples) >= len(sampled_columns)):
                    snip = _snippet(new)
                    if snip:
                        rep.samples.append((name, snip))
                        sampled_columns.add(name)
                changes[col] = (stored, store.cipher.encrypt(new))
                if table == "captures" and col == "delta_ciphertext" and row["kind"] in _TEXT_HASH_KINDS:
                    extra = {"hash": store.cipher.digest(new), "chars": len(new)}
                elif table == "thread_latest" and col == "text_ciphertext":
                    extra = {"hash": store.cipher.digest(new)}
            if changes:
                rep.rows_changed += 1
                pending.append((row["_rowid"], changes, extra))
            if not dry_run and len(pending) >= BATCH:
                rep.conflicts += _apply(conn, table, pending)
                pending = []
        if not dry_run and pending:
            rep.conflicts += _apply(conn, table, pending)
    if not dry_run:
        try:
            conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except Exception:
            pass
    rep.ms = round((time.perf_counter() - t0) * 1000.0, 1)
    return rep


def _apply(conn, table: str, pending: list[tuple[int, dict[str, tuple[str, str]], dict[str, Any]]]) -> int:
    """Write one batch; a row that changed since it was read is left for the next run. Returns conflicts."""
    conflicts = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for rowid, changes, extra in pending:
            sets = [f"{col}=?" for col in changes] + [f"{col}=?" for col in extra]
            where = " AND ".join(f"{col} IS ?" for col in changes)
            params = [new for _, new in changes.values()] + list(extra.values()) + [rowid] + \
                     [old for old, _ in changes.values()]
            cur = conn.execute(f"UPDATE {table} SET {', '.join(sets)} WHERE rowid=? AND {where}", params)
            conflicts += cur.rowcount == 0
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return conflicts


def format_report(rep: Report, *, dry_run: bool, db: Path) -> str:
    lines = [f"{'DRY RUN - nothing written' if dry_run else 'Scrubbed'}: {db}",
             f"read {rep.rows_read} rows / {rep.values_read} encrypted values in {rep.ms / 1000:.1f} s; "
             f"{'would change' if dry_run else 'changed'} {rep.rows_changed} rows"]
    if rep.by_kind:
        lines.append("secrets by kind: " + ", ".join(f"{k} {n}" for k, n in rep.by_kind.most_common()))
        lines.append("by column (rows: secrets by kind):")
        for name in sorted(rep.by_column, key=lambda n: -sum(rep.by_column[n].values())):
            kinds = ", ".join(f"{k} {n}" for k, n in rep.by_column[name].most_common())
            lines.append(f"  {name}: {rep.rows_by_column[name]} rows: {kinds}")
    else:
        lines.append("no secrets found")
    if rep.undecryptable:
        lines.append("could not decrypt (left alone): " + ", ".join(f"{k} {n}" for k, n in rep.undecryptable.items()))
    if rep.conflicts:
        lines.append(f"{rep.conflicts} rows changed while scrubbing and were left alone: run again")
    if rep.samples:
        lines.append("samples (redacted text, secret hidden):")
        lines.extend(f"  [{name}] {snip}" for name, snip in rep.samples)
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    doc = (__doc__ or "").split("\n\n")[0]
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.scrub_secrets", description=doc)
    parser.add_argument("--db", default=None, help="the memory database (default: the real one)")
    parser.add_argument("--dry-run", action="store_true", help="report what would be hidden; write nothing")
    parser.add_argument("--samples", type=int, default=8, help="redacted snippets to show (default 8; 0: none)")
    args = parser.parse_args(argv)
    db = Path(args.db) if args.db else default_db_path()
    if not db.exists():
        print(f"no database at {db}", file=sys.stderr)
        return 2
    store = Store.open(db)
    try:
        rep = scrub(store, dry_run=args.dry_run, samples=max(0, args.samples))
    finally:
        store.close()
    out = format_report(rep, dry_run=args.dry_run, db=db)
    sys.stdout.buffer.write((out + "\n").encode("utf-8", "replace"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
