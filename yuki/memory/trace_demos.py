"""The demo learner's code half: a task the user did, as steps Yuki's hands can take, in parts (report 78 §6.1-6.2,
phase C4).

The user's words of 7 Oct: "so when i tell it to do something it doesnt have to guess in the pool of data, it just
knows what i usually do and it will do it from the pool of data." The task pass (C2, :mod:`yuki.memory.trace_tasks`)
groups the user's own actions into tasks with a checked outcome; :meth:`yuki.memory.practice_learn.Learner.learn_task`
turns one such task into a procedure through the practice tables. This module is everything in that path that is code
and no model:

* **Which tasks** (:func:`due_tasks`): only a task whose outcome is a checked fact (:func:`trace_tasks.learnable`),
  never ``unclear``, never one the user rejected on the Day page; its detours are never steps.
* **The rows** (:func:`task_actions`): the task's own actions with every machine field a step needs (the keyed name
  digest, the AutomationId's digest and the process start for the ``aid`` proof, the container digest), detours marked.
* **What a row may become** (:func:`row_step`, through :func:`yuki.memory.practice.demo_step`): a click on a named
  command control, a ``pick`` in a container for a content control, keys, typing as a slot (D8), an app switch, a page
  change only when ``literal_ok`` and its address was kept, a Warp command (the thinker's), a scroll. Only the user's
  own rows (``user`` / ``user_injected``) whose identity was taken (never ``stale``, never ``unresolved``).
* **AutomationIds** (:func:`proven_aid`): kept on a click step only where that app's AutomationIds are proven - the
  same element seen with the same AutomationId in 2 sessions of the app (two process starts) and no element of that app
  seen with two (report 78 §5.1).
* **Parts** (:func:`split_parts`): a way of more than 8 steps is cut into parts of at most 8 at app switches and link
  points (``ui_links``), each part ending on a checked state the trace recorded there (report 78 §5.3's facts, or a
  window or page verified); at most 4 parts, else the way stays a hint for the thinker. The last part ends on the
  task's own checked outcome.
* **The target** (:func:`target_sig`): the outcome's kind and target (element key, app, page digest, file digest) as a
  keyed digest, so two demos combine only on the same target (decision D3).
* **Pick digests** (:func:`name_digest`): the very keyed digest C1 takes over an element's name
  (:meth:`yuki.memory.trace.ActionTrace._element_cols`), for the skill runner's live match.

No regex, no keyword list, no per-app branch: equality of machine fields, counts and times.
"""

from __future__ import annotations

import json
import time
from collections.abc import Sequence
from typing import Any

from yuki.memory import practice as P

#: The trace's columns the demo learner reads (more than the task pass: the aid proof and the pick digests).
COLUMNS = ("id, at, ended_at, process, page_key, kind, source, during_run, resolved, role, name_ciphertext,"
           " name_digest, position, aid_ciphertext, aid_digest, element_key, container_digest, chord, typed, n,"
           " clip_seq, file_ext, file_digest, exit_code, outcome, facts, proc_started, run_key")
#: The user's own rows (C1's source labels; Yuki's are ``yuki``).
USER_SOURCES = ("user", "user_injected")
#: Identities a step may rest on (C1's ``resolved``): never ``stale`` (the element changed under the press) and never
#: ``unresolved`` (no identity at all).
BAD_IDENTITY = ("stale", "unresolved")
#: Kinds of action a step can be made of; the rest are shown as hints only (a right click, a drag, a copy, a file
#: dialog's own row).
STEP_KINDS = frozenset({"click", "dclick", "menu", "select", "invoke", "keys", "type", "paste", "switch", "nav",
                        "command", "scroll"})
SHOWN_KINDS = STEP_KINDS | {"rclick", "drag", "copy", "file_open", "file_save"}
#: The task's actions shown to the model at most.
MAX_LINES = 80
#: Tasks looked at per pass at most, and a task older than this is not learned from (its rows may be gone).
TASKS_PER_PASS = 3
MAX_TASK_AGE_S = 7 * 86400.0
#: ``nudge_state`` names: the pass's cursor and retries, and its health for the doctor.
STATE_KEY = "trace:demos_state"
HEALTH_KEY = "trace:demos"
#: A task whose model call failed is tried again on later ticks this many times at most.
TRIES = 3


# ---------------------------------------------------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------------------------------------------------


