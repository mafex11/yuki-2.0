"""Log retention: log files older than :data:`LOG_RETENTION_DAYS` days are deleted (report 76: 265 MB in 9 days).

The memory service calls :func:`prune_logs` with its other prune pass (every few hours; a walk of a few hundred
files). Plumbing, no judgement: a file goes when its last write is older than the cutoff. Files that grow in place
(the request ledger, the voice turn ledger, the crash and boot logs) are never deleted by age here: they are one
file each, and the ledgers are read by the window. A file another process still holds open is skipped (Windows
refuses the delete) and goes on a later pass. Nothing outside the logs folder is touched; links are not followed.
Logs only counts, sizes and errors.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any

#: How long a log file is kept after its last write.
LOG_RETENTION_DAYS = 14
#: Files that grow in place, never deleted by age.
KEEP_NAMES: frozenset[str] = frozenset({"requests.csv", "voice_turns.csv", "memory-crash.log", "ui-boot.log"})


def prune_logs(logs_dir: Path, *, days: float = LOG_RETENTION_DAYS, now: float | None = None,
               keep: frozenset[str] = KEEP_NAMES) -> dict[str, Any]:
    """Delete files under ``logs_dir`` last written more than ``days`` ago (except ``keep``), then the folders that
    are left empty (a session's screenshot folder). Returns ``{"files", "bytes", "dirs", "skipped"}``. Never raises."""
    out = {"files": 0, "bytes": 0, "dirs": 0, "skipped": 0}
    root = Path(logs_dir)
    if days <= 0 or not root.is_dir():
        return out
    now = time.time() if now is None else float(now)
    cutoff = now - float(days) * 86400.0
    dirs: list[Path] = []
    for here, subdirs, files in os.walk(root, followlinks=False):
        base = Path(here)
        dirs.extend(base / d for d in subdirs)
        for name in files:
            if name in keep:
                continue
            path = base / name
            try:
                st = path.lstat()
                if path.is_symlink() or st.st_mtime >= cutoff:
                    continue
                path.unlink()
                out["files"] += 1
                out["bytes"] += int(st.st_size)
            except OSError:
                out["skipped"] += 1
    for d in sorted(dirs, key=lambda p: len(p.parts), reverse=True):   # the deepest first
        try:
            # an empty folder untouched for an hour (one emptied by this pass goes on the next: its time just moved)
            if not d.is_symlink() and d.stat().st_mtime < now - 3600.0 and not any(d.iterdir()):
                d.rmdir()
                out["dirs"] += 1
        except OSError:
            continue
    return out


__all__ = ["KEEP_NAMES", "LOG_RETENTION_DAYS", "prune_logs"]
