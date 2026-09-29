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
* drops, entirely, every command that holds a secret (:class:`TerminalRules`:
  the fixed-format detector every memory entry point uses, then a
  high-entropy-word test whose thresholds are data in the TOML) - a dropped
  command is never stored and never reaches the model; only its count is
  logged;
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

from yuki.memory import secret_filter
from yuki.memory.secret_filter import shannon_bits
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


@dataclass(frozen=True)
class TerminalRules:
    """The privacy file's ``[terminal]`` table."""

    enabled: bool = True
    database: str = ""
    include_agent_commands: bool = True
    entropy_min_length: int = 24
    entropy_min_bits: float = 3.5
    #: Kept for the log's shape; always empty (``secret_patterns`` is no longer read).
    errors: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, table: dict) -> TerminalRules:
        # A user file written before 2026-09-25 may still hold ``secret_patterns`` (22 regular
        # expressions naming secret words and tools): it is ignored. What counts as a secret is the
        # shared fixed-format detector plus the measured entropy test below.
        table = table or {}
        return cls(
            enabled=bool(table.get("enabled", True)),
            database=str(table.get("database", "") or ""),
            include_agent_commands=bool(table.get("include_agent_commands", True)),
            entropy_min_length=int(table.get("entropy_min_length", 24)),
            entropy_min_bits=float(table.get("entropy_min_bits", 3.5)),
        )

    def _random_word(self, word: str) -> bool:
        if len(word) < self.entropy_min_length:
            return False
        if any(ch.islower() for ch in word) + any(ch.isupper() for ch in word) + any(ch.isdigit() for ch in word) < 2:
            return False
        return shannon_bits(word) >= self.entropy_min_bits

    def secret_reason(self, *texts: str) -> str | None:
        """``"secret:<kind>"`` / ``"high_entropy"`` when any text holds a secret, else None.

        First the shared fixed-format detector every memory entry point uses
        (:mod:`yuki.memory.secret_filter`: vendor key formats, private keys, JWTs, a URL's password,
        Authorization credentials, cards, IFSC), then the terminal-only measured test: any long
        high-entropy word. A password given as a flag (``mysql -pX``, ``--password X``) is no fixed
        format and is kept (the known limit of docs/MEMORY.md "Secret filter"). A command is dropped
        whole (not redacted): a command line is short and its secret is its point. Content-free: the
        reason names the rule, never the text.
        """
        for text in texts:
            if not text:
                continue
            found = secret_filter.find(text)
            if found:
                return f"secret:{found[0].kind}"
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


# ---------------------------------------------------------------------------
# Warp's saved window and tab state: what its opaque windows hold (2026-09-29)
# ---------------------------------------------------------------------------
#
# Warp draws its window with the GPU: UI Automation sees one element, so a request
# like "open the LLM OS tab" had nothing to aim at (29 Sept: 5 questions, 3 blind
# clicks, a cancel). Warp keeps its own session in the same database: windows
# (position, size, which tab shows), tabs, their panes, each pane's working folder,
# and the vertical-tab title Warp shows for it. :func:`read_session` reads only
# those tables and columns, read-only like the command history above - never
# ``blocks``, never output, never a command. The saved state can lag the live one
# by up to a few minutes (Warp writes it when it changes): the window's live
# title is the truth about the tab showing.

#: The tables and columns the session read touches (and nothing else).
_SESSION_SQL = {
    "windows": "SELECT id, active_tab_index, window_width, window_height, origin_x, origin_y, fullscreen_state, "
               "vertical_tabs_panel_open FROM windows ORDER BY id",
    "tabs": "SELECT id, window_id, custom_title FROM tabs ORDER BY window_id, id",
    "nodes": "SELECT id, tab_id FROM pane_nodes WHERE is_leaf = 1",
    "leaves": "SELECT pane_node_id, is_focused, custom_vertical_tabs_title FROM pane_leaves",
    "panes": "SELECT id, cwd FROM terminal_panes",
}
#: A saved window matches a live one when every edge is within this many px (after DPI scaling).
MATCH_SLACK_PX = 12


@dataclass
class WarpTab:
    """One tab of a Warp window, as Warp saved it."""

    index: int            # 0-based place in its window's tab strip
    title: str            # the vertical-tab title Warp keeps for it ("" when it keeps none)
    folder: str           # the working folder of its focused pane
    active: bool          # the tab showing when Warp saved the state
    panes: int = 1


@dataclass
class WarpWindow:
    """One Warp window, as Warp saved it."""

    rect: tuple[int, int, int, int]   # left, top, right, bottom as saved
    tabs: list[WarpTab]
    active_tab: int
    fullscreen: int = 0
    vertical_tabs: bool = False


def clean_title(title: str) -> str:
    """A title without the status glyphs an app puts before it (a spinner, a mark: Unicode symbols,
    punctuation, spaces - by Unicode category, a fixed machine property)."""
    import unicodedata

    text = " ".join(str(title or "").split())
    while text and unicodedata.category(text[0])[0] in "SPZ":
        text = text[1:].lstrip()
    return text


