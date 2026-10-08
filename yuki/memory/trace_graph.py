"""The workflow graph's queries (report 78 §5.4, phase C6): what the user usually does, read from C2's tables.

The user's words of 7 Oct: "the things i click, what are they? why i click? when i click? ... so when i tell it to do
something it doesnt have to guess in the pool of data, it just knows what i usually do." C1 records the clicks, C2
groups them into tasks with a goal and a checked outcome, families and usual paths, and transitions; this module only
**reads** them, to answer five questions:

* **How do I usually do X** (:func:`how_usually`): the families nearest to the request's words by their goals'
  embedding (the shared embedder's vector, as :func:`yuki.memory.trace_families.hints`), each with its usual path
  (each step with how many of the routes it was built from took it), its branches, how many times, when last, the
  apps it spans, and what usually follows it.
* **What do I do in <app>** (:func:`in_app`): the app's families, its top go-tos and shortcuts (C1b's tallies,
  :func:`yuki.memory.trace_habits.go_tos`, read without counting as served) and its usual transitions.
* **What comes next** (:func:`next_after_element`, :func:`next_after_task`): after an element, its transitions and the
  next step of every usual path that holds it; after a task, the families the next task in the same stretch of work
  belonged to.
* **Which apps does this task span** (:func:`task_apps`): its apps with how many of its own steps were in each.
* **What did I do between T1 and T2** (:func:`tasks_between`): the tasks with their goals, outcomes and apps.

And one for the Work page and the check-ins (:func:`done_evidence`): tasks whose outcome the PC checked (a file saved,
typed text sent, a page reached ...) in a time range, newest first, each citing its task and the action that is its
outcome, so a page or a check-in can say "you saved a file (.pptx) at 4 PM" and keep the ids internal.

Rules every query keeps:

* **The user's corrections.** A task they said was not one task or not what they did (``user_verdict`` ``not_one`` /
  ``not_mine``) is never shown as theirs, never evidence, never a family's label; a renamed one shows their words.
* **Checked outcomes only as evidence.** :func:`done_evidence` returns only learnable tasks
  (:func:`yuki.memory.trace_tasks.learnable`: a recorded outcome fact, never ``unclear``, never rejected).
* **Names only from the element registry** (``ui_elements``), which keeps a command control's name on first use, a
  menu item's, tab's or title's after 3 distinct days, a content control's never; an element without a kept name is
  its role and position ("an unnamed ListItem"); one the registry no longer has (forgotten, unused 60 days) is left
  out. A name in ``ui_actions`` is never read here.
* **Bounded and indexed.** Every list has a hard cap (:data:`MAX_FAMILIES`, :data:`MAX_TASKS`, :data:`MAX_STEPS`,
  :data:`MAX_TRANSITIONS`) and every line a character cap; tasks are read by ``ui_tasks_span`` /
  ``ui_tasks_family`` / ``ui_tasks_window``, transitions by their primary key (process, from key); the families table
  is small by retention (90 days unused) and read at most :data:`FAMILY_SCAN` rows.
* **Local time.** Times are epoch seconds plus words in the PC's local time ("today, 4 PM", "25 September, 2 AM").
* **Read only, never raises**: an empty answer on any error or on a database before migration 39.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import numpy as np

from yuki.memory import trace_families as F
from yuki.memory.trace_tasks import REJECTED, UNCLEAR, element_label, learnable, outcome_words

#: Lists returned at most.
MAX_FAMILIES = 8
MAX_TASKS = 40
MAX_STEPS = 16
MAX_BRANCHES = 4
MAX_TRANSITIONS = 8
MAX_EVIDENCE = 12
#: Families read at most per question (newest first); retention keeps the table far smaller.
FAMILY_SCAN = 2_000
#: How near a family's goals must be to the words to count as an answer to "how do I usually do X" (the brain's own
#: floor for a family, :func:`yuki.memory.trace_families.hints`).
HOW_FLOOR = 0.45
#: A task lies inside one episode window (closed after 3 hours by the episode builder); a range read reaches back this
#: far for tasks that began before it and run into it.
TASK_SPAN_MAX_S = 6 * 3600.0
#: Character caps.
LABEL_CHARS = 140
STEP_CHARS = F.STEP_CHARS
BRANCH_CHARS = 240
#: Tasks of a family looked at for what usually follows it.
FOLLOW_TASKS = 30
#: The outcome facts (``trace_tasks.FACT_KINDS``) that mean the user made, sent or ran something: the only ones offered
#: as "done" evidence (second verification V3). The rest (a window came up, a page reached, a file opened, an item
#: picked, a setting switched) show a step happened, not that the work the task's goal names got finished.
DONE_FACTS = ("file_saved", "field_submitted", "paste_sent", "command_ok")

#: An outcome fact as a short neutral phrase (the MCP tools speak about "the user"; the Work page and the check-ins use
#: :func:`yuki.memory.trace_tasks.outcome_words`, second person).
FACT_PHRASES = {
    "file_saved": "a file saved", "file_opened": "a file opened", "page_reached": "a new page reached",
    "window_verified": "the app's window came up", "field_submitted": "typed text sent",
    "toggle_state": "a setting switched", "selected": "an item picked", "command_ok": "a terminal command worked",
    "paste_sent": "pasted in and sent", UNCLEAR: "not checked (maybe not done)",
}

_NOT_REJECTED = "(user_verdict IS NULL OR user_verdict NOT IN ('not_one', 'not_mine'))"


def fact_phrase(kind: str | None, ext: str | None = None) -> str:
    """An outcome fact as a neutral phrase ("a file saved (.pptx)", "not checked (maybe not done)")."""
    words = FACT_PHRASES.get(kind or UNCLEAR, FACT_PHRASES[UNCLEAR])
    if kind in ("file_saved", "file_opened") and ext:
        words += f" ({ext})"
    return words


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


def _app_key(app: Any) -> str | None:
    from yuki.memory.store import app_key

    return app_key(str(app or "")) if app else None


# ---------------------------------------------------------------------------------------------------------------------
# Families
# ---------------------------------------------------------------------------------------------------------------------

_FAMILY_COLUMNS = ("id, label_ciphertext, label_task, n_tasks, apps, path_keys, variants_keys, vec_ciphertext, last_at,"
                   " path_n")


def _families(store: Any) -> list[Any]:
    try:
        return store._query(f"SELECT {_FAMILY_COLUMNS} FROM ui_families WHERE n_tasks > 0 ORDER BY last_at DESC, id DESC"
                            " LIMIT ?", (FAMILY_SCAN,))
    except Exception:  # noqa: BLE001 - a database before migration 39
        return []


def _label(store: Any, f: Any) -> str:
    """A family's label (a member's goal), only while the task it was taken from is still one of the family's members
    and not one the user rejected. A family whose live tasks are all gone keeps its old label; a rejection takes the
    task out of the family (``trace_families.drop_member``), so a label whose task is no longer a member is stale
    even after the rejected task's own row expired (second verification V1). A label whose member aged out (90 days)
    is hidden too: the family is then shown without one."""
    if f["label_task"] is not None:
        try:
            member = store._query("SELECT 1 FROM ui_family_members WHERE family_id = ? AND task_id = ?",
                                  (int(f["id"]), int(f["label_task"])))
            got = store._query("SELECT user_verdict FROM ui_tasks WHERE id = ?", (int(f["label_task"]),))
        except Exception:  # noqa: BLE001 - a database before the members table: nothing to vouch for the label
            return ""
        if not member or (got and got[0][0] in REJECTED):
            return ""
    return _clip(_dec(store, f["label_ciphertext"]), LABEL_CHARS)


def _latest_task(store: Any, fid: int) -> Any | None:
    try:
        got = store._query(
            "SELECT t.id, t.goal_ciphertext, t.started_at, t.ended_at, t.apps, t.outcome_fact, t.outcome_action,"
            " t.user_verdict, a.at AS outcome_at, a.file_ext FROM ui_tasks t LEFT JOIN ui_actions a ON a.id ="
            f" t.outcome_action WHERE t.family_id = ? AND {_NOT_REJECTED.replace('user_verdict', 't.user_verdict')}"
            " ORDER BY t.ended_at DESC, t.id DESC LIMIT 1", (int(fid),))
    except Exception:  # noqa: BLE001
        return None
    return got[0] if got else None


def _routes(f: Any) -> tuple[int, int]:
    """A family's ``(tasks, routes its path was built from)``."""
    n_tasks = int(f["n_tasks"] or 0)
    return n_tasks, int(f["path_n"] or 0) or min(n_tasks, F.PATH_TASKS)


