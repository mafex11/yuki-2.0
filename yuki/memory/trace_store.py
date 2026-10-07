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
"""

from __future__ import annotations

import bisect
import json
import sqlite3
import time
from collections import Counter
from collections.abc import Iterable, Sequence
from datetime import datetime
from typing import Any

from yuki.memory.trace_privacy import COMMAND, PROMOTE_DAYS, TITLE

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


def _upsert_element(conn: sqlite3.Connection, store: Any, el: dict[str, Any]) -> None:
    """One use of an element: counts, distinct days, and its name only as its class allows."""
    key = el.get("element_key")
    if not key:
        return
    at = float(el["at"])
    day = day_of(at)
    row = conn.execute("SELECT days, n, last_day, name_ciphertext FROM ui_elements WHERE element_key=?",
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
        return
    days = int(row["days"] or 0) + (1 if row["last_day"] != day else 0)
    set_name = None
    if row["name_ciphertext"] is None and name and (cls == COMMAND or (cls == TITLE and days >= PROMOTE_DAYS)):
        set_name = store._enc(name)
    conn.execute(
        "UPDATE ui_elements SET n=n+1, days=?, last_day=?, last_at=max(coalesce(last_at, 0), ?),"
        " name_ciphertext=coalesce(name_ciphertext, ?), aid_ciphertext=coalesce(aid_ciphertext, ?) WHERE element_key=?",
        (days, day, at, set_name, store._enc(el["aid"]) if el.get("aid") else None, key))


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
            _upsert_element(conn, store, el)
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


__all__ = ["ACTION_COLUMNS", "ELEMENT_DAYS", "RAW_DAYS", "attach_episodes", "capture_ids", "day_of", "forget",
           "load_game_evidence", "match_runs", "paste_joins", "purge", "save_game_evidence", "summary",
           "undo_forget", "update_action", "write_batch"]
