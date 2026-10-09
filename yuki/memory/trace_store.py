"""The action trace's tables (migration 38): writes, the element registry, retention, forgetting, read helpers.

Functions over a :class:`yuki.memory.store.Store` (its connection, lock and key), kept out of ``store.py`` so the
trace stays one owner's code. Everything content-bearing is encrypted with the memory key; digests are keyed HMACs.

* :func:`write_batch`: one transaction per batch (the trace worker writes at most every 2 s): the action rows and the
  element registry (counts, distinct days, names only as :mod:`yuki.memory.trace_privacy` allows: a command
  control's name on first use, a title-class name once used on 3 distinct days, a content control's never).
* :func:`purge`: decision D4, raw rows (actions, links, file facts) older than 30 days; elements unused for 60 days.
* :func:`forget` / :func:`undo_forget`: forget an hour (any time range) or an app: the raw rows and what was learned
  from them (elements, file facts, links), with a diary line in ``memory_changelog`` (pass ``trace``) and the
  before-image in ``undo_images`` (surface ``trace``) so it can be undone while the rows would still be kept.
* :func:`attach_episodes`: the episode id of each row once its episode window is final (episodes are written after
  the fact).
* :func:`match_runs`: Yuki's own actions that made no tagged input (UI Automation pattern calls, the agent's shell
  scripts) labelled ``yuki`` by the run trace's step and time.
* :func:`summary`, :func:`paste_joins`: content-free read helpers (the CLI, the doctor, the acceptance analyzer).
* :func:`load_game_evidence` / :func:`save_game_evidence`: the hard game gate's learned set (``ui_game_processes``).
* :func:`scrub_names` / :func:`scrub_names_once`: phase F (fPrivacy): the names stored before the current name rule
  (:data:`yuki.memory.trace_privacy.NAME_RULE`) that it would not keep (a document nested in a list item: a chat
  app's message) cleared by the live rule's own function, once per rule version, from the memory service's start.
  CLI: ``python -m yuki.memory.trace_store scrub-names [--dry-run] [--db PATH]``.
"""

from __future__ import annotations

import bisect
import itertools
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from yuki.memory.trace_privacy import (COMMAND, CONTENT, NAME_RULE, NESTED_CONTENT_ROLES, PROMOTE_DAYS, TITLE,
                                       nested_content)

RAW_DAYS = 30.0          # decision D4
ELEMENT_DAYS = 60.0      # report 78 §8
DAY_S = 86400.0

#: ``ui_actions`` columns a batch row may set (anything else in a row dict is ignored).
ACTION_COLUMNS = (
    "at", "ended_at", "process", "page_key", "kind", "source", "via", "run_key", "run_step", "during_run", "role",
    "name_ciphertext", "name_digest", "position", "aid_ciphertext", "aid_digest", "element_key", "container_digest",
    "point", "button", "chord", "typed", "n", "clip_seq", "clip_formats", "file_ciphertext", "file_ext",
    "file_digest", "command_ciphertext", "exit_code", "outcome", "outcome_ciphertext", "facts", "capture_id",
    "episode_id", "resolved", "lookup_ms", "travel_px", "gap_ms", "dwell_ms", "end_role", "end_name_ciphertext",
    "end_name_digest", "end_element_key", "end_position", "proc_started",
)
_INSERT = (f"INSERT INTO ui_actions({', '.join(ACTION_COLUMNS)}) VALUES ({', '.join('?' * len(ACTION_COLUMNS))})")
_ELEMENT_COLUMNS = ("element_key", "process", "role", "name_class", "name_ciphertext", "aid_ciphertext", "days", "n",
                    "first_at", "last_at", "last_day")


def day_of(at: float) -> str:
    """The local day of an epoch time (``YYYY-MM-DD``)."""
    return datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d")


# -- writes ------------------------------------------------------------------------------------------------------------


def _upsert_element(conn: sqlite3.Connection, store: Any, el: dict[str, Any]) -> str | None:
    """One use of an element: counts, distinct days, and its name only as its class allows. Returns, for a
    command-type element the registry knows as an item of a list (capQ2a; second verify V2-1), which of its raw rows
    must keep no name: ``"all"`` (just found to be an item: rows written before, while it was taken for a command, too)
    or ``"batch"`` (this batch's rows: the use came without its current-item mark); else None."""
    key = el.get("element_key")
    if not key:
        return None
    at = float(el["at"])
    day = day_of(at)
    row = conn.execute("SELECT days, n, last_day, name_ciphertext, name_class FROM ui_elements WHERE element_key=?",
                       (key,)).fetchone()
    cls = el.get("name_class") or ""
    name = el.get("name") or None
    if row is None:
        days = 1
        keep = name if cls == COMMAND or (cls == TITLE and days >= PROMOTE_DAYS) else None
        conn.execute(
            "INSERT INTO ui_elements(element_key, process, role, name_class, name_ciphertext, aid_ciphertext, days, n,"
            " first_at, last_at, last_day) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (key, el.get("process"), el.get("role"), cls, store._enc(keep) if keep else None,
             store._enc(el["aid"]) if el.get("aid") else None, days, 1, at, at, day))
        return None
    days = int(row["days"] or 0) + (1 if row["last_day"] != day else 0)
    if cls == CONTENT and row["name_class"] != CONTENT:
        # phase F (fPrivacy): the name rule now says content for this element (a document nested in a list item: a
        # chat app's message bubble). The element key carries its role and ancestry, so every earlier use was the
        # same element: the registry and every raw row of it lose the name, whatever the days
        conn.execute("UPDATE ui_elements SET name_class=?, name_ciphertext=NULL, n=n+1, days=?, last_day=?,"
                     " last_at=max(coalesce(last_at, 0), ?) WHERE element_key=?", (CONTENT, days, day, at, key))
        return "content"
    sticky = cls == COMMAND and row["name_class"] == TITLE
    if sticky:
        # capQ2a second verify V2-1: the content mark is sticky. A command-type element is only ever stored as a
        # title when it was seen as an item of a list or of a set with a current item (the element key carries its
        # role and ancestors, so only the current-item mark can differ between uses: which thread is open). A later
        # use with no current item in view is still that item: the title rule, never its name before 3 days
        cls = TITLE
    set_name = None
    if row["name_ciphertext"] is None and name and (cls == COMMAND or (cls == TITLE and days >= PROMOTE_DAYS)):
        set_name = store._enc(name)
    scrub = "batch" if sticky else None
    if cls == TITLE and row["name_class"] == COMMAND:
        # capQ2a: an element kept as a command control is now known to be an item of a list (a sidebar's thread drawn
        # as a Button): from this use on it follows the title rule, its name kept only once used on 3 distinct days.
        # Second verify V2-1: its raw rows written while it was taken for a command lose their name too
        conn.execute("UPDATE ui_elements SET name_class=?" + (", name_ciphertext=NULL" if days < PROMOTE_DAYS else "")
                     + " WHERE element_key=?", (TITLE, key))
        if days < PROMOTE_DAYS:
            set_name = None
        scrub = "all"
    conn.execute(
        "UPDATE ui_elements SET n=n+1, days=?, last_day=?, last_at=max(coalesce(last_at, 0), ?),"
        " name_ciphertext=coalesce(name_ciphertext, ?), aid_ciphertext=coalesce(aid_ciphertext, ?) WHERE element_key=?",
        (days, day, at, set_name, store._enc(el["aid"]) if el.get("aid") else None, key))
    return scrub