def _usual_indexes(nodes: Sequence[dict[str, Any]], n_tasks: int, n_routes: int) -> set[int]:
    """The backbone indexes of the usual path (:func:`yuki.memory.trace_families.usual`); a family of one: every
    node (its one route)."""
    if n_tasks < 2:
        return set(range(len(nodes)))
    usual = {id(nd) for nd in F.usual(nodes, n_routes)}
    return {i for i, nd in enumerate(nodes) if id(nd) in usual}


def family_view(store: Any, f: Any, *, now: float | None = None, steps: int = MAX_STEPS,
                branches: int = MAX_BRANCHES, follow: bool = True, cosine: float | None = None) -> dict[str, Any]:
    """One family as the graph answers it: ``{"id", "label", "tasks", "apps", "last_at", "last_words", "of" (the
    routes the path's counts are out of), "steps": [{"words", "n"}] (the usual path; a family of one: its one route),
    "branches": [{"words", "n", "where"}], "latest": {"goal", "when", "at", "outcome", "checked", "renamed"} | None,
    "followed_by": [{"label", "n", "of"}], "cosine"}``. Worded from the registry's kept names only."""
    n_tasks, n_routes = _routes(f)
    try:
        nodes = json.loads(f["path_keys"] or "[]")
        variants = json.loads(f["variants_keys"] or "[]")
    except (TypeError, ValueError):
        nodes, variants = [], []
    names = F._names(store, [nd.get("k") for nd in nodes] + [k for b in variants for k in b.get("keys") or []])
    keep = _usual_indexes(nodes, n_tasks, n_routes)
    path = []
    #: backbone index -> its 1-based position in ``path`` (the numbered list the reader sees), so a branch's "after
    #: step N" points at a step that is shown (review C6-2: the backbone also holds steps few routes took)
    shown_at: dict[int, int] = {}
    #: the last backbone index the shown path covers when it was cut at ``steps`` (None: the whole path is shown)
    cut_at: int | None = None
    for i, nd in enumerate(nodes):
        if i not in keep:
            continue
        words = F.step_words(str(nd.get("k") or ""), nd, names)
        if words:
            path.append({"words": _clip(words, STEP_CHARS), "n": int(nd.get("n") or 0)})
            shown_at[i] = len(path)
        if len(path) >= steps:
            cut_at = i
            break
    worded_branches = []
    if n_tasks >= 2:
        for b in variants:
            bw = [w for w in (F.step_words(k, a, names) for k, a in zip(b.get("keys") or [], b.get("attrs") or []))
                  if w]
            if not bw:
                continue
            after = int(b.get("after", -1))
            pos = max((p for i, p in shown_at.items() if i <= after), default=0)
            # past a path cut at ``steps``: no shown step it follows (second verification V5)
            where = ("later on" if cut_at is not None and after > cut_at
                     else "at the start" if pos == 0 else f"after step {pos}")
            worded_branches.append({"words": _clip("; ".join(bw), BRANCH_CHARS), "n": int(b.get("n") or 0),
                                    "where": where})
            if len(worded_branches) >= branches:
                break
    latest = None
    t = _latest_task(store, int(f["id"]))
    if t is not None:
        latest = {"goal": _clip(_dec(store, t["goal_ciphertext"]), LABEL_CHARS), "at": float(t["started_at"]),
                  "when": F.when_words(t["started_at"], now), "outcome": fact_phrase(t["outcome_fact"], t["file_ext"]),
                  "checked": learnable(dict(t)), "renamed": t["user_verdict"] == "renamed"}
    out = {"id": int(f["id"]), "label": _label(store, f), "tasks": n_tasks, "apps": _apps(f["apps"])[:6],
           "last_at": f["last_at"], "last_words": F.when_words(f["last_at"], now), "of": n_routes, "steps": path,
           "branches": worded_branches, "latest": latest, "followed_by": [],
           "cosine": None if cosine is None else round(float(cosine), 3)}
    if follow:
        out["followed_by"] = followed_by(store, int(f["id"]))
    return out


