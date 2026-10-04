"""Shared plumbing for the live-check log readers (report 73).

Read-only. Every reader prints only event names, counts, timings, numbers, booleans and short code-controlled
tokens (tool names, app exe names from a short list, exception class names). It never prints message text,
names, titles, window content, requests, replies or error messages: privacy rule for anything written about the
user's PC.

Usage pattern (each reader):
    python scripts/live/<reader>.py --since 18:00          # today at 18:00
    python scripts/live/<reader>.py --since "2026-10-04 18:00"
    python scripts/live/<reader>.py --since 45m            # the last 45 minutes (also 2h)
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

#: App exe names a reader may print (lower case, without ``.exe``); anything else prints as ``other``.
KNOWN_APPS = frozenset({
    "spotify", "discord", "arc", "chrome", "msedge", "firefox", "t3 code", "t3code", "t3-code", "notepad",
    "explorer", "yuki", "pythonw", "python", "warp", "code", "slack", "whatsapp", "applicationframehost",
})


def log_dir() -> Path:
    """Yuki's logs: the installed app's ``%LOCALAPPDATA%\\Yuki\\logs`` (the dev tree's ``logs`` as a fallback)."""
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "Yuki" / "logs"
    if local.is_dir():
        return local
    return Path(__file__).resolve().parents[2] / "logs"


def parse_since(text: str) -> float:
    """``HH:MM`` (today), ``YYYY-MM-DD HH:MM[:SS]``, an ISO time, or ``<n>m`` / ``<n>h`` ago -> epoch seconds."""
    text = text.strip()
    m = re.fullmatch(r"-?(\d+(?:\.\d+)?)([mh])", text)
    if m:
        n = float(m.group(1)) * (60 if m.group(2) == "m" else 3600)
        return time.time() - n
    m = re.fullmatch(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", text)
    if m:
        now = datetime.now()
        at = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3) or 0), microsecond=0)
        return at.timestamp()
    return datetime.fromisoformat(text.replace(" ", "T")).timestamp()


def arg_parser(doc: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=doc.strip().splitlines()[0])
    p.add_argument("--since", required=True, help="HH:MM today, 'YYYY-MM-DD HH:MM', ISO, or 45m / 2h ago")
    p.add_argument("--until", default=None, help="same forms as --since (default: now)")
    return p


def window(args: argparse.Namespace) -> tuple[float, float]:
    since = parse_since(args.since)
    until = parse_since(args.until) if getattr(args, "until", None) else time.time() + 60
    return since, until


def hhmm(ts: float | None) -> str:
    return datetime.fromtimestamp(ts).strftime("%H:%M:%S") if isinstance(ts, (int, float)) else "--:--:--"


def record_ts(r: dict[str, Any]) -> float | None:
    ts = r.get("ts")
    if isinstance(ts, (int, float)):
        return float(ts)
    for key in ("ts", "at", "timestamp"):
        v = r.get(key)
        if isinstance(v, str):
            try:
                return datetime.fromisoformat(v).timestamp()
            except ValueError:
                continue
    return None


def files(pattern: str, since: float, base: Path | None = None) -> list[Path]:
    """Files under the log dir matching ``pattern`` written to at or after ``since`` (oldest first)."""
    root = base or log_dir()
    out = [p for p in root.glob(pattern) if p.is_file() and p.stat().st_mtime >= since - 1]
    return sorted(out, key=lambda p: p.stat().st_mtime)


def jsonl(paths: list[Path], since: float, until: float) -> Iterator[dict[str, Any]]:
    """Records with a time in ``[since, until)`` from JSONL files (bad lines skipped)."""
    for path in paths:
        try:
            fh = path.open(encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                try:
                    r = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(r, dict):
                    continue
                ts = record_ts(r)
                if ts is None or ts < since or ts >= until:
                    continue
                r["_ts"] = ts
                r["_file"] = path.name
                yield r


def csv_rows(path: Path, since: float, until: float, time_col: str = "timestamp") -> list[dict[str, str]]:
    if not path.is_file():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                ts = datetime.fromisoformat(str(row.get(time_col) or "")).timestamp()
            except ValueError:
                continue
            if since <= ts < until:
                row["_ts"] = ts   # type: ignore[assignment]
                out.append(row)
    return out


_ERR_TOKENS = re.compile(r"\b([A-Z][A-Za-z]*(?:Error|Exception|Timeout|Unavailable|Refused|Denied)|1\d{3}|"
                         r"NET-\d{4}|0x[0-9a-fA-F]{8})\b")


def error_class(value: Any) -> str:
    """A content-free label for an error string: exception class names and codes only (else ``unclassified``)."""
    if value in (None, "", False):
        return ""
    tokens = []
    for t in _ERR_TOKENS.findall(str(value)):
        if t not in tokens:
            tokens.append(t)
    return "+".join(tokens[:3]) or "unclassified"


def app_name(process_name: Any) -> str:
    name = str(process_name or "").lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name if name in KNOWN_APPS else ("" if not name else "other")


def num(v: Any, nd: int = 0) -> str:
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return "-"
    return f"{v:.{nd}f}"


def stats(values: list[float]) -> str:
    vals = sorted(v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool))
    if not vals:
        return "n=0"
    mid = vals[len(vals) // 2]
    return f"n={len(vals)} min={vals[0]:.0f} median={mid:.0f} max={vals[-1]:.0f}"


def verdict(label: str, ok: bool | None, detail: str = "") -> None:
    """One pass/fail line: PASS, FAIL, or NOT RUN (``ok is None``: no evidence in the window)."""
    mark = "NOT RUN" if ok is None else ("PASS" if ok else "FAIL")
    print(f"  [{mark:7}] {label}" + (f"  ({detail})" if detail else ""))


def header(title: str, since: float, until: float) -> None:
    end = "now" if until > time.time() else hhmm(until)
    where = r"%LOCALAPPDATA%\Yuki\logs" if "AppData" in str(log_dir()) else "repo logs"   # no user name printed
    print(f"== {title}  window {datetime.fromtimestamp(since):%Y-%m-%d %H:%M:%S} -> {end}  logs {where}")


def session_records(since: float, until: float, which: str = "*") -> list[dict[str, Any]]:
    """Agent session records (``sessions/*.jsonl``), ``which`` a glob suffix: ``*-worker``, ``*-window``, ..."""
    pats = {"worker": "sessions/*-worker.jsonl", "window": "sessions/*-window.jsonl",
            "frontdesk": "sessions/*-frontdesk.jsonl", "ui": "sessions/*[0-9].jsonl", "*": "sessions/*.jsonl"}
    return list(jsonl(files(pats.get(which, which), since), since, until))


if sys.stdout.encoding and sys.stdout.encoding.lower() not in ("utf-8", "utf8"):
    try:
        sys.stdout.reconfigure(encoding="utf-8")   # type: ignore[attr-defined]
    except Exception:
        pass

