"""Links and transitions from the action trace, at once, in code (report 78 §5.1 and §5.4, phase C2).

Two things code works out from the trace rows as they come, with no model and no guess:

**Links** (``ui_links``, migration 38's table, filled here): only exact joins on machine identities, three kinds.

* ``clipboard``: a paste of the user's whose clipboard sequence number is the one the user's latest copy before it
  set. The copy must be the *latest* copy row at or before the paste: Windows' sequence number only grows within one
  boot, so a later copy row with another number means the clipboard changed since, and an equal number after a
  restart can never be joined to a copy from before it (a later copy always sits between them).
* ``file``: a file opened whose path digest is that of a file saved earlier (``file_save``). A download is the
  browser's save of the file, so "a download then an open" is the same join.
* ``url``: a click or invoke on a ``Hyperlink`` in one app, then within :data:`URL_S` a browser (another app) in
  front with a new page: its ``nav`` or ``switch`` row carries ``page_reached``, or the switch row's page differs from
  that app's page before. One link per hyperlink press.

An Alt+Tab or a taskbar switch is a ``switch`` *action*, never a link. Only the user's own rows join (``user`` /
``user_injected``), never Yuki's. A near miss (another sequence number, another digest, a page 3.1 s later, a page
in the same app) is no link.

**Transitions** (``ui_transitions``): per app, element key -> the next element key the user acted on in that app,
with the count, the median gap (over the last :data:`GAPS_KEPT` gaps) and the last use. Digests only. Like the task
pass's pieces, a transition never spans a pause of :data:`PIECE_GAP_S` with no input from the user at all.

Both are kept up to date incrementally by :func:`update` (the practice worker's tick): the rows written since its
cursor (``ui_actions.id``), at most :data:`BATCH` a call, with the cursor and each app's last element in one encrypted
``nudge_state`` value (:data:`STATE_KEY`). Retention (30 days for links with their rows, 60 days unused for
transitions) and the forget cascade are :mod:`yuki.memory.trace_store`'s.
"""

from __future__ import annotations

import json
import sqlite3
import statistics
import time
from collections.abc import Iterable, Sequence
from typing import Any

#: The three link kinds (report 78 §5.1).
LINK_KINDS = ("clipboard", "file", "url")
#: A hyperlink press and the browser's new page in front: at most this far apart (s).
URL_S = 3.0
#: A pause with no input from the user at all of at least this long cuts the trace (the task pass's own pre-cut,
#: report 78 §5.2): no transition spans it.
PIECE_GAP_S = 120.0
#: The last gaps of a transition kept for its median.
GAPS_KEPT = 15
#: Rows read per :func:`update` call at most.
BATCH = 5_000
#: ``nudge_state`` name of the cursor and each app's last element (encrypted JSON).
STATE_KEY = "trace:links"
#: The user's own input (dictation, Deskflow, G Hub count as theirs, report 78 §4.2).
USER_SOURCES = ("user", "user_injected")
#: How an element was found when its identity counts (never ``stale``, never ``unresolved``).
IDENTITY_RESOLVED = frozenset({"point", "event", "snapshot", "focus"})
#: The kinds of action that act on an element (a transition's ends); hovers and scrolls are not.
ELEMENT_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag", "type", "paste", "keys"})
#: The kinds of press a hyperlink is followed by.
PRESS_KINDS = ("click", "dclick", "invoke")
#: Raw rows are kept this long (decision D4): a join never looks further back.
LOOKBACK_S = 30 * 86400.0

_COLUMNS = ("id, at, process, kind, source, resolved, role, element_key, clip_seq, file_digest, facts, page_key")


def _facts(row: Any) -> dict[str, Any]:
    try:
        got = json.loads(row["facts"] or "{}")
    except (TypeError, ValueError, KeyError, IndexError):
        return {}
    return got if isinstance(got, dict) else {}


def _user(row: Any) -> bool:
    return row["source"] in USER_SOURCES


