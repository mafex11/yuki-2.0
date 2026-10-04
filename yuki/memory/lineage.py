"""Which program started which: launch facts for the windows that come to the front (appOpen F3, 5 Oct 2026).

Live 5 Oct: Yuki's "open Valorant" showed the Riot Client's tray window. Four minutes later the user started the game
themselves (Start, the Riot Client, Play): ``VALORANT-Win64-Shipping.exe`` started by ``VALORANT.exe`` started by the
already running ``RiotClientServices.exe``. Nothing kept that lineage, so nothing could learn "the VALORANT entry starts
the game through the Riot Client; the game's window is ``VALORANT-Win64-Shipping``" from what the user did.

This module keeps it, content-free and in memory only: when the timeline opens a stretch for a window of a process it
has not seen yet, :func:`note` records the process's image and program path, when it started, and its parents (image,
path, start), plus whether Yuki was acting at that moment. Nothing is written to the database: the practice learner
(same service process) reads the recent ones (:func:`between`) when it looks at what the user did right after one of
Yuki's openings, and :func:`start_entries` maps a parent program back to the Start entries whose shortcut runs it (path
equality). A record older than :data:`KEEP_S` is dropped; a service restart forgets them (the learner then has only the
timeline's own rows).

Cost: one parent snapshot (~12 ms) and a few process reads, once per new process brought to the front; nothing on a
window of a process already seen. No model, no content, no list of apps.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

#: How long a launch record is kept (seconds): the learner looks at most this far back.
KEEP_S = 2 * 3600.0
#: Records kept at most (newest last).
MAX_RECORDS = 300
#: Parents recorded per process.
DEPTH = 4
#: How long the Start entries' targets are cached in this process (they change when something is installed).
ENTRIES_TTL_S = 3600.0

_lock = threading.Lock()
_records: deque[dict[str, Any]] = deque(maxlen=MAX_RECORDS)
_seen: dict[int, float] = {}
_entries: tuple[float, dict[str, list[dict[str, str]]]] | None = None


def _facts(pid: int, ppids: dict[int, int]) -> dict[str, Any] | None:
    import psutil

    try:
        process = psutil.Process(int(pid))
        started = float(process.create_time())
    except Exception:
        return None
    try:
        exe = process.exe() or ""
    except Exception:
        exe = ""
    try:
        image = (process.name() or os.path.basename(exe)).lower()
    except Exception:
        image = os.path.basename(exe).lower()
    return {"pid": int(pid), "image": image, "exe": exe, "started": started, "ppid": int(ppids.get(int(pid)) or 0)}


def _ppid_map() -> dict[int, int]:
    try:
        import psutil

        return dict(psutil._psplatform.ppid_map())   # noqa: SLF001 - one system snapshot for every parent
    except Exception:
        return {}


def note(pid: int, at: float | None = None, *, by_yuki: bool = False) -> dict[str, Any] | None:
    """Record the launch facts of a process whose window came to the front, once per process (its pid and start time).
    Never raises; returns the record, or ``None`` (seen already, gone, unreadable)."""
    try:
        if not pid:
            return None
        at = time.time() if at is None else float(at)
        import psutil

        try:
            started = float(psutil.Process(int(pid)).create_time())
        except Exception:
            return None
        with _lock:
            if _seen.get(int(pid)) == started:
                return None   # a window of a process already recorded: nothing more is read
        ppids = _ppid_map()
        me = _facts(int(pid), ppids)
        if me is None:
            return None
        with _lock:
            _seen[int(pid)] = me["started"]
            if len(_seen) > 4 * MAX_RECORDS:
                for old in list(_seen)[: len(_seen) - 2 * MAX_RECORDS]:
                    _seen.pop(old, None)
        parents = []
        current = me
        for _ in range(DEPTH):
            if not current["ppid"] or current["ppid"] == current["pid"]:
                break
            parent = _facts(current["ppid"], ppids)
            if parent is None or parent["started"] > current["started"]:
                break   # gone, or a newer process reusing the pid: not its parent
            parents.append({k: parent[k] for k in ("image", "exe", "started")})
            current = parent
        record = {"at": at, "image": me["image"], "exe": me["exe"], "started": me["started"], "parents": parents,
                  "by_yuki": bool(by_yuki)}
        with _lock:
            _records.append(record)
            while _records and at - _records[0]["at"] > KEEP_S:
                _records.popleft()
        return record
    except Exception:
        return None


def add(record: dict[str, Any]) -> None:
    """Keep a record as :func:`note` would (tests, a replay)."""
    with _lock:
        _records.append(dict(record))


def clear() -> None:
    with _lock:
        _records.clear()
        _seen.clear()


def between(since: float, until: float | None = None) -> list[dict[str, Any]]:
    """The records whose process came to the front in ``[since, until)``, oldest first (copies)."""
    with _lock:
        return [dict(r) for r in _records if r["at"] >= since and (until is None or r["at"] < until)]


def start_entries(exes: list[str]) -> dict[str, list[dict[str, str]]]:
    """``{program path (normcased): [{"name", "arguments"}]}``: the Start entries whose shortcut runs each program
    (path equality with the entry's shortcut target; ``yuki.actions.launch``'s own reading of the Start menu). Read once
    an hour at most; ``{}`` when the Start menu cannot be read."""
    global _entries
    wanted = {os.path.normcase(os.path.abspath(e)) for e in exes if e}
    if not wanted:
        return {}
    now = time.monotonic()
    with _lock:
        cached = _entries if _entries is not None and now - _entries[0] < ENTRIES_TTL_S else None
    if cached is None:
        try:
            from yuki.actions import launch as L

            apps = L.get_start_apps()
            links = L._start_links(apps)   # noqa: SLF001 - the launcher's own reader of the shortcuts
        except Exception:
            return {}
        table: dict[str, list[dict[str, str]]] = {}
        for app in apps:
            link = links.get(app.get("appid") or "") or {}
            target = str(link.get("target") or "")
            if not target or "://" in target:
                continue
            table.setdefault(os.path.normcase(os.path.abspath(target)), []).append(
                {"name": str(app.get("name") or ""), "arguments": str(link.get("arguments") or "")})
        with _lock:
            _entries = (now, table)
        cached = _entries
    return {exe: list(cached[1].get(exe) or []) for exe in wanted if cached[1].get(exe)}


def set_start_entries(table: dict[str, list[dict[str, str]]]) -> None:
    """Use this table of Start entries by program instead of reading the Start menu (tests)."""
    global _entries
    with _lock:
        _entries = (time.monotonic(), {os.path.normcase(os.path.abspath(k)): list(v) for k, v in table.items()})


__all__ = ["add", "between", "clear", "note", "set_start_entries", "start_entries"]