def _scored(store: Any, query_vec: Any) -> list[tuple[float, Any]]:
    """Every family with a centre, with its cosine to ``query_vec`` (best first)."""
    if query_vec is None:
        return []
    q = np.asarray(query_vec, dtype=np.float32).ravel()
    q = q / (float(np.linalg.norm(q)) or 1.0)
    out = []
    for f in _families(store):
        fv = F._vec(store, f["vec_ciphertext"])
        if fv is None or fv.shape != q.shape:
            continue
        out.append((float(fv @ q), f))
    out.sort(key=lambda x: -x[0])
    return out


def how_usually(store: Any, query_vec: Any, *, apps: Sequence[str] = (), floor: float = HOW_FLOOR,
                limit: int = 3, now: float | None = None) -> list[dict[str, Any]]:
    """"How do I usually do X": the families nearest to the request's vector that clear ``floor`` (``apps``: only
    families spanning one of these apps), best first, at most ``limit`` (:data:`MAX_FAMILIES`), each a
    :func:`family_view`. ``[]`` with no vector or nothing near. Never raises."""
    try:
        keys = {k for k in (_app_key(a) for a in apps or ()) if k}
        out = []
        for c, f in _scored(store, query_vec):
            if c < floor:
                break
            if keys and not keys & {a.casefold() for a in _apps(f["apps"])}:
                continue
            out.append(family_view(store, f, now=now, cosine=c))
            if len(out) >= min(int(limit), MAX_FAMILIES):
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def families(store: Any, *, app: str | None = None, query_vec: Any = None, floor: float = HOW_FLOOR,
             limit: int = MAX_FAMILIES, now: float | None = None) -> list[dict[str, Any]]:
    """The user's families: with ``query_vec``, the nearest ones clearing ``floor`` (:func:`how_usually`); else the
    most recently used; ``app`` keeps the families spanning that app. At most ``limit`` (:data:`MAX_FAMILIES`). Never
    raises."""
    limit = max(1, min(int(limit), MAX_FAMILIES))
    if query_vec is not None:
        return how_usually(store, query_vec, apps=[app] if app else (), floor=floor, limit=limit, now=now)
    try:
        key = _app_key(app)
        out = []
        for f in _families(store):
            if key and key not in {a.casefold() for a in _apps(f["apps"])}:
                continue
            out.append(family_view(store, f, now=now))
            if len(out) >= limit:
                break
        return out
    except Exception:  # noqa: BLE001
        return []


