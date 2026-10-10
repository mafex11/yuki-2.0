"""Follow one request across Yuki's logs, content-free (phase F, fPrivacy; plan item F6).

    python -m yuki.log.follow <request_id> [--logs DIR] [--db PATH] [--json] [--pad S]

One ``request_id`` follows a request from the user's words to the result:

* the agent's session log (``<logs>/sessions/<session>.jsonl``): every record of the request carries it
  (:meth:`yuki.log.events.SessionLogger.begin_request`; the id is ``<session id>-<n>``, so its file is found by name),
  and the other files of the same run: after the front desk's hand-off the worker's records carry the front desk's id
  in ``<run>-worker.jsonl``, and the tray's UI log (``<run>.jsonl``) has the ``handoff`` / ``queued`` lines;
* the voice logs (``<logs>/voice/*.jsonl``) where a record names it;
* the memory service's log (``<logs>/memory-*.jsonl``): the ``acting`` line (the marker carries the same id,
  :mod:`yuki.memory.acting`) and, inside the request's time, the state changes and guards around it (moments, the
  game guard, timeline gaps, the Spotify reader, tab reads);
* the moments Yuki caused (the ``moments`` table, phase F ``fMoments``, read-only; skipped when it is not there yet);
* its ``requests.csv`` line (by its ``request_id`` column, else by its session and start time).

Everything printed goes through :func:`yuki.log.events.content_free` (ids, code words, counts, times, 12-hex
digests), whatever the logs themselves kept (``YUKI_LOG_CONTENT=1``): this tool never shows a word, a title or a
name. Read-only: files are only read, the database is opened ``mode=ro``.

``--logs``: by default the logs of this checkout (``yuki.core.paths.logs_dir``: a dev checkout's ``logs/``), then,
when they name nothing, the installed app's (``%LOCALAPPDATA%\\Yuki\\logs``). A search through every session or voice
file (an id whose own file is not found) reads only files written since request ids exist
(:data:`STAMPED_SINCE`): the older logs (before 5 Oct 2026 they kept the words) are never opened.
"""

from __future__ import annotations

import argparse
import csv
import glob
import json
import sqlite3
import sys
from collections.abc import Iterable, Iterator, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from yuki.log.events import content_free, log_digest

#: Memory-log record types shown when they fall inside the request's time (the rest only when they name the request).
MEMORY_TYPES = frozenset({
    "acting", "acting_leak", "moment", "moment_source", "site_front", "tab_strip", "tab_census", "volume_event",
    "timeline_gap", "input_refused_game", "game_gate", "spotify_read", "spotify_probe", "media_command",
    "call_detected", "nudge_gate", "lookup_refused_game", "timeline_mic_no_call",
})
#: Seconds before the request's first record and after its last that the memory log and moments are read for.
PAD_BEFORE_S = 10.0
PAD_AFTER_S = 30.0
#: ``moments`` columns that may be shown (plus any ``*_digest``, cut to 12 hex); never a ``*_ciphertext``.
MOMENT_COLUMNS = ("id", "at", "kind", "app", "service", "by", "request_id", "evidence", "means", "lag_s", "source")
#: Request ids are stamped from phase F (10 Oct 2026) on; a file last written before this time cannot hold one, so a
#: search through every file skips it unopened (review r1 L6: the logs before 5 Oct kept the words).
STAMPED_SINCE = datetime(2026, 10, 9).timestamp()
#: :class:`yuki.log.events.SessionLogger`'s own session id (a time); the tray's front desk and worker add ``-<lane>``.
SESSION_ID_FORMAT = "%Y%m%d-%H%M%S"


def _stamped(paths: Iterable[Path]) -> list[Path]:
    """The files written since request ids exist (:data:`STAMPED_SINCE`), by their modification time."""
    out = []
    for path in paths:
        try:
            if path.stat().st_mtime >= STAMPED_SINCE:
                out.append(path)
        except OSError:
            continue
    return sorted(out)