# -- links ---------------------------------------------------------------------------------------------------------


def _clipboard_link(conn: sqlite3.Connection, r: Any) -> tuple[int, int, str, float] | None:
    if r["kind"] != "paste" or r["clip_seq"] is None or not _user(r):
        return None
    copy = conn.execute(
        "SELECT id, at, clip_seq, source FROM ui_actions INDEXED BY ui_actions_kind WHERE kind = 'copy' AND at <= ?"
        " AND at >= ? ORDER BY at DESC, id DESC LIMIT 1", (float(r["at"]), float(r["at"]) - LOOKBACK_S)).fetchone()
    if copy is None or copy["clip_seq"] is None or int(copy["clip_seq"]) != int(r["clip_seq"]) or not _user(copy):
        return None
    return int(copy["id"]), int(r["id"]), "clipboard", round(float(r["at"]) - float(copy["at"]), 2)


def _file_link(conn: sqlite3.Connection, r: Any) -> tuple[int, int, str, float] | None:
    if r["kind"] != "file_open" or not r["file_digest"] or not _user(r):
        return None
    save = conn.execute(
        "SELECT id, at, source FROM ui_actions INDEXED BY ui_actions_file WHERE file_digest = ? AND kind = 'file_save'"
        " AND at < ? AND at >= ? AND id != ? ORDER BY at DESC LIMIT 1",
        (r["file_digest"], float(r["at"]), float(r["at"]) - LOOKBACK_S, int(r["id"]))).fetchone()
    if save is None or not _user(save):
        return None
    return int(save["id"]), int(r["id"]), "file", round(float(r["at"]) - float(save["at"]), 2)


def _new_page(conn: sqlite3.Connection, r: Any) -> bool:
    """The row shows a browser with a new page in front: ``page_reached``, or a switch row whose page differs from
    that app's page before it (both machine fields)."""
    if r["kind"] not in ("nav", "switch"):
        return False
    if _facts(r).get("page_reached"):
        return True
    if r["kind"] != "switch" or not r["page_key"]:
        return False
    before = conn.execute(
        "SELECT page_key FROM ui_actions WHERE process = ? AND at < ? AND page_key IS NOT NULL AND id != ?"
        " ORDER BY at DESC LIMIT 1", (r["process"], float(r["at"]), int(r["id"]))).fetchone()
    return before is not None and before["page_key"] != r["page_key"]


def _url_link(conn: sqlite3.Connection, r: Any, taken: set[int]) -> tuple[int, int, str, float] | None:
    if not _user(r) or not _new_page(conn, r):
        return None
    press = conn.execute(
        "SELECT id, at, process, source FROM ui_actions INDEXED BY ui_actions_at WHERE at <= ? AND at >= ?"
        " AND role = 'Hyperlink' AND kind IN ('click', 'dclick', 'invoke') AND process != ? ORDER BY at DESC LIMIT 1",
        (float(r["at"]), float(r["at"]) - URL_S, r["process"])).fetchone()
    if press is None or not _user(press) or int(press["id"]) in taken:
        return None
    if conn.execute("SELECT 1 FROM ui_links WHERE from_action = ? AND kind = 'url'", (int(press["id"]),)).fetchone():
        return None
    return int(press["id"]), int(r["id"]), "url", round(float(r["at"]) - float(press["at"]), 2)


def find_links(conn: sqlite3.Connection, rows: Iterable[Any]) -> list[tuple[int, int, str, float]]:
    """The links the rows complete (each row as the later end of its join): ``[(from, to, kind, lag_s)]``."""
    out: list[tuple[int, int, str, float]] = []
    taken: set[int] = set()
    for r in sorted(rows, key=lambda x: (float(x["at"]), int(x["id"]))):
        link = _clipboard_link(conn, r) or _file_link(conn, r) or _url_link(conn, r, taken)
        if link is not None:
            out.append(link)
            if link[2] == "url":
                taken.add(link[0])
    return out


# -- transitions ---------------------------------------------------------------------------------------------------


