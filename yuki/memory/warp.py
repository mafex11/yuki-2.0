"""The Warp terminal's command history -> journal sources of kind ``terminal``.

Contract: ``docs/MEMORY.md`` ("Terminals").  Warp draws its window with the
GPU, so the watcher sees nothing of it through UI Automation; Warp itself
keeps every command in its own SQLite database
(``%LOCALAPPDATA%\\warp\\Warp\\data\\warp.sqlite``, WAL).  :class:`WarpReader`
runs in ``yuki-memory`` every :data:`EVERY_S`:

* opens that database **read-only** (``mode=ro`` URI, ``PRAGMA query_only``;
  never ``immutable``, which is unsafe while Warp writes its WAL), reads the
  rows of ``commands`` after the checkpoint (``source_checkpoints``
  ``warp.commands`` = the last ``commands.id`` consumed) and closes it;
* reads only ``id, command, exit_code, start_ts, completed_ts, pwd, shell,
  git_branch, session_id, is_agent_executed`` - never the ``blocks`` table,
  never any command **output**;
* waits for a command's completion (``completed_ts``) up to
  :data:`SETTLE_S` so its exit code is known; the checkpoint only moves over
  a contiguous run of taken rows;
* drops, entirely, every command the privacy file's ``[terminal]`` rules
  call a secret (:class:`TerminalRules`: regular expressions and a
  high-entropy-word test, both data in the TOML) - a dropped command is
  never stored and never reaches the model; only its count is logged;
* writes the rest with :meth:`yuki.memory.store.Store.add_terminal_commands`:
  one ``terminal`` thread per folder, one capture per folder and pass
  (encrypted, 30-day TTL like every capture), checkpoint moved in the same
  transaction - so the journal worker turns them into facts
  (:mod:`yuki.memory.journal`, one source per command) and a crash never
  journals a command twice.

Nothing is read while memory is paused, while ``[terminal] enabled`` is
false or while Warp is a blocked app; the checkpoint then moves past what
was written meanwhile, so it is never journaled later.  On the very first
read the checkpoint starts at the newest command: history from before Yuki
read Warp is not journaled.

Terminal *time* comes from the foreground timeline (Warp in front), not
from Warp's timestamps: most of its rows (73% on 2026-09-24) never get a
``completed_ts``, so durations from the database would be wrong.

Logs are content-free: counts per drop reason, rows read / kept / waiting, ms.

Public API::

    WarpReader(store, *, privacy=None, log=None, db_path=None, every_s=EVERY_S, settle_s=SETTLE_S)
        .read_once(now=None) -> dict          # one pass, content-free counts
        .run(stop, pause_path=None) / .stop() / .set_paused(bool)
    TerminalRules.from_dict(table) / .secret_reason(*texts) -> str | None
    probe(db_path=None, privacy=None, settle_s=SETTLE_S, now=None) -> dict   # counts over the whole history
    default_db_path() -> Path;  parse_warp_ts(text) -> float | None
"""

from __future__ import annotations

import math
import os
import re
import sqlite3
import threading
import time
import traceback
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from yuki.memory.store import Store
from yuki.memory.timeline import PrivacySection

#: Checkpoint name in ``source_checkpoints``.
CHECKPOINT = "warp.commands"
#: How often the service reads Warp's history.
EVERY_S = 180.0
#: A command without a completion time is taken once it is this old (its exit code then unknown).
SETTLE_S = 600.0
#: Rows read per pass (plumbing bound; the rest are read at the next pass).
READ_LIMIT = 500
#: Longest command line kept (a longer one is cut; Warp's are ~13 chars on average).
MAX_COMMAND_CHARS = 1_000
#: Busy timeout of the read-only connection.
DB_TIMEOUT_S = 2.0

_COLUMNS = ("id, command, exit_code, start_ts, completed_ts, pwd, shell, git_branch, session_id, "
            "is_agent_executed")