def write_batch(store: Any, rows: Sequence[dict[str, Any]], elements: Sequence[dict[str, Any]] = (),
                files: Sequence[dict[str, Any]] = ()) -> list[int]:
    """Write one batch in one transaction; returns the new action ids in order.

    ``rows``: column -> value (already encrypted where a ``*_ciphertext`` column); ``elements``: one use each
    (``element_key``, ``process``, ``role``, ``name_class``, ``name`` (plain, already filtered), ``aid``, ``at``);
    ``files``: decision D6 facts, ``{"row": index into rows, "kind", "outcome", "data", "ms", "file_digest"}``
    (``data`` a dict, encrypted here)."""

    def op(conn: sqlite3.Connection) -> list[int]:
        ids = []
        for r in rows:
            values = {**r, "during_run": int(r.get("during_run") or 0)}
            ids.append(int(conn.execute(_INSERT, [values.get(c) for c in ACTION_COLUMNS]).lastrowid))
        for el in elements:
            scrub = _upsert_element(conn, store, el)
            if scrub == "content":
                # phase F: every raw row of a nested document keeps a digest only (start and drag end alike)
                conn.execute("UPDATE ui_actions SET name_ciphertext=NULL WHERE element_key=?"
                             " AND name_ciphertext IS NOT NULL", (el["element_key"],))
                conn.execute("UPDATE ui_actions SET end_name_ciphertext=NULL WHERE end_element_key=?"
                             " AND end_name_ciphertext IS NOT NULL", (el["element_key"],))
                continue
            if scrub == "all" or (scrub and ids):
                # capQ2a second verify V2-1: a list's item keeps a digest only in its raw rows (by the element_key
                # index; a drag's end element only in this batch)
                floor = 0 if scrub == "all" else ids[0]
                conn.execute("UPDATE ui_actions SET name_ciphertext=NULL WHERE element_key=? AND id>=?"
                             " AND name_ciphertext IS NOT NULL", (el["element_key"], floor))
                if ids:
                    conn.execute("UPDATE ui_actions SET end_name_ciphertext=NULL WHERE id>=? AND end_element_key=?"
                                 " AND end_name_ciphertext IS NOT NULL", (ids[0], el["element_key"]))
        for f in files:
            index = f.get("row")
            action = ids[index] if isinstance(index, int) and 0 <= index < len(ids) else f.get("action_id")
            conn.execute("INSERT INTO ui_files(at, action_id, file_digest, kind, outcome, data_ciphertext, parse_ms)"
                         " VALUES (?,?,?,?,?,?,?)",
                         (float(f.get("at") or time.time()), action, f.get("file_digest"), f.get("kind") or "flp",
                          f.get("outcome") or "ok",
                          store._enc(json.dumps(f["data"], ensure_ascii=False)) if f.get("data") else None,
                          f.get("ms")))
        return ids

    if not rows and not elements and not files:
        return []
    return store._write(op)


def update_action(store: Any, action_id: int, **fields: Any) -> None:
    """Set columns of one written row (a hover's dwell once its rest ended, a double click)."""
    cols = [c for c in fields if c in ACTION_COLUMNS]
    if not cols:
        return
    store._write(lambda conn: conn.execute(
        f"UPDATE ui_actions SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
        [fields[c] for c in cols] + [int(action_id)]))


# -- what was on screen ------------------------------------------------------------------------------------------------


def capture_ids(store: Any, wants: Iterable[tuple[str, float]]) -> dict[tuple[str, float], int | None]:
    """The latest capture of each app at or before a time: ``{(process, at): capture id}`` (what was on screen).

    Two queries per app per batch (the capture just before the batch's first row, and those inside its span), walked
    newest-first on the ``captures_at`` index (measured on a copy of the real database: ~0.03-0.1 ms each, against
    ~2-6 ms for a per-row query that sorted the app's captures); rows are assigned in Python."""
    by_process: dict[str, list[float]] = {}
    for process, at in set(wants):
        by_process.setdefault(process, []).append(float(at))
    out: dict[tuple[str, float], int | None] = {}
    for process, ats in by_process.items():
        lo, hi = min(ats), max(ats)
        caps: list[tuple[float, int]] = []
        try:
            caps = _captures_span(store, process, lo, hi)
        except Exception:  # noqa: BLE001 - an older database
            caps = []
        times = [c[0] for c in caps]
        for at in ats:
            i = bisect.bisect_right(times, at) - 1
            out[(process, at)] = caps[i][1] if i >= 0 and times[i] >= at - CAPTURE_LOOKBACK_S else None
    return out


#: How far back the capture for a row may be (s).
CAPTURE_LOOKBACK_S = 6 * 3600.0


def _captures_span(store: Any, process: str, lo: float, hi: float) -> list[tuple[float, int]]:
    """``[(at, id)]`` oldest first: the app's last capture at or before ``lo`` (within the lookback) and every one in
    ``(lo, hi]``."""
    sub = "c.thread_id IN (SELECT id FROM threads WHERE process = ?)"
    out: list[tuple[float, int]] = []
    for hint in (" INDEXED BY captures_at", ""):
        try:
            before = store._query(
                f"SELECT c.at, c.id FROM captures c{hint} WHERE c.at <= ? AND c.at >= ? AND {sub}"
                " ORDER BY c.at DESC LIMIT 1", (lo, lo - CAPTURE_LOOKBACK_S, process))
            inside = store._query(
                f"SELECT c.at, c.id FROM captures c{hint} WHERE c.at > ? AND c.at <= ? AND {sub} ORDER BY c.at",
                (lo, hi, process)) if hi > lo else []
        except sqlite3.OperationalError:
            continue                                          # no such index (an older database): plain query
        out = [(float(r[0]), int(r[1])) for r in before] + [(float(r[0]), int(r[1])) for r in inside]
        break
    return out


# -- retention -----------------------------------------------------------------------------------------------------


def purge(store: Any, *, now: float | None = None, raw_days: float = RAW_DAYS,
          element_days: float = ELEMENT_DAYS) -> dict[str, int]:
    """Decision D4: raw rows older than ``raw_days``; elements unused for ``element_days``. Counts only."""
    now = time.time() if now is None else float(now)
    raw_cut, el_cut = now - raw_days * DAY_S, now - element_days * DAY_S

    def op(conn: sqlite3.Connection) -> dict[str, int]:
        out = {
            "files": conn.execute("DELETE FROM ui_files WHERE at < ?", (raw_cut,)).rowcount,
            "links": conn.execute(
                "DELETE FROM ui_links WHERE coalesce(created_at, 0) < ? OR from_action IN"
                " (SELECT id FROM ui_actions WHERE at < ?) OR to_action IN (SELECT id FROM ui_actions WHERE at < ?)",
                (raw_cut, raw_cut, raw_cut)).rowcount,
            "actions": conn.execute("DELETE FROM ui_actions WHERE at < ?", (raw_cut,)).rowcount,
            "elements": conn.execute("DELETE FROM ui_elements WHERE coalesce(last_at, 0) < ?", (el_cut,)).rowcount,
            # a forget's before-image (undone or not): once the keep is past, nothing in it could come back anyway,
            # so what the user asked to forget never outlives the 30 days
            "undo_images": conn.execute("DELETE FROM undo_images WHERE surface = 'trace' AND at < ?",
                                        (raw_cut,)).rowcount,
        }
        out.update(_purge_c2(conn, now, raw_cut, el_cut, store))
        return out

    return store._write(op)


#: capC2 (report 78 §8): a family not seen for this long is deleted; transitions go with the elements (60 days).
FAMILY_DAYS = 90.0
TRANSITION_DAYS = ELEMENT_DAYS


