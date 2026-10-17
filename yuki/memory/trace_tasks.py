"""The user's stored tasks (capC2), read and corrected: :func:`day_tasks`, :func:`correct_task`,
:func:`undo_correction`, :func:`task_rows`, :func:`learnable`, :func:`outcome_words`.

The task pass (Haiku cutting each finished stretch of the click trace into tasks) was deleted with the old learners
(audit 17 Oct 2026, decision 3): no new task is written. The tasks already stored still list on the Day page, where the
user can rename one or say it was not theirs (the skill learner reads those "not mine" verdicts,
``yuki.skills.learn.occasions.not_mine_spans``), until they age out with the trace (30 days).
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from yuki.memory.trace_links import USER_SOURCES

UNCLEAR = "unclear"
#: The user's verdicts on a task (the Day page).
VERDICTS = ("renamed", "not_one", "not_mine")
#: Verdicts that take a task out of families, paths and examples.
REJECTED = ("not_one", "not_mine")


# ---------------------------------------------------------------------------------------------------------------------
# Rows, pieces, facts
# ---------------------------------------------------------------------------------------------------------------------

_ROW_COLUMNS = ("id, at, ended_at, process, page_key, kind, source, during_run, resolved, role, name_ciphertext,"
                " position, element_key, chord, typed, n, clip_seq, file_ext, file_digest, exit_code, outcome,"
                " outcome_ciphertext, facts")


def is_user(row: dict[str, Any]) -> bool:
    return row.get("source") in USER_SOURCES


def kept(row: dict[str, Any]) -> bool:
    """A row a task may be built from: the user's own, its identity not stale, not a hover (a rest of the pointer)."""
    return is_user(row) and row.get("resolved") != "stale" and row.get("kind") != "hover"


@dataclass
class Fact:
    fid: str
    kind: str
    action: int
    target: str | None
    words: str


# ---------------------------------------------------------------------------------------------------------------------
# Step lines
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Corrections as worked examples
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# The answer, checked
# ---------------------------------------------------------------------------------------------------------------------


def window_only(fact: Fact, rows: Sequence[dict[str, Any]], detour_ids: set[int] | frozenset[int] = frozenset()
                ) -> str | None:
    """capQ2b: why a window coming to the front is no outcome of this task, or None when it may be. Machine facts
    on the task's own rows (not its detours): ``"nothing_else_there"`` when the user did nothing else in the app
    that came up (one switch, or taskbar clicks, is no learnable task), ``"work_went_on"`` when they went on acting
    in it afterwards (the window coming up began that work; it was not its end), ``"already_there"`` when the app
    that came up is the app of the user's last action before it (switch rows not counted): the window went back to
    the app already in use (another program took the focus for a moment and gave it back), so nothing new came up
    (second verification V2)."""
    if fact.kind != "window_verified":
        return None
    app = str(fact.target or "")
    order = {int(r["id"]): k for k, r in enumerate(rows)}
    at = order.get(int(fact.action))
    acts = [r for r in rows if int(r["id"]) not in detour_ids and int(r["id"]) != int(fact.action)
            and r.get("kind") != "switch"]
    there = [r for r in acts if str(r.get("process") or "") == app]
    if not app or not there:
        return "nothing_else_there"
    if at is not None and any(order[int(r["id"])] > at for r in there):
        return "work_went_on"
    before = [r for r in acts if at is not None and order[int(r["id"])] < at]
    if before and str(before[-1].get("process") or "") == app:
        return "already_there"
    return None


def learnable(task: Any, store: Any = None) -> bool:
    """Whether a task may ever teach a trial or a way (C4's demo learner): only a checked outcome, never ``unclear``,
    never one the user said was not one task or not what they did. An ``unclear`` task may still be shown as a past
    example (report 78 §5.3).

    With ``store`` (review F3), a stored ``window_verified`` outcome is re-checked on the task's own rows by
    :func:`window_only`, as a new answer is: tasks written before that rule (a single switch, a browsing session
    ended by a window coming up) are not learnable either. Read-only: the stored row is never changed."""
    get = task.get if isinstance(task, dict) else (lambda k: getattr(task, k, None))
    if get("outcome_fact") in (None, "", UNCLEAR) or get("user_verdict") in REJECTED:
        return False
    if store is None or get("outcome_fact") != "window_verified":
        return True
    return not stored_window_only(store, task)


