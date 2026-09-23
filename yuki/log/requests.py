"""Per-request accounting: token counts from a ``usage`` object, and the CSV ledger.

``logs/requests.csv`` holds one line per finished request (final, error or
cancel) so cost and speed can be compared across sessions without opening any
JSONL. It is append-only; the header is written only when the file is created.
Several agents in one process (the UI's two lanes) append to it, so writes go
through one process-wide lock.

``scripts/costs.py`` reads it back.
"""

from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import Any

from yuki.log.events import _as_plain

#: Column order of ``requests.csv``. Readers use the header, so new columns go
#: at the end and old rows stay readable.
REQUEST_CSV_FIELDS: tuple[str, ...] = (
    "timestamp",
    "session",
    "lane",
    "request",
    "model",
    "effort",
    "wall_s",
    "wait_s",
    "model_s",
    "tool_s",
    "calls",
    "tool_calls",
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
    "cost_usd",
    "outcome",
)

#: The token keys shared by ``request_summary``, ``startup_cost`` and the CSV.
TOKEN_KEYS: tuple[str, ...] = (
    "input_tokens",
    "cache_write_tokens",
    "cache_read_tokens",
    "output_tokens",
)

_csv_lock = threading.Lock()


def usage_tokens(usage: Any) -> dict[str, int]:
    """Token counts from one response's ``usage`` (SDK object or dict).

    ``input_tokens`` is the API's uncached input; cache writes and reads are
    reported separately and billed at their own rates.
    """
    data = _as_plain(usage)
    if not isinstance(data, dict):
        data = {}
    return {
        "input_tokens": int(data.get("input_tokens") or 0),
        "cache_write_tokens": int(data.get("cache_creation_input_tokens") or 0),
        "cache_read_tokens": int(data.get("cache_read_input_tokens") or 0),
        "output_tokens": int(data.get("output_tokens") or 0),
    }


def append_request_row(path: Path, row: dict[str, Any]) -> None:
    """Append one request to the CSV, writing the header if the file is new.

    Unknown keys in ``row`` are ignored; missing ones are written empty.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _csv_lock:
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REQUEST_CSV_FIELDS, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({key: _cell(row.get(key)) for key in REQUEST_CSV_FIELDS})


def _cell(value: Any) -> Any:
    """CSV cell for a value: ``None`` empty, dicts as ``name=count`` pairs."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key}={count}" for key, count in value.items())
    return value