def _purge_c2(conn: sqlite3.Connection, now: float, raw_cut: float, el_cut: float,
              store: Any = None) -> dict[str, int]:
    """capC2's tables: tasks, their ranges and the pass's runs after D4's 30 days; transitions unused for 60 days;
    families unused for 90 days, and family members older than that (a family outlives its tasks: element keys, a
    short label and each member's route; the families that lost a member are rebuilt from the rest). Counts of what
    went (only the tables where something did); ``{}`` on a database before migration 39."""
    from yuki.memory import trace_families

    try:
        fam_cut = now - FAMILY_DAYS * DAY_S
        out = {
            "task_actions": conn.execute("DELETE FROM ui_task_actions WHERE task_id IN (SELECT id FROM ui_tasks WHERE"
                                         " ended_at < ?)", (raw_cut,)).rowcount,
            "tasks": conn.execute("DELETE FROM ui_tasks WHERE ended_at < ?", (raw_cut,)).rowcount,
            "task_runs": conn.execute("DELETE FROM ui_task_runs WHERE at < ?", (raw_cut,)).rowcount,
            "transitions": conn.execute("DELETE FROM ui_transitions WHERE coalesce(last_at, 0) < ?",
                                        (now - TRANSITION_DAYS * DAY_S,)).rowcount,
        }
        # members older than a family's keep (after the tasks went, so none is counted again from its task row)
        old = conn.execute("SELECT task_id, family_id FROM ui_family_members WHERE ended_at < ?", (fam_cut,)).fetchall()
        conn.execute("DELETE FROM ui_family_members WHERE ended_at < ?", (fam_cut,))
        before = conn.execute("SELECT count(*) FROM ui_families").fetchone()[0]
        if store is not None:
            for fid in sorted({int(r["family_id"]) for r in old}):
                trace_families.rebuild(conn, store, fid, now=now)
        conn.execute("DELETE FROM ui_families WHERE coalesce(last_at, updated_at, 0) < ?", (fam_cut,))
        out["families"] = before - conn.execute("SELECT count(*) FROM ui_families").fetchone()[0]
        out["family_members"] = len(old) + conn.execute(
            "DELETE FROM ui_family_members WHERE family_id NOT IN (SELECT id FROM ui_families)").rowcount
    except sqlite3.OperationalError:
        return {}
    return {k: v for k, v in out.items() if v}


#: A typing run or a drag lasts at most this long (s): how far back a pause's check looks for a row still going on.
_LONGEST_RUN_S = 6 * 3600.0


def _stretch(conn: sqlite3.Connection, lo: float, hi: float, cursor: int) -> tuple[float, float | None]:
    """The stretch of the user's input around ``lo``..``hi`` that a transition can span: from the 2-minute pause
    before ``lo`` to the one after ``hi`` (``trace_links.transition_pairs`` clears every app's last element at such a
    pause, so a replay from a pause is exact). Returns ``(start, end)``; ``end`` None when the stretch runs to the
    newest row folded (``cursor``). Only the user's rows count, as in the fold."""
    from yuki.memory import trace_links

    users = tuple(trace_links.USER_SOURCES)
    um = ",".join("?" * len(users))
    gap = trace_links.PIECE_GAP_S

    def input_until(t: float) -> float | None:
        """The end of the user's last input before ``t`` (a long run counts all along)."""
        got = conn.execute(f"SELECT max(max(at, coalesce(ended_at, at))) FROM ui_actions WHERE at < ? AND at >= ?"
                           f" AND id <= ? AND source IN ({um})", (t, t - _LONGEST_RUN_S, cursor, *users)).fetchone()
        return float(got[0]) if got is not None and got[0] is not None else None

    # back: the latest moment at or before lo with no input in the 2 minutes before it (lo itself, or a row's time),
    # looked for a block of hours at a time: each block's rows oldest first with the input before each row
    start: float | None = None
    top = lo
    while start is None:
        bottom = top - _LONGEST_RUN_S
        rows = conn.execute(f"SELECT at, ended_at FROM ui_actions WHERE at < ? AND at >= ? AND id <= ?"
                            f" AND source IN ({um}) ORDER BY at, id", (top, bottom, cursor, *users)).fetchall()
        running = input_until(bottom)
        marks: list[tuple[float, float | None]] = []           # (a moment, the input's end before it)
        for r in rows:
            at = float(r["at"])
            marks.append((at, running))
            end = max(at, float(r["ended_at"])) if r["ended_at"] is not None else at
            running = end if running is None else max(running, end)
        marks.append((top, running))
        for at, before in reversed(marks):
            if before is None or at - before >= gap:
                start = at
                break
        if start is None:
            top = bottom
    # forward: the first input after hi that comes 2 minutes or more after all input before it
    last_input = input_until(hi + 1e-6)
    probe_at, probe_id = hi, -1
    while True:
        rows = conn.execute(f"SELECT id, at, ended_at FROM ui_actions WHERE (at > ? OR (at = ? AND id > ?)) AND id <= ?"
                            f" AND source IN ({um}) ORDER BY at, id LIMIT 2000",
                            (probe_at, probe_at, probe_id, cursor, *users)).fetchall()
        if not rows:
            return start, None
        for r in rows:
            at = float(r["at"])
            if at > hi and last_input is not None and at - last_input >= gap:
                return start, at
            end = max(at, float(r["ended_at"])) if r["ended_at"] is not None else at
            last_input = end if last_input is None else max(last_input, end)
        probe_at, probe_id = float(rows[-1]["at"]), int(rows[-1]["id"])