def followed_by(store: Any, fid: int, *, limit: int = 2) -> list[dict[str, Any]]:
    """What usually comes after a family's tasks: for each of its latest :data:`FOLLOW_TASKS` live tasks, the next
    task the user started in the same stretch of work (the same episode window), counted by that task's family:
    ``[{"family", "label", "n", "of"}]``, most often first (rejected tasks on neither side), only a follower seen at
    least twice and at least half the times looked at (``n >= max(2, ceil(of / 2))``, the usual path's rule). Never
    raises."""
    try:
        mine = store._query(f"SELECT id, started_at, window_start FROM ui_tasks WHERE family_id = ? AND {_NOT_REJECTED}"
                            " ORDER BY started_at DESC LIMIT ?", (int(fid), FOLLOW_TASKS))
        counts: Counter[int] = Counter()
        for t in mine:
            if t["window_start"] is None:
                continue
            nxt = store._query(
                f"SELECT family_id FROM ui_tasks WHERE window_start = ? AND id != ? AND {_NOT_REJECTED} AND"
                " (started_at > ? OR (started_at = ? AND id > ?)) ORDER BY started_at, id LIMIT 1",
                (t["window_start"], int(t["id"]), float(t["started_at"]), float(t["started_at"]), int(t["id"])))
            if nxt and nxt[0][0] is not None:
                counts[int(nxt[0][0])] += 1
        out = []
        # "usually" only (second verification V6): the rule of the usual path (``F.usual``), at least twice and at
        # least half the times looked at; one occurrence is never a habit
        need = max(2, math.ceil(len(mine) / 2))
        for other, n in counts.most_common(limit):
            if n < need:
                break
            got = store._query(f"SELECT {_FAMILY_COLUMNS} FROM ui_families WHERE id = ?", (other,))
            label = _label(store, got[0]) if got else ""
            if label:
                out.append({"family": other, "label": label, "n": n, "of": len(mine)})
        return out
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------------------------------------------------
# Elements, transitions, apps
# ---------------------------------------------------------------------------------------------------------------------