def element_row(row: Any) -> bool:
    """A row whose element can be a transition's end: the user's own, identity taken, an acting kind."""
    return (_user(row) and bool(row["element_key"]) and row["resolved"] in IDENTITY_RESOLVED
            and row["kind"] in ELEMENT_KINDS)


def transition_pairs(rows: Sequence[Any], last: dict[str, list[Any]] | None = None,
                     last_input: float | None = None) -> tuple[list[tuple[str, str, str, float, float]],
                                                               dict[str, list[Any]], float | None]:
    """Fold rows (oldest first) into per-app pairs ``[(process, from_key, to_key, gap_s, at)]``; ``last`` (each app's
    last element ``[key, at]``) and ``last_input`` (the user's last input time) carry over between calls. A pause of
    :data:`PIECE_GAP_S` without the user's input clears every app's last element."""
    last = {k: list(v) for k, v in (last or {}).items()}
    pairs: list[tuple[str, str, str, float, float]] = []
    for r in rows:
        if not _user(r):
            continue
        at = float(r["at"])
        if last_input is not None and at - last_input >= PIECE_GAP_S:
            last = {}
        last_input = at if last_input is None else max(last_input, at)
        if not element_row(r):
            continue
        proc, key = str(r["process"] or ""), str(r["element_key"])
        prev = last.get(proc)
        if prev is not None and prev[0] != key:
            pairs.append((proc, str(prev[0]), key, round(max(0.0, at - float(prev[1])), 3), at))
        last[proc] = [key, at]
    return pairs, last, last_input


def _median(gaps: Sequence[float]) -> float | None:
    return round(float(statistics.median(gaps)), 3) if gaps else None


def add_pairs(conn: sqlite3.Connection, pairs: Iterable[tuple[str, str, str, float, float]], sign: int = 1) -> int:
    """Add (``sign`` 1) or take away (``sign`` -1, a forget) transition pairs. Returns the rows changed."""
    changed = 0
    for proc, a, b, gap, at in pairs:
        row = conn.execute("SELECT n, gaps, last_at FROM ui_transitions WHERE process = ? AND from_key = ? AND"
                           " to_key = ?", (proc, a, b)).fetchone()
        try:
            gaps = [float(g) for g in json.loads(row["gaps"] or "[]")] if row is not None else []
        except (TypeError, ValueError):
            gaps = []
        if sign > 0:
            gaps = (gaps + [float(gap)])[-GAPS_KEPT:]
            if row is None:
                conn.execute("INSERT INTO ui_transitions(process, from_key, to_key, n, median_gap_s, gaps, last_at)"
                             " VALUES (?,?,?,?,?,?,?)", (proc, a, b, 1, _median(gaps), json.dumps(gaps), float(at)))
            else:
                conn.execute("UPDATE ui_transitions SET n = n + 1, median_gap_s = ?, gaps = ?, last_at = max(coalesce("
                             "last_at, 0), ?) WHERE process = ? AND from_key = ? AND to_key = ?",
                             (_median(gaps), json.dumps(gaps), float(at), proc, a, b))
            changed += 1
            continue
        if row is None:
            continue
        if float(gap) in gaps:
            gaps.remove(float(gap))
        n = int(row["n"] or 0) - 1
        if n <= 0:
            conn.execute("DELETE FROM ui_transitions WHERE process = ? AND from_key = ? AND to_key = ?", (proc, a, b))
        else:
            conn.execute("UPDATE ui_transitions SET n = ?, median_gap_s = ?, gaps = ? WHERE process = ? AND"
                         " from_key = ? AND to_key = ?", (n, _median(gaps), json.dumps(gaps), proc, a, b))
        changed += 1
    return changed


# -- the incremental pass ------------------------------------------------------------------------------------------


