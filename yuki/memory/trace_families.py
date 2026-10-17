"""The family tables' upkeep for forgetting and expiry (capC2): :func:`rebuild` and :func:`drop_member`.

The families pass (the local embedder grouping the user's tasks, the usual paths and hints the old loop's frame showed)
was deleted with the old learners (audit 17 Oct 2026, decision 3). The ``ui_families`` rows already stored stay until
they expire or the user forgets that time or app; ``yuki.memory.trace_store`` rebuilds a family from the members left
when one goes, and a task correction drops a member (:mod:`yuki.memory.trace_tasks`).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from typing import Any

import numpy as np

from yuki.memory.trace_tasks import task_rows

#: Steps of one task compared at most, and tasks a usual path is built from at most (the latest).
MAX_KEYS = 80
PATH_TASKS = 12
#: The kinds of action that are a step of a route (pointer and field actions on an element).
ELEMENT_STEP_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag", "type", "paste"})
IDENTITY_RESOLVED = frozenset({"point", "event", "snapshot", "focus"})


# ---------------------------------------------------------------------------------------------------------------------
# Routes as tokens
# ---------------------------------------------------------------------------------------------------------------------


def step_token(row: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """One action as a route step: ``(token, attrs)``. The token is an element key, or a machine form (``app|keys|
    Ctrl+K``, ``app|switch``, ``app|file_save|.docx``, ``app|command``, ``app|copy``); ``attrs`` hold the kind, role,
    position and app for wording. Scrolls, hovers and page arrivals are no step."""
    kind, proc = row.get("kind") or "", str(row.get("process") or "")
    attrs = {"kind": kind, "app": proc}
    if kind == "keys" and row.get("chord"):
        return f"{proc}|keys|{row['chord']}", attrs
    if kind in ELEMENT_STEP_KINDS and row.get("element_key") and row.get("resolved") in IDENTITY_RESOLVED:
        return str(row["element_key"]), {**attrs, "role": row.get("role") or "", "pos": row.get("position") or ""}
    if kind == "switch":
        return f"{proc}|switch", attrs
    if kind in ("file_save", "file_open"):
        return f"{proc}|{kind}|{row.get('file_ext') or ''}", {**attrs, "ext": row.get("file_ext") or ""}
    if kind in ("command", "copy"):
        return f"{proc}|{kind}", attrs
    return None


def route(rows: Sequence[dict[str, Any]]) -> list[tuple[str, dict[str, Any], float]]:
    """A task's own rows as route steps ``[(token, attrs, at)]``, repeats in a row folded, at most :data:`MAX_KEYS`."""
    out: list[tuple[str, dict[str, Any], float]] = []
    for r in rows:
        got = step_token(r)
        if got is None:
            continue
        if out and out[-1][0] == got[0]:
            out[-1] = (got[0], got[1], float(r["at"]))
            continue
        out.append((got[0], got[1], float(r["at"])))
    return out[:MAX_KEYS]


def lcs_pairs(a: Sequence[str], b: Sequence[str]) -> list[tuple[int, int]]:
    """The index pairs of a longest common subsequence of two token lists."""
    n, m = len(a), len(b)
    if not n or not m:
        return []
    dp = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        ai, row, nxt = a[i], dp[i], dp[i + 1]
        for j in range(m - 1, -1, -1):
            row[j] = nxt[j + 1] + 1 if ai == b[j] else max(nxt[j], row[j + 1])
    pairs, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            pairs.append((i, j))
            i, j = i + 1, j + 1
        elif dp[i + 1][j] >= dp[i][j + 1]:
            i += 1
        else:
            j += 1
    return pairs