def read_session(db_path: Path | None = None, *, rules: TerminalRules | None = None) -> list[WarpWindow]:
    """Warp's saved windows and tabs, read-only; ``[]`` when there is no database or it cannot be read.

    Titles and folders holding a secret (the shared detector, then the terminal
    entropy rule) are left out.  Never raises.
    """
    rules = rules or TerminalRules()
    path = Path(db_path) if db_path else default_db_path()
    try:
        if not path.exists():
            return []
        conn = _open_ro(path)
    except Exception:
        return []
    try:
        rows = {name: conn.execute(sql).fetchall() for name, sql in _SESSION_SQL.items()}
    except Exception:
        return []
    finally:
        conn.close()

    def safe(text: Any) -> str:
        value = str(text or "").strip()
        return "" if not value or rules.secret_reason(value) else value

    tab_of_node = {int(r["id"]): int(r["tab_id"]) for r in rows["nodes"] if r["tab_id"] is not None}
    cwd_of = {int(r["id"]): safe(r["cwd"]) for r in rows["panes"]}
    leaves: dict[int, list[tuple[bool, str, str]]] = {}
    for r in rows["leaves"]:
        node = int(r["pane_node_id"])
        tab = tab_of_node.get(node)
        if tab is None:
            continue
        leaves.setdefault(tab, []).append(
            (bool(r["is_focused"]), clean_title(safe(r["custom_vertical_tabs_title"])), cwd_of.get(node, ""))
        )
    tabs_of: dict[int, list[Any]] = {}
    for r in rows["tabs"]:
        tabs_of.setdefault(int(r["window_id"]), []).append(r)
    out: list[WarpWindow] = []
    for w in rows["windows"]:
        try:
            left, top = int(round(float(w["origin_x"]))), int(round(float(w["origin_y"])))
            width, height = int(round(float(w["window_width"]))), int(round(float(w["window_height"])))
        except (TypeError, ValueError):
            continue
        active = int(w["active_tab_index"] or 0)
        tabs: list[WarpTab] = []
        for index, t in enumerate(tabs_of.get(int(w["id"]), [])):
            panes = leaves.get(int(t["id"]), [])
            focused = next((p for p in panes if p[0]), panes[0] if panes else (False, "", ""))
            title = clean_title(safe(t["custom_title"])) or focused[1] or next((p[1] for p in panes if p[1]), "")
            tabs.append(WarpTab(index=index, title=title, folder=focused[2], active=index == active,
                                panes=max(len(panes), 1)))
        out.append(WarpWindow(rect=(left, top, left + width, top + height), tabs=tabs, active_tab=active,
                              fullscreen=int(w["fullscreen_state"] or 0),
                              vertical_tabs=bool(w["vertical_tabs_panel_open"])))
    return out


def window_for(rect: tuple[int, int, int, int], windows: list[WarpWindow],
               scales: tuple[float, ...] = (1.0,)) -> WarpWindow | None:
    """The saved window whose rectangle is the live window's ``rect`` (screen px), at one of ``scales``."""
    best: tuple[int, WarpWindow] | None = None
    for window in windows:
        for scale in scales or (1.0,):
            saved = tuple(int(round(v * scale)) for v in window.rect)
            off = max(abs(a - b) for a, b in zip(saved, rect))
            if off <= MATCH_SLACK_PX and (best is None or off < best[0]):
                best = (off, window)
    return best[1] if best else None


def window_data(rect: tuple[int, int, int, int], live_title: str, *, privacy: Any = None,
                db_path: Path | None = None, scales: tuple[float, ...] = (1.0,)) -> dict[str, Any] | None:
    """What Warp's own saved state says about the live window at ``rect``: its tabs (title, folder, which one
    shows), for a window UI Automation cannot see into. ``None`` when memory may not read Warp (the
    ``[terminal]`` rules, a blocked app) or no saved window matches. The live title marks the tab showing when
    it names one; otherwise the saved active tab is given as saved."""
    section = PrivacySection(privacy, "terminal", TerminalRules.from_dict)
    rules: TerminalRules = section.get()
    if not rules.enabled:
        return None
    try:
        if privacy is not None and privacy.rules().check_app("warp.exe", "Warp"):
            return None
    except Exception:
        return None
    path = Path(db_path) if db_path else (Path(os.path.expandvars(rules.database)) if rules.database
                                          else default_db_path())
    windows = read_session(path, rules=rules)
    window = window_for(rect, windows, scales)
    if window is None:
        return None
    live = clean_title(live_title)
    showing = next((t.index for t in window.tabs if t.title and live and (t.title == live or live.startswith(t.title)
                                                                          or t.title.startswith(live))), None)
    try:
        saved_at = max(p.stat().st_mtime for p in (path, path.with_name(path.name + "-wal")) if p.exists())
    except ValueError:
        saved_at = 0.0
    tabs = []
    for tab in window.tabs:
        is_showing = tab.index == showing if showing is not None else tab.active
        tabs.append({"n": tab.index + 1, "title": live if is_showing and live else tab.title,
                     "folder": tab.folder, "showing": is_showing, "panes": tab.panes})
    return {"source": "Warp's own saved window state (read-only)", "saved_at": saved_at, "tabs": tabs,
            "vertical_tabs": window.vertical_tabs, "showing_from": "title" if showing is not None else "saved"}


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


__all__ = ["WarpReader", "TerminalRules", "WarpTab", "WarpWindow", "clean_title", "read_session", "window_data",
           "window_for", "probe", "default_db_path", "parse_warp_ts", "CHECKPOINT", "EVERY_S",
           "SETTLE_S"]
