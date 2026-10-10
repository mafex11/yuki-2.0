"""The skill learner (phase L, ``docs/research/audit-2026-10-09/learner/design.md`` revision 2; decisions D21-D24).

Yuki learns which of its capabilities the user actually uses, with which fixed values, in which program, when, and
writes each habit as a learned skill: a ``skills`` row the catalog loads (``yuki.skills.catalog``), the Playbook shows,
the routine miner keys on and the card lane may offer. Nobody writes the user's habits into code or data: this package
holds no app key, site, page kind, person, time or default (the capabilities' data and the user's moments say all).

The pipeline (each a module here)::

    moments (+ timeline, trace, runs) --occasions--> occasions  (D18 acting filter, late sends, live-kind rules)
        --bind--> capability per occasion (its serves and bind data)  --group--> habits (split by key values only)
        --compile--> rung, executable end, commit (engine D24), probe  --write--> skills rows (+ naming, lName)
        --> occasions.skill_id (the routine miner reads it; one skill per moment by construction)

* :func:`rebuild` - the full pass (nightly, and ``python -m yuki.skills.learn rebuild``): the moments' retention, one
  batched naming call for new or changed groups, written in one transaction. < 2 s CPU over 28 days (design §9.4).
* :func:`incremental` - after a moments refresh that wrote end moments, while the user is idle, at most every 10
  minutes (``yuki.memory.moments.MomentHub``): the last hour's occasions, mapped onto the skills already written; no
  new skill, no model call. < 0.2 s.
* :func:`nightly` - the portrait scheduler's night pass (``yuki.memory.portrait.PortraitScheduler._night``), never
  with a game in front or a call open (:func:`quiet`).
* :func:`routine_inputs` - the routine miner's onsets keyed by learned skill id (phase L lWire's ``keyed_onsets``).

Logs are content-free (``skills_learned``: counts, CPU ms, code words). ``occasions`` and the learned rows stay out of
the MCP.
"""

from __future__ import annotations

import json
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

#: The incremental pass recomputes this much of the recent past (s): a visit's anchor looks back at most 10 minutes.
INCREMENTAL_S = 3600.0
#: Where the full pass leaves the browsers it measured for the incremental passes (content-free app keys and counts).
BROWSERS_STATE = "learn:browsers"
BROWSERS_FRESH_S = 26 * 3600.0
DAY_S = 86400.0

LogFn = Callable[..., Any]


@dataclass
class Result:
    """One learner pass (content-free counters; the groups and plans stay in RAM for the CLI report)."""

    ok: bool = True
    error: str = ""
    counts: Counter = field(default_factory=Counter)
    built: Any = None
    groups: list[Any] = field(default_factory=list)
    seen: list[Any] = field(default_factory=list)
    compiled: dict[str, Any] = field(default_factory=dict)
    planned: list[Any] = field(default_factory=list)
    merges: list[tuple[str, str]] = field(default_factory=list)
    cpu_s: float = 0.0
    wall_s: float = 0.0
    named: int = 0