#: Word boundaries of the high-entropy test (plumbing: what a "word" is).
_WORD_SPLIT = re.compile(r"[\s'\"=:,;()<>|&/\\]+")
_CLASSES = (re.compile(r"[a-z]"), re.compile(r"[A-Z]"), re.compile(r"[0-9]"))


def default_db_path() -> Path:
    """``%LOCALAPPDATA%\\warp\\Warp\\data\\warp.sqlite``."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "warp" / "Warp" / "data" / "warp.sqlite"


def parse_warp_ts(value: Any) -> float | None:
    """Warp's UTC timestamp text (``2026-09-24 08:10:00.076684200``) as epoch seconds."""
    if value is None:
        return None
    text = str(value).strip().replace("T", " ").rstrip("Z")
    if not text:
        return None
    head, _, frac = text.partition(".")
    try:
        base = datetime.strptime(head, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    digits = "".join(ch for ch in frac if ch.isdigit())
    return base + (float("0." + digits) if digits else 0.0)


def shannon_bits(word: str) -> float:
    """Entropy of ``word``'s characters, bits per character."""
    if not word:
        return 0.0
    n = len(word)
    return -sum((c / n) * math.log2(c / n) for c in Counter(word).values())


@dataclass(frozen=True)
class TerminalRules:
    """The privacy file's ``[terminal]`` table."""

    enabled: bool = True
    database: str = ""
    include_agent_commands: bool = True
    patterns: tuple[re.Pattern, ...] = ()
    entropy_min_length: int = 24
    entropy_min_bits: float = 3.5
    #: Patterns that did not compile (index: error); such a file keeps working with the others.
    errors: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, table: dict) -> TerminalRules:
        table = table or {}
        patterns, errors = [], []
        for i, raw in enumerate(table.get("secret_patterns", []) or []):
            try:
                patterns.append(re.compile(str(raw), re.IGNORECASE))
            except re.error as exc:
                errors.append(f"{i}: {exc}")
        return cls(
            enabled=bool(table.get("enabled", True)),
            database=str(table.get("database", "") or ""),
            include_agent_commands=bool(table.get("include_agent_commands", True)),
            patterns=tuple(patterns),
            entropy_min_length=int(table.get("entropy_min_length", 24)),
            entropy_min_bits=float(table.get("entropy_min_bits", 3.5)),
            errors=tuple(errors),
        )

    def _random_word(self, word: str) -> bool:
        if len(word) < self.entropy_min_length:
            return False
        if sum(1 for c in _CLASSES if c.search(word)) < 2:
            return False
        return shannon_bits(word) >= self.entropy_min_bits

    def secret_reason(self, *texts: str) -> str | None:
        """``"pattern:<n>"`` / ``"high_entropy"`` when any text looks like it holds a secret, else None.

        Content-free: the reason names the rule, never the text.
        """
        for text in texts:
            if not text:
                continue
            for i, pattern in enumerate(self.patterns):
                if pattern.search(text):
                    return f"pattern:{i}"
            if any(self._random_word(w) for w in _WORD_SPLIT.split(text) if w):
                return "high_entropy"
        return None