def stored_window_only(store: Any, task: Any) -> str | None:
    """:func:`window_only` for a stored task (its own rows, less its detours), or None. A task whose rows cannot be
    read is not learnable from its window: ``"rows_gone"``."""
    get = task.get if isinstance(task, dict) else (lambda k: getattr(task, k, None))
    if get("outcome_fact") != "window_verified":
        return None
    try:
        rows = task_rows(store, int(get("id")))
        action = int(get("outcome_action"))
    except Exception:  # noqa: BLE001 - unreadable: no window outcome to learn from
        return "rows_gone"
    if not rows:
        return "rows_gone"
    return window_only(Fact("", "window_verified", action, get("outcome_target"), ""), rows)


# ---------------------------------------------------------------------------------------------------------------------
# Windows due, cost
# ---------------------------------------------------------------------------------------------------------------------


def _apps(raw: Any) -> list[str]:
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in got] if isinstance(got, list) else []


# ---------------------------------------------------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# The Day page (decision D5): tasks inside each session, correctable
# ---------------------------------------------------------------------------------------------------------------------

_OUTCOME_SAID = {
    "file_saved": "you saved a file", "file_opened": "you opened a file", "page_reached": "you got to the page",
    "window_verified": "the app you wanted came up", "field_submitted": "you sent what you typed",
    "toggle_state": "you switched a setting", "selected": "you picked an item", "command_ok": "your command worked",
    "paste_sent": "you pasted it in and sent it", UNCLEAR: "not sure it got done",
}


def outcome_words(kind: str | None, ext: str | None = None) -> str:
    """A task's outcome in plain words (second person)."""
    words = _OUTCOME_SAID.get(kind or UNCLEAR, _OUTCOME_SAID[UNCLEAR])
    if kind in ("file_saved", "file_opened") and ext:
        words += f" ({ext})"
    return words


def day_tasks(store: Any, since: float, until: float) -> list[dict[str, Any]]:
    """The tasks that started in ``[since, until)``, oldest first: ``[{"id", "ref", "start_at", "end_at", "goal",
    "apps", "outcome", "outcome_words", "verdict", "episode_id"}]``. Reads only. Never raises (``[]``)."""
    try:
        rows = store._query(
            "SELECT t.id, t.episode_id, t.started_at, t.ended_at, t.apps, t.goal_ciphertext, t.outcome_fact,"
            " t.user_verdict, a.file_ext FROM ui_tasks t LEFT JOIN ui_actions a ON a.id = t.outcome_action"
            " WHERE t.started_at >= ? AND t.started_at < ? ORDER BY t.started_at, t.id LIMIT 400",
            (float(since), float(until)))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        try:
            goal = store._dec(r["goal_ciphertext"]) if r["goal_ciphertext"] else ""
        except Exception:  # noqa: BLE001
            continue
        out.append({"id": int(r["id"]), "ref": f"task:{int(r['id'])}", "start_at": float(r["started_at"]),
                    "end_at": float(r["ended_at"]), "goal": goal or "", "apps": _apps(r["apps"]),
                    "outcome": r["outcome_fact"] or UNCLEAR,
                    "outcome_words": outcome_words(r["outcome_fact"], r["file_ext"]),
                    "verdict": r["user_verdict"], "episode_id": r["episode_id"]})
    return out


