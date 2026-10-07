"""The cost of Record how I work, from the on/off hours (report 78 §9; report 96, acceptance tool a).

With ``[trace] ab_hours = true`` in the privacy file, the memory service records in even hours and not in odd ones,
samples the CPU of the app in front and of itself every 10 seconds, and logs a ``trace_ab`` line every 10 minutes
(app process keys and numbers only). This reads those lines from ``logs\\memory-*.jsonl`` and prints, per app, the
mean CPU share in on hours against off hours, plus the memory service's own CPU and RAM. Two days of it is the §9
check: no measurable rise in the apps' CPU, at most 0.3 points of total CPU and 30 MB more for the service.

Usage::

    python scripts/trace_ab_report.py [--logs DIR] [--days 2] [--json]
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def collect(logs: Path, since: float) -> dict[str, dict[str, list[tuple[float, int]]]]:
    """``{app: {"on"|"off": [(mean, n), ...]}}`` from every ``trace_ab`` line since ``since``."""
    out: dict[str, dict[str, list[tuple[float, int]]]] = defaultdict(lambda: defaultdict(list))
    for path in sorted(logs.glob("memory-*.jsonl")):
        try:
            if path.stat().st_mtime < since:
                continue
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            if '"trace_ab"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("type") != "trace_ab" or float(rec.get("ts") or 0) < since:
                continue
            for key, value in (rec.get("samples") or {}).items():
                mode, _, app = key.partition(":")
                if isinstance(value, dict) and mode in ("on", "off"):
                    out[app][mode].append((float(value.get("mean") or 0.0), int(value.get("n") or 0)))
    return out


def weighted(values: list[tuple[float, int]]) -> tuple[float | None, int]:
    n = sum(k for _v, k in values)
    if not n:
        return None, 0
    return sum(v * k for v, k in values) / n, n


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--logs", type=Path, default=None)
    p.add_argument("--days", type=float, default=2.0)
    p.add_argument("--json", action="store_true")
    args = p.parse_args(argv)
    logs = args.logs
    if logs is None:
        from yuki.core.paths import data_dir, logs_dir

        logs = next((f for f in (data_dir() / "logs", logs_dir()) if f.is_dir() and any(f.glob("memory-*.jsonl"))),
                    None)
    if logs is None or not any(Path(logs).glob("memory-*.jsonl")):
        print(f"No memory-*.jsonl logs found in {logs or 'the usual folders'}; pass --logs.", file=sys.stderr)
        return 2
    print(f"Logs read from {logs}.", file=sys.stderr)
    data = collect(logs, time.time() - args.days * 86400.0)
    table = {}
    for app, modes in sorted(data.items()):
        on, n_on = weighted(modes.get("on", []))
        off, n_off = weighted(modes.get("off", []))
        table[app] = {"on": None if on is None else round(on, 3), "off": None if off is None else round(off, 3),
                      "samples_on": n_on, "samples_off": n_off,
                      "rise": None if on is None or off is None else round(on - off, 3)}
    if args.json:
        print(json.dumps(table, indent=2))
        return 0
    if not table:
        print("No trace_ab lines yet: set [trace] ab_hours = true and leave it on for two days.")
        return 0
    print(f"{'app':<22} {'on %':>8} {'off %':>8} {'rise':>8} {'samples on/off':>16}")
    for app, row in table.items():
        label = {"__service__": "memory service CPU", "__rss_mb__": "memory service MB"}.get(app, app)
        print(f"{label:<22} {row['on'] if row['on'] is not None else '-':>8} "
              f"{row['off'] if row['off'] is not None else '-':>8} {row['rise'] if row['rise'] is not None else '-':>8} "
              f"{row['samples_on']:>7}/{row['samples_off']:<8}")
    service = table.get("__service__", {})
    rss = table.get("__rss_mb__", {})
    if service.get("rise") is not None:
        print(f"\nService CPU rise: {service['rise']} points of total CPU (budget 0.3).")
    if rss.get("rise") is not None:
        print(f"Service RAM rise: {rss['rise']} MB (budget 30).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