def _elements(store: Any, keys: Sequence[str]) -> dict[str, dict[str, Any]]:
    """``{element key: {"process", "role", "name" (kept name or None)}}`` for the keys the registry still has."""
    out: dict[str, dict[str, Any]] = {}
    keys = sorted({k for k in keys if k})
    for start in range(0, len(keys), 400):
        part = keys[start:start + 400]
        try:
            rows = store._query("SELECT element_key, process, role, name_ciphertext FROM ui_elements WHERE element_key"
                                " IN (%s)" % ",".join("?" * len(part)), part)
        except Exception:  # noqa: BLE001
            rows = []
        for r in rows:
            out[r["element_key"]] = {"process": r["process"], "role": r["role"] or "",
                                     "name": _dec(store, r["name_ciphertext"]) or None}
    return out


def element_words(key: str, el: dict[str, Any]) -> str:
    """An element as the graph names it: its kept name with its role, else its role ("an unnamed ListItem")."""
    return element_label({"role": el.get("role"), "element_key": key, "kind": "click"}, el.get("name"))


def _gap_words(seconds: Any) -> str:
    if seconds is None:
        return ""
    s = float(seconds)
    if s < 1:
        return "at once"
    if s < 60:
        return f"about {round(s)} s later"
    return f"about {round(s / 60)} min later"


def transitions(store: Any, process: str, *, from_key: str | None = None,
                limit: int = MAX_TRANSITIONS) -> list[dict[str, Any]]:
    """An app's usual transitions, most used first: ``[{"from", "to", "n", "gap", "last_at"}]`` worded from kept
    names (a pair with an element the registry no longer has is left out); ``from_key``: only what follows that
    element. Read by the primary key (process, from key). Never raises."""
    limit = max(1, min(int(limit), MAX_TRANSITIONS))
    try:
        if from_key:
            rows = store._query("SELECT from_key, to_key, n, median_gap_s, last_at FROM ui_transitions WHERE process = ?"
                                " AND from_key = ? ORDER BY n DESC, last_at DESC LIMIT ?",
                                (str(process), str(from_key), limit * 3))
        else:
            rows = store._query("SELECT from_key, to_key, n, median_gap_s, last_at FROM ui_transitions WHERE process = ?"
                                " ORDER BY n DESC, last_at DESC LIMIT ?", (str(process), limit * 3))
    except Exception:  # noqa: BLE001
        return []
    els = _elements(store, [k for r in rows for k in (r["from_key"], r["to_key"])])
    out = []
    for r in rows:
        a, b = els.get(r["from_key"]), els.get(r["to_key"])
        if a is None or b is None:
            continue
        out.append({"from": element_words(r["from_key"], a), "to": element_words(r["to_key"], b), "n": int(r["n"] or 0),
                    "gap": _gap_words(r["median_gap_s"]), "last_at": r["last_at"]})
        if len(out) >= limit:
            break
    return out


