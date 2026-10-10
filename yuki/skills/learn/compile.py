"""From a group to what runs: the route ladder, the executable end, the commit and the done-check (phase L design
§4.3-§4.5), and the step compiler ported from ``yuki.memory.trace_demos`` (design §7: ``usable`` / ``BAD_IDENTITY``,
the row -> step compiler, ``split_parts`` and ``sends()`` move here before phase P deletes that module).

**The ladder** (design §4.4). A learned skill never brings a rung A of its own: rung A is its capability's (the catalog
binds the skill's key values into the capability's rungs, ``yuki.skills.catalog.specialise``). What compiling decides
is which rung the habit stands on, kept in its provenance, and the rung-B path when one can be built:

* ``A-link`` when at least :data:`LINK_SHARE` of the group's occasions carry an address the app's own link data can
  turn into a link (a page of a site file's kind or a domain's front page; a chat address that the chat app's
  ``[chat] links`` match): the capability's link rungs serve it;
* ``A-capability`` when the capability has rung-A routes (its bind sources fill them);
* ``B`` a recorded identity path: the constant identity steps of at least :data:`B_MIN_COVERED` covered occasions
  (``group.common_path``), compiled with the ported step compiler; never when the rows sent what was typed
  (:func:`sends`: the commit stays the user's), never past a commit (its last step, the commit itself, is left out),
  and every step held by the consent check at run time (``yuki.skills.uia_route.run_route``);
* ``C`` the capability's own hand-off with the skill pinned (always; never with a game in front).

**Executable end and commit** (design §4.3, D24): the capability's ``pre_commit_end`` when the habit's own end is a
commit, else the capability's own end; the commit is the engine's table (``yuki.skills.engine.run_commit``), so a
missing capability can never make a join or a send Yuki's. **The done-check** is the capability's probe for that end;
a group with no capability has none and is never written (route C only, listed in the report).
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from yuki.skills.learn.group import Group, common_path, step_token

#: A-link: the share of occasions that must carry a buildable address (design §4.4).
LINK_SHARE = 0.8
#: Rung B: at least this many covered occasions must agree (design §4.4).
B_MIN_COVERED = 2
#: The covers whose steps a recorded path may be built from.
COVERED = ("full", "partial")

# ---------------------------------------------------------------------------------------------------------------------
# The step compiler (ported from yuki.memory.trace_demos)
# ---------------------------------------------------------------------------------------------------------------------

#: The trace's columns the step compiler reads.
COLUMNS = ("id, at, ended_at, process, page_key, kind, source, during_run, resolved, role, name_ciphertext,"
           " name_digest, position, aid_ciphertext, aid_digest, element_key, container_digest, chord, typed, n,"
           " clip_seq, file_ext, file_digest, exit_code, outcome, facts, proc_started, run_key")
#: The user's own rows (the trace's source labels; Yuki's are ``yuki``).
USER_SOURCES = ("user", "user_injected")
#: Identities a step may rest on (the trace's ``resolved``): never ``stale`` and never ``unresolved``.
BAD_IDENTITY = ("stale", "unresolved")
#: Kinds of action a step can be made of.
STEP_KINDS = frozenset({"click", "dclick", "menu", "select", "invoke", "keys", "type", "paste", "switch", "nav",
                        "command", "scroll"})


def facts_of(row: dict[str, Any]) -> dict[str, Any]:
    raw = row.get("facts")
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def usable(row: dict[str, Any]) -> str | None:
    """Why a row may not be a step's evidence (``None``: it may): not the user's own, a stale or missing identity,
    inside a detour, a hover. (Ported from ``trace_demos.usable``.)"""
    if row.get("source") not in USER_SOURCES:
        return "not the user's own action"
    if row.get("resolved") in BAD_IDENTITY or not row.get("resolved"):
        return f"its identity was not taken ({row.get('resolved') or 'none'})"
    if row.get("detour"):
        return "a detour"
    if row.get("kind") == "hover":
        return "a hover"
    return None


def sends(rows: Sequence[dict[str, Any]]) -> bool:
    """Whether one of these rows sent what was typed or pasted (machine facts: the field was sent and emptied).
    (Ported from ``trace_demos.sends``.)"""
    return any(facts_of(r).get("field_submitted") or facts_of(r).get("paste_sent") for r in rows)


def element_names(store: Any, rows: Sequence[dict[str, Any]]) -> dict[int, str | None]:
    """``{row id: the name it may be shown and replayed by}``: the element registry's kept name (the trace's rules: a
    command control's at once, a menu item's or tab's after 3 days, a content control's never), else a command
    control's name from the row itself."""
    from yuki.memory.trace_privacy import COMMAND, name_class

    keys = sorted({r["element_key"] for r in rows if r.get("element_key")})
    kept: dict[str, str | None] = {}
    for start in range(0, len(keys), 400):
        part = keys[start:start + 400]
        try:
            for r in store._query("SELECT element_key, name_ciphertext FROM ui_elements WHERE element_key IN (%s)"
                                  % ",".join("?" * len(part)), part):
                kept[r["element_key"]] = store._dec(r["name_ciphertext"]) if r["name_ciphertext"] else None
        except Exception:  # noqa: BLE001
            pass
    out: dict[int, str | None] = {}
    for r in rows:
        name = kept.get(r.get("element_key") or "")
        if not name and r.get("name_ciphertext") and name_class(r.get("role")) == COMMAND:
            try:
                name = store._dec(r["name_ciphertext"])
            except Exception:  # noqa: BLE001
                name = None
        out[int(r["id"])] = " ".join(str(name).split()) if name else None
    return out


