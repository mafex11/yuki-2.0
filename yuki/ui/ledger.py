"""What Yuki did, as rows: the request log and the cards. No Qt, no memory client of its own.

Used by the tray's window ops (:mod:`yuki.ui.window_host`, ``activity``, ``plan``, ``card_feedback``) and by the HTML
window's own process (:mod:`yuki.ui.web_data`), which must not load Qt. ``read_request_log`` and ``card_row`` moved
here from ``yuki.ui.window_host`` unchanged (Phase E3, 2026-10-03); window_host re-exports them.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime
from pathlib import Path
from typing import Any

from yuki.ui.cards import card_offer, card_title, epoch_of, offer_lines, offer_open

#: A finished request's ``outcome`` (``requests.csv``, :mod:`yuki.agent.loop`) as the ledger states it.
REQUEST_STATES = {"final": "done", "error": "failed", "cancelled": "stopped", "withdrawn": "replaced",
                  "abandoned": "abandoned"}
#: Bytes read from the end of ``requests.csv`` (thousands of rows; the file only grows).
REQUEST_LOG_TAIL = 512 * 1024


def read_request_log(path: Path, since: float | None = None, limit: int = 50,
                     tail_bytes: int = REQUEST_LOG_TAIL) -> list[dict[str, Any]]:
    """The newest finished requests from ``requests.csv`` (written by the agent, one row per request), newest
    first, as ledger rows: ``{"kind": "request", "at", "what", "where", "state", "outcome", "wall_s", "cost_usd",
    "tools", "model", "session"}``. Reads only the file's tail; a missing or broken file gives ``[]``."""
    try:
        with open(path, "rb") as f:
            header = f.readline().decode("utf-8", "replace")
            size = f.seek(0, 2)
            start = max(len(header.encode("utf-8")), size - tail_bytes)
            f.seek(start)
            body = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    if start > len(header.encode("utf-8")):
        body = body.split("\n", 1)[1] if "\n" in body else ""   # the first line read is a partial one

    def num(row: dict[str, Any], key: str) -> float | None:
        try:
            return float(row.get(key) or "")
        except ValueError:
            return None

    rows: list[dict[str, Any]] = []
    for row in csv.DictReader(io.StringIO(header + body)):
        try:
            at = datetime.fromisoformat(str(row.get("timestamp") or "")).timestamp()
        except ValueError:
            continue
        if since is not None and at < since:
            continue
        tools: dict[str, int] = {}
        for pair in str(row.get("tool_calls") or "").split():   # "name=count name=count": our own fixed format
            name, _, count = pair.partition("=")
            if name and count.isdigit():
                tools[name] = int(count)
        outcome = str(row.get("outcome") or "")
        rows.append({"kind": "request", "at": at, "what": row.get("request") or "", "where": row.get("lane") or "",
                     "state": REQUEST_STATES.get(outcome, outcome or "unknown"), "outcome": outcome,
                     "wall_s": num(row, "wall_s"), "cost_usd": num(row, "cost_usd"), "tools": tools,
                     "model": row.get("model") or "", "session": row.get("session") or ""})
    rows.sort(key=lambda r: r["at"], reverse=True)
    return rows[:max(0, int(limit))]


def card_row(nudge: dict[str, Any]) -> dict[str, Any]:
    """A card (nudge) as the window's ledger and inbox show it: what it said, its offer and what became of it."""
    offer = card_offer(nudge)
    at = nudge.get("_seen_at") or nudge.get("shown_at") or nudge.get("at")
    return {"kind": "card", "card": nudge.get("id"), "at": at, "card_kind": nudge.get("kind"),
            "title": card_title(nudge), "text": nudge.get("text") or "", "why": nudge.get("reason") or "",
            "reaction": nudge.get("reaction"), "outcome": nudge.get("outcome") or nudge.get("_offer_state"),
            "offer": ({k: offer.get(k) for k in ("do", "label", "steps", "wont", "expires_at")} if offer else None),
            "lines": offer_lines(nudge), "open": offer_open(nudge), "missed": bool(nudge.get("missed"))}


def at_epoch(value: Any) -> float:
    """A row's ``at`` as epoch seconds (0 when unreadable), for sorting."""
    return epoch_of(value) or 0.0


__all__ = ["REQUEST_STATES", "REQUEST_LOG_TAIL", "read_request_log", "card_row", "at_epoch"]