def build_path(routes: Sequence[list[tuple[str, dict[str, Any], float]]]) -> tuple[list[dict[str, Any]],
                                                                                    list[dict[str, Any]]]:
    """Merge and branch: the most typical route (the one sharing the most steps with the others) is the backbone;
    every route is aligned to it by its longest common subsequence. Each backbone step: ``{"k", "kind", "app",
    "role", "pos", "ext", "n" (routes that took it), "last"}``; each branch (steps a route took between two backbone
    steps instead): ``{"after" (backbone index, -1 before the first), "keys": [...], "attrs": [...], "n", "last"}``."""
    routes = [r for r in routes if r]
    if not routes:
        return [], []
    tokens = [[t for t, _a, _at in r] for r in routes]
    if len(routes) == 1:
        best = 0
    else:
        scores = [sum(len(lcs_pairs(tokens[i], tokens[j])) for j in range(len(routes)) if j != i)
                  for i in range(len(routes))]
        best = max(range(len(routes)), key=lambda i: (scores[i], len(tokens[i]) * -1, -i))
    nodes = [{"k": t, **{k: v for k, v in a.items() if v}, "n": 0, "last": 0.0} for t, a, _at in routes[best]]
    branches: dict[tuple[int, tuple[str, ...]], dict[str, Any]] = {}
    for r, toks in zip(routes, tokens):
        pairs = lcs_pairs([nd["k"] for nd in nodes], toks)
        matched = {j: i for i, j in pairs}
        for i, j in pairs:
            nodes[i]["n"] += 1
            nodes[i]["last"] = max(float(nodes[i]["last"]), float(r[j][2]))
        runs: list[tuple[int, list[int]]] = []
        after, run = -1, []
        for j in range(len(toks)):
            if j in matched:
                if run:
                    runs.append((after, run))
                    run = []
                after = matched[j]
            else:
                run.append(j)
        if run:
            runs.append((after, run))
        for after, js in runs:
            key = (after, tuple(toks[j] for j in js))
            b = branches.setdefault(key, {"after": after, "keys": list(key[1]),
                                          "attrs": [{k: v for k, v in r[j][1].items() if v} for j in js],
                                          "n": 0, "last": 0.0})
            b["n"] += 1
            b["last"] = max(float(b["last"]), max(float(r[j][2]) for j in js))
    return nodes, sorted(branches.values(), key=lambda b: (-b["n"], b["after"]))


# ---------------------------------------------------------------------------------------------------------------------
# Vectors
# ---------------------------------------------------------------------------------------------------------------------


def _vec(store: Any, blob: Any) -> np.ndarray | None:
    if blob is None:
        return None
    try:
        v = np.frombuffer(store.cipher.decrypt_bytes(blob), dtype=np.float32)
    except Exception:  # noqa: BLE001
        return None
    norm = float(np.linalg.norm(v))
    return v / norm if norm else None


def _blob(store: Any, v: np.ndarray) -> bytes:
    v = np.asarray(v, dtype=np.float32).ravel()
    norm = float(np.linalg.norm(v)) or 1.0
    return store.cipher.encrypt_bytes((v / norm).astype(np.float32).tobytes())


# ---------------------------------------------------------------------------------------------------------------------
# Building families (the memory service)
# ---------------------------------------------------------------------------------------------------------------------


def add_member(conn: sqlite3.Connection, store: Any, tid: int, fid: int) -> bool:
    """Count one task in a family (inside the caller's transaction): its route (element keys and machine fields, no
    goal, no names), apps, span and goal embedding go to ``ui_family_members``, so the family keeps it after the task
    itself expires at 30 days. False when the task has no vector (or is gone)."""
    t = conn.execute("SELECT started_at, ended_at, apps, vec_ciphertext FROM ui_tasks WHERE id = ?",
                     (int(tid),)).fetchone()
    if t is None or t["vec_ciphertext"] is None:
        return False
    steps = [[tok, attrs, at] for tok, attrs, at in route(task_rows(store, int(tid), conn=conn))]
    conn.execute("INSERT OR REPLACE INTO ui_family_members(task_id, family_id, started_at, ended_at, apps, route,"
                 " vec_ciphertext) VALUES (?,?,?,?,?,?,?)",
                 (int(tid), int(fid), float(t["started_at"]), float(t["ended_at"]), t["apps"],
                  json.dumps(steps, separators=(",", ":")), t["vec_ciphertext"]))
    return True


def drop_member(conn: sqlite3.Connection, tid: int) -> None:
    """Take a task out of its family's count (the user's rejection or rename, a forget)."""
    conn.execute("DELETE FROM ui_family_members WHERE task_id = ?", (int(tid),))


def _members(conn: sqlite3.Connection, store: Any, fid: int) -> list[sqlite3.Row]:
    """The family's members, newest first. A live task of the family with no member row yet (placed before the
    members table, or brought back by a forget's undo) is counted first."""
    for t in conn.execute(
            "SELECT t.id FROM ui_tasks t LEFT JOIN ui_family_members m ON m.task_id = t.id WHERE t.family_id = ?"
            " AND m.task_id IS NULL AND t.vec_ciphertext IS NOT NULL AND (t.user_verdict IS NULL OR t.user_verdict"
            " NOT IN ('not_one', 'not_mine'))", (int(fid),)).fetchall():
        add_member(conn, store, int(t["id"]), fid)
    return conn.execute("SELECT task_id, started_at, ended_at, apps, route, vec_ciphertext FROM ui_family_members"
                        " WHERE family_id = ? ORDER BY ended_at DESC, task_id DESC", (int(fid),)).fetchall()