def in_app(store: Any, app: str, *, now: float | None = None, limit: int = 5,
           with_families: bool = True) -> dict[str, Any]:
    """"What do I do in <app>": ``{"app", "families": [...] (spanning it, newest first), "elements": [...] and
    "chords": [...] (its go-tos and shortcuts, C1b's tallies, read without counting as served), "transitions":
    [...]}``. ``with_families`` False: ``families`` stays empty (a caller that already read them). Never raises."""
    from yuki.memory import trace_habits

    key = _app_key(app) or str(app or "")
    out: dict[str, Any] = {"app": key, "families": [], "elements": [], "chords": [], "transitions": []}
    if not key:
        return out
    if with_families:
        out["families"] = families(store, app=key, limit=limit, now=now)
    try:
        got = trace_habits.go_tos(store, [key], now=now, detail=True, count=False)
    except Exception:  # noqa: BLE001
        got = []
    if got:
        out["elements"] = [{"label": e["label"], "n": e["n"], "days": e["days"], "last_at": e.get("last_at")}
                           for e in got[0].get("elements") or []]
        out["chords"] = [{"chord": c["chord"], "n": c["n"], "days": c["days"], "last_at": c.get("last_at")}
                         for c in got[0].get("chords") or []]
    out["transitions"] = transitions(store, key)
    return out


def next_after_element(store: Any, element_key: str, *, limit: int = 5) -> dict[str, Any]:
    """"What comes next" after an element: ``{"element" (its words), "app", "transitions": [{"to", "n", "gap"}],
    "paths": [{"family", "label", "next"}]}`` (for each family whose path holds the element, the next step of its
    usual path after it: the step most of the routes took, never one only a few took). ``{}`` when
    the registry does not have it. Never raises."""
    try:
        el = _elements(store, [element_key]).get(element_key)
        if el is None:
            return {}
        out: dict[str, Any] = {"element": element_words(element_key, el), "app": el["process"],
                               "transitions": [{k: t[k] for k in ("to", "n", "gap")}
                                               for t in transitions(store, el["process"], from_key=element_key,
                                                                    limit=limit)],
                               "paths": []}
        for f in _families(store):
            if len(out["paths"]) >= limit:
                break
            try:
                nodes = json.loads(f["path_keys"] or "[]")
            except (TypeError, ValueError):
                continue
            keys = [nd.get("k") for nd in nodes]
            if element_key not in keys:
                continue
            # the next step of the usual path (review C6-3), not of the raw backbone: a step only a few of the routes
            # took is never "what comes next"; a family of one: its one route's next step
            i = keys.index(element_key)
            keep = _usual_indexes(nodes, *_routes(f))
            j = next((j for j in range(i + 1, len(nodes)) if j in keep), None)
            if j is None:
                continue
            nd = nodes[j]
            words = F.step_words(str(nd.get("k") or ""), nd, F._names(store, [nd.get("k")]))
            label = _label(store, f)
            if words and label:
                out["paths"].append({"family": int(f["id"]), "label": label, "next": _clip(words, STEP_CHARS)})
        return out
    except Exception:  # noqa: BLE001
        return {}


def _task(store: Any, task_id: int) -> Any | None:
    try:
        got = store._query("SELECT id, family_id, apps, user_verdict, started_at, window_start FROM ui_tasks WHERE"
                           " id = ?", (int(task_id),))
    except Exception:  # noqa: BLE001
        return None
    return got[0] if got else None


def next_after_task(store: Any, task_id: int, *, limit: int = 2) -> dict[str, Any]:
    """"What comes next" after a task: ``{"task", "family", "followed_by": [...]}`` (:func:`followed_by` of its
    family). ``{}`` for a task the user rejected, or none. Never raises."""
    t = _task(store, task_id)
    if t is None or t["user_verdict"] in REJECTED:
        return {}
    out: dict[str, Any] = {"task": int(t["id"]), "family": t["family_id"], "followed_by": []}
    if t["family_id"] is not None:
        out["followed_by"] = followed_by(store, int(t["family_id"]), limit=limit)
    return out


