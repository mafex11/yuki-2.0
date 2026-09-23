"""Summarise ``logs/requests.csv``: what Yuki's requests cost and how long they took.

Prints, as plain text tables:

* today's totals and all-time totals,
* per-model averages (cost, wall time, model calls per request),
* the 5 slowest and the 5 most expensive requests.

Costs are the estimates written at request time from ``Settings.pricing``
(Anthropic list prices unless overridden), not a bill. Reads only the CSV;
touches nothing else.

Usage::

    uv run python scripts/costs.py [path/to/requests.csv]
"""

from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Sequence

DEFAULT_CSV = Path(__file__).resolve().parent.parent / "logs" / "requests.csv"

TOP_N = 5
REQUEST_WIDTH = 48


def load(path: Path) -> list[dict[str, Any]]:
    """Read the CSV into rows with numeric fields parsed (blank -> ``None``)."""
    with path.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    for row in rows:
        for key in ("wall_s", "wait_s", "model_s", "tool_s", "cost_usd"):
            row[key] = _float(row.get(key))
        for key in ("calls", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens"):
            value = _float(row.get(key))
            row[key] = int(value) if value is not None else 0
    return rows


def _float(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def totals(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Sums over a set of requests."""
    priced = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    return {
        "requests": len(rows),
        "cost": sum(priced),
        "unpriced": len(rows) - len(priced),
        "wall": sum(r["wall_s"] or 0.0 for r in rows),
        "model": sum(r["model_s"] or 0.0 for r in rows),
        "tool": sum(r["tool_s"] or 0.0 for r in rows),
        "calls": sum(r["calls"] for r in rows),
        "input": sum(r["input_tokens"] for r in rows),
        "cache_write": sum(r["cache_write_tokens"] for r in rows),
        "cache_read": sum(r["cache_read_tokens"] for r in rows),
        "output": sum(r["output_tokens"] for r in rows),
    }


def table(headers: Sequence[str], body: Iterable[Sequence[Any]], *, right: set[int] | None = None) -> str:
    """Plain text table; columns in ``right`` are right-aligned."""
    right = right or set()
    lines = [[str(h) for h in headers]] + [[str(c) for c in row] for row in body]
    widths = [max(len(line[i]) for line in lines) for i in range(len(headers))]

    def fmt(line: list[str]) -> str:
        return "  ".join(
            cell.rjust(widths[i]) if i in right else cell.ljust(widths[i]) for i, cell in enumerate(line)
        ).rstrip()

    out = [fmt(lines[0]), "  ".join("-" * w for w in widths)]
    out += [fmt(line) for line in lines[1:]]
    return "\n".join(out)


def money(value: float | None) -> str:
    return "-" if value is None else f"${value:.4f}"


def secs(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}"


def short(text: str, width: int = REQUEST_WIDTH) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= width else text[: width - 3] + "..."


def model_name(model_id: str) -> str:
    return "+".join(part.split(".")[-1] for part in (model_id or "?").split("+"))


def totals_section(rows: list[dict[str, Any]]) -> str:
    today = date.today().isoformat()
    groups = [
        (f"today ({today})", [r for r in rows if (r.get("timestamp") or "").startswith(today)]),
        ("all time", rows),
    ]
    body = []
    for label, group in groups:
        t = totals(group)
        body.append([
            label,
            t["requests"],
            money(t["cost"]) + (f" (+{t['unpriced']} unpriced)" if t["unpriced"] else ""),
            secs(t["wall"]),
            secs(t["model"]),
            secs(t["tool"]),
            t["calls"],
            t["input"],
            t["cache_write"],
            t["cache_read"],
            t["output"],
        ])
    return table(
        ["", "requests", "cost", "wall s", "model s", "tool s", "calls",
         "in", "cache wr", "cache rd", "out"],
        body,
        right=set(range(1, 11)),
    )


def per_model_section(rows: list[dict[str, Any]]) -> str:
    models: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        models.setdefault(model_name(row.get("model") or "?"), []).append(row)
    body = []
    for name, group in sorted(models.items()):
        priced = [r["cost_usd"] for r in group if r["cost_usd"] is not None]
        n = len(group)
        body.append([
            name,
            n,
            money(sum(priced) / len(priced)) if priced else "-",
            secs(sum(r["wall_s"] or 0.0 for r in group) / n),
            f"{sum(r['calls'] for r in group) / n:.1f}",
            money(sum(priced)) if priced else "-",
        ])
    return table(
        ["model", "requests", "avg cost", "avg wall s", "avg calls", "total cost"],
        body,
        right={1, 2, 3, 4, 5},
    )


def top_section(rows: list[dict[str, Any]], key: str) -> str:
    ranked = sorted(
        (r for r in rows if r[key] is not None), key=lambda r: r[key], reverse=True
    )[:TOP_N]
    body = [
        [
            (r.get("timestamp") or "")[:16].replace("T", " "),
            model_name(r.get("model") or "?"),
            r.get("effort") or "",
            secs(r["wall_s"]),
            r["calls"],
            money(r["cost_usd"]),
            r.get("outcome") or "",
            short(r.get("request") or ""),
        ]
        for r in ranked
    ]
    return table(
        ["when", "model", "effort", "wall s", "calls", "cost", "outcome", "request"],
        body,
        right={3, 4, 5},
    )


def main(argv: Sequence[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    args = list(sys.argv[1:] if argv is None else argv)
    path = Path(args[0]) if args else DEFAULT_CSV
    if not path.exists():
        print(f"no request log at {path}")
        return 1
    rows = load(path)
    if not rows:
        print(f"{path} has no requests yet")
        return 0
    sections = [
        ("Totals", totals_section(rows)),
        ("Per model (averages per request)", per_model_section(rows)),
        (f"{TOP_N} slowest requests (wall time)", top_section(rows, "wall_s")),
        (f"{TOP_N} most expensive requests", top_section(rows, "cost_usd")),
    ]
    print(f"{path}  ({len(rows)} requests; costs are list-price estimates)\n")
    for title, body in sections:
        print(title)
        print(body)
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