def _jsonl(path: Path, needle: str | None = None) -> Iterator[dict[str, Any]]:
    """The records of one JSONL file (a line that does not contain ``needle`` is not parsed)."""
    try:
        fh = path.open(encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            if needle is not None and needle not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict):
                yield rec


def _run_of(stem: str) -> str:
    """The run a session file belongs to: the leading :data:`SESSION_ID_FORMAT` of its id (the tray's lanes add
    ``-frontdesk`` / ``-worker`` to it, its UI log is the bare id), else the id without its last ``-`` part."""
    head = "-".join(stem.split("-")[:2])
    try:
        datetime.strptime(head, SESSION_ID_FORMAT)
        return head
    except ValueError:
        return stem.rsplit("-", 1)[0] if "-" in stem else stem


def _session_files(folder: Path, request_id: str) -> list[Path]:
    """Where a request's session records can be: its own file (``<session>.jsonl``, the id being ``<session>-<n>``)
    and the files of the same run (review fWire r1 M1: after the front desk's hand-off the worker keeps the front
    desk's id in ``<run>-worker.jsonl``, and the UI log's ``handoff`` / ``queued`` lines are in ``<run>.jsonl``);
    an id with no file of its own (a voice id): every session file written since request ids exist."""
    stem = request_id.rsplit("-", 1)[0] if "-" in request_id else ""
    own = folder / f"{stem}.jsonl"
    if not stem or not own.exists():
        return _stamped(folder.glob("*.jsonl"))
    run = _run_of(stem)
    siblings = [p for p in folder.glob(f"{glob.escape(run)}*.jsonl") if p.stem == run or p.stem.startswith(run + "-")]
    return sorted({own, *siblings})


def session_records(logs: Path, request_id: str) -> list[dict[str, Any]]:
    """The session-log records stamped with ``request_id`` (:func:`_session_files`: its own file and its run's)."""
    folder = logs / "sessions"
    files = _session_files(folder, request_id)
    out: list[dict[str, Any]] = []
    for path in files:
        for rec in _jsonl(path, request_id):
            if rec.get("request_id") == request_id:
                out.append({**rec, "_source": "session", "_file": path.name})
    return out


def voice_records(logs: Path, request_id: str) -> list[dict[str, Any]]:
    """Voice-log records that name the request."""
    out: list[dict[str, Any]] = []
    for path in _stamped((logs / "voice").glob("*.jsonl")):
        for rec in _jsonl(path, request_id):
            if rec.get("request_id") == request_id:
                out.append({**rec, "_source": "voice", "_file": path.name})
    return out


def _memory_files(logs: Path, t0: float | None, t1: float | None) -> list[Path]:
    """The memory logs that can hold ``[t0, t1]`` (each file is named by its service's start time)."""
    files = []
    for path in logs.glob("memory-*.jsonl"):
        try:
            started = datetime.strptime(path.stem[len("memory-"):], "%Y%m%d-%H%M%S").timestamp()
        except ValueError:
            continue
        files.append((started, path))
    files.sort()
    if t0 is None or t1 is None:
        return [p for _s, p in files]
    out = []
    for i, (started, path) in enumerate(files):
        nxt = files[i + 1][0] if i + 1 < len(files) else float("inf")
        if started <= t1 and nxt >= t0:
            out.append(path)
    return out


def memory_records(logs: Path, request_id: str, t0: float | None, t1: float | None) -> list[dict[str, Any]]:
    """Memory-log records naming the request, and those of :data:`MEMORY_TYPES` inside ``[t0, t1]``."""
    out: list[dict[str, Any]] = []
    needle = request_id if t0 is None or t1 is None else None   # no span yet: only the lines that name it
    for path in _memory_files(logs, t0, t1):
        for rec in _jsonl(path, needle):
            ts = rec.get("ts")
            named = rec.get("request_id") == request_id
            inside = (t0 is not None and t1 is not None and isinstance(ts, (int, float)) and t0 <= ts <= t1
                      and rec.get("type") in MEMORY_TYPES)
            if named or inside:
                out.append({**rec, "_source": "memory", "_file": path.name})
    return out


