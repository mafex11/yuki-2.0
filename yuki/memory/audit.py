"""Run a portrait audit now ("connect the dots"; maintenance command).

Contract: ``docs/MEMORY.md`` -> "Portrait audit: connect the dots".

The portrait scheduler audits after every run that reads the journal
(:meth:`yuki.memory.portrait.PortraitWorker.audit`). This command asks for one
now: through the ``audit_portrait`` flag when the memory service is running for
the database (its scheduler runs it within a minute, so nothing writes the
database from two processes), else in this process. Nothing is hand-edited: the
audit's operations are validated and applied like any portrait run's, its
questions go through :mod:`yuki.memory.questions`, and its findings are stored
with their reasoning (``portrait_audits``).

Usage::

    python -m yuki.memory.audit [--all | --facts 87,98] [--db PATH] [--log-dir DIR] [--dry-run] [--mode auto|now|flag]

``--all`` audits every current fact (else only the facts new evidence touched since
the last audit; the first audit ever is always whole). ``--dry-run`` shows which facts
would be reviewed and why, and how many calls it would take; no model call.
Costs: one Sonnet call per group of facts with overlapping evidence (~45k characters
of evidence each), about $0.05-0.15 a call.
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from yuki.memory.store import AUDIT_FLAG, Store, flag_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.audit", description=__doc__.split("\n\n")[0])
    parser.add_argument("--all", action="store_true", help="audit every current fact")
    parser.add_argument("--facts", default=None, help="audit exactly these facts, e.g. 87,98 (runs here, no flag)")
    parser.add_argument("--db", default=None, help="the memory database (default: the real one)")
    parser.add_argument("--log-dir", default=None, help="where the model calls are logged (default logs/memory)")
    parser.add_argument("--dry-run", action="store_true", help="show what would be reviewed; no model calls")
    parser.add_argument("--mode", choices=("auto", "now", "flag"), default="auto",
                        help="auto: the audit flag if the memory service is running, else a run here")
    args = parser.parse_args(argv)
    fact_ids = [int(x.strip().lstrip("Ff")) for x in args.facts.split(",") if x.strip()] if args.facts else None
    out = print
    store = Store.open(args.db)
    try:
        from yuki.memory.portrait import PortraitWorker

        worker = PortraitWorker(store, log_dir=Path(args.log_dir) if args.log_dir else None)
        if args.dry_run:
            now = time.time()
            under, reasons, recent = worker.audit_scope(now, full=args.all, fact_ids=fact_ids)
            chunks = worker._audit_chunks(under, worker._audit_evidence(under, recent)) if under else []
            out(f"{len(under)} facts under review, {len(chunks)} call(s), {len(recent)} recent journal entries")
            for f in under:
                out(f"  F{f.id} [{f.kind}] {f.subject}: {reasons.get(f.id)}")
            return 0
        running = False
        if args.mode == "auto":
            try:
                from yuki.memory.api import service_running

                running = service_running(store.path)
            except Exception:
                running = False
        if (args.mode == "flag" or running) and not fact_ids:
            path = flag_path(store.path, AUDIT_FLAG)
            path.write_text("all" if args.all else "new", encoding="utf-8")
            out(f"audit requested ({path.name} flag; the memory service runs it within a minute - it must be "
                "running this version of Yuki)")
            return 0
        result = worker.audit(full=args.all, reason="audit command", fact_ids=fact_ids)
        if result is None:
            out("nothing to review: no fact is new or touched by new evidence since the last audit (use --all)")
            return 0
        stats = result.stats
        out(f"audit run {result.run_id}: {'ok' if result.ok else 'error: ' + str(result.error)}; "
            f"{stats.calls} call(s), ${stats.cost_usd:.3f}, {stats.input_tokens} in / {stats.output_tokens} out tokens")
        for f in result.findings:
            out(f"- {f['finding']} -> {f['decision']} (facts {f['fact_ids']}, new {f['new_fact_ids']}, "
                f"questions {f['question_ids']}): {f['reasoning']}")
        return 0 if result.ok else 1
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