def _log(log: LogFn | None, event: str, **fields: Any) -> None:
    if log is None:
        return
    try:
        log(event, **{k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001 - a log never breaks a pass
        pass


def quiet(store: Any, now: float | None = None, *, idle_s: float | None = None,
          idle_fn: Callable[[], float] | None = None) -> str | None:
    """Why the learner must not run now (``None``: it may): the user not idle for ``idle_s`` (when given), a game in
    front, a call open, a meeting or a full-screen app (design §9.1: never in a game or call). Fails closed."""
    now = time.time() if now is None else float(now)
    if idle_s is not None:
        try:
            if idle_fn is None:
                from yuki.memory.watcher import user_idle_s as idle_fn
            if float(idle_fn()) < float(idle_s):
                return "not idle"
        except Exception:  # noqa: BLE001
            return "idle unknown"
    try:
        from yuki.memory.gamegate import game_in_front

        if game_in_front():
            return "a game is in front"
    except Exception:  # noqa: BLE001
        return "game unknown"
    try:
        from yuki.memory.portrait import scheduled_busy

        busy = scheduled_busy(store, None, now)
    except Exception:  # noqa: BLE001
        busy = None
    return busy


def _merge_none(groups: list[Any], seen: list[Any], compiled: dict[str, Any], caps: dict[str, Any],
                browsers: dict[str, int]) -> list[tuple[str, str]]:
    """Design §4.2's merge: a group no capability serves whose executable end is a page of a domain (sends in a
    browser chat on a site: the commit is the user's words, so the end Yuki may reach is that site's page) joins the
    passing site group whose bound site is on the same registrable domain (same capability end, same bound value)."""
    from yuki.memory.moments import registrable_domain
    from yuki.skills.learn.bind import NONE

    targets: dict[tuple[str, str], Any] = {}
    for g in groups:
        cap = caps.get(g.capability)
        if cap is None or g.capability == NONE:
            continue
        site_slot = next((s for s, src in cap.bind.items() if src == "site"), "")
        if site_slot and g.values.get(site_slot):
            targets[(compiled[g.gid].exec_end, registrable_domain(str(g.values[site_slot])))] = g
    merges: list[tuple[str, str]] = []
    for pool in (groups, seen):
        for g in list(pool):
            if g.capability != NONE or g.end != "message_sent":
                continue
            hosts = Counter(registrable_domain(o.src.get("host")) for o in g.occasions
                            if o.src.get("host") and o.app in browsers)
            if not hosts:
                continue
            domain = hosts.most_common(1)[0][0]
            target = targets.get(("page", domain))
            if target is None:
                continue
            target.occasions.extend(g.occasions)
            target.merged_from.append(g.gid)
            for o in g.occasions:
                o.group = target
            pool.remove(g)
            merges.append((g.gid, target.gid))
    return merges


def _occasion_keys(occasions: Sequence[Any], caps: dict[str, Any]) -> None:
    """Each occasion's group key digest and its means value (content-free columns of ``occasions``)."""
    import hashlib

    for o in occasions:
        g = o.group
        if g is not None:
            o.key_value_digest = hashlib.sha256(json.dumps(list(g.keys), separators=(",", ":")).encode()).hexdigest()[:16]
        cap = caps.get(o.capability)
        if cap is not None:
            for s in cap.slots:
                if s.role == "means" and s.name in cap.bind and o.attrs.get(s.name):
                    o.means = str(o.attrs[s.name])[:40]
                    break


def run(store: Any, *, now: float | None = None, name: bool = True, worker: Any = None, log: LogFn | None = None,
        write_rows: bool = True) -> Result:
    """The full pass (see the module docstring). Never raises: ``Result.ok`` False and ``Result.error``."""
    from yuki.skills.learn import bind as B
    from yuki.skills.learn import compile as C
    from yuki.skills.learn import group as G
    from yuki.skills.learn import occasions as O
    from yuki.skills.learn import write as W

    now = time.time() if now is None else float(now)
    res = Result()
    c0, w0 = time.thread_time(), time.perf_counter()
    try:
        idx = B.load_index(store, now)
        built = O.build(store, now=now, browsers=idx.browsers)
        res.built = built
        B.bind(built.occasions, idx)
        for o in built.occasions:
            if o.capability == B.NONE and o.by == "asked":
                # an asked run's end is its capability's own end (a tab brought forward): bound there, never a serve
                cap = next((c for c in idx.caps if c.end == o.kind), None)
                if cap is not None:
                    o.capability, o.attrs = cap.key, B.attrs_for(cap, o.src)
        caps = dict(idx.by_key)
        groups, seen = G.build(built.occasions, caps, store.cipher, now)
        # a skill run of a learned skill counts on that skill's own group (its key values are the skill's)
        by_key = {G.skill_key(store.cipher, g.capability, g.keys): g for g in groups if g.capability in caps}
        for o in built.occasions:
            target = by_key.get(str(o.facts.get("skill") or "")) if o.by == "asked" else None
            if target is not None and o.group is not target:
                if o.group is not None and o in o.group.occasions:
                    o.group.occasions.remove(o)
                target.occasions.append(o)
                o.group = target
        for g in groups:
            g.bar = G.bar(g.occasions, now)
        for g in groups:
            cap = caps.get(g.capability)
            if cap is not None:
                res.compiled[g.gid] = C.compile_group(g, cap, store, idx)
        res.merges = _merge_none(groups, seen, res.compiled, caps, idx.browsers)
        for g in groups:
            if g.merged_from:
                g.bar = G.bar(g.occasions, now)
        res.groups, res.seen = groups, seen
        _occasion_keys(built.occasions, caps)
        planned, counts = W.plan(store, groups, caps, res.compiled, now)
        res.planned = planned
        res.counts.update(counts)
        if name and write_rows:
            res.named = W.name_planned(store, planned, worker=worker, log=log)
        else:
            for p in planned:
                p.provenance["offer_static"] = W.offer_static(p.cap, p.compiled, p.provenance.get("everyday"))
        if write_rows:
            cap_ids = W.capability_ids(store)
            res.counts.update(W.write(store, planned, now=now, cap_ids=cap_ids))
            res.counts["occasions_saved"] = O.save(store, built.occasions, since=built.since, until=now, now=now)
            try:
                store.set_nudge_state(BROWSERS_STATE, json.dumps({"at": now, "browsers": built.browsers}))
            except Exception:  # noqa: BLE001
                pass
        res.counts["occasions"] = len(built.occasions)
        res.counts["groups"] = len(groups)
        res.counts["seen_groups"] = len(seen)
        res.counts["merged"] = len(res.merges)
        res.counts["skills"] = sum(1 for p in planned if not p.group.usage)
        res.counts["usage_records"] = sum(1 for p in planned if p.group.usage)
    except Exception as exc:  # noqa: BLE001 - logged; the next night tries again
        res.ok, res.error = False, type(exc).__name__
        import traceback

        res.counts["error"] = 1
        _log(log, "error", where="learn.run", error=type(exc).__name__, traceback=traceback.format_exc()[-800:])
    res.cpu_s = round(time.thread_time() - c0, 3)
    res.wall_s = round(time.perf_counter() - w0, 3)
    _log(log, "skills_learned", mode="full", ok=res.ok, cpu_ms=round(res.cpu_s * 1000), named=res.named,
         **{k: int(v) for k, v in res.counts.items() if isinstance(v, int)})
    return res


def rebuild(store: Any, *, now: float | None = None, log: LogFn | None = None, worker: Any = None,
            name: bool = True) -> Result:
    """``python -m yuki.skills.learn rebuild``: the full pass, written."""
    return run(store, now=now, name=name, worker=worker, log=log, write_rows=True)


def incremental(store: Any, *, now: float | None = None, log: LogFn | None = None) -> dict[str, Any]:
    """The last :data:`INCREMENTAL_S` of occasions rebuilt and mapped onto the learned skills already written (no new
    skill, no model call; the night pass makes those). Content-free counts; never raises."""
    from yuki.skills.learn import bind as B
    from yuki.skills.learn import group as G
    from yuki.skills.learn import occasions as O

    now = time.time() if now is None else float(now)
    c0, w0 = time.thread_time(), time.perf_counter()
    out: dict[str, Any] = {"ok": True}
    try:
        browsers: dict[str, int] | None = None
        try:
            kept = json.loads(store.nudge_state(BROWSERS_STATE) or "{}")
            if isinstance(kept, dict) and now - float(kept.get("at") or 0.0) <= BROWSERS_FRESH_S:
                browsers = {str(k): int(v) for k, v in (kept.get("browsers") or {}).items()}
        except Exception:  # noqa: BLE001
            browsers = None
        idx = B.load_index(store, now, browsers=browsers)
        lo = now - INCREMENTAL_S
        built = O.build(store, now=now, since=lo, browsers=idx.browsers)
        B.bind(built.occasions, idx)
        try:
            rows = store._query("SELECT id, key FROM skills WHERE origin = 'learned' AND deleted_at IS NULL AND state"
                                " NOT IN ('deleted', 'not_mine', 'retired')")
        except Exception:  # noqa: BLE001 - a database before migration 43
            rows = []
        ids = {str(k): int(i) for i, k in rows}
        mapped = 0
        for o in built.occasions:
            cap = idx.by_key.get(o.capability)
            if cap is None:
                continue
            keys: list[tuple[str, str]] = []
            lost = False
            for s in cap.slots:
                if s.role != "key" or s.name not in cap.bind:
                    continue
                v = o.attrs.get(s.name)
                if v in (None, ""):
                    lost = lost or s.required
                    continue
                keys.append((s.name, G.value_digest(store.cipher, s.name, v)))
            if lost:
                continue
            sid = ids.get(G.skill_key(store.cipher, cap.key, tuple(sorted(keys))))
            if sid is not None:
                o.group = SimpleNamespace(skill_id=sid, keys=tuple(sorted(keys)))
                mapped += 1
        _occasion_keys(built.occasions, idx.by_key)
        out["saved"] = O.save(store, built.occasions, since=lo, until=now, now=now)
        out["mapped"] = mapped
        out["occasions"] = len(built.occasions)
        out["dropped_yuki_acting"] = int(built.dropped.get("yuki_acting", 0))
    except Exception as exc:  # noqa: BLE001
        out = {"ok": False, "error": type(exc).__name__}
    out["cpu_ms"] = round((time.thread_time() - c0) * 1000.0, 1)
    out["wall_ms"] = round((time.perf_counter() - w0) * 1000.0, 1)
    _log(log, "skills_learned", mode="incremental", **out)
    return out


def nightly(store: Any, *, now: float | None = None, worker: Any = None, log: LogFn | None = None) -> Result | None:
    """The night pass (the portrait scheduler, after its nightly run, beside the routine miner): never with a game in
    front or a call open (:func:`quiet`); ``None`` when it held back (logged)."""
    now = time.time() if now is None else float(now)
    why = quiet(store, now)
    if why:
        _log(log, "skills_learned", mode="full", skipped=why)
        return None
    return run(store, now=now, name=True, worker=worker, log=log, write_rows=True)


def routine_inputs(store: Any, since: float, until: float, *, skill_ids: Sequence[int] | None = None) -> Any:
    """The routine miner's inputs keyed by learned skill (design §5, critique M1; phase L lWire wires
    ``routines.keyed_onsets`` to it): day-onsets per ``skill:<id>`` (``|<entity key>`` for a capability whose fill slot
    is a ``routine_key``, a playlist) from the user's own timed occasions (never Yuki's or asked ones, never a late
    send's, never a skill whose run commits: sends and joins), the seen ones apart from hold joins (D7). One skill per
    moment by construction: no ``ambiguous`` key."""
    from yuki.memory import routines as R
    from yuki.skills.catalog import capabilities

    caps = {c.key: c for c in capabilities().capabilities}
    wanted = {int(i) for i in skill_ids} if skill_ids is not None else None
    lo = since - R.ONSET_GAP_S
    try:
        rows = store._query("SELECT o.skill_id, o.at, o.kind, o.app, o.entity_key, o.facts, o.capability, s.key"
                            " FROM occasions o JOIN skills s ON s.id = o.skill_id WHERE o.skill_id IS NOT NULL AND"
                            " o.by = 'user' AND o.context_known = 1 AND o.at >= ? AND o.at < ? ORDER BY o.at",
                            (float(lo), float(until)))
    except Exception:  # noqa: BLE001
        rows = []
    every: dict[str, list[float]] = defaultdict(list)
    seen: dict[str, list[float]] = defaultdict(list)
    out = R.Inputs()
    from yuki.skills.engine import run_commit

    for sid, at, kind, app, ekey, facts, cap_key, skey in rows:
        if wanted is not None and int(sid) not in wanted:
            continue
        cap = caps.get(str(cap_key))
        if cap is None or run_commit(cap) != "none":
            # routines feed offers, and an offer never commits: a skill whose run sends or joins (the user's words)
            # makes no routine (design section 5: message_sent leaves the routine kinds)
            out.unmapped[f"{kind}|commit"] += 1
            continue
        key = f"skill:{int(sid)}"
        routine_slot = cap is not None and any(s.routine_key for s in cap.slots)
        if routine_slot:
            if not ekey:
                out.unmapped[f"{kind}|no_entity"] += 1
                continue
            key += f"|{str(ekey)[:16]}"
        if key not in out.keys:
            out.keys[key] = R.KeyInfo(key, str(skey), str(app or ""), str(kind), str(ekey)[:16] if routine_slot else None,
                                      False, str(getattr(cap, "kind", "change")))
        out.moments += 1
        every[key].append(float(at))
        try:
            hold = bool(json.loads(facts or "{}").get("hold"))
        except (TypeError, ValueError):
            hold = False
        if not hold:
            seen[key].append(float(at))
    out.onsets = {k: [t for t in R._collapse(v) if since <= t < until] for k, v in every.items()}
    out.seen = {k: [t for t in R._collapse(v) if since <= t < until] for k, v in seen.items()}
    out.presence = R.Presence(store.presence_spans(since, until))
    return out


__all__ = ["INCREMENTAL_S", "BROWSERS_STATE", "Result", "quiet", "run", "rebuild", "incremental", "nightly",
           "routine_inputs"]