def _open_ro(path: Path) -> sqlite3.Connection:
    """Warp's database, read-only (WAL-safe: no ``immutable``; Warp keeps writing)."""
    conn = sqlite3.connect(f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=DB_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=1")
    return conn


def _command_item(r: sqlite3.Row, start: float | None, done: float | None, now: float) -> dict[str, Any]:
    command = (r["command"] or "").strip()
    if len(command) > MAX_COMMAND_CHARS:
        command = command[: MAX_COMMAND_CHARS - 1] + "…"
    item: dict[str, Any] = {
        "id": int(r["id"]), "at": start if start is not None else now, "command": command,
        "pwd": r["pwd"] or "", "branch": r["git_branch"] or None, "exit_code": r["exit_code"],
        "shell": r["shell"] or None, "session": r["session_id"], "agent": bool(r["is_agent_executed"]),
    }
    if start is not None and done is not None and done >= start:
        item["seconds"] = round(done - start, 1)
    return item


def _triage(
    rows: list[sqlite3.Row], rules: TerminalRules, now: float, settle_s: float,
) -> tuple[int | None, dict[str, list[dict[str, Any]]], Counter, int]:
    """(last id taken, kept commands by folder, drop reasons, rows still waiting for completion)."""
    last: int | None = None
    groups: dict[str, list[dict[str, Any]]] = {}
    dropped: Counter = Counter()
    waiting = 0
    for i, r in enumerate(rows):
        start, done = parse_warp_ts(r["start_ts"]), parse_warp_ts(r["completed_ts"])
        if done is None and start is not None and now - start < settle_s:
            waiting = len(rows) - i  # the checkpoint moves only over a contiguous run
            break
        last = int(r["id"])
        command = (r["command"] or "").strip()
        if not command:
            dropped["empty"] += 1
            continue
        if r["is_agent_executed"] and not rules.include_agent_commands:
            dropped["agent"] += 1
            continue
        reason = rules.secret_reason(command, r["pwd"] or "", r["git_branch"] or "")
        if reason:
            dropped[reason] += 1
            continue
        item = _command_item(r, start, done, now)
        groups.setdefault(item["pwd"], []).append(item)
    return last, groups, dropped, waiting


class WarpReader:
    """Reads new Warp commands into the store for the journal worker. Thread-safe to :meth:`stop`."""

    def __init__(
        self,
        store: Store,
        *,
        privacy: Any = None,
        log: Callable[..., Any] | None = None,
        db_path: Path | None = None,
        every_s: float = EVERY_S,
        settle_s: float = SETTLE_S,
    ) -> None:
        from yuki.memory.privacy import PrivacyConfig

        self.store = store
        self.privacy = privacy if privacy is not None else PrivacyConfig()
        self._rules = PrivacySection(self.privacy, "terminal", TerminalRules.from_dict)
        self._log_fn = log
        self._db_path = Path(db_path) if db_path else None
        self.every_s = float(every_s)
        self.settle_s = float(settle_s)
        self._paused = False
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.counters: Counter = Counter()

    # -- config ------------------------------------------------------------

    def rules(self) -> TerminalRules:
        return self._rules.get()

    def db_path(self, rules: TerminalRules | None = None) -> Path:
        if self._db_path is not None:
            return self._db_path
        rules = rules or self.rules()
        return Path(os.path.expandvars(rules.database)) if rules.database else default_db_path()

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:
            pass

    def _skip_reason(self, rules: TerminalRules) -> str | None:
        if self._paused:
            return "paused"
        if not rules.enabled:
            return "disabled"
        try:
            reason = self.privacy.rules().check_app("warp.exe", "Warp")
        except Exception:
            reason = None
        return f"app_{reason}" if reason else None

    # -- one pass ------------------------------------------------------------

    def read_once(self, now: float | None = None) -> dict[str, Any]:
        """Read Warp's new commands once; returns content-free counts (also logged when anything happened)."""
        with self._lock:
            now = time.time() if now is None else float(now)
            t0 = time.perf_counter()
            rules = self.rules()
            out: dict[str, Any] = {"outcome": "ok", "read": 0, "kept": 0, "dropped": {}, "waiting": 0,
                                   "captures": 0, "folders": 0}
            path = self.db_path(rules)
            try:
                if not path.exists():
                    out["outcome"] = "no_database"
                    return out
                skip = self._skip_reason(rules)
                checkpoint = self.store.source_checkpoint(CHECKPOINT)
                conn = _open_ro(path)
                try:
                    max_id = int(conn.execute("SELECT coalesce(max(id), 0) FROM commands").fetchone()[0])
                    after = int(checkpoint) if checkpoint is not None else None
                    if after is None or skip or max_id < after:
                        # first read, paused/disabled/blocked, or Warp's database was reset:
                        # move past everything there is, reading no command text
                        passed = int(conn.execute("SELECT count(*) FROM commands WHERE id > ?",
                                                  (after or 0,)).fetchone()[0]) if after is not None else 0
                        self.store.set_source_checkpoint(CHECKPOINT, str(max_id))
                        out["outcome"] = "first_read" if after is None else (skip or "reset")
                        out["skipped"] = passed if after is not None else max_id
                        return out
                    rows = conn.execute(
                        f"SELECT {_COLUMNS} FROM commands WHERE id > ? ORDER BY id LIMIT ?", (after, READ_LIMIT)
                    ).fetchall()
                finally:
                    conn.close()
                out["read"] = len(rows)
                last, groups, dropped, waiting = _triage(rows, rules, now, self.settle_s)
                out["waiting"] = waiting
                out["dropped"] = dict(dropped)
                out["kept"] = sum(len(v) for v in groups.values())
                out["folders"] = len(groups)
                if last is not None and last > after:
                    ids = self.store.add_terminal_commands((CHECKPOINT, str(last)), list(groups.items()), at=now)
                    out["captures"] = len(ids)
                if rules.errors:
                    out["pattern_errors"] = list(rules.errors)
            except Exception as exc:
                out["outcome"] = "error"
                out["error"] = f"{type(exc).__name__}: {exc}"
                self.counters["errors"] += 1
                self._log("error", where="warp.read", error=out["error"], traceback=traceback.format_exc())
            finally:
                out["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
                self.counters["passes"] += 1
                self.counters["kept"] += out.get("kept", 0)
                self.counters["dropped"] += sum(out.get("dropped", {}).values())
                if out["outcome"] != "ok" or out["read"]:
                    self._log("warp_read", **out)
            return out

    # -- loop ----------------------------------------------------------------

    def run(self, stop: threading.Event | None = None, pause_path: Path | None = None) -> None:
        """Read every ``every_s`` until stopped; while the ``paused`` flag exists, passes only skip."""
        stop = stop or self._stop
        self._stop = stop
        self._log("warp_start", every_s=self.every_s, settle_s=self.settle_s, db_exists=self.db_path().exists())
        while not stop.is_set():
            if pause_path is not None:
                self._paused = Path(pause_path).exists()
            self.read_once()
            stop.wait(self.every_s)
        self._log("warp_stop", **dict(self.counters))

    def stop(self) -> None:
        self._stop.set()

    def stats(self) -> dict[str, Any]:
        return dict(self.counters)


def probe(
    db_path: Path | None = None, privacy: Any = None, *, settle_s: float = SETTLE_S, now: float | None = None,
) -> dict[str, Any]:
    """Counts only, over Warp's whole history: how many commands would be journaled or dropped (and why).

    Read-only; nothing is stored; no command text is returned.
    """
    section = PrivacySection(privacy, "terminal", TerminalRules.from_dict)
    rules: TerminalRules = section.get()
    path = Path(db_path) if db_path else (Path(os.path.expandvars(rules.database)) if rules.database
                                           else default_db_path())
    now = time.time() if now is None else float(now)
    t0 = time.perf_counter()
    conn = _open_ro(path)
    try:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM commands ORDER BY id").fetchall()
    finally:
        conn.close()
    _, groups, dropped, waiting = _triage(rows, rules, now, settle_s)
    kept = [c for cs in groups.values() for c in cs]
    return {
        "database": str(path), "commands": len(rows), "would_journal": len(kept),
        "dropped": sum(dropped.values()), "dropped_by_reason": dict(sorted(dropped.items())),
        "waiting_for_completion": waiting, "folders": len(groups),
        "with_exit_code": sum(1 for c in kept if c["exit_code"] is not None),
        "agent_commands": sum(1 for c in kept if c["agent"]),
        "ms": round((time.perf_counter() - t0) * 1000.0, 1),
    }


__all__ = ["WarpReader", "TerminalRules", "probe", "default_db_path", "parse_warp_ts", "CHECKPOINT", "EVERY_S",
           "SETTLE_S"]