def task_apps(store: Any, task_id: int) -> list[dict[str, Any]]:
    """"Which apps does this task span": ``[{"app", "steps"}]`` (its own steps in each app, detours left out, most
    first; an app it names with no step of its own counts 0). ``[]`` for a task the user rejected. Never raises."""
    from yuki.memory.trace_tasks import task_rows

    t = _task(store, task_id)
    if t is None or t["user_verdict"] in REJECTED:
        return []
    try:
        counts = Counter(str(r.get("process") or "") for r in task_rows(store, int(task_id)) if r.get("process"))
    except Exception:  # noqa: BLE001
        counts = Counter()
    for a in _apps(t["apps"]):
        counts.setdefault(a, 0)
    return [{"app": a, "steps": n} for a, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


# ---------------------------------------------------------------------------------------------------------------------
# Tasks in a time range, and checked evidence
# ---------------------------------------------------------------------------------------------------------------------

_TASK_SQL = (
    "SELECT t.id, t.started_at, t.ended_at, t.apps, t.goal_ciphertext, t.outcome_fact, t.outcome_action,"
    " t.user_verdict, t.family_id, a.at AS outcome_at, a.file_ext, f.n_tasks AS family_tasks"
    " FROM ui_tasks t LEFT JOIN ui_actions a ON a.id = t.outcome_action LEFT JOIN ui_families f ON f.id = t.family_id"
    " WHERE t.started_at >= ? AND t.started_at < ? AND t.ended_at > ?"
    " AND (t.user_verdict IS NULL OR t.user_verdict NOT IN ('not_one', 'not_mine'))")


def tasks_between(store: Any, since: float, until: float, *, limit: int = MAX_TASKS,
                  now: float | None = None) -> dict[str, Any]:
    """"What did I do between T1 and T2": the tasks overlapping ``[since, until)``, oldest first, at most ``limit``
    (:data:`MAX_TASKS`): ``{"tasks": [{"id", "ref", "start_at", "end_at", "when", "goal", "renamed", "apps",
    "outcome" (fact kind), "outcome_phrase", "checked", "outcome_at", "outcome_action", "family_tasks"}], "more"
    (tasks past the cap), "left_out" (tasks the user said were not theirs or not one task, a count)}``. Read by the
    start-time index. Never raises."""
    limit = max(1, min(int(limit), MAX_TASKS))
    empty: dict[str, Any] = {"tasks": [], "more": 0, "left_out": 0}
    try:
        lo, hi = float(since), float(until)
        rows = store._query(_TASK_SQL + " ORDER BY t.started_at, t.id LIMIT ?", (lo - TASK_SPAN_MAX_S, hi, lo, limit + 1))
        n_all = store._query("SELECT count(*) FROM ui_tasks WHERE started_at >= ? AND started_at < ? AND ended_at > ?"
                             " AND (user_verdict IS NULL OR user_verdict NOT IN ('not_one', 'not_mine'))",
                             (lo - TASK_SPAN_MAX_S, hi, lo))[0][0]
        rejected = store._query("SELECT count(*) FROM ui_tasks WHERE started_at >= ? AND started_at < ? AND ended_at > ?"
                                " AND user_verdict IN ('not_one', 'not_mine')", (lo - TASK_SPAN_MAX_S, hi, lo))[0][0]
    except Exception:  # noqa: BLE001 - a database before migration 39
        return empty
    out = []
    for r in rows[:limit]:
        out.append({"id": int(r["id"]), "ref": f"task:{int(r['id'])}", "start_at": float(r["started_at"]),
                    "end_at": float(r["ended_at"]), "when": F.when_words(r["started_at"], now),
                    "goal": _clip(_dec(store, r["goal_ciphertext"]), LABEL_CHARS),
                    "renamed": r["user_verdict"] == "renamed", "apps": _apps(r["apps"])[:6],
                    "outcome": r["outcome_fact"] or UNCLEAR, "outcome_phrase": fact_phrase(r["outcome_fact"], r["file_ext"]),
                    "checked": learnable(dict(r)), "outcome_at": r["outcome_at"], "outcome_action": r["outcome_action"],
                    "family_tasks": int(r["family_tasks"] or 0)})
    return {"tasks": out, "more": max(0, int(n_all) - len(out)), "left_out": int(rejected or 0)}


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
        if at is None or not lo <= float(at) < hi or not learnable(dict(r)):
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


__all__ = ["DONE_FACTS", "FACT_PHRASES", "clock", "HOW_FLOOR", "MAX_EVIDENCE", "MAX_FAMILIES", "MAX_STEPS", "MAX_TASKS", "MAX_TRANSITIONS",
           "done_evidence", "element_words", "fact_phrase", "families", "family_view", "followed_by", "how_usually",
           "in_app", "next_after_element", "next_after_task", "task_apps", "tasks_between", "transitions"]
