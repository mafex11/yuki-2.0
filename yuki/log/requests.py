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

from yuki.log.events import _as_plain, content_kept
from yuki.memory.secret_filter import redact_text

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
    # phase F (fPrivacy): the request's id (its session log's records, the acting marker, the memory log and the
    # moments carry the same one: ``python -m yuki.log.follow <request_id>``), and phase S's skill facts, all code
    # words or numbers: the skill and route that ran, the done-check's and the re-check's outcome, and how many
    # moments by Yuki followed within 10 s
    "request_id",
    "skill",
    "route",
    "done_check",
    "recheck",
    "moments_after",
    # the brain rebuild (brain-design.md 8, 10.1): a request the new brain ran, code words and numbers only (empty
    # on the old loop's rows): the plan's kind (act / answer / ask), the steps handed to the executor, the replans,
    # the done-check's probe, the outcome level, ms from the call to the plan's header, model calls, commit checks
    "plan_kind",
    "plan_steps",
    "replans",
    "probe",
    "verdict",
    "plan_ms",
    "plan_calls",
    "check_calls",
)
#: Cells that must be one code word (a skill key, a route, a check's outcome) or a number; anything else is left empty
#: (never a sentence or a name in the ledger).
CODE_COLUMNS: tuple[str, ...] = ("request_id", "skill", "route", "done_check", "recheck", "plan_kind", "probe",
                                 "verdict")

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


def append_request_row(path: Path, row: dict[str, Any], *, content: bool | None = None) -> None:
    """Append one request to the CSV, writing the header if the file is new.

    Unknown keys in ``row`` are ignored; missing ones are written empty. ``content`` (default
    :func:`yuki.log.events.content_kept`, off): keep the request's words in the ``request`` column; off, the column
    is left empty (report 76: the ledger kept the raw request text outside the encrypted database). The words stay in
    memory (the conversation turns, encrypted); the window's ledger shows a row without them as "A request".
    """
    if not (content_kept() if content is None else content):
        row = {**row, "request": ""}
    row = {**row, **{k: (row.get(k) if _code_cell(row.get(k)) else None) for k in CODE_COLUMNS if k in row}}
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _csv_lock:
        if path.exists() and path.stat().st_size > 0:
            _upgrade_header(path)
        new = not path.exists() or path.stat().st_size == 0
        with path.open("a", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=REQUEST_CSV_FIELDS, extrasaction="ignore")
            if new:
                writer.writeheader()
            writer.writerow({key: _cell(row.get(key)) for key in REQUEST_CSV_FIELDS})


def _code_cell(value: Any) -> bool:
    """A cell of :data:`CODE_COLUMNS`: empty, a number, or one code word / id (letters, digits, ``_ . : + -``)."""
    if value is None or isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return (isinstance(value, str) and 0 < len(value) <= 64 and value.isascii()
            and all(c.isalnum() or c in "_.:+-" for c in value))


def _upgrade_header(path: Path) -> None:
    """A ledger written before columns were added keeps its rows: its header line is rewritten to today's (old rows
    simply have the new columns empty). A header that is not an older form of today's is left alone and the file
    moved aside (``requests-<time>.csv``), so no row is ever read under the wrong column."""
    import os
    import time

    try:
        with path.open(encoding="utf-8", newline="") as fh:
            first = fh.readline()
            if not first:
                return
            header = next(csv.reader([first]))
            if tuple(header) == REQUEST_CSV_FIELDS:
                return
            rest = fh.read()
    except OSError:
        return
    if tuple(header) == REQUEST_CSV_FIELDS[:len(header)]:
        tmp = path.with_name(path.name + ".upgrade.tmp")
        with tmp.open("w", encoding="utf-8", newline="") as out:
            csv.writer(out).writerow(REQUEST_CSV_FIELDS)
            out.write(rest)
        os.replace(tmp, path)
    else:
        os.replace(path, path.with_name(f"{path.stem}-{time.strftime('%Y%m%d-%H%M%S')}{path.suffix}"))


def _cell(value: Any) -> Any:
    """CSV cell for a value: ``None`` empty, dicts as ``name=count`` pairs."""
    if value is None:
        return ""
    if isinstance(value, dict):
        return " ".join(f"{key}={count}" for key, count in value.items())
    if isinstance(value, str):
        return redact_text(value)  # the request column is the user's words: no secret in the ledger
    return value