def facts_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("facts")
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def task_actions(store: Any, task_id: int) -> list[dict[str, Any]]:
    """The task's actions in its pieces' time ranges, oldest first, each with ``detour`` (inside a detour range the
    task pass marked) - every source and identity, so the checks can see what they refuse. ``[]`` when the task's
    ranges are gone."""
    try:
        ranges = store._query(
            "SELECT r.detour, a.at AS a0, b.at AS a1 FROM ui_task_actions r LEFT JOIN ui_actions a ON a.id ="
            " r.first_action LEFT JOIN ui_actions b ON b.id = r.last_action WHERE r.task_id = ?", (int(task_id),))
    except Exception:  # noqa: BLE001 - a database before migration 39
        return []
    spans, detours = [], []
    for rg in ranges:
        if rg["a0"] is None or rg["a1"] is None:
            continue
        lo, hi = min(float(rg["a0"]), float(rg["a1"])), max(float(rg["a0"]), float(rg["a1"]))
        (detours if rg["detour"] else spans).append((lo, hi))
    rows: dict[int, dict[str, Any]] = {}
    for lo, hi in spans:
        for r in store._query(f"SELECT {COLUMNS} FROM ui_actions WHERE at >= ? AND at <= ? ORDER BY at, id",
                              (lo, hi)):
            d = dict(r)
            d["detour"] = any(a <= float(d["at"]) <= b for a, b in detours)
            rows[int(d["id"])] = d
    return sorted(rows.values(), key=lambda r: (float(r["at"]), int(r["id"])))


def usable(row: dict[str, Any]) -> str | None:
    """Why a row may not be a step's evidence (``None``: it may): not the user's own (Yuki's rows never teach the
    user's way), a stale or missing identity, inside a detour, a hover."""
    if row.get("source") not in USER_SOURCES:
        return "not the user's own action"
    if row.get("resolved") in BAD_IDENTITY or not row.get("resolved"):
        return f"its identity was not taken ({row.get('resolved') or 'none'})"
    if row.get("detour"):
        return "a detour"
    if row.get("kind") == "hover":
        return "a hover"
    return None


def element_names(store: Any, rows: Sequence[dict[str, Any]]) -> dict[int, str | None]:
    """``{row id: the name it may be shown and replayed by}`` (C1's rules, :func:`practice_learn._element_names`)."""
    from yuki.memory import practice_learn as PL

    return PL._element_names(store, list(rows))   # noqa: SLF001 - one rule for every learner


# ---------------------------------------------------------------------------------------------------------------------
# AutomationIds: proven per app (report 78 §5.1)
# ---------------------------------------------------------------------------------------------------------------------


def proven_aid(store: Any, row: dict[str, Any], cache: dict[Any, Any] | None = None) -> str | None:
    """The row's AutomationId when that app's AutomationIds are proven, else ``None``: the element was seen with this
    very AutomationId (its keyed digest) in 2 or more sessions of the app (distinct process starts) and never with
    another, no element of that app was ever seen with two different ones across its sessions, and no other element of
    that app was ever seen with this one (second verification V2: an id must tell this control apart).

    ``cache`` (review F8: one dict for one task's pass): each element's proof and each app's "an element of it changed
    its AutomationId" answer are read once, not once per click row."""
    digest, key, process = row.get("aid_digest"), row.get("element_key"), row.get("process")
    if not digest or not key or not process or not row.get("aid_ciphertext"):
        return None
    cache = cache if cache is not None else {}
    try:
        ek = ("element", str(key))
        if ek not in cache:
            cache[ek] = [(r[0], int(r[1] or 0)) for r in store._query(
                "SELECT aid_digest, count(DISTINCT proc_started) FROM ui_actions WHERE element_key = ? AND aid_digest IS"
                " NOT NULL AND proc_started IS NOT NULL AND source IN ('user', 'user_injected') GROUP BY aid_digest",
                (str(key),))]
        mine = cache[ek]
        if len(mine) != 1 or mine[0][0] != digest or mine[0][1] < 2:
            return None
        pk = ("app", str(process))
        if pk not in cache:
            cache[pk] = bool(store._query(
                "SELECT 1 FROM ui_actions WHERE process = ? AND aid_digest IS NOT NULL AND element_key IS NOT NULL"
                " GROUP BY element_key HAVING count(DISTINCT aid_digest) > 1 LIMIT 1", (str(process),)))
        if cache[pk]:
            return None
        # second verification V2: an AutomationId shared by another element of that app (a generic id: a Win32
        # control id, a template's) is no identity of this one
        sk = ("shared", str(process), str(digest))
        if sk not in cache:
            cache[sk] = bool(store._query(
                "SELECT 1 FROM ui_actions WHERE process = ? AND aid_digest = ? AND element_key IS NOT NULL AND"
                " element_key != ? LIMIT 1", (str(process), str(digest), str(key))))
        if cache[sk]:
            return None
        return store._dec(row["aid_ciphertext"]) or None
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------------------------------------------------