def proven_aid(store: Any, row: dict[str, Any], cache: dict[Any, Any] | None = None) -> str | None:
    """The row's AutomationId when that app's AutomationIds are proven (the same element seen with this very id in 2
    sessions of the app and never with another; no element of the app seen with two; no other element with this
    one), else ``None``. (Ported from ``trace_demos.proven_aid``.)"""
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


def row_step(store: Any, row: dict[str, Any], name: str | None, *, slot: str = "",
             cache: dict[Any, Any] | None = None) -> dict[str, Any] | None:
    """One usable row as a skill step (``yuki.memory.practice.demo_step``: a click on a named command control, a pick
    in a container by the name's keyed digest, keys, typing as a slot, an app switch), with a proven AutomationId on a
    click. A page change is never a learned step here (an address would be fixed text; the capability opens pages).
    ``None``: it can only be a hint. (Ported from ``trace_demos.row_step``.)"""
    from yuki.memory import practice as P

    if usable(row) is not None or row.get("kind") not in STEP_KINDS or row.get("kind") == "nav":
        return None
    aid = proven_aid(store, row, cache) if row.get("kind") in ("click", "dclick", "menu", "select", "invoke") else None
    return P.demo_step(row, name, slot=slot, aid=aid, url=None, literal_ok=False)


def end_of(kind: str, row: dict[str, Any], name: str | None) -> dict[str, Any] | None:
    """A checked state as a part's end (``practice.clean_end``). (Ported from ``trace_demos.end_of``.)"""
    from yuki.memory import practice as P

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
                  kinds: Sequence[str] | None = None) -> dict[str, Any] | None:
    """The last checked state the trace recorded on the user's own rows in ``[lo, hi)``, as a part's end (only the
    states the step runner checks live: a window of an app in front, a page reached). (Ported.)"""
    from yuki.memory import practice as P

    kinds = P.PART_END_KINDS if kinds is None else kinds
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
    """The action ids among ``ids`` that are one end of a cross-app link (``ui_links``). (Ported.)"""
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
    """The path's steps (``[(row, step)]`` in order) as parts of at most ``practice.MAX_RUN_STEPS`` steps, cut only
    where the app changes or at a link point and only on a checked state recorded there; the last part ends on
    ``final_end``. ``([], why)`` when it can't be cut so (it stays a hint for the thinker). (Ported from
    ``trace_demos.split_parts``.)"""
    from yuki.memory import practice as P

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
# Compiling a group
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Compiled:
    """What runs a group's habit and how it is checked (content-free but for the B path, kept encrypted)."""

    rung: str = "C"
    link_share: float = 0.0
    exec_end: str = ""
    commit: str = "user"
    end_commit: str = "user"
    probe: str = ""
    routes: list[dict[str, Any]] = field(default_factory=list)
    b_why: str = ""
    means: dict[str, dict[str, int]] = field(default_factory=dict)
    covered: int = 0


