"""Print today's memory: journal facts, capture health, DB size, model cost.

    uv run python scripts/memory_report.py                 # today, default DB
    uv run python scripts/memory_report.py --date 2026-09-22
    uv run python scripts/memory_report.py --db C:\\path\\memory.db --all

Read-only apart from opening the database (which creates it if missing).
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yuki.memory.store import Store, default_db_path


def _fmt_ms(value: float | None) -> str:
    return "-" if value is None else f"{value:.0f} ms"


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n} B"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", type=Path, default=None, help=f"database path (default {default_db_path()})")
    parser.add_argument("--date", default=None, help="local day YYYY-MM-DD (default today)")
    parser.add_argument("--all", action="store_true", help="every day, not just one")
    parser.add_argument("--min-importance", type=int, default=1)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    day = datetime.strptime(args.date, "%Y-%m-%d") if args.date else datetime.now()
    since = None if args.all else day.replace(hour=0, minute=0, second=0, microsecond=0)
    until = None if args.all else since + timedelta(days=1)
    label = "all time" if args.all else since.strftime("%Y-%m-%d (%A)")

    with Store.open(args.db) as store:
        facts = [f for f in store.journal_between(since, until) if f.importance >= args.min_importance]
        print(f"Yuki memory report - {label}")
        print(f"database: {store.path}  ({_fmt_bytes(store.db_size_bytes())})")
        print()
        print(f"Journal: {len(facts)} facts")
        for f in facts:
            when = datetime.fromtimestamp(f.at).strftime("%H:%M" if not args.all else "%Y-%m-%d %H:%M")
            where = f.app + (f" / {f.host}" if f.host else "")
            print(f"  {when}  [{f.importance:>2}]  {where}: {f.fact}")

        health = store.health_summary(since, until)
        print()
        print(
            f"Capture health: {health['count']} captures, avg {_fmt_ms(health['avg_ms'])}, "
            f"max {_fmt_ms(health['max_ms'])}, {health['chars']} chars"
        )
        if health["by_outcome"]:
            print("  outcomes: " + ", ".join(f"{k}={v}" for k, v in health["by_outcome"].items()))
        for row in health["by_app"]:
            print(f"  {row['app'] or '(unknown)':<28} {row['n']:>6}  avg {_fmt_ms(row['avg_ms'])}  {row['chars'] or 0} chars")

        batches = store.batch_summary(since, until)
        print()
        cost = batches["cost_usd"] or 0.0
        print(
            f"Journal model: {batches['calls']} calls ({batches['failures']} failed), "
            f"{batches['captures']} captures -> {batches['facts']} facts, "
            f"avg {_fmt_ms(batches['avg_latency_ms'])}"
        )
        print(
            f"  tokens: in {batches['input_tokens']}, out {batches['output_tokens']}, "
            f"cache write {batches['cache_write_tokens']}, cache read {batches['cache_read_tokens']}"
        )
        print(f"  cost: ${cost:.4f}   captures still pending: {batches['captures_pending']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