def row_step(store: Any, row: dict[str, Any], name: str | None, *, slot: str = "", literal_ok: bool = True,
             cache: dict[Any, Any] | None = None) -> dict[str, Any] | None:
    """One usable row (:func:`usable`) as a step (:func:`yuki.memory.practice.demo_step`), with a proven AutomationId
    on a click and a page's address read from the timeline for a page change (``literal_ok`` only). ``cache``: the
    AutomationId proofs read once a pass (:func:`proven_aid`)."""
    if usable(row) is not None or row.get("kind") not in STEP_KINDS:
        return None
    url = None
    if row.get("kind") == "nav" and literal_ok:
        from yuki.memory import practice_learn as PL

        url = PL.page_url(store, row)
    aid = proven_aid(store, row, cache) if row.get("kind") in ("click", "dclick", "menu", "select", "invoke") else None
    return P.demo_step(row, name, slot=slot, aid=aid, url=url, literal_ok=literal_ok)


def step_words(step: dict[str, Any] | None, row: dict[str, Any], name: str | None) -> str:
    """A step in plain words for the thinker and the Playbook page (content-free but for kept names)."""
    app = str(row.get("process") or "")
    where = f" in {app}" if app else ""
    if step is None:
        from yuki.memory import practice_learn as PL

        return P._clip(PL.action_line(row, name, float(row["at"])).split(" · ", 1)[-1] + where, P.STEP_CHARS)
    do = step.get("do")
    if do == "click":
        return P._clip(f'Click {step.get("role") or "the control"} "{step.get("name")}"{where}', P.STEP_CHARS)
    if do == "pick":
        box = (step.get("in") or {}).get("role") or "list"
        what = f"the {step['slot']}" if step.get("slot") else "the same item as always"
        return P._clip(f"Pick {what} in the {box}{where}", P.STEP_CHARS)
    if do == "keys":
        return P._clip(f"Press {'+'.join(k.capitalize() if len(k) > 1 else k.upper() for k in step['keys'])}{where}",
                       P.STEP_CHARS)
    if do == "type":
        return P._clip(f"Type the {step.get('slot') or 'words from the request'}{where}", P.STEP_CHARS)
    if do == "open_app":
        return P._clip(f"Open {step.get('name')}", P.STEP_CHARS)
    if do == "open_url":
        # the address stays in the step itself; the words (shown to the model and on the page) never carry it
        return P._clip(f"Open the same page again{where}", P.STEP_CHARS)
    if do == "command":
        return P._clip(f"Run the terminal command for the {step.get('slot') or 'request'}{where} (your terminal tool)",
                       P.STEP_CHARS)
    return P._clip(f"Scroll {step.get('direction')}{where}", P.STEP_CHARS)


def sends(rows: Sequence[dict[str, Any]]) -> bool:
    """Whether one of these rows sent what was typed or pasted (machine facts: the field was sent and emptied)."""
    return any(facts_of(r).get("field_submitted") or facts_of(r).get("paste_sent") for r in rows)


# ---------------------------------------------------------------------------------------------------------------------
# Checked states and parts
# ---------------------------------------------------------------------------------------------------------------------


def end_of(kind: str, row: dict[str, Any], name: str | None) -> dict[str, Any] | None:
    """A checked state as a part's end (:func:`yuki.memory.practice.clean_end`)."""
    facts = facts_of(row)
    process = str(row.get("process") or "")
    if kind == "window_verified":
        app = str(facts.get("window_verified") or process or "")
        return P.clean_end({"kind": kind, "app": app}) if app else None
    if kind in ("field_submitted", "toggle_state", "selected", "paste_sent"):
        return P.clean_end({"kind": kind, "app": process, "role": row.get("role"), "name": name})
    if kind in ("file_saved", "file_opened"):
        return P.clean_end({"kind": kind, "app": process, "ext": row.get("file_ext")})
    return P.clean_end({"kind": kind, "app": process})