def _link_ok(o: Any, index: Any, store: Any) -> bool:
    """Whether the occasion carries an address the app's own link data can build a link from (critique L4)."""
    src = o.src or {}
    if o.kind == "page":
        if src.get("page") is None and src.get("site"):
            return True            # a domain's (or a site's) front page
        try:
            from yuki.connect.templates import site

            data = site(str(src.get("site") or ""))
        except Exception:  # noqa: BLE001
            return False
        kind = str(src.get("page") or "")
        # the site's own address shape for that page kind, or its front page's (a kind whose template is the root)
        return bool((data.get("urls") or {}).get(kind)) or (
            str((data.get("pages") or {}).get(kind) or "") == "/" and bool((data.get("site") or {}).get("home")))
    if o.kind == "message_sent" and src.get("url_ct"):
        app = index.chat_app(str(src.get("app") or ""), str(src.get("host") or ""))
        if not app:
            return False
        try:
            from yuki.connect.templates import match_template
            from yuki.skills.chat import app_data

            url = store._dec(src["url_ct"])     # in RAM only, never kept or logged
            return bool(url) and any(isinstance(link, dict) and match_template(str(link.get("url") or ""), url)
                                     for link in app_data(app).get("links") or [])
        except Exception:  # noqa: BLE001
            return False
    return False


def _probe(cap: Any) -> str:
    for r in cap.routes:
        if r.rung == "A" and r.done is not None:
            call = str(r.done.args.get("call") or "") if r.done.probe == "driver" else ""
            return f"{r.done.probe}:{call}" if call else r.done.probe
    return ""


def means_counts(group: Group, cap: Any) -> dict[str, dict[str, int]]:
    """Per means slot, how often each value showed on the user's own occasions (a late send counts for means; an asked
    occasion never does: D22)."""
    out: dict[str, dict[str, int]] = {}
    for s in cap.slots:
        if s.role != "means" or s.name not in cap.bind:
            continue
        c = Counter(str(o.attrs.get(s.name)) for o in group.occasions if o.by == "user" and o.attrs.get(s.name))
        if c:
            out[s.name] = dict(c.most_common(8))
    return out


def compile_group(group: Group, cap: Any, store: Any, index: Any) -> Compiled:
    """The ladder, executable end, commit and probe of one capability-backed group (see the module docstring)."""
    from yuki.skills.engine import run_commit, served_commit

    out = Compiled()
    out.end_commit = served_commit(cap, group.end)
    out.exec_end = cap.pre_commit_end if (out.end_commit != "none" and cap.pre_commit_end) else cap.end
    out.commit = run_commit(cap)
    out.probe = _probe(cap)
    out.means = means_counts(group, cap)
    counted = [o for o in group.occasions if o.counts]
    if counted:
        out.link_share = round(sum(1 for o in counted if _link_ok(o, index, store)) / len(counted), 3)
    covered = [o for o in group.occasions if o.by == "user" and o.cover in COVERED and o.steps]
    out.covered = len(covered)
    has_a = any(r.rung == "A" for r in cap.routes)
    if has_a and out.link_share >= LINK_SHARE:
        out.rung = "A-link"
    elif has_a:
        out.rung = "A-capability"
    else:
        route, why = compile_b(group, cap, store, commit=out.end_commit)
        out.b_why = why
        if route is not None:
            out.rung, out.routes = "B", [route]
    return out


