"""Families of tasks and their usual paths (report 78 §5.4 and §6.4, phase C2).

A **family** is the tasks the user did for the same goal, the same way: the goal's embedding (the shared embedder,
the same as practice memory's) near the family's centre, and the steps' element keys sharing a long common
subsequence with the family's route. A family of two tasks or more gets a **usual path** ("merge and branch"): the
route of its most typical task (the one sharing the most steps with the others), each step with how many of the
family's tasks took it and when last, and the branches some tasks took instead, with their counts. Detours are
dropped (they are not the task's own steps). Element keys and machine fields only (the chord of a shortcut, a file's
extension, the kind of action); never a name. Names come only when the path is worded for the brain, from the element
registry, which keeps them by C1's rules.

* :func:`refresh` (the practice worker's tick, in the memory service): embeds new or renamed goals, puts each task in
  its family (or a new one), takes out tasks the user said were not one task or not what they did, and rebuilds each
  family it touched.
* :func:`rebuild` (also the trace forget's cascade, inside its transaction): one family from its members again. A
  member (``ui_family_members``) is a counted task's route, apps, span and goal embedding, kept as long as the family
  (90 days), so a family accumulates: a monthly task still reaches 2 tasks and a usual path after the first one's
  30-day task row has gone. Only the user's rejection or rename and a forget take a member out.
* :func:`hints` (:func:`yuki.memory.practice.relevant`): the nearest family's usual path (at most :data:`PATH_STEPS`
  steps) and one trimmed past task of it, within hard character caps.

Families unused for :data:`KEEP_DAYS` are deleted by :func:`yuki.memory.trace_store.purge`, and so are members older
than that (their family is rebuilt from the rest).
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np

from yuki.memory.trace_tasks import REJECTED, UNCLEAR, outcome_words, task_rows

#: A task joins a family when its goal is this near the family's centre (cosine) and its steps share at least
#: :data:`FAMILY_LCS` of the shorter route; at :data:`SAME_GOAL_COSINE` the goal alone is enough (the same goal by
#: another route is a branch of the same family). Starting values, to be checked on real data in C3.
FAMILY_COSINE = 0.75
SAME_GOAL_COSINE = 0.92
FAMILY_LCS = 0.34
#: Steps of one task compared at most, and tasks a usual path is built from at most (the latest).
MAX_KEYS = 80
PATH_TASKS = 12
#: What the brain is shown (``detail``: the second value).
PATH_STEPS, DETAIL_PATH_STEPS = 8, 16
EXAMPLE_STEPS, DETAIL_EXAMPLE_STEPS = 6, 16
PATH_CHARS, DETAIL_PATH_CHARS = 700, 1_600
EXAMPLE_CHARS, DETAIL_EXAMPLE_CHARS = 600, 1_600
STEP_CHARS = 100
GOAL_SHOWN = 140
#: Branches shown with ``detail``.
DETAIL_BRANCHES = 4
#: A family unused this long is deleted (report 78 §8).
KEEP_DAYS = 90.0
#: Goals embedded per :func:`refresh` at most.
EMBED_BATCH = 32
#: The kinds of action that are a step of a route (pointer and field actions on an element).
ELEMENT_STEP_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag", "type", "paste"})
IDENTITY_RESOLVED = frozenset({"point", "event", "snapshot", "focus"})
DAY_S = 86400.0


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


def lcs_ratio(a: Sequence[str], b: Sequence[str]) -> float:
    """The longest common subsequence's length over the shorter route's (0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(lcs_pairs(a, b)) / min(len(a), len(b))


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


def usual(nodes: Sequence[dict[str, Any]], n_tasks: int) -> list[dict[str, Any]]:
    """The usual path: the backbone steps most of the family's tasks took (at least half, and at least 2)."""
    need = max(2, math.ceil(n_tasks / 2))
    return [nd for nd in nodes if int(nd.get("n") or 0) >= need]


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
    old = conn.execute("SELECT path_keys, variants_keys, label_ciphertext, label_task FROM ui_families WHERE id = ?",
                       (int(fid),)).fetchone()
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
    else:
        nodes, branches = json.loads(old["path_keys"]), json.loads(old["variants_keys"] or "[]")
    conn.execute(
        "UPDATE ui_families SET label_ciphertext = ?, label_task = ?, n_tasks = ?, apps = ?, path_keys = ?,"
        " variants_keys = ?, vec_ciphertext = ?, last_at = ?, updated_at = ? WHERE id = ?",
        (label, label_task, len(members), json.dumps([a for a, _n in apps.most_common(6)]),
         json.dumps(nodes, separators=(",", ":")), json.dumps(branches[:12], separators=(",", ":")),
         _blob(store, centre) if centre is not None else None, max(float(m["ended_at"]) for m in members), now,
         int(fid)))
    return "rebuilt"


def _choose(conn: sqlite3.Connection, store: Any, vec: np.ndarray, toks: Sequence[str]) -> int | None:
    best, best_cos = None, -1.0
    for f in conn.execute("SELECT id, vec_ciphertext, path_keys FROM ui_families WHERE vec_ciphertext IS NOT NULL"):
        fv = _vec(store, f["vec_ciphertext"])
        if fv is None or fv.shape != vec.shape:
            continue
        c = float(fv @ vec)
        if c < FAMILY_COSINE:
            continue
        try:
            backbone = [nd["k"] for nd in json.loads(f["path_keys"] or "[]")]
        except (TypeError, ValueError, KeyError):
            backbone = []
        if c >= SAME_GOAL_COSINE or lcs_ratio(toks, backbone) >= FAMILY_LCS:
            if c > best_cos:
                best, best_cos = int(f["id"]), c
    return best


def refresh(store: Any, embedder: Any, *, now: float | None = None) -> dict[str, Any]:
    """Families kept up to date (the practice worker's tick; cheap when nothing changed: two indexed reads).

    1. Tasks the user said were not one task or not what they did leave their family.
    2. Goals with no vector (new, or renamed by the user) are embedded (at most :data:`EMBED_BATCH`); a renamed one
       leaves its family to be placed again.
    3. Each task with a vector and no family joins the nearest family that fits (:func:`_choose`) or starts one.
    4. Every family touched is rebuilt (:func:`rebuild`)."""
    now = time.time() if now is None else float(now)
    try:
        todo = store._query(
            "SELECT id, goal_ciphertext, family_id, user_verdict, vec_ciphertext IS NULL AS novec FROM ui_tasks WHERE"
            " (vec_ciphertext IS NULL AND (user_verdict IS NULL OR user_verdict NOT IN ('not_one', 'not_mine')))"
            " OR (family_id IS NULL AND vec_ciphertext IS NOT NULL AND (user_verdict IS NULL OR user_verdict NOT IN"
            " ('not_one', 'not_mine'))) OR (family_id IS NOT NULL AND user_verdict IN ('not_one', 'not_mine'))"
            " ORDER BY id LIMIT 200")
    except Exception:  # noqa: BLE001 - a database before migration 39
        return {"skipped": "no tasks"}
    if not todo:
        return {"tasks": 0}
    touched: set[int] = set()
    vectors: dict[int, bytes] = {}
    need = [r for r in todo if r["novec"] and r["user_verdict"] not in REJECTED][:EMBED_BATCH]
    model = getattr(embedder, "model_name", None)
    if need and embedder is not None:
        texts = []
        for r in need:
            try:
                texts.append(store._dec(r["goal_ciphertext"]) or "")
            except Exception:  # noqa: BLE001
                texts.append("")
        try:
            got = embedder.embed(texts)
            for r, text, v in zip(need, texts, got):
                if text:
                    vectors[int(r["id"])] = _blob(store, np.asarray(v, dtype=np.float32))
        except Exception:  # noqa: BLE001 - no embedder now: tried again on a later tick
            vectors = {}

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        out = Counter()
        for r in todo:
            tid, fid = int(r["id"]), r["family_id"]
            if r["user_verdict"] in REJECTED:
                if fid is not None:
                    conn.execute("UPDATE ui_tasks SET family_id = NULL WHERE id = ?", (tid,))
                    drop_member(conn, tid)
                    touched.add(int(fid))
                    out["left"] += 1
                continue
            if tid in vectors:
                conn.execute("UPDATE ui_tasks SET vec_ciphertext = ?, embed_model = ?, family_id = NULL WHERE id = ?",
                             (vectors[tid], model, tid))
                drop_member(conn, tid)
                if fid is not None:
                    touched.add(int(fid))
                out["embedded"] += 1
            elif r["novec"]:
                continue                             # no vector yet: placed once it has one
            row = conn.execute("SELECT vec_ciphertext FROM ui_tasks WHERE id = ?", (tid,)).fetchone()
            vec = _vec(store, row["vec_ciphertext"]) if row is not None else None
            if vec is None:
                continue
            toks = [t for t, _a, _at in route(task_rows(store, tid, conn=conn))]
            fam = _choose(conn, store, vec, toks)
            if fam is None:
                fam = int(conn.execute("INSERT INTO ui_families(n_tasks, updated_at) VALUES (0, ?)",
                                       (now,)).lastrowid)
                out["new"] += 1
            conn.execute("UPDATE ui_tasks SET family_id = ? WHERE id = ?", (fam, tid))
            add_member(conn, store, tid, fam)
            # its centre at once, so the next task of this pass can join it; the usual path once, below
            rebuild(conn, store, fam, now=now, path=False)
            touched.add(fam)
            out["placed"] += 1
        for fid in touched:
            rebuild(conn, store, fid, now=now)
        return {"tasks": len(todo), **dict(out), "families": len(touched)}

    return store._write(op)


# ---------------------------------------------------------------------------------------------------------------------
# What the brain sees (practice.relevant)
# ---------------------------------------------------------------------------------------------------------------------


def _clip(text: Any, cap: int) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= cap else t[: max(0, cap - 1)].rstrip() + "…"


def when_words(at: float | None, now: float | None = None) -> str:
    """A natural date and time: "today, 2:05 PM", "yesterday, 9 AM", "25 September, 2 AM"."""
    if not at:
        return ""
    d = datetime.fromtimestamp(float(at))
    today = datetime.fromtimestamp(time.time() if now is None else float(now)).date()
    hour = d.hour % 12 or 12
    clock = f"{hour}{':%02d' % d.minute if d.minute else ''} {'AM' if d.hour < 12 else 'PM'}"
    days = (today - d.date()).days
    day = "today" if days == 0 else "yesterday" if days == 1 else f"{d.day} {d:%B}"
    if days > 300:
        day += f" {d.year}"
    return f"{day}, {clock}"


def _names(store: Any, keys: Sequence[str]) -> dict[str, str | None]:
    """``{element key: kept name or None}`` for the keys the registry still has (a forgotten or long unused element
    is absent: never worded)."""
    out: dict[str, str | None] = {}
    keys = sorted({k for k in keys if k and "|" not in k})
    for start in range(0, len(keys), 400):
        part = keys[start:start + 400]
        try:
            for r in store._query("SELECT element_key, name_ciphertext FROM ui_elements WHERE element_key IN (%s)"
                                  % ",".join("?" * len(part)), part):
                try:
                    name = store._dec(r["name_ciphertext"]) if r["name_ciphertext"] else None
                except Exception:  # noqa: BLE001
                    name = None
                out[r["element_key"]] = " ".join(str(name).split()) if name else None
        except Exception:  # noqa: BLE001
            pass
    return out


_VERB = {"click": "click", "dclick": "double click", "rclick": "right click", "menu": "choose", "select": "select",
         "invoke": "use", "drag": "drag", "type": "type into", "paste": "paste into"}


def step_words(token: str, attrs: dict[str, Any], names: dict[str, str | None]) -> str | None:
    """One route step in plain words for the brain, or None (an element the registry no longer has)."""
    from yuki.memory.trace_tasks import element_label

    app = attrs.get("app") or "the app"
    if "|" in token:
        parts = token.split("|")
        kind = parts[1] if len(parts) > 1 else ""
        if kind == "keys":
            return _clip(f"in {app}: press {parts[2] if len(parts) > 2 else ''}", STEP_CHARS)
        if kind == "switch":
            return f"bring {app} to the front"
        if kind in ("file_save", "file_open"):
            ext = f" ({parts[2]})" if len(parts) > 2 and parts[2] else ""
            return f"in {app}: {'save' if kind == 'file_save' else 'open'} a file{ext}"
        if kind == "command":
            return f"in {app}: run a terminal command"
        if kind == "copy":
            return f"in {app}: copy"
        return None
    if token not in names:
        return None
    label = element_label({"role": attrs.get("role"), "position": attrs.get("pos"), "element_key": token,
                           "kind": attrs.get("kind")}, names[token])
    return _clip(f"in {app}: {_VERB.get(str(attrs.get('kind') or ''), 'use')} {label}", STEP_CHARS)


def _fit(steps: Sequence[str], n: int, chars: int) -> list[str]:
    out, used = [], 0
    for s in steps[:n]:
        if used + len(s) + 2 > chars:
            break
        out.append(s)
        used += len(s) + 2
    return out


def hints(store: Any, query_vec: Any, apps_in_play: Sequence[str] = (), *, floor: float = 0.45,
          app_floor: float = 0.35, detail: bool = False, now: float | None = None) -> dict[str, Any]:
    """The nearest family's usual path and one trimmed past task of it: ``{"path": {...} | None, "example": {...} |
    None}``. The family must clear ``floor`` (``app_floor`` when one of its apps is in play) by its goals' centre
    against the request's words. ``path``: ``{"family", "tasks", "steps" (at most :data:`PATH_STEPS`, worded from kept
    names only), "apps", "last_at", "branches", "cosine"}`` (only for 2 tasks or more; ``detail``: more steps and the
    branches worded); ``example``: ``{"goal", "when", "at", "apps", "outcome", "steps"}`` (an ``unclear`` task may be
    one). Hard character caps. Never raises (``{"path": None, "example": None}``)."""
    empty: dict[str, Any] = {"path": None, "example": None}
    if query_vec is None:
        return empty
    try:
        q = np.asarray(query_vec, dtype=np.float32).ravel()
        q = q / (float(np.linalg.norm(q)) or 1.0)
        in_play = {str(a).casefold() for a in apps_in_play or () if a}
        best, best_cos = None, -1.0
        for f in store._query("SELECT id, label_ciphertext, n_tasks, apps, path_keys, variants_keys, vec_ciphertext,"
                              " last_at FROM ui_families WHERE vec_ciphertext IS NOT NULL AND n_tasks > 0"):
            fv = _vec(store, f["vec_ciphertext"])
            if fv is None or fv.shape != q.shape:
                continue
            c = float(fv @ q)
            apps = {str(a).casefold() for a in json.loads(f["apps"] or "[]")}
            if c < (app_floor if apps & in_play else floor):
                continue
            if c > best_cos:
                best, best_cos = f, c
        if best is None:
            return empty
        out: dict[str, Any] = {"path": None, "example": None}
        n_tasks = int(best["n_tasks"] or 0)
        nodes = json.loads(best["path_keys"] or "[]")
        branches = json.loads(best["variants_keys"] or "[]")
        label = store._dec(best["label_ciphertext"]) if best["label_ciphertext"] else ""
        ex = store._query(
            "SELECT id, goal_ciphertext, started_at, ended_at, apps, outcome_fact, outcome_action FROM ui_tasks WHERE"
            " family_id = ? AND (user_verdict IS NULL OR user_verdict NOT IN ('not_one', 'not_mine'))"
            " ORDER BY ended_at DESC LIMIT 1", (int(best["id"]),))
        ex_route = route(task_rows(store, int(ex[0]["id"]))) if ex else []
        keys = [nd["k"] for nd in nodes] + [k for b in branches for k in b.get("keys") or []] + [t for t, _a, _at in
                                                                                                ex_route]
        names = _names(store, keys)
        if n_tasks >= 2:
            steps = [w for w in (step_words(nd["k"], nd, names) for nd in usual(nodes, n_tasks)) if w]
            n_steps, chars = (DETAIL_PATH_STEPS, DETAIL_PATH_CHARS) if detail else (PATH_STEPS, PATH_CHARS)
            steps = _fit(steps, n_steps, chars)
            if steps:
                path = {"family": _clip(label, GOAL_SHOWN), "tasks": n_tasks, "steps": steps,
                        "apps": json.loads(best["apps"] or "[]")[:4], "last_at": best["last_at"],
                        "branches": sum(1 for b in branches if int(b.get("n") or 0) >= 2), "cosine": round(best_cos, 3)}
                if detail:
                    worded = []
                    for b in branches[:DETAIL_BRANCHES]:
                        bw = [w for w in (step_words(k, a, names) for k, a in zip(b.get("keys") or [],
                                                                                  b.get("attrs") or [])) if w]
                        if bw:
                            worded.append(_clip(f"{int(b.get('n') or 0)} of {n_tasks} times, "
                                                f"{'at the start' if int(b.get('after', -1)) < 0 else 'after step ' + str(int(b['after']) + 1)}"
                                                f": " + "; ".join(bw), 240))
                    path["variants"] = worded
                out["path"] = path
        if ex:
            r = ex[0]
            ext = None
            if r["outcome_action"]:
                got = store._query("SELECT file_ext FROM ui_actions WHERE id = ?", (int(r["outcome_action"]),))
                ext = got[0][0] if got else None
            words = [w for w in (step_words(t, a, names) for t, a, _at in ex_route) if w]
            n_steps, chars = (DETAIL_EXAMPLE_STEPS, DETAIL_EXAMPLE_CHARS) if detail else (EXAMPLE_STEPS, EXAMPLE_CHARS)
            out["example"] = {"goal": _clip(store._dec(r["goal_ciphertext"]) if r["goal_ciphertext"] else "",
                                            GOAL_SHOWN),
                              "when": when_words(r["started_at"], now), "at": r["started_at"],
                              "apps": json.loads(r["apps"] or "[]")[:4],
                              "outcome": outcome_words(r["outcome_fact"] or UNCLEAR, ext),
                              "steps": _fit(words, n_steps, chars), "more": max(0, len(words) - n_steps)}
        return out
    except Exception:  # noqa: BLE001 - a hint is never worth an error on the request
        return empty


__all__ = ["FAMILY_COSINE", "FAMILY_LCS", "KEEP_DAYS", "PATH_STEPS", "SAME_GOAL_COSINE", "add_member", "build_path",
           "drop_member", "hints", "lcs_pairs", "lcs_ratio", "rebuild", "refresh", "route", "step_token", "step_words", "usual",
           "when_words"]