def state_between(rows: Sequence[dict[str, Any]], names: dict[int, str | None], lo: float, hi: float,
                  kinds: Sequence[str] = P.PART_END_KINDS) -> dict[str, Any] | None:
    """The last checked state the trace recorded on the user's own rows in ``[lo, hi)`` (report 78 §5.3), as a part's
    end; ``None`` when there is none. ``kinds``: by default only the states the skill runner checks live at a part's
    end (review F7: a window of the app in front, a page reached); a fact recorded on a row between two steps (a control
    selected, a field sent) could not be checked when the part is run, so it is no cut point."""
    best = None
    for r in rows:
        at = float(r["at"])
        if at < lo or at >= hi or r.get("source") not in USER_SOURCES or r.get("resolved") == "stale" or r.get("detour"):
            continue
        facts = facts_of(r)
        for kind in kinds:
            if kind in facts and facts[kind] is not None and facts[kind] is not False:
                end = end_of(kind, r, names.get(int(r["id"])))
                if end is not None:
                    best = end
    return best


def link_ends(store: Any, ids: Sequence[int]) -> set[int]:
    """The action ids among ``ids`` that are one end of a cross-app link (``ui_links``: a copy and its paste, a file
    saved and opened, a link and its page)."""
    ids = [int(i) for i in ids]
    if not ids:
        return set()
    try:
        marks = ",".join("?" * len(ids))
        rows = store._query(f"SELECT from_action, to_action FROM ui_links WHERE from_action IN ({marks}) OR to_action IN"
                            f" ({marks})", (*ids, *ids))
    except Exception:  # noqa: BLE001
        return set()
    wanted = set(ids)
    return {int(x) for r in rows for x in (r[0], r[1]) if x is not None and int(x) in wanted}


def split_parts(store: Any, picked: Sequence[tuple[dict[str, Any], dict[str, Any]]], rows: Sequence[dict[str, Any]],
                names: dict[int, str | None], final_end: dict[str, Any]) -> tuple[list[dict[str, Any]], str]:
    """The way's steps (``[(row, step)]`` in order) as parts (``([{"run", "end"}], "")``) or ``([], why)``.

    At most :data:`yuki.memory.practice.MAX_RUN_STEPS` steps a part. A part may end only where the app changes (the
    next step acts in another app, or this step is the switch to one) or at a link point (an end of a ``ui_links``
    row between this step and the next), and only where the trace recorded a checked state between this step and the
    next (:func:`state_between`) - that state is the part's end. The last part ends on the task's own outcome
    (``final_end``). Parts are as long as they can be (fewest parts); more than
    :data:`yuki.memory.practice.MAX_PARTS`, or a stretch of more than 8 steps with no such point: ``[]`` (the way
    stays a hint for the thinker)."""
    n = len(picked)
    if n == 0:
        return [], "no steps"
    if n <= P.MAX_RUN_STEPS:
        return [{"run": [s for _r, s in picked], "end": final_end}], ""
    ordered = sorted(rows, key=lambda r: (float(r["at"]), int(r["id"])))
    links = link_ends(store, [int(r["id"]) for r in ordered])
    ends: dict[int, dict[str, Any]] = {}
    for i in range(n - 1):
        row, step = picked[i]
        nxt_row, nxt_step = picked[i + 1]
        lo, hi = float(row["at"]), float(nxt_row["at"])
        between = [r for r in ordered if lo <= float(r["at"]) < hi]
        app_change = (str(row.get("process") or "") != str(nxt_row.get("process") or "")
                      or step.get("do") == "open_app" or nxt_step.get("do") == "open_app")
        link_point = any(int(r["id"]) in links for r in between)
        if not (app_change or link_point):
            continue
        end = state_between(ordered, names, lo, hi)
        if end is not None:
            ends[i] = end
    parts: list[dict[str, Any]] = []
    start = 0
    while start < n:
        if n - start <= P.MAX_RUN_STEPS:
            parts.append({"run": [s for _r, s in picked[start:]], "end": final_end})
            break
        cut = max((i for i in ends if start <= i < start + P.MAX_RUN_STEPS), default=None)
        if cut is None:
            return [], "more than 8 steps in a row with no app switch or link point that ends on a checked state"
        parts.append({"run": [s for _r, s in picked[start:cut + 1]], "end": ends[cut]})
        start = cut + 1
    if len(parts) > P.MAX_PARTS:
        return [], f"{len(parts)} parts: longer than {P.MAX_PARTS} parts stays a hint"
    clean = P.clean_parts(parts)
    return (clean, "") if clean else ([], "the parts did not hold together")