def _load(store: Any) -> dict[str, Any]:
    raw = store.nudge_state(STATE_KEY)
    if not raw:
        return {"cursor": 0, "last": {}, "input": None}
    try:
        got = json.loads(store._dec(raw) or "{}")
    except Exception:  # noqa: BLE001 - unreadable: start again from the rows kept
        return {"cursor": 0, "last": {}, "input": None}
    return got if isinstance(got, dict) else {"cursor": 0, "last": {}, "input": None}


def update(store: Any, *, now: float | None = None, batch: int = BATCH) -> dict[str, Any]:
    """Join and fold the trace rows written since the last call (at most ``batch``): new links into ``ui_links``, new
    transitions into ``ui_transitions``, in one transaction with the cursor. Cheap when nothing is new (one indexed
    read). Content-free counts."""
    now = time.time() if now is None else float(now)
    try:
        newest = store._query("SELECT max(id) FROM ui_actions")
    except Exception:  # noqa: BLE001 - a database before migration 38
        return {"skipped": "no trace"}
    newest_id = int(newest[0][0] or 0) if newest else 0
    state = _load(store)
    cursor = int(state.get("cursor") or 0)
    if newest_id <= cursor:
        return {"rows": 0}

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        rows = conn.execute(f"SELECT {_COLUMNS} FROM ui_actions WHERE id > ? ORDER BY id LIMIT ?",
                            (cursor, int(batch))).fetchall()
        if not rows:
            return {"rows": 0, "cursor": newest_id, "links": 0, "transitions": 0}
        links = find_links(conn, rows)
        added = 0
        for a, b, kind, lag in links:
            added += conn.execute("INSERT OR IGNORE INTO ui_links(from_action, to_action, kind, lag_s, created_at)"
                                  " VALUES (?,?,?,?,?)", (a, b, kind, lag, now)).rowcount
        ordered = sorted(rows, key=lambda x: (float(x["at"]), int(x["id"])))
        pairs, last, last_input = transition_pairs(ordered, state.get("last") or {}, state.get("input"))
        changed = add_pairs(conn, pairs)
        new_state = {"cursor": int(rows[-1]["id"]), "last": last, "input": last_input}
        conn.execute("INSERT INTO nudge_state(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE"
                     " SET value = excluded.value, updated_at = excluded.updated_at",
                     (STATE_KEY, store._enc(json.dumps(new_state, separators=(",", ":"))), now))
        return {"rows": len(rows), "cursor": new_state["cursor"], "links": added, "transitions": changed,
                "more": new_state["cursor"] < newest_id}

    return store._write(op)


def links_between(store: Any, first: int, last: int) -> list[dict[str, Any]]:
    """The links whose two ends both fall within an action id range: ``[{"from", "to", "kind", "lag_s"}]``."""
    try:
        rows = store._query("SELECT from_action, to_action, kind, lag_s FROM ui_links WHERE from_action >= ? AND"
                            " from_action <= ? AND to_action >= ? AND to_action <= ?",
                            (int(first), int(last), int(first), int(last)))
    except Exception:  # noqa: BLE001
        return []
    return [{"from": int(r[0]), "to": int(r[1]), "kind": r[2], "lag_s": r[3]} for r in rows]


def next_steps(store: Any, process: str, from_key: str, *, limit: int = 3) -> list[dict[str, Any]]:
    """What usually comes next in an app after an element: ``[{"key", "n", "median_gap_s", "last_at"}]``."""
    try:
        rows = store._query("SELECT to_key, n, median_gap_s, last_at FROM ui_transitions WHERE process = ? AND"
                            " from_key = ? ORDER BY n DESC, last_at DESC LIMIT ?", (process, from_key, int(limit)))
    except Exception:  # noqa: BLE001
        return []
    return [{"key": r[0], "n": int(r[1]), "median_gap_s": r[2], "last_at": r[3]} for r in rows]


__all__ = ["BATCH", "GAPS_KEPT", "LINK_KINDS", "PIECE_GAP_S", "STATE_KEY", "URL_S", "add_pairs", "element_row",
           "find_links", "links_between", "next_steps", "transition_pairs", "update"]