def _forget_c2(conn: sqlite3.Connection, store: Any, actions: list[dict[str, Any]], *, since: float | None,
               until: float | None, process: str | None, now: float) -> dict[str, Any]:
    """capC2's part of a forget, inside its transaction and before the rows go: the tasks built on any forgotten row
    (one of its ranges holds the row's time), their ranges, the families they were in (rebuilt from the tasks left,
    deleted when none is left; for an app, every family of that app), and the transitions (an app's all; for a time,
    the pairs the forgotten rows made are taken back as if they had never been). Returns the before-images for undo
    and counts. ``{}`` on a database before migration 39."""
    from yuki.memory import trace_families, trace_links

    try:
        conn.execute("SELECT 1 FROM ui_tasks LIMIT 1")
    except sqlite3.OperationalError:
        return {}
    image: dict[str, Any] = {"tasks": [], "task_actions": [], "families": [], "members": [], "transitions": [],
                             "tkeys": []}
    ats = sorted(float(a["at"]) for a in actions)
    hit: set[int] = set()
    if ats:
        cand = conn.execute("SELECT id FROM ui_tasks WHERE started_at <= ? AND ended_at >= ?",
                            (ats[-1], ats[0])).fetchall()
        for t in cand:
            for rg in conn.execute(
                    "SELECT a.at AS a0, b.at AS a1 FROM ui_task_actions r LEFT JOIN ui_actions a ON a.id ="
                    " r.first_action LEFT JOIN ui_actions b ON b.id = r.last_action WHERE r.task_id = ?",
                    (int(t["id"]),)).fetchall():
                if rg["a0"] is None or rg["a1"] is None:
                    continue
                lo, hi = min(rg["a0"], rg["a1"]), max(rg["a0"], rg["a1"])
                i = bisect.bisect_left(ats, lo)
                if i < len(ats) and ats[i] <= hi:
                    hit.add(int(t["id"]))
                    break
    if process:
        for t in conn.execute("SELECT id, apps FROM ui_tasks").fetchall():
            try:
                if process in json.loads(t["apps"] or "[]"):
                    hit.add(int(t["id"]))
            except (TypeError, ValueError):
                continue
    # family members (a counted task's route, kept after the task expires): a live task's goes with the task; an
    # expired task's goes when its span holds a forgotten row, or (for an app) when it was in that app
    gone_members: set[int] = set(hit)
    if ats:
        for m in conn.execute("SELECT m.task_id, m.started_at, m.ended_at FROM ui_family_members m LEFT JOIN ui_tasks t"
                              " ON t.id = m.task_id WHERE t.id IS NULL AND m.started_at <= ? AND m.ended_at >= ?",
                              (ats[-1], ats[0])).fetchall():
            i = bisect.bisect_left(ats, float(m["started_at"]))
            if i < len(ats) and ats[i] <= float(m["ended_at"]):
                gone_members.add(int(m["task_id"]))
    if process:
        for m in conn.execute("SELECT task_id, apps FROM ui_family_members").fetchall():
            try:
                if process in json.loads(m["apps"] or "[]"):
                    gone_members.add(int(m["task_id"]))
            except (TypeError, ValueError):
                continue
    fams: set[int] = set()
    if gone_members:
        mm = ",".join("?" * len(gone_members))
        members = _rows(conn, f"SELECT * FROM ui_family_members WHERE task_id IN ({mm})", sorted(gone_members))
        image["members"] = [{**m, "vec_ciphertext": m["vec_ciphertext"].hex() if m.get("vec_ciphertext") else None}
                            for m in members]
        fams |= {int(m["family_id"]) for m in members}
    if hit:
        marks = ",".join("?" * len(hit))
        tasks = _rows(conn, f"SELECT * FROM ui_tasks WHERE id IN ({marks})", sorted(hit))
        image["tasks"] = [{**t, "vec_ciphertext": t["vec_ciphertext"].hex() if t.get("vec_ciphertext") else None}
                          for t in tasks]
        image["task_actions"] = _rows(conn, f"SELECT * FROM ui_task_actions WHERE task_id IN ({marks})", sorted(hit))
        fams |= {int(t["family_id"]) for t in tasks if t.get("family_id") is not None}
    if process:
        for f in conn.execute("SELECT id, apps FROM ui_families").fetchall():
            try:
                if process in json.loads(f["apps"] or "[]"):
                    fams.add(int(f["id"]))
            except (TypeError, ValueError):
                continue
    if fams:
        fm = ",".join("?" * len(fams))
        image["families"] = [{**f, "vec_ciphertext": f["vec_ciphertext"].hex() if f.get("vec_ciphertext") else None}
                             for f in _rows(conn, f"SELECT * FROM ui_families WHERE id IN ({fm})", sorted(fams))]
    if hit:
        marks = ",".join("?" * len(hit))
        conn.execute(f"DELETE FROM ui_task_actions WHERE task_id IN ({marks})", sorted(hit))
        conn.execute(f"DELETE FROM ui_tasks WHERE id IN ({marks})", sorted(hit))
    if gone_members:
        mm = ",".join("?" * len(gone_members))
        conn.execute(f"DELETE FROM ui_family_members WHERE task_id IN ({mm})", sorted(gone_members))
    # transitions
    state_after: dict[str, Any] | None = None
    if process and since is None and until is None:
        image["transitions"] = _rows(conn, "SELECT * FROM ui_transitions WHERE process = ?", (process,))
        image["tkeys"] = [[r["process"], r["from_key"], r["to_key"]] for r in image["transitions"]]
        conn.execute("DELETE FROM ui_transitions WHERE process = ?", (process,))
    elif actions:
        state = trace_links._load(store)
        cursor = int(state.get("cursor") or 0)
        start, end = _stretch(conn, ats[0], ats[-1], cursor)
        around = _rows(conn, "SELECT id, at, ended_at, process, kind, source, resolved, element_key FROM ui_actions"
                       " WHERE at >= ? AND at < ? AND id <= ? ORDER BY at, id",
                       (start, end if end is not None else float("inf"), cursor))
        gone = {int(a["id"]) for a in actions}
        with_rows, _l, _i = trace_links.transition_pairs(around)
        without, w_last, w_input = trace_links.transition_pairs([r for r in around if int(r["id"]) not in gone])
        if end is None:
            # the stretch runs to the newest row folded: the carried state is the one without the forgotten rows
            state_after = {"last": w_last, "input": w_input}
        take, give = Counter(with_rows) - Counter(without), Counter(without) - Counter(with_rows)
        keys = sorted({(p, a, b) for (p, a, b, _g, _t) in list(take) + list(give)})
        for p, a, b in keys:
            row = conn.execute("SELECT * FROM ui_transitions WHERE process = ? AND from_key = ? AND to_key = ?",
                               (p, a, b)).fetchone()
            if row is not None:
                image["transitions"].append(dict(row))
        image["tkeys"] = [list(k) for k in keys]
        trace_links.add_pairs(conn, list(take.elements()), sign=-1, before=start)
        trace_links.add_pairs(conn, list(give.elements()), sign=1)
    labels = {int(f["id"]): f.get("label_task") for f in image["families"]}
    for fid in sorted(fams):
        # deleted when no member is left; its label goes when it came from a forgotten task
        trace_families.rebuild(conn, store, fid, now=now,
                               relabel=labels.get(fid) is None or int(labels[fid]) in gone_members)
    # each app's last element (the next transition's start) when it was forgotten
    state = trace_links._load(store)
    last = state.get("last") or {}
    gone_keys = {a.get("element_key") for a in actions if a.get("element_key")}
    changed = False
    if state_after is not None:
        last = {k: list(v) for k, v in (state_after["last"] or {}).items()}
        state["input"] = state_after["input"]
        changed = True
    for proc in list(last):
        if (process and proc == process) or (state_after is None and last[proc] and last[proc][0] in gone_keys):
            last.pop(proc)
            changed = True
    if changed:
        state["last"] = last
        conn.execute("UPDATE nudge_state SET value = ? WHERE name = ?",
                     (store._enc(json.dumps(state, separators=(",", ":"))), trace_links.STATE_KEY))
    counts = {"tasks": len(hit), "families": len(fams), "family_members": len(image["members"]),
              "transitions": len(image["tkeys"])}
    return {"image": image, "counts": counts}