# ---------------------------------------------------------------------------------------------------------------------
# Targets, digests
# ---------------------------------------------------------------------------------------------------------------------


def target_sig(store: Any, kind: str, target: Any, task_id: Any = None) -> str:
    """The outcome's kind and target as a short keyed digest (the demo run key's second half): two demos combine only
    when it is equal (decision D3: the same outcome kind on the same target).

    Review F5: an outcome with no target (C2 keeps none for a file saved or opened without its file's digest, a page
    reached without its page key) is no "same target" for anything: its signature is the task's own, so it never
    combines with another task and stays a trial until one of Yuki's runs proves it."""
    if target is None or str(target) == "":
        return store.cipher.digest("demo-target", str(kind or ""), "", "no-target", str(task_id))[:16]
    return store.cipher.digest("demo-target", str(kind or ""), str(target))[:16]


def steps_sig(store: Any, pairs: Sequence[tuple[dict[str, Any], dict[str, Any] | None]]) -> str:
    """Second verification V3: the way's steps as a short keyed digest, so two demos count together toward D3 only
    when the user did the *same* thing (decision D3: "after the user did it 2 times"), not merely reached the same
    target. Over each step's machine shape - what it does, the control's role and kept name, the picked item's digest
    and its container, the keys, the app opened, the address - never the slot names or expected words the model chose,
    nor an AutomationId (proven later or not); an action that can only be a hint by its kind and element key."""
    shape = []
    for row, step in pairs:
        if step is None:
            shape.append(["hint", str(row.get("kind") or ""), str(row.get("element_key") or "")])
            continue
        box = step.get("in") if isinstance(step.get("in"), dict) else {}
        shape.append([str(step.get("do") or ""), " ".join(str(step.get("role") or "").split()).casefold(),
                      " ".join(str(step.get("name") or "").split()).casefold(), str(step.get("item") or ""),
                      "slot" if step.get("do") in ("pick", "type", "command") and "item" not in step else "",
                      str(box.get("role") or ""), str(box.get("digest") or ""),
                      "+".join(sorted(str(k).casefold() for k in step.get("keys") or [])),
                      str(step.get("url") or "").rstrip("/"), str(step.get("direction") or "")])
    return store.cipher.digest("demo-steps", json.dumps(shape, separators=(",", ":")))[:12]


def demo_sig(target: str, steps: str) -> str:
    """The demo run key's evidence half (:func:`yuki.memory.practice.demo_key`): the outcome's target signature and
    the steps' signature (V3). Two demos combine only when both are equal."""
    return f"{target}.{steps}"


def container_digest(store: Any, process: str, parent_role: str, parent_aid: str, grand_role: str) -> str:
    """The keyed digest C1 takes over a content control's container (:meth:`yuki.memory.trace.ActionTrace.
    _element_cols`: ``digest("ui-container", process, parent role, parent AutomationId, grandparent role)``), for the
    skill runner's live pick (second verification V8)."""
    from yuki.memory.store import app_key

    return store.cipher.digest("ui-container", app_key(str(process or "")) or str(process or ""),
                               str(parent_role or ""), str(parent_aid or ""), str(grand_role or ""))


def name_digest(store: Any, process: str, role: str, name: str) -> str:
    """The keyed digest C1 takes over an element's name (:meth:`yuki.memory.trace.ActionTrace._element_cols`:
    ``digest("ui-name", process, role, normalise_name(name))``), for matching a picked item live."""
    from yuki.memory.store import app_key
    from yuki.memory.trace_privacy import normalise_name

    return store.cipher.digest("ui-name", app_key(str(process or "")) or str(process or ""), str(role or ""),
                               normalise_name(name))


# ---------------------------------------------------------------------------------------------------------------------
# Which tasks, the pass's state, health
# ---------------------------------------------------------------------------------------------------------------------