def moment_records(db: Path | None, request_id: str, t0: float | None, t1: float | None) -> tuple[list[dict], str]:
    """The moments that name the request or fall inside ``[t0, t1]`` (read-only), and a note (``ok``,
    ``no_db``, ``no_moments_table``)."""
    if db is None or not Path(db).exists():
        return [], "no_db"
    try:
        conn = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return [], "no_db"
    conn.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(moments)").fetchall()]
        if not cols:
            return [], "no_moments_table"
        where, params = [], []
        if "request_id" in cols:
            where.append("request_id = ?")
            params.append(request_id)
        if t0 is not None and t1 is not None and "at" in cols:
            where.append("(at >= ? AND at <= ?)")
            params += [t0, t1]
        if not where:
            return [], "ok"
        rows = conn.execute(f"SELECT * FROM moments WHERE {' OR '.join(where)} ORDER BY at", params).fetchall()
    except sqlite3.Error:
        return [], "no_moments_table"
    finally:
        conn.close()
    out = []
    for r in rows:
        rec: dict[str, Any] = {"type": "moment_row", "_source": "moments"}
        for key in r.keys():
            if key.endswith("_ciphertext"):
                continue
            if key.endswith("_digest"):
                rec[key] = log_digest(r[key])
            elif key in MOMENT_COLUMNS:
                rec[key] = r[key]
        if isinstance(rec.get("at"), (int, float)):
            rec["ts"] = float(rec["at"])
        if "by" in rec:
            rec["by_yuki"] = rec.pop("by") == "yuki"   # "by" is a free field in the log vocabulary: a flag here
        out.append(rec)
    return out, "ok"


def _number(value: Any) -> Any:
    """A CSV cell as a number when it is one (so counts and times survive :func:`content_free`); "" as None."""
    if value in (None, ""):
        return None
    try:
        return float(value) if any(c in str(value) for c in ".eE") else int(value)
    except (TypeError, ValueError):
        return value


