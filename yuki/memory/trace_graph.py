"""The checked outcomes of the user's stored tasks (capC6), read only: :func:`done_evidence`.

The task pass that cut tasks from the click trace (``trace_tasks.TaskPass``) and the workflow graph's other queries
(families, transitions, "what comes next") were deleted with the old learners (audit 17 Oct 2026, decision 3). The
tasks already stored still show as evidence on the Work page and in the check-ins' done lines until they age out
(30 days, ``trace_store``'s purge). Names only from the element registry's kept names; tasks the user rejected are
never shown as theirs.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any


from yuki.memory import trace_families as F
from yuki.memory.trace_tasks import learnable, outcome_words

MAX_EVIDENCE = 12
#: A task lies inside one episode window (closed after 3 hours by the episode builder); a range read reaches back this
#: far for tasks that began before it and run into it.
TASK_SPAN_MAX_S = 6 * 3600.0
#: Character caps.
LABEL_CHARS = 140
#: The outcome facts (``trace_tasks.FACT_KINDS``) that mean the user made, sent or ran something: the only ones offered
#: as "done" evidence (second verification V3). The rest (a window came up, a page reached, a file opened, an item
#: picked, a setting switched) show a step happened, not that the work the task's goal names got finished.
DONE_FACTS = ("file_saved", "field_submitted", "paste_sent", "command_ok")


def clock(at: float | None) -> str:
    """A 12-hour clock time as people say it, in the PC's local time: "4 PM", "9:06 AM"."""
    if not at:
        return ""
    d = datetime.fromtimestamp(float(at))
    return f"{d.hour % 12 or 12}{'' if d.minute == 0 else f':{d.minute:02d}'} {'AM' if d.hour < 12 else 'PM'}"


def _clip(text: Any, cap: int) -> str:
    return F._clip(text, cap)


def _apps(raw: Any) -> list[str]:
    try:
        got = json.loads(raw or "[]")
    except (TypeError, ValueError):
        return []
    return [str(a) for a in got if a] if isinstance(got, list) else []


def _dec(store: Any, blob: Any) -> str:
    if not blob:
        return ""
    try:
        return " ".join(str(store._dec(blob) or "").split())
    except Exception:  # noqa: BLE001 - another key: treated as absent
        return ""


# ---------------------------------------------------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Elements, transitions, apps
# ---------------------------------------------------------------------------------------------------------------------


# ---------------------------------------------------------------------------------------------------------------------
# Tasks in a time range, and checked evidence
# ---------------------------------------------------------------------------------------------------------------------

_TASK_SQL = (
    "SELECT t.id, t.started_at, t.ended_at, t.apps, t.goal_ciphertext, t.outcome_fact, t.outcome_action,"
    " t.user_verdict, t.family_id, a.at AS outcome_at, a.file_ext, f.n_tasks AS family_tasks"
    " FROM ui_tasks t LEFT JOIN ui_actions a ON a.id = t.outcome_action LEFT JOIN ui_families f ON f.id = t.family_id"
    " WHERE t.started_at >= ? AND t.started_at < ? AND t.ended_at > ?"
    " AND (t.user_verdict IS NULL OR t.user_verdict NOT IN ('not_one', 'not_mine'))")


def _thread_ids(store: Any, thread_id: int) -> list[int]:
    """A thread of work and every thread merged into it (``merged_into``, followed down; bounded)."""
    rows = store._query("WITH RECURSIVE m(id, depth) AS (SELECT ?, 0 UNION SELECT w.id, m.depth + 1 FROM work_threads w"
                        " JOIN m ON w.merged_into = m.id WHERE m.depth < 16) SELECT DISTINCT id FROM m LIMIT 200",
                        (int(thread_id),))
    return [int(r[0]) for r in rows]


def done_evidence(store: Any, since: float, until: float, *, limit: int = MAX_EVIDENCE,
                  thread: int | None = None) -> list[dict[str, Any]]:
    """Tasks whose outcome the PC checked, the outcome inside ``[since, until)``, newest first, at most ``limit``
    (:data:`MAX_EVIDENCE`): learnable tasks only (a recorded fact, never ``unclear``, never one the user rejected)
    whose fact is one of :data:`DONE_FACTS` (something made, sent or run; a window or page coming up is no proof).
    ``thread`` (a thread of work's id): only tasks with at least one of the user's journal entries on that thread (or
    one merged into it) inside their span, so the cap applies to that thread's tasks, not to everything newer (review
    C6-1; which thread a task belongs to - most of its span's entries - is still the caller's to decide).
    ``[{"task" (id), "action" (the outcome's action id), "fact", "at" (the outcome's time), "start_at", "end_at"
    (the task's span), "goal", "apps",
    "words" (second person: "you saved a file (.pptx)"), "clock" ("4 PM")}]``: the ids are for the caller's records,
    the words for the user. Never raises."""
    limit = max(1, min(int(limit), MAX_EVIDENCE))
    try:
        lo, hi = float(since), float(until)
        sql = (_TASK_SQL + " AND t.outcome_fact IN (%s) AND t.outcome_action IS NOT NULL"
               % ",".join("?" * len(DONE_FACTS)))
        params: list[Any] = [lo - TASK_SPAN_MAX_S, hi, lo, *DONE_FACTS]
        if thread is not None:
            ids = _thread_ids(store, int(thread))
            if not ids:
                return []
            sql += (" AND EXISTS (SELECT 1 FROM journal j WHERE j.work_thread_id IN (%s) AND j.at >= t.started_at"
                    " AND j.at < t.ended_at + 1 AND j.superseded_at IS NULL AND j.by_yuki = 0)" % ",".join("?" * len(ids)))
            params += ids
        rows = store._query(sql + " ORDER BY t.started_at DESC, t.id DESC LIMIT ?", (*params, limit * 4))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        at = r["outcome_at"]
        if at is None or not lo <= float(at) < hi or not learnable(dict(r), store):
            continue
        goal = _clip(_dec(store, r["goal_ciphertext"]), LABEL_CHARS)
        if not goal:
            continue
        out.append({"task": int(r["id"]), "action": int(r["outcome_action"]), "fact": r["outcome_fact"],
                    "at": float(at), "start_at": float(r["started_at"]), "end_at": float(r["ended_at"]),
                    "goal": goal, "apps": _apps(r["apps"])[:4],
                    "words": outcome_words(r["outcome_fact"], r["file_ext"]), "clock": clock(float(at))})
    out.sort(key=lambda e: -e["at"])
    return out[:limit]


__all__ = ["DONE_FACTS", "clock", "MAX_EVIDENCE", "done_evidence"]