def compile_b(group: Group, cap: Any, store: Any, *, commit: str) -> tuple[dict[str, Any] | None, str]:
    """A rung-B route for the group (design §4.4), or ``(None, why)``: the constant identity steps of its covered
    occasions, compiled from the latest covered occasion's own rows; nothing that sent what was typed; the commit step
    left out; each step carrying its recorded path (``yuki.skills.uia_route.recorded_path``) for the replay guard."""
    covered = sorted((o for o in group.occasions if o.by == "user" and o.cover in COVERED and o.steps),
                     key=lambda o: o.at)
    if len(covered) < B_MIN_COVERED:
        return None, "fewer than two covered occasions"
    tokens = [[t for t in o.steps if not t.startswith("page:") and not t.endswith("|front")] for o in covered]
    path = common_path(tokens)
    if not path:
        return None, "no common identity steps"
    last = covered[-1]
    try:
        rows = [dict(r) for r in store._query(f"SELECT {COLUMNS} FROM ui_actions WHERE at >= ? AND at <= ?"
                                              f" ORDER BY at, id", (float(last.anchor_at or last.at), float(last.at)))]
    except Exception:  # noqa: BLE001
        return None, "its rows are gone"
    from yuki.memory.store import app_key

    for r in rows:
        r["process"] = app_key(str(r.get("process") or "")) or ""
    # the user's own rows only, as the occasion's steps were read (review r2 L10: a row of Yuki's with the same element
    # key is no step of the user's path)
    mine = [r for r in rows if r["process"] == last.app and r.get("source") in USER_SOURCES and not r.get("during_run")]
    if sends(mine):
        return None, "it sent what was typed (the commit stays the user's)"
    picked_rows: list[dict[str, Any]] = []
    j = 0
    for r in mine:
        got = step_token(r)
        if got is not None and j < len(path) and got[0] == path[j]:
            picked_rows.append(r)
            j += 1
    if j < len(path):
        return None, "the latest occasion's rows don't hold the path"
    if commit != "none" and picked_rows:
        picked_rows = picked_rows[:-1]      # the last step is the commit itself: it stays the user's
    if not picked_rows:
        return None, "nothing before the commit"
    names = element_names(store, picked_rows)
    cache: dict[Any, Any] = {}
    picked: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for r in picked_rows:
        step = row_step(store, r, names.get(int(r["id"])), cache=cache)
        if step is None or step.get("do") in ("type", "command"):
            return None, "a step can't be repeated as it is (it stays a hint)"
        picked.append((r, step))
    final_end = end_of("window_verified", {"process": last.app, "facts": "{}"}, None)
    parts, why = split_parts(store, picked, mine, names, final_end or {"kind": "window_verified", "app": last.app})
    if not parts:
        return None, why
    try:
        from yuki.skills.uia_route import recorded_path

        rec = recorded_path(store, [int(r["id"]) for r, _s in picked]).get("steps") or []
    except Exception:  # noqa: BLE001
        rec = []
    steps: list[dict[str, Any]] = []
    k = 0
    for p in parts:
        for s in p["run"]:
            step = dict(s)
            if k < len(rec) and isinstance(rec[k], dict):
                step["rec"] = rec[k]
            steps.append(step)
            k += 1
    return {"rung": "B", "variant": "learned", "via": "uia_steps", "needs_focus": True,
            "args": {"steps": steps, "apps": [last.app]}}, ""


__all__ = ["LINK_SHARE", "B_MIN_COVERED", "COLUMNS", "USER_SOURCES", "BAD_IDENTITY", "STEP_KINDS", "facts_of", "usable",
           "sends", "element_names", "proven_aid", "row_step", "end_of", "state_between", "link_ends", "split_parts",
           "Compiled", "means_counts", "compile_group", "compile_b"]