def request_rows(path: Path, request_id: str, summaries: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Its ``requests.csv`` line(s): by ``request_id``, else by session and start time (the agent writes the
    request's ``started_at`` to the second)."""
    if not path.exists():
        return []
    keys = {(str(s.get("session") or ""), str(s.get("started_at") or "")) for s in summaries}
    out = []
    try:
        with path.open(encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                if row.get("request_id") == request_id or (
                        not row.get("request_id") and (row.get("session") or "", row.get("timestamp") or "") in keys):
                    rec = {k: _number(v) for k, v in row.items() if k and k != "request"}
                    rec.update({"type": "requests_csv", "_source": "requests.csv"})
                    try:
                        rec["ts"] = datetime.fromisoformat(row.get("timestamp") or "").timestamp()
                    except ValueError:
                        pass
                    out.append(rec)
    except OSError:
        return []
    return out


def follow(request_id: str, *, logs: Path, db: Path | None = None, pad_before: float = PAD_BEFORE_S,
           pad_after: float = PAD_AFTER_S) -> dict[str, Any]:
    """Everything the logs hold about one request, oldest first, content-free: ``{"request_id", "span",
    "records", "notes", "counts"}``."""
    session = session_records(logs, request_id)
    voice = voice_records(logs, request_id)
    stamps = [float(r["ts"]) for r in session + voice if isinstance(r.get("ts"), (int, float))]
    t0 = min(stamps) - pad_before if stamps else None
    t1 = max(stamps) + pad_after if stamps else None
    memory = memory_records(logs, request_id, t0, t1)
    if t0 is None:   # only the memory log knew it (a voice hand's marker): its own lines give the span
        named = [float(r["ts"]) for r in memory
                 if r.get("request_id") == request_id and isinstance(r.get("ts"), (int, float))]
        if named:
            t0, t1 = min(named) - pad_before, max(named) + pad_after
            memory = memory_records(logs, request_id, t0, t1)
    moments, note = moment_records(db, request_id, t0, t1)
    summaries = [r for r in session if r.get("type") == "request_summary"]
    rows = request_rows(logs / "requests.csv", request_id, summaries)
    merged = []
    for rec in session + voice + memory + moments + rows:
        source, name = rec.pop("_source"), rec.pop("_file", None)
        clean = content_free(rec)
        clean["_source"] = source
        if name:
            clean["_file"] = name
        merged.append(clean)
    merged.sort(key=lambda r: (float(r.get("ts") or 0.0), r.get("_source") or ""))
    counts: dict[str, int] = {}
    for r in merged:
        counts[r["_source"]] = counts.get(r["_source"], 0) + 1
    return {"request_id": request_id, "span": [t0, t1], "records": merged, "notes": {"moments": note},
            "counts": counts}


def _line(rec: dict[str, Any]) -> str:
    ts = rec.get("ts")
    when = datetime.fromtimestamp(float(ts)).strftime("%H:%M:%S.%f")[:-3] if isinstance(ts, (int, float)) else "--"
    skip = {"ts", "_source", "_file", "type", "session", "request_id"}
    rest = " ".join(f"{k}={json.dumps(v, ensure_ascii=False, separators=(',', ':'))}" for k, v in rec.items()
                    if k not in skip)
    return f"{when}  {rec.get('_source', ''):<12} {rec.get('type', '')}  {rest}"


def default_logs() -> list[Path]:
    """The logs folders tried without ``--logs``: this checkout's (``logs_dir``), then the installed app's
    (``data_dir()/logs``) when that is another folder (review r1 L6: a dev checkout's default named nothing)."""
    from yuki.core.paths import data_dir, logs_dir

    out = [Path(logs_dir())]
    installed = Path(data_dir()) / "logs"
    try:
        same = installed.resolve() == out[0].resolve()
    except OSError:
        same = installed == out[0]
    if not same:
        out.append(installed)
    return out


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.log.follow",
                                     description="One request across the logs, content-free (read-only).")
    parser.add_argument("request_id")
    parser.add_argument("--logs", help="the logs folder (default: this checkout's logs, then the installed app's,"
                                       " %%LOCALAPPDATA%%\\Yuki\\logs)")
    parser.add_argument("--db", help="the memory database for the moments (default: Yuki's own; opened read-only)")
    parser.add_argument("--json", action="store_true", help="one JSON object instead of lines")
    parser.add_argument("--pad", type=float, default=PAD_AFTER_S, help="seconds after the request's last record")
    args = parser.parse_args(list(argv) if argv is not None else None)
    folders = [Path(args.logs)] if args.logs else default_logs()
    if args.db:
        db: Path | None = Path(args.db)
    else:
        try:
            from yuki.memory.store import default_db_path

            db = default_db_path()
        except Exception:  # noqa: BLE001
            db = None
    logs = folders[0]
    out = follow(args.request_id, logs=logs, db=db, pad_after=args.pad)
    for other in folders[1:]:   # this checkout's logs named nothing: the installed app's
        if out["records"]:
            break
        logs = other
        out = follow(args.request_id, logs=logs, db=db, pad_after=args.pad)
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0 if out["records"] else 1
    if not out["records"]:
        print(f"nothing in {logs} names {args.request_id}")
        return 1
    print(f"request {out['request_id']}: " + ", ".join(f"{k} {v}" for k, v in sorted(out["counts"].items()))
          + f" (moments: {out['notes']['moments']})")
    for rec in out["records"]:
        print(_line(rec))
    return 0


if __name__ == "__main__":
    sys.exit(main())