def _undo_c2(conn: sqlite3.Connection, image: dict[str, Any], store: Any = None) -> Counter:
    """Put capC2's before-images back (tasks, ranges, families as they were, transitions as they were). A family's
    id is never used again (AUTOINCREMENT), so its before-image never lands on a newer family; a family put back whose
    members no longer match its count (a task joined since) is rebuilt from them (``store`` given)."""
    restored: Counter = Counter()
    try:
        conn.execute("SELECT 1 FROM ui_tasks LIMIT 1")
    except sqlite3.OperationalError:
        return restored

    def put(table: str, row: dict[str, Any], blob: tuple[str, ...] = ()) -> None:
        values = {k: (bytes.fromhex(v) if k in blob and isinstance(v, str) else v) for k, v in row.items()}
        cols = list(values)
        conn.execute(f"INSERT OR REPLACE INTO {table}({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                     [values[c] for c in cols])

    for f in image.get("families") or []:
        put("ui_families", f, ("vec_ciphertext",))
        restored["families"] += 1
    for m in image.get("members") or []:
        put("ui_family_members", m, ("vec_ciphertext",))
        restored["family_members"] += 1
    for t in image.get("tasks") or []:
        put("ui_tasks", t, ("vec_ciphertext",))
        restored["tasks"] += 1
    for r in image.get("task_actions") or []:
        conn.execute("INSERT INTO ui_task_actions(task_id, first_action, last_action, detour) VALUES (?,?,?,?)",
                     (r["task_id"], r["first_action"], r["last_action"], r["detour"]))
    for p, a, b in image.get("tkeys") or []:
        conn.execute("DELETE FROM ui_transitions WHERE process = ? AND from_key = ? AND to_key = ?", (p, a, b))
    for r in image.get("transitions") or []:
        put("ui_transitions", r)
        restored["transitions"] += 1
    if store is not None:
        from yuki.memory import trace_families

        for f in image.get("families") or []:
            fid = int(f["id"])
            members = conn.execute("SELECT count(*) FROM ui_family_members WHERE family_id = ?", (fid,)).fetchone()[0]
            unplaced = conn.execute(
                "SELECT count(*) FROM ui_tasks t LEFT JOIN ui_family_members m ON m.task_id = t.id WHERE t.family_id"
                " = ? AND m.task_id IS NULL AND t.vec_ciphertext IS NOT NULL", (fid,)).fetchone()[0]
            if int(members) + int(unplaced) != int(f.get("n_tasks") or 0):
                trace_families.rebuild(conn, store, fid)    # something joined since: counted again
    return restored


# -- forgetting ----------------------------------------------------------------------------------------------------


def _rows(conn: sqlite3.Connection, sql: str, params: Sequence[Any]) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _recount_element(conn: sqlite3.Connection, key: str) -> None:
    """An element after some of its uses were forgotten: counts and days from what is left, the name only while its
    class still allows it; gone when nothing is left."""
    left = conn.execute("SELECT at FROM ui_actions WHERE element_key=? OR end_element_key=?", (key, key)).fetchall()
    if not left:
        conn.execute("DELETE FROM ui_elements WHERE element_key=?", (key,))
        return
    days = {day_of(r[0]) for r in left}
    row = conn.execute("SELECT name_class FROM ui_elements WHERE element_key=?", (key,)).fetchone()
    drop_name = row is not None and row[0] == TITLE and len(days) < PROMOTE_DAYS
    conn.execute(
        "UPDATE ui_elements SET n=?, days=?, first_at=?, last_at=?, last_day=?"
        + (", name_ciphertext=NULL" if drop_name else "") + " WHERE element_key=?",
        (len(left), len(days), min(r[0] for r in left), max(r[0] for r in left), day_of(max(r[0] for r in left)), key))


def forget(store: Any, *, since: float | None = None, until: float | None = None, process: str | None = None,
           by: str = "user", now: float | None = None) -> dict[str, Any]:
    """Forget the trace of a time range (an hour) or of an app (``process``: its process key), or both together.

    Deletes the raw rows and cascades to what was learned from them: elements (recounted, or removed when nothing
    is left; forgetting an app removes all of its elements), file facts and links. One diary line goes to
    ``memory_changelog`` (pass ``trace``, action ``forget``, content-free words) and the before-image to
    ``undo_images`` (surface ``trace``) so :func:`undo_forget` can bring it back. Returns counts and the diary id."""
    if since is None and until is None and not process:
        raise ValueError("say which hour or which app to forget")
    now = time.time() if now is None else float(now)
    where, params = ["1=1"], []
    if since is not None:
        where.append("at >= ?")
        params.append(float(since))
    if until is not None:
        where.append("at < ?")
        params.append(float(until))
    if process:
        where.append("process = ?")
        params.append(process)
    cond = " AND ".join(where)

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        actions = _rows(conn, f"SELECT * FROM ui_actions WHERE {cond}", params)
        ids = [a["id"] for a in actions]
        keys = sorted({k for a in actions for k in (a.get("element_key"), a.get("end_element_key")) if k})
        marks = ",".join("?" * len(ids)) or "NULL"
        files = _rows(conn, f"SELECT * FROM ui_files WHERE action_id IN ({marks})", ids) if ids else []
        links = _rows(conn, f"SELECT * FROM ui_links WHERE from_action IN ({marks}) OR to_action IN ({marks})",
                      ids + ids) if ids else []
        el_where = "process = ?" if process and since is None and until is None else None
        if el_where:
            elements = _rows(conn, "SELECT * FROM ui_elements WHERE process = ?", (process,))
        else:
            km = ",".join("?" * len(keys)) or "NULL"
            elements = _rows(conn, f"SELECT * FROM ui_elements WHERE element_key IN ({km})", keys) if keys else []
        # capC2: tasks, families and transitions learned from these rows (read before the rows go)
        c2 = _forget_c2(conn, store, actions, since=since, until=until, process=process, now=now)
        if ids:
            conn.execute(f"DELETE FROM ui_files WHERE action_id IN ({marks})", ids)
            conn.execute(f"DELETE FROM ui_links WHERE from_action IN ({marks}) OR to_action IN ({marks})", ids + ids)
            conn.execute(f"DELETE FROM ui_actions WHERE id IN ({marks})", ids)
        if el_where:
            conn.execute("DELETE FROM ui_elements WHERE process = ?", (process,))
        else:
            for key in keys:
                _recount_element(conn, key)
        counts = {"actions": len(ids), "elements": len(elements), "files": len(files), "links": len(links),
                  **(c2.get("counts") or {})}
        scope = {"since": since, "until": until, "process": process}
        words = (f"Forgot {counts['actions']} recorded actions" + (" of one app" if process else "")
                 + (" in a chosen time" if since is not None or until is not None else "") + ".")
        diary = int(conn.execute(
            "INSERT INTO memory_changelog(at, run_id, pass, action, fact_ids, new_fact_ids, cites, text_ciphertext)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (now, None, "trace", "forget", json.dumps(counts), None, None, store._enc(words))).lastrowid)
        image = {"scope": scope, "actions": actions, "elements": elements, "files": files, "links": links,
                 "c2": c2.get("image") or {}}
        conn.execute(
            "INSERT INTO undo_images(run_id, surface, item_id, action, by, at, before_ciphertext) VALUES"
            " (?,?,?,?,?,?,?)", (None, "trace", diary, "forget", by, now, store._enc(json.dumps(image))))
        return {**counts, "diary_id": diary}

    return store._write(op)


def undo_forget(store: Any, diary_id: int, *, now: float | None = None) -> dict[str, Any]:
    """Bring back what :func:`forget` removed (rows still inside the 30-day keep only). Once only."""
    now = time.time() if now is None else float(now)
    raw_cut = now - RAW_DAYS * DAY_S

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT id, before_ciphertext, undone_at FROM undo_images WHERE surface='trace'"
                           " AND item_id=? AND action='forget'", (int(diary_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "nothing to undo"}
        if row["undone_at"] is not None:
            return {"ok": False, "error": "already undone"}
        image = json.loads(store._dec(row["before_ciphertext"]))
        restored = Counter()
        for a in image.get("actions", []):
            if float(a.get("at") or 0) < raw_cut:
                continue
            cols = [c for c in ("id", *ACTION_COLUMNS) if c in a]
            conn.execute(f"INSERT OR IGNORE INTO ui_actions({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [a[c] for c in cols])
            restored["actions"] += 1
        for e in image.get("elements", []):
            cols = [c for c in _ELEMENT_COLUMNS if c in e]
            conn.execute(f"INSERT OR REPLACE INTO ui_elements({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [e[c] for c in cols])
            restored["elements"] += 1
        for f in image.get("files", []):
            conn.execute("INSERT OR IGNORE INTO ui_files(id, at, action_id, file_digest, kind, outcome,"
                         " data_ciphertext, parse_ms) VALUES (?,?,?,?,?,?,?,?)",
                         (f["id"], f["at"], f["action_id"], f["file_digest"], f["kind"], f["outcome"],
                          f["data_ciphertext"], f["parse_ms"]))
            restored["files"] += 1
        for link in image.get("links", []):
            conn.execute("INSERT OR IGNORE INTO ui_links(id, from_action, to_action, kind, lag_s, created_at)"
                         " VALUES (?,?,?,?,?,?)", (link["id"], link["from_action"], link["to_action"], link["kind"],
                                                   link["lag_s"], link.get("created_at")))
            restored["links"] += 1
        restored.update(_undo_c2(conn, image.get("c2") or {}, store))
        conn.execute("UPDATE undo_images SET undone_at=? WHERE id=?", (now, row["id"]))
        conn.execute(
            "INSERT INTO memory_changelog(at, run_id, pass, action, fact_ids, new_fact_ids, cites, text_ciphertext)"
            " VALUES (?,?,?,?,?,?,?,?)", (now, None, "trace", "undo", json.dumps(dict(restored)), None,
                                          json.dumps([int(diary_id)]),
                                          store._enc(f"Brought back {restored['actions']} recorded actions.")))
        return {"ok": True, **dict(restored)}

    return store._write(op)


# -- the name rule's scrub (phase F, fPrivacy) ---------------------------------------------------------------------

#: ``source_checkpoints`` name holding the name rule (:data:`yuki.memory.trace_privacy.NAME_RULE`) whose scrub has
#: run on this database.
NAME_RULE_CHECKPOINT = "trace.name_rule"
#: The scrub's time for recovering stored ancestries (s): past it, a row is judged by the parent kept on it alone.
RECOVER_BUDGET_S = 20.0
#: Service starts the scrub may end with ancestries it could not recover (review r1 L3): until then the checkpoint is
#: left at ``<rule>:try<n>`` and the next start looks again; the last try clears those rows' names (fails closed).
SCRUB_TRIES = 3
#: While a game runs (the hard game gate holds) the start-up scrub waits, re-asking the gate this often (s): a
#: one-off job, never a CPU burst during a game (plan: background CPU work waits for "any game running").
GAME_WAIT_S = 60.0


def _role_names() -> tuple[str, ...]:
    """UI Automation's control types as the trace names them, and "" (a role that could not be read)."""
    from yuki.memory.trace_uia import ROLE_NAMES

    return tuple(sorted(set(ROLE_NAMES.values()))) + ("",)


def recover_path(cipher: Any, key: str, process: str, role: str, name_digest: str | None, parent: str,
                 seen: dict[tuple[str, ...], int] | None = None, *, deadline: float | None = None
                 ) -> tuple[str, ...] | None:
    """The ancestry (control types, nearest first) an element key was made from, or ``None`` (not found in time).

    The trace keys an element by ``digest("ui-el", process, role, ancestry, name digest)``
    (:meth:`yuki.memory.trace.TraceWorker._element_cols`), the ancestry being up to ``trace_uia.ANCESTORS`` control
    types joined by ``/``. The digest is keyed, so the roles cannot be read back, but control types are a fixed
    vocabulary and the nearest one is kept on the row (``position``): the key is computed for each candidate path
    with that first role and compared. A match is the very path the trace read, never an estimate (capNames WIP
    8d45bb4, lifted). ``seen``: paths found before, tried first."""
    from yuki.memory.trace_uia import ANCESTORS

    def made(path: Sequence[str]) -> str:
        return cipher.digest("ui-el", process or "", role or "", "/".join(path), name_digest or "")

    first = parent or ""
    for path in sorted(seen or {}, key=lambda p: -(seen or {})[p]):
        if (path[0] if path else "") == first and made(path) == key:
            return path
    if not first and made(()) == key:
        return ()
    roles = _role_names()
    for length in range(1, ANCESTORS + 1):
        for rest in itertools.product(roles, repeat=length - 1):
            if deadline is not None and time.perf_counter() > deadline:
                return None
            path = (first, *rest)
            if made(path) == key:
                return path
    return None


def _parent_of(position: str | None) -> str:
    """The parent's control type kept on a row: ``position`` is ``[index/]parent role``."""
    text = str(position or "")
    return text.partition("/")[2] if "/" in text else text


def scrub_names(store: Any, *, dry_run: bool = False, budget_s: float = RECOVER_BUDGET_S,
                fail_closed: bool = False) -> dict[str, Any]:
    """Clear the names stored before the current name rule that it would not keep (phase F, fPrivacy).

    Reads the raw rows of documents and panes (:data:`yuki.memory.trace_privacy.NESTED_CONTENT_ROLES`) that still
    hold a name, as a click's element or a drag's end, recovers each element's stored ancestry
    (:func:`recover_path`) and asks the live rule itself (:func:`yuki.memory.trace_privacy.nested_content`). A path
    not found in time is judged by the parent kept on the row alone. Where the rule says content: the row's name
    (``name_ciphertext`` / ``end_name_ciphertext``) is set to NULL, and the element registry keeps that element as
    content with no name (no row is deleted, so no count changes). One transaction, with SQLite's ``secure_delete``
    on (the old values are overwritten, as ``scrub_secrets``). Returns content-free counts: rows and elements by
    process, role and parent; ``by_parent`` (the parent alone decided) and ``by_ancestry`` (only the recovered path
    did); ``unrecovered``; ``ms``. ``dry_run``: counts only, nothing written. ``fail_closed``: a row whose ancestry
    was not recovered (and whose parent alone does not decide) is cleared too (:func:`scrub_names_once`'s last try)."""
    t0 = time.perf_counter()
    marks = ",".join("?" * len(NESTED_CONTENT_ROLES))
    roles = sorted(NESTED_CONTENT_ROLES)
    try:
        starts = store._query(
            f"SELECT id, process, role, position, element_key, name_digest FROM ui_actions WHERE role IN ({marks})"
            " AND name_ciphertext IS NOT NULL", roles)
        ends = store._query(
            f"SELECT id, process, end_role AS role, end_position AS position, end_element_key AS element_key,"
            f" end_name_digest AS name_digest FROM ui_actions WHERE end_role IN ({marks})"
            " AND end_name_ciphertext IS NOT NULL", roles)
        named = store._query(
            f"SELECT element_key, process, role FROM ui_elements WHERE role IN ({marks})"
            " AND (name_ciphertext IS NOT NULL OR coalesce(name_class, '') != ?)", [*roles, CONTENT])
    except sqlite3.OperationalError:
        return {"skipped": "no_trace_tables"}
    deadline = t0 + max(0.0, float(budget_s))
    seen: Counter = Counter()
    verdicts: dict[tuple[str, str, str, str, str], tuple[bool, str]] = {}

    def judge(r: Any) -> tuple[bool, str]:
        """(content, how): how = ``parent`` | ``ancestry`` | ``title`` | ``unrecovered``."""
        parent = _parent_of(r["position"])
        ident = (str(r["process"] or ""), str(r["role"] or ""), str(r["element_key"] or ""),
                 str(r["name_digest"] or ""), parent)
        if ident in verdicts:
            return verdicts[ident]
        if nested_content(ident[1], (), parent or None):
            out = (True, "parent")
        else:
            path = None
            if ident[2]:
                path = recover_path(store.cipher, ident[2], ident[0], ident[1], ident[3] or None, parent, seen,
                                    deadline=deadline)
            if path is None:
                out = (bool(fail_closed), "unrecovered")
            else:
                seen[path] += 1
                out = (True, "ancestry") if nested_content(ident[1], path, parent or None) else (False, "title")
        verdicts[ident] = out
        return out

    row_ids: list[int] = []
    end_ids: list[int] = []
    keys: set[str] = set()
    by_where: Counter = Counter()
    how: Counter = Counter()
    for kind, rows, target in (("start", starts, row_ids), ("end", ends, end_ids)):
        for r in rows:
            content, why = judge(r)
            how[why] += 1
            if not content:
                continue
            target.append(int(r["id"]))
            if r["element_key"]:
                keys.add(str(r["element_key"]))
            by_where[f"{r['process'] or ''}|{r['role'] or ''}|{_parent_of(r['position'])}|{kind}"] += 1
    registry = [str(e["element_key"]) for e in named if str(e["element_key"]) in keys]
    out: dict[str, Any] = {
        "rule": NAME_RULE, "dry_run": bool(dry_run), "rows": len(row_ids), "end_rows": len(end_ids),
        "elements": len(registry), "by_parent": how["parent"], "by_ancestry": how["ancestry"],
        "kept_title": how["title"], "unrecovered": how["unrecovered"], "fail_closed": bool(fail_closed),
        "where": dict(sorted(by_where.items())),
    }
    if not dry_run and (row_ids or end_ids or registry):
        def op(conn: sqlite3.Connection) -> None:
            for chunk in _chunks(row_ids):
                conn.execute(f"UPDATE ui_actions SET name_ciphertext=NULL WHERE id IN ({','.join('?' * len(chunk))})",
                             chunk)
            for chunk in _chunks(end_ids):
                conn.execute("UPDATE ui_actions SET end_name_ciphertext=NULL WHERE id IN"
                             f" ({','.join('?' * len(chunk))})", chunk)
            for chunk in _chunks(registry):
                conn.execute(f"UPDATE ui_elements SET name_class=?, name_ciphertext=NULL WHERE element_key IN"
                             f" ({','.join('?' * len(chunk))})", [CONTENT, *chunk])

        _secure_write(store, op)
    out["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
    return out


def _chunks(values: Sequence[Any], size: int = 400) -> Iterable[Sequence[Any]]:
    for i in range(0, len(values), size):
        yield values[i:i + size]


def _secure_write(store: Any, op: Any) -> Any:
    """``store._write(op)`` with SQLite's ``secure_delete`` on for it (the connection's own setting put back). The
    store's lock (re-entrant: ``_write`` takes it again) is held across the setting, the write and the put-back, so no
    other write runs with the setting changed."""
    lock, conn = getattr(store, "_lock", None), getattr(store, "_conn", None)
    if lock is None or conn is None:
        return store._write(op)
    with lock:
        try:
            previous: int | None = int(conn.execute("PRAGMA secure_delete").fetchone()[0])
            conn.execute("PRAGMA secure_delete=ON")
        except sqlite3.Error:
            previous = None
        try:
            return store._write(op)
        finally:
            if previous is not None:
                try:
                    conn.execute(f"PRAGMA secure_delete={previous}")
                except sqlite3.Error:
                    pass


def scrub_names_once(store: Any, log: Any = None, *, hold: Any = None, stop: Any = None,
                     wait_s: float = GAME_WAIT_S) -> dict[str, Any] | None:
    """Run :func:`scrub_names` once per name rule (``source_checkpoints['trace.name_rule']``); the memory service
    calls it at start, off its main thread. Logs ``trace_name_scrub`` (content-free counts, ``complete``,
    ``attempt``). ``None``: already done for this rule (or stopped while waiting). A failure leaves the checkpoint as
    it was, so the next start tries again.

    Review r1 L3: it never fails open. A run that leaves ancestries unrecovered sets the checkpoint to
    ``<rule>:try<n>``, not ``<rule>``, so the next start looks again; the :data:`SCRUB_TRIES`-th run clears those rows'
    names (``fail_closed``) and sets ``<rule>``. ``hold``: while it returns true (the hard game gate holds:
    :func:`yuki.memory.gamegate.gate_holds`) the scrub waits, asking again every ``wait_s`` (``stop``, an Event, ends
    the wait; logged once, ``trace_name_scrub_wait``)."""
    try:
        mark = store.source_checkpoint(NAME_RULE_CHECKPOINT)
    except sqlite3.OperationalError:
        return None
    if mark == NAME_RULE:
        return None
    tries = 0
    if isinstance(mark, str) and mark.startswith(f"{NAME_RULE}:try"):
        try:
            tries = max(0, int(mark[len(f"{NAME_RULE}:try"):]))
        except ValueError:
            tries = 0
    if hold is not None:
        waited = 0
        while _holds(hold):
            if not waited and log is not None:
                _log_quietly(log, "trace_name_scrub_wait", why="game")
            waited += 1
            if stop is not None:
                if stop.wait(max(0.0, float(wait_s))):
                    return None
            else:
                time.sleep(max(0.0, float(wait_s)))
    last = tries + 1 >= SCRUB_TRIES
    counts = scrub_names(store, fail_closed=last)
    if counts.get("skipped"):
        return counts
    complete = last or not counts.get("unrecovered")
    store.set_source_checkpoint(NAME_RULE_CHECKPOINT, NAME_RULE if complete else f"{NAME_RULE}:try{tries + 1}")
    counts.update({"complete": complete, "attempt": tries + 1})
    if log is not None:
        _log_quietly(log, "trace_name_scrub", **counts)
    return counts


def _holds(hold: Any) -> bool:
    """Whether a hold (the game gate) holds; one that raises holds (fails closed, as the gate)."""
    try:
        return bool(hold())
    except Exception:  # noqa: BLE001
        return True


def _log_quietly(log: Any, type_: str, **fields: Any) -> None:
    try:
        log(type_, **fields)
    except Exception:  # noqa: BLE001 - logging never breaks the scrub
        pass


# -- later joins ---------------------------------------------------------------------------------------------------


#: ``nudge_state`` name of :func:`attach_episodes`' cursor: the last episode id it has gone through.
EPISODE_CURSOR = "trace:episode_cursor"


def attach_episodes(store: Any, *, limit: int = 500) -> int:
    """Set ``episode_id`` on rows once their final, current episode exists (episodes are written after the fact).

    Walks the episodes, not the rows: each final episode newer than the cursor (``nudge_state``
    :data:`EPISODE_CURSOR`) labels the rows in its span that have no episode yet, or one a later run replaced
    (``expired_at``). Rows no episode covers are simply never touched again, so they cannot crowd out newer rows.
    At most ``limit`` episodes a call; returns how many rows were labelled."""

    def op(conn: sqlite3.Connection) -> int:
        row = conn.execute("SELECT value FROM nudge_state WHERE name = ?", (EPISODE_CURSOR,)).fetchone()
        try:
            cursor = int(row[0]) if row is not None and row[0] is not None else 0
        except (TypeError, ValueError):
            cursor = 0
        episodes = conn.execute(
            "SELECT id, started_at, ended_at FROM episodes WHERE id > ? AND final = 1 AND expired_at IS NULL"
            " ORDER BY id LIMIT ?", (cursor, int(limit))).fetchall()
        if not episodes:
            return 0
        done = 0
        for ep in episodes:
            done += conn.execute(
                "UPDATE ui_actions SET episode_id = ? WHERE at >= ? AND at <= ? AND (episode_id IS NULL OR NOT EXISTS"
                " (SELECT 1 FROM episodes e2 WHERE e2.id = ui_actions.episode_id AND e2.expired_at IS NULL))",
                (int(ep[0]), float(ep[1]), float(ep[2]))).rowcount
        conn.execute("INSERT INTO nudge_state(name, value, updated_at) VALUES (?, ?, ?) ON CONFLICT(name) DO UPDATE"
                     " SET value = excluded.value, updated_at = excluded.updated_at",
                     (EPISODE_CURSOR, str(int(episodes[-1][0])), time.time()))
        return done

    try:
        return store._write(op)
    except Exception:  # noqa: BLE001 - an older database without final episodes
        return 0


#: How far a run step's span is stretched when matching an action to it (s): the step's own clock and the
#: action's differ by the time the tool took to start.
RUN_PAD_S = 0.5


def match_runs(store: Any, *, since: float, until: float | None = None) -> int:
    """Label ``yuki`` the actions a Yuki run made without its input stamp, matched by the run trace's step and time.

    Two kinds are matched, both only while a run was in progress (``during_run``): event-only actions (an invoke or
    a selection with no input at all: UI Automation pattern calls) and untagged injected input (the agent's shell
    scripts). Each gets the step's ``run_key`` and ``run_step`` when an action step of a run (agent or hand) in the
    same app spans its time. Hardware input (``user``) is never relabelled: during a run it is a takeover."""
    until = time.time() if until is None else float(until)
    try:
        rows = store._query(
            "SELECT id, at, process FROM ui_actions WHERE during_run = 1 AND run_key IS NULL AND at >= ? AND at <= ?"
            " AND (via = 'event_only' OR source = 'user_injected')", (float(since), until))
        if not rows:
            return 0
        steps = store._query(
            "SELECT run_key, step, at, ended_at, app FROM runs WHERE step > 0 AND kind = 'action' AND at >= ?"
            " AND at <= ?", (float(since) - 600.0, until + 60.0))
    except Exception:  # noqa: BLE001
        return 0
    updates: list[tuple[str, int, int]] = []
    for r in rows:
        best = None
        for s in steps:
            start = float(s["at"]) - RUN_PAD_S
            end = float(s["ended_at"] or s["at"]) + RUN_PAD_S
            if start <= float(r["at"]) <= end and (not s["app"] or s["app"] == r["process"]):
                if best is None or float(s["at"]) > float(best["at"]):
                    best = s
        if best is not None:
            updates.append((best["run_key"], int(best["step"]), int(r["id"])))
    if updates:
        store._write(lambda conn: conn.executemany(
            "UPDATE ui_actions SET source='yuki', via='run_step', run_key=?, run_step=? WHERE id=?", updates))
    return len(updates)


# -- reading (content-free) ----------------------------------------------------------------------------------------


def summary(store: Any, *, since: float, until: float | None = None) -> dict[str, Any]:
    """Counts of the trace between two times, by app, kind, source and how the element was found. Content-free."""
    until = time.time() if until is None else float(until)
    out: dict[str, Any] = {"since": since, "until": until}
    for col in ("process", "kind", "source", "resolved"):
        rows = store._query(f"SELECT coalesce({col}, ''), count(*) FROM ui_actions WHERE at >= ? AND at < ?"
                            f" GROUP BY 1 ORDER BY 2 DESC", (float(since), until))
        out[col] = {str(r[0]): int(r[1]) for r in rows}
    out["actions"] = sum(out["kind"].values())
    out["during_run"] = int(store._query("SELECT count(*) FROM ui_actions WHERE at >= ? AND at < ? AND during_run=1",
                                         (float(since), until))[0][0])
    out["typed"] = int(store._query("SELECT coalesce(sum(typed), 0) FROM ui_actions WHERE at >= ? AND at < ?",
                                    (float(since), until))[0][0])
    out["elements"] = int(store._query("SELECT count(*) FROM ui_elements")[0][0])
    out["named_elements"] = int(store._query("SELECT count(*) FROM ui_elements WHERE name_ciphertext IS NOT NULL")
                                [0][0])
    out["files"] = int(store._query("SELECT count(*) FROM ui_files WHERE at >= ? AND at < ?",
                                    (float(since), until))[0][0])
    return out


def paste_joins(store: Any, *, since: float, until: float | None = None) -> list[dict[str, Any]]:
    """Each paste with the copy it pasted (the same clipboard sequence number): ``{"copy", "paste", "from", "to",
    "lag_s"}`` (action ids, process keys). Content-free; C2 turns these into ``ui_links``."""
    until = time.time() if until is None else float(until)
    rows = store._query(
        "SELECT p.id AS paste, p.process AS to_app, p.at AS pat, c.id AS copy, c.process AS from_app, c.at AS cat"
        " FROM ui_actions p JOIN ui_actions c ON c.kind = 'copy' AND c.clip_seq = p.clip_seq AND c.at <= p.at"
        " WHERE p.kind = 'paste' AND p.clip_seq IS NOT NULL AND p.at >= ? AND p.at < ? ORDER BY p.at",
        (float(since), until))
    return [{"copy": int(r["copy"]), "paste": int(r["paste"]), "from": r["from_app"], "to": r["to_app"],
             "lag_s": round(float(r["pat"]) - float(r["cat"]), 2)} for r in rows]


# -- the hard game gate's learned set ------------------------------------------------------------------------------


def load_game_evidence(store: Any) -> dict[str, dict[str, Any]]:
    """``ui_game_processes`` as the gate's evidence dict (``{}`` on an older database)."""
    try:
        rows = store._query("SELECT process, first_seen, last_seen, evidence FROM ui_game_processes")
    except Exception:  # noqa: BLE001
        return {}
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        try:
            entry = json.loads(r["evidence"]) if r["evidence"] else {}
        except (TypeError, ValueError):
            entry = {}
        if isinstance(entry, dict):
            entry.setdefault("first_seen", r["first_seen"])
            entry.setdefault("last_seen", r["last_seen"])
            out[str(r["process"])] = entry
    return out


def save_game_evidence(store: Any, evidence: dict[str, dict[str, Any]]) -> None:
    """Replace ``ui_game_processes`` with the gate's evidence (a handful of rows)."""

    def op(conn: sqlite3.Connection) -> None:
        conn.execute("DELETE FROM ui_game_processes")
        for process, entry in evidence.items():
            conn.execute("INSERT INTO ui_game_processes(process, first_seen, last_seen, evidence) VALUES (?,?,?,?)",
                         (process, entry.get("first_seen"), entry.get("last_seen"),
                          json.dumps(entry, sort_keys=True)))

    store._write(op)


# -- CLI -----------------------------------------------------------------------------------------------------------


class _ReadOnly:
    """The memory database opened ``mode=ro`` with its key read (never made): the small surface the dry run uses
    (``_query``, ``cipher``, ``source_checkpoint``); no migration, nothing written."""

    def __init__(self, path: Any) -> None:
        import threading
        from pathlib import Path

        from yuki.memory.crypto import FieldCipher, load_or_create_key
        from yuki.memory.store import KEY_FILENAME

        path = Path(path)
        key = path.parent / KEY_FILENAME
        if not key.exists():
            raise FileNotFoundError(f"no {KEY_FILENAME} beside {path}")
        self.cipher = FieldCipher(load_or_create_key(key))   # exists: read and unwrapped, never made
        self._conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=10.0,
                                     check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()

    def _query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def _write(self, fn: Any) -> Any:
        raise PermissionError("read-only")

    def source_checkpoint(self, name: str) -> str | None:
        rows = self._query("SELECT value FROM source_checkpoints WHERE name=?", (name,))
        return rows[0]["value"] if rows else None

    def close(self) -> None:
        self._conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    """``scrub-names [--dry-run] [--db PATH]``: phase F's name scrub, counts only (no name is ever printed). The dry
    run opens the database read-only (the live one by default); a real run needs ``--db`` (a copy: on the live
    database the memory service runs it itself, once per rule, at its start)."""
    import argparse
    from pathlib import Path

    parser = argparse.ArgumentParser(prog="python -m yuki.memory.trace_store")
    sub = parser.add_subparsers(dest="cmd", required=True)
    scrub = sub.add_parser("scrub-names", help="clear stored names the current name rule would not keep")
    scrub.add_argument("--dry-run", action="store_true", help="counts only; the database is opened read-only")
    scrub.add_argument("--db", help="the database (default: the live one, dry run only)")
    args = parser.parse_args(argv)
    from yuki.memory.store import Store, default_db_path

    db = Path(args.db) if args.db else default_db_path()
    if args.dry_run:
        ro = _ReadOnly(db)
        try:
            counts = scrub_names(ro, dry_run=True)
            counts["checkpoint"] = ro.source_checkpoint(NAME_RULE_CHECKPOINT)
        finally:
            ro.close()
    else:
        if not args.db or Path(args.db).resolve() == Path(default_db_path()).resolve():
            parser.error("a real run needs --db pointing at a copy; the memory service scrubs the live database")
        store = Store.open(db)
        try:
            counts = scrub_names_once(store) or {"skipped": "already_done", "rule": NAME_RULE}
        finally:
            store.close()
    print(json.dumps(counts, indent=1, sort_keys=True))
    return 0


__all__ = ["ACTION_COLUMNS", "ELEMENT_DAYS", "NAME_RULE_CHECKPOINT", "RAW_DAYS", "attach_episodes", "capture_ids",
           "day_of", "forget", "load_game_evidence", "match_runs", "paste_joins", "purge", "recover_path",
           "save_game_evidence", "scrub_names", "scrub_names_once", "summary", "undo_forget", "update_action",
           "write_batch"]


if __name__ == "__main__":
    raise SystemExit(main())
