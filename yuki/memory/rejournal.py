"""Re-journal stored captures with the current journal prompts (maintenance command).

Contract: ``docs/MEMORY.md`` -> "Personality: what is true and what is a joke" (re-journal).

When the journal's prompts improve (natures: fact, trait, story, humor,
uncertain - :mod:`yuki.memory.personality`), what memory already wrote from
captures that are still stored (captures are kept 30 days; a call's words are a
capture of kind ``my_call_side``) can be read again. Nothing is hand-edited: the
same pipeline journals the same captures again.

For each thread the selected captures belong to, the captures are journaled again
by :meth:`yuki.memory.journal.JournalWorker.rejournal`, batch by batch. In each
new batch's transaction the facts the earlier batches of those captures wrote are
superseded (``journal.superseded_at`` / ``superseded_by`` = the new batch; the
new batch's ``supersedes`` lists the batches it replaced): they stay in the
database as history, and every reader leaves them out. A call's commitments stay
on their own to-do path (not written twice). A reading that finds no facts where
the earlier one had some writes nothing; it is read once more, and if that finds
nothing either the earlier facts are kept (Haiku answered a noisy 13k-character
call with empty lists in 2 of 6 readings on 2026-09-25). A batch whose captures are partly
gone (past the TTL) is left alone. Then a portrait refresh is triggered: through
the ``refresh_portrait`` flag when the memory service is running for this
database (its scheduler runs it within a minute), else in this process.

Usage::

    python -m yuki.memory.rejournal --thread-kind my_call_side --since 2026-09-24 [--until 2026-09-26]
    python -m yuki.memory.rejournal --batch 1087,1955
        [--db PATH] [--log-dir DIR] [--dry-run] [--portrait auto|now|flag|skip]

``--dry-run`` lists what would be re-journaled (and the facts it would replace) and
calls no model. Costs: one Haiku call per ~12k characters of captures (a 30-minute
call is ~5 calls, ~$0.01 each), plus the portrait refresh (Sonnet, ~$0.1-0.3).
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from yuki.memory.store import REFRESH_FLAG, PendingCapture, Store, flag_path


def _when(value: str | None) -> float | None:
    """A ``--since``/``--until`` value (ISO date or date-time, local time) as epoch seconds."""
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


def _line(at: float, text: str, nature: str) -> str:
    return f"  {datetime.fromtimestamp(at):%m-%d %H:%M} [{nature}] {text}"


def plan(store: Store, *, thread_kind: str | None, since: float | None, until: float | None,
         batches: Sequence[int] = ()) -> tuple[dict[int, list[tuple[PendingCapture, int]]], list[str]]:
    """``({thread id: [(capture, its batch)]}, notes)``: the captures to journal again, whole batches only.

    ``batches``: the journal batches to read again (``--batch``), instead of a kind or time window.
    """
    if batches:
        batches = {int(b) for b in batches}
    else:
        batches = {b for _, b in store.journaled_captures(thread_kind=thread_kind, since=since, until=until)}
    # every capture of those batches (a batch is replaced whole), and only batches whose captures all survive
    whole = store.journaled_captures(batch_ids=batches)
    found: dict[int, int] = defaultdict(int)
    for _, b in whole:
        found[b] += 1
    expected = {int(r["id"]): int(r["capture_count"] or 0) for r in store._query(
        "SELECT id, capture_count FROM journal_batches WHERE id IN (%s)" % ",".join("?" * len(batches)),
        sorted(batches))} if batches else {}
    notes: list[str] = []
    keep = set()
    for b in sorted(batches):
        if found[b] < expected.get(b, 0):
            notes.append(f"batch {b}: {expected[b] - found[b]} of its {expected[b]} captures are gone (past the "
                         f"30-day TTL); left as it is")
        else:
            keep.add(b)
    by_thread: dict[int, list[tuple[PendingCapture, int]]] = defaultdict(list)
    for cap, b in whole:
        if b in keep:
            by_thread[cap.thread_id].append((cap, b))
    return dict(by_thread), notes


def run(store: Store, *, thread_kind: str | None, since: float | None, until: float | None,
        batches: Sequence[int] = (), dry_run: bool = False, portrait: str = "auto", log_dir: Path | None = None,
        client: Any = None, portrait_client: Any = None, out=print) -> dict[str, Any]:
    """Re-journal and report (``out`` gets the human-readable lines). Returns a content-free summary."""
    from yuki.memory.journal import EMPTY_REJOURNAL, JournalWorker

    groups, notes = plan(store, thread_kind=thread_kind, since=since, until=until, batches=batches)
    for n in notes:
        out(f"note: {n}")
    summary: dict[str, Any] = {"threads": len(groups), "captures": sum(len(v) for v in groups.values()),
                               "batches_replaced": 0, "facts_before": 0, "facts_after": 0, "cost_usd": 0.0,
                               "failed": [], "portrait": None}
    if not groups:
        out("Nothing to re-journal: no stored, journaled captures match.")
        return summary
    worker = JournalWorker(store, client=client, log_dir=log_dir)
    for thread_id, items in groups.items():
        info = store.thread_info(thread_id)
        old_batches = sorted({b for _, b in items})
        before = store.journal_of_batches(old_batches)
        summary["facts_before"] += len(before)
        out(f"\nthread {thread_id} ({info.app if info else '?'}, {info.kind if info else '?'}): "
            f"{len(items)} captures, batches {old_batches}")
        out(" BEFORE:")
        for j in before:
            out(_line(j.at, j.fact, j.nature))
        if not before:
            out("  (no facts)")
        if dry_run:
            continue
        captures = [c for c, _ in items]
        batch_of = {c.id: b for c, b in items}
        out(" AFTER:")
        for _tid, caps in worker._batches(captures):
            result = worker.rejournal(thread_id, caps, sorted({batch_of[c.id] for c in caps}))
            summary["cost_usd"] += result.cost_usd or 0.0
            if result.error == EMPTY_REJOURNAL:   # one more reading before keeping the earlier facts
                out("  (no facts on this reading; reading it once more)")
                result = worker.rejournal(thread_id, caps, sorted({batch_of[c.id] for c in caps}))
                summary["cost_usd"] += result.cost_usd or 0.0
            if result.error == EMPTY_REJOURNAL:
                summary["kept"] = summary.get("kept", 0) + 1
                out("  no facts found twice: the earlier facts above are kept as they are")
                continue
            if not result.ok:
                summary["failed"].append({"thread": thread_id, "captures": [c.id for c in caps], "error": result.error})
                out(f"  FAILED ({result.error}); these captures keep their earlier facts")
                continue
            summary["batches_replaced"] += len({batch_of[c.id] for c in caps})
            summary["facts_after"] += len(result.facts)
            for fact, nature in zip(result.facts, result.natures):
                out(f"  [{nature}] {fact}")
    if dry_run:
        out("\n(dry run: nothing was changed, no model was called)")
        return summary
    summary["portrait"] = refresh_portrait(store, mode=portrait, log_dir=log_dir, client=portrait_client, out=out)
    out(f"\nre-journal cost ${summary['cost_usd']:.4f}")
    return summary


def refresh_portrait(store: Store, *, mode: str = "auto", log_dir: Path | None = None, client: Any = None,
                     out=print) -> dict[str, Any] | None:
    """Trigger the portrait refresh: the flag for a running service, else a run here (``mode`` auto)."""
    if mode == "skip":
        return None
    running = False
    if mode == "auto":
        try:
            from yuki.memory.api import service_running

            running = service_running(store.path)
        except Exception:
            running = False
    if mode == "flag" or (mode == "auto" and running):
        path = flag_path(store.path, REFRESH_FLAG)
        path.write_text(f"{time.time()}\n", encoding="utf-8")
        out(f"portrait: refresh requested ({path.name} flag; the memory service runs it within a minute)")
        return {"mode": "flag"}
    from yuki.memory.portrait import PortraitWorker

    result = PortraitWorker(store, client=client, log_dir=log_dir).run("refresh")
    stats = result.stats
    out(f"portrait: refresh run {'ok' if result.ok else 'FAILED: ' + str(result.error)}, "
        f"{result.journal_facts} journal facts, ${(stats.cost_usd if stats else 0):.4f}")
    return {"mode": "now", "ok": result.ok, "run_id": result.run_id,
            "cost_usd": stats.cost_usd if stats else 0.0, "portrait": result.portrait}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.rejournal", description=__doc__.split("\n\n")[0])
    parser.add_argument("--thread-kind", default=None, help="threads.kind to re-journal (e.g. my_call_side)")
    parser.add_argument("--since", default=None, help="captures at or after this local date/time (ISO)")
    parser.add_argument("--until", default=None, help="captures before this local date/time (ISO)")
    parser.add_argument("--batch", default="", help="journal batch ids to read again, comma-separated (e.g. 1087,1955)")
    parser.add_argument("--db", default=None, help="the memory database (default: the real one)")
    parser.add_argument("--log-dir", default=None, help="where the model calls are logged (default logs/memory)")
    parser.add_argument("--dry-run", action="store_true", help="list what would change; no model calls")
    parser.add_argument("--portrait", choices=("auto", "now", "flag", "skip"), default="auto",
                        help="auto: the refresh flag if the memory service is running, else a run here")
    args = parser.parse_args(argv)
    batches = [int(b) for b in args.batch.replace(" ", "").split(",") if b]
    if not (args.thread_kind or args.since or batches):
        parser.error("give --batch, --thread-kind and/or --since (re-journaling everything is never the default)")
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    with Store.open(args.db) as store:
        summary = run(store, thread_kind=args.thread_kind, since=_when(args.since), until=_when(args.until),
                      batches=batches,
                      dry_run=args.dry_run, portrait=args.portrait,
                      log_dir=Path(args.log_dir) if args.log_dir else None)
    print({k: v for k, v in summary.items() if k != "portrait"})
    return 1 if summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