def load_state(store: Any) -> dict[str, Any]:
    try:
        got = json.loads(store.nudge_state(STATE_KEY) or "{}")
    except Exception:  # noqa: BLE001
        got = {}
    got = got if isinstance(got, dict) else {}
    return {"cursor": int(got.get("cursor") or 0), "tries": {str(k): int(v) for k, v in (got.get("tries") or {}).items()},
            "redo": [int(x) for x in got.get("redo") or [] if str(x).isdigit()],
            "retired_tasks": {str(k): int(v) for k, v in (got.get("retired_tasks") or {}).items()},
            "forgotten": {str(k): int(v) for k, v in (got.get("forgotten") or {}).items()},
            "log": int(got.get("log") or 0)}


def save_state(store: Any, state: dict[str, Any]) -> None:
    store.set_nudge_state(STATE_KEY, json.dumps(state, sort_keys=True))


def due_tasks(store: Any, now: float, *, limit: int = TASKS_PER_PASS,
              state: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Tasks not looked at yet (past the pass's cursor, or put back by an undone rejection), oldest first, each with
    ``learnable``: a checked outcome, not rejected (:func:`yuki.memory.trace_tasks.learnable`), ended in the last
    :data:`MAX_TASK_AGE_S`. The list stops after ``limit`` learnable ones; the unlearnable ones before them are listed
    so the pass's cursor moves past them for good."""
    from yuki.memory import trace_tasks as T

    state = state if state is not None else load_state(store)
    redo = list(state.get("redo") or [])
    try:
        rows = store._query("SELECT id, episode_id, started_at, ended_at, apps, goal_ciphertext, outcome_fact,"
                            " outcome_action, outcome_target, family_id, user_verdict FROM ui_tasks WHERE (id > ? OR"
                            " id IN (%s)) AND valid_to IS NULL ORDER BY id LIMIT 500"
                            % ",".join("?" * max(1, len(redo))), (int(state.get("cursor") or 0), *(redo or [-1])))
    except Exception:  # noqa: BLE001
        return []
    out, n = [], 0
    for r in rows:
        d = dict(r)
        d["learnable"] = T.learnable(d, store) and float(d["ended_at"] or 0) >= now - MAX_TASK_AGE_S
        out.append(d)
        n += int(d["learnable"])
        if n >= limit:
            break
    return out


def demo_events(store: Any) -> list[dict[str, Any]]:
    """Every live object's demo evidence: ``[{"playbook_id", "run_key", "task", "type", "at"}]``."""
    try:
        rows = store._query("SELECT e.playbook_id, e.run_key, e.type, e.at FROM playbook_events e JOIN playbooks p ON"
                            " p.id = e.playbook_id WHERE p.valid_to IS NULL AND e.run_key LIKE ? AND e.type IN"
                            " ('demonstrated', 'demo_withdrawn', 'demo_restored') ORDER BY e.at, e.id",
                            (P.DEMO_RUN_PREFIX + "%",))
    except Exception:  # noqa: BLE001
        return []
    return [{"playbook_id": int(r[0]), "run_key": str(r[1]), "task": P.demo_task(r[1]), "type": r[2], "at": float(r[3])}
            for r in rows]


def health(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """What the demo learner holds, content-free: ways and trials learned from what the user did (live), tasks learned
    from today, how many were refused in code today (kept by the pass)."""
    now = time.time() if now is None else float(now)
    ways = trials = hints = 0
    for o in P.live(store, ["procedure"]):
        if o.origin != "demo":
            continue
        if o.trial:
            trials += 1
        elif o.hint_only:
            hints += 1   # second verification V11: a way the hands cannot take is a hint for the thinker, not "alone"
        else:
            ways += 1
    try:
        prior = json.loads(store.nudge_state(HEALTH_KEY) or "{}")
    except Exception:  # noqa: BLE001
        prior = {}
    prior = prior if isinstance(prior, dict) else {}
    from datetime import datetime

    day = datetime.fromtimestamp(now).strftime("%Y-%m-%d")
    same_day = prior.get("day") == day
    return {"at": now, "day": day, "ways": ways, "trials": trials, "hints": hints,
            "learned_today": int(prior.get("learned_today") or 0) if same_day else 0,
            "refused_today": int(prior.get("refused_today") or 0) if same_day else 0}


__all__ = ["COLUMNS", "HEALTH_KEY", "STATE_KEY", "container_digest", "demo_sig", "due_tasks", "demo_events",
           "element_names", "end_of", "facts_of", "steps_sig",
           "health", "link_ends", "load_state", "name_digest", "proven_aid", "row_step", "save_state", "sends",
           "split_parts", "state_between", "step_words", "target_sig", "task_actions", "usable"]