def _route_of(member: sqlite3.Row) -> list[tuple[str, dict[str, Any], float]]:
    try:
        return [(str(t), dict(a or {}), float(at)) for t, a, at in json.loads(member["route"] or "[]")]
    except (TypeError, ValueError):
        return []


def rebuild(conn: sqlite3.Connection, store: Any, fid: int, *, now: float | None = None, path: bool = True,
            relabel: bool = False) -> str:
    """One family again from its members (inside the caller's transaction): its centre, label, apps, count, last use
    and usual path; deleted when no member is left. Members outlive their tasks (``ui_family_members``), so a task
    done again after the older ones expired adds to the family instead of resetting it. ``path`` False (a task just
    placed, more may follow in the same pass): the usual path is kept as it was (a new family takes its one task's
    route), the rest is worked out. The label is the goal of the live task nearest the centre; with no live task it is
    kept as it was, unless ``relabel`` (a forget took the task it came from: then it goes). Returns ``"deleted"`` or
    ``"rebuilt"``."""
    now = time.time() if now is None else float(now)
    members = _members(conn, store, fid)
    if not members:
        conn.execute("UPDATE ui_tasks SET family_id = NULL WHERE family_id = ?", (int(fid),))
        conn.execute("DELETE FROM ui_families WHERE id = ?", (int(fid),))
        return "deleted"
    vecs = [(m, _vec(store, m["vec_ciphertext"])) for m in members]
    vecs = [(m, v) for m, v in vecs if v is not None]
    dims = Counter(v.shape for _m, v in vecs).most_common(1)
    vecs = [(m, v) for m, v in vecs if dims and v.shape == dims[0][0]]
    centre = np.mean([v for _m, v in vecs], axis=0) if vecs else None
    old = conn.execute("SELECT path_keys, variants_keys, label_ciphertext, label_task, path_n FROM ui_families WHERE"
                       " id = ?", (int(fid),)).fetchone()
    live = {int(r["id"]): r["goal_ciphertext"] for r in conn.execute(
        "SELECT id, goal_ciphertext FROM ui_tasks WHERE family_id = ? AND (user_verdict IS NULL OR user_verdict NOT IN"
        " ('not_one', 'not_mine'))", (int(fid),)).fetchall()}
    near = sorted(vecs, key=lambda mv: -float(mv[1] @ centre)) if centre is not None else [(m, None) for m in members]
    label_task = next((int(m["task_id"]) for m, _v in near if int(m["task_id"]) in live), None)
    if label_task is not None:
        label = live[label_task]
    elif old is not None and not relabel:
        label, label_task = old["label_ciphertext"], old["label_task"]
    else:
        label = None
    apps = Counter(a for m in members for a in json.loads(m["apps"] or "[]"))
    if path or old is None or not old["path_keys"] or old["path_keys"] == "[]":
        chosen = members[:PATH_TASKS] if path else members[:1]
        nodes, branches = build_path([_route_of(m) for m in chosen])
        path_n = len(chosen)
    else:
        nodes, branches = json.loads(old["path_keys"]), json.loads(old["variants_keys"] or "[]")
        path_n = int(old["path_n"] or 0) or min(len(members), PATH_TASKS)
    conn.execute(
        "UPDATE ui_families SET label_ciphertext = ?, label_task = ?, n_tasks = ?, apps = ?, path_keys = ?,"
        " variants_keys = ?, vec_ciphertext = ?, last_at = ?, updated_at = ?, path_n = ? WHERE id = ?",
        (label, label_task, len(members), json.dumps([a for a, _n in apps.most_common(6)]),
         json.dumps(nodes, separators=(",", ":")), json.dumps(branches[:12], separators=(",", ":")),
         _blob(store, centre) if centre is not None else None, max(float(m["ended_at"]) for m in members), now,
         path_n, int(fid)))
    return "rebuilt"


# ---------------------------------------------------------------------------------------------------------------------
# What the brain sees (practice.relevant)
# ---------------------------------------------------------------------------------------------------------------------


def _clip(text: Any, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[: max(0, cap - 1)].rstrip() + "…"


__all__ = ["add_member", "build_path", "drop_member", "lcs_pairs", "rebuild", "route", "step_token"]