def correct_task(store: Any, task_id: int, verdict: str, words: str = "", *,
                 now: float | None = None) -> dict[str, Any]:
    """The user's correction of one task: ``renamed`` (``words``: the goal in their words), ``not_one`` (it was not
    one task) or ``not_mine`` (it is not what they did). Stored in ``user_verdict``; the pass's own goal is kept for
    the worked example; a renamed goal is embedded again (its family is worked out again on the next tick). Returns
    ``{"ok", "verdict", "goal"}`` or ``{"ok": False, "error"}`` in plain words. Never raises."""
    now = time.time() if now is None else float(now)
    if verdict not in VERDICTS:
        return {"ok": False, "error": "That can't be corrected here."}
    text = " ".join(str(words or "").split())
    if verdict == "renamed":
        if not text:
            return {"ok": False, "error": "Say what it was in a few words."}
        if len(text) > 200:
            return {"ok": False, "error": "Say it in fewer words."}

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT goal_ciphertext, model_goal_ciphertext, user_verdict FROM ui_tasks WHERE id = ?",
                           (int(task_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "That task isn't there any more."}
        model_goal = row["model_goal_ciphertext"] or row["goal_ciphertext"]
        if verdict == "renamed":
            conn.execute("UPDATE ui_tasks SET user_verdict = ?, verdict_at = ?, model_goal_ciphertext = ?,"
                         " goal_ciphertext = ?, vec_ciphertext = NULL, embed_model = NULL, updated_at = ? WHERE id = ?",
                         (verdict, now, model_goal, store._enc(text), now, int(task_id)))
            return {"ok": True, "verdict": verdict, "goal": text}
        fam = conn.execute("SELECT family_id FROM ui_tasks WHERE id = ?", (int(task_id),)).fetchone()["family_id"]
        conn.execute("UPDATE ui_tasks SET user_verdict = ?, verdict_at = ?, model_goal_ciphertext = ?,"
                     " goal_ciphertext = ?, family_id = NULL, updated_at = ? WHERE id = ?",
                     (verdict, now, model_goal, model_goal, now, int(task_id)))
        # a rejected task leaves its family at once: the usual path the brain is shown never counts it, even
        # before the next idle refresh (a light rebuild of one family from its kept routes)
        from yuki.memory import trace_families

        trace_families.drop_member(conn, int(task_id))
        if fam is not None:
            trace_families.rebuild(conn, store, int(fam), now=now)
        return {"ok": True, "verdict": verdict, "goal": store._dec(model_goal) if model_goal else ""}

    try:
        return store._write(op)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"That didn't work ({type(exc).__name__})."}


def undo_correction(store: Any, task_id: int, *, now: float | None = None) -> dict[str, Any]:
    """Take a correction back: the pass's own goal and no verdict again. Never raises."""
    now = time.time() if now is None else float(now)

    def op(conn: sqlite3.Connection) -> dict[str, Any]:
        row = conn.execute("SELECT model_goal_ciphertext, goal_ciphertext, user_verdict FROM ui_tasks WHERE id = ?",
                           (int(task_id),)).fetchone()
        if row is None:
            return {"ok": False, "error": "That task isn't there any more."}
        if row["user_verdict"] is None:
            return {"ok": True, "verdict": None}
        goal = row["model_goal_ciphertext"] or row["goal_ciphertext"]
        renamed = row["user_verdict"] == "renamed"
        conn.execute("UPDATE ui_tasks SET user_verdict = NULL, verdict_at = NULL, goal_ciphertext = ?,"
                     " model_goal_ciphertext = NULL, updated_at = ?" + (", vec_ciphertext = NULL, embed_model = NULL"
                                                                        if renamed else "") + " WHERE id = ?",
                     (goal, now, int(task_id)))
        return {"ok": True, "verdict": None, "goal": store._dec(goal) if goal else ""}

    try:
        return store._write(op)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"That didn't work ({type(exc).__name__})."}


def task_rows(store: Any, task_id: int, *, conn: sqlite3.Connection | None = None) -> list[dict[str, Any]]:
    """A task's own actions, oldest first: the rows of its pieces' time ranges that a task may be built from
    (:func:`kept`), less its detours."""
    q = (lambda sql, p=(): conn.execute(sql, p).fetchall()) if conn is not None else store._query
    ranges = q("SELECT r.first_action, r.last_action, r.detour, a.at AS a0, b.at AS a1 FROM ui_task_actions r"
               " LEFT JOIN ui_actions a ON a.id = r.first_action LEFT JOIN ui_actions b ON b.id = r.last_action"
               " WHERE r.task_id = ?", (int(task_id),))
    rows: dict[int, dict[str, Any]] = {}
    detours: list[tuple[float, float]] = []
    for rg in ranges:
        if rg["a0"] is None or rg["a1"] is None:
            continue
        lo, hi = min(float(rg["a0"]), float(rg["a1"])), max(float(rg["a0"]), float(rg["a1"]))
        if rg["detour"]:
            detours.append((lo, hi))
            continue
        for r in q(f"SELECT {_ROW_COLUMNS} FROM ui_actions WHERE at >= ? AND at <= ? ORDER BY at, id", (lo, hi)):
            rows[int(r["id"])] = dict(r)
    out = [r for r in rows.values() if kept(r) and not any(lo <= float(r["at"]) <= hi for lo, hi in detours)]
    return sorted(out, key=lambda r: (float(r["at"]), int(r["id"])))


__all__ = ["UNCLEAR", "VERDICTS", "Fact", "correct_task", "day_tasks", "kept", "learnable", "outcome_words",
           "stored_window_only", "task_rows", "undo_correction"]
