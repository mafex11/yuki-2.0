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
  new skill, no model call. < 0.2 s. On a database no full pass was ever written to (the first start after the
  install), that slot runs the first full pass and lName's one-time back-fill of stated usuals (design §9.3).
* :func:`nightly` - the portrait scheduler's night pass (``yuki.memory.portrait.PortraitScheduler._night``), never
  with a game in front or a call open (:func:`quiet`); then the back-fill if it has not run yet.
* :func:`routine_inputs` - the routine miner's onsets keyed by learned skill id (phase L lWire's ``keyed_onsets``).

Logs are content-free (``skills_learned``: counts, CPU ms, code words). ``occasions`` and the learned rows stay out of
the MCP.
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

#: The incremental pass recomputes this much of the recent past (s): a visit's anchor looks back at most 10 minutes.
INCREMENTAL_S = 3600.0
#: Where the full pass leaves the browsers it measured for the incremental passes (content-free app keys and counts).
#: Its absence also means no full pass was ever written: the first incremental slot runs the first full pass instead
#: (design §9.3: at first start the learner rebuilds over the days on disk), at most every :data:`FIRST_RETRY_S`.
BROWSERS_STATE = "learn:browsers"
BROWSERS_FRESH_S = 26 * 3600.0
FIRST_RETRY_S = 3600.0
DAY_S = 86400.0

#: One full pass at a time (the night's and a first-start pass on the moments thread).
_RUN_LOCK = threading.Lock()
_first_tried_at = 0.0

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
    #: The naming step's content-free record (lName's ``NamingRun`` fields, plus ``plan`` in a dry run).
    naming: dict[str, Any] = field(default_factory=dict)


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
    front, or a call open (``portrait.scheduled_busy`` with no live probe: the calls table; design §9.1: never in a
    game or call). Fails closed."""
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
                browsers: dict[str, int], now: float) -> list[tuple[str, str]]:
    """Design §4.2's merge: occasions no capability serves whose executable end is a page of a domain (sends in a
    browser chat on a site: the commit is the user's words, so the end Yuki may reach is that site's page) join the
    passing site group whose bound site is on the same registrable domain (same capability end, same bound value).
    A ``none`` group is split by each occasion's own registrable domain first (review r1 L5): each part joins only its
    own domain's site group; what joins none stays in the ``none`` group (report only)."""
    from yuki.memory.moments import registrable_domain
    from yuki.skills.learn import group as G
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
            parts: dict[str, list[Any]] = {}
            for o in g.occasions:
                if o.src.get("host") and o.app in browsers:
                    parts.setdefault(registrable_domain(o.src.get("host")), []).append(o)
            moved: set[int] = set()
            for domain, its in sorted(parts.items(), key=lambda kv: -len(kv[1])):
                target = targets.get(("page", domain))
                if target is None or not domain:
                    continue
                target.occasions.extend(its)
                if g.gid not in target.merged_from:
                    target.merged_from.append(g.gid)
                for o in its:
                    o.group = target
                    moved.add(id(o))
                merges.append((g.gid, target.gid))
            if not moved:
                continue
            g.occasions = [o for o in g.occasions if id(o) not in moved]
            if not g.occasions:
                pool.remove(g)
            else:
                g.bar = G.bar(g.occasions, now)
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


def _settings_of(worker: Any) -> Any:
    """The settings the naming and back-fill calls use: the portrait worker's, else ``None`` (they load their own)."""
    return getattr(worker, "settings", None) if worker is not None else None


def run(store: Any, *, now: float | None = None, name: bool = True, worker: Any = None, log: LogFn | None = None,
        write_rows: bool = True, name_dry_run: bool = False) -> Result:
    """The full pass (see the module docstring). ``name``: run the naming step (lName); ``name_dry_run``: that step
    makes no call and writes no cache (the CLI's ``--dry-run``: its plan goes to the report). One pass at a time (a
    first-start pass and the night's never overlap). Never raises: ``Result.ok`` False and ``Result.error``."""
    with _RUN_LOCK:
        return _run(store, now=now, name=name, worker=worker, log=log, write_rows=write_rows,
                    name_dry_run=name_dry_run)


def _run(store: Any, *, now: float | None, name: bool, worker: Any, log: LogFn | None, write_rows: bool,
         name_dry_run: bool) -> Result:
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
        built = O.build(store, now=now, browsers=idx.browsers, index=idx)
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
        res.merges = _merge_none(groups, seen, res.compiled, caps, idx.browsers, now)
        for g in groups:
            if g.merged_from:
                g.bar = G.bar(g.occasions, now)
        res.groups, res.seen = groups, seen
        _occasion_keys(built.occasions, caps)
        planned, counts = W.plan(store, groups, caps, res.compiled, now)
        res.planned = planned
        res.counts.update(counts)
        if name:
            n0 = time.thread_time()
            res.naming = W.name_planned(store, planned, settings=_settings_of(worker), log=log,
                                        dry_run=name_dry_run or not write_rows)
            res.naming["cpu_ms"] = round((time.thread_time() - n0) * 1000.0, 1)
            res.named = int(res.naming.get("named") or 0)
        else:
            for p in planned:
                p.provenance["offer_static"] = W.offer_static(p.cap, p.compiled, p.provenance.get("everyday"),
                                                              usage=p.group.usage)
        if write_rows:
            cap_ids = W.capability_ids(store)
            # each key's latest occasion this pass, passing or not: a row used or seen lately stays (review r2 L6)
            last_seen = {G.skill_key(store.cipher, g.capability, g.keys): g.bar.last_at for g in (*groups, *seen)
                         if g.capability in caps and g.bar.last_at}
            res.counts.update(W.write(store, planned, now=now, cap_ids=cap_ids, last_seen=last_seen))
            res.counts["occasions_saved"] = O.save(store, built.occasions, since=built.since, until=now, now=now,
                                                   prune=True)
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
         naming_called=bool(res.naming.get("called")) if res.naming else None,
         naming_error=res.naming.get("error") if res.naming else None,
         **{k: int(v) for k, v in res.counts.items() if isinstance(v, int)})
    return res


def rebuild(store: Any, *, now: float | None = None, log: LogFn | None = None, worker: Any = None,
            name: bool = True) -> Result:
    """``python -m yuki.skills.learn rebuild``: the full pass, written."""
    return run(store, now=now, name=name, worker=worker, log=log, write_rows=True)


def _backfill(store: Any, res: Result | None, *, worker: Any = None, log: LogFn | None = None) -> None:
    """lName's one-time back-fill of the user's stated usuals (``yuki.skills.learn.backfill.run_once``; design §5,
    §9.3: at first start, after the first rebuild), after a written full pass that went through. Every later call is
    one ``nudge_state`` read. Its call and summary log to its own memory file; here only a content-free line."""
    if res is None or not res.ok:
        return
    try:
        from yuki.skills.learn import backfill
    except ImportError:
        return
    try:
        if not backfill.due(store):
            return
        got = backfill.run_once(store, settings=_settings_of(worker))
        _log(log, "skills_learned", mode="backfill_usuals", why=got.why, proposed=got.proposed, carded=got.carded)
    except Exception as exc:  # noqa: BLE001 - it tries again after the next full pass
        _log(log, "error", where="learn.backfill", error=type(exc).__name__)


def _first_pass_due(store: Any, now: float) -> bool:
    """No full pass was ever written on this database (:data:`BROWSERS_STATE` is absent), and none was tried in the
    last :data:`FIRST_RETRY_S` by this process."""
    if now - _first_tried_at < FIRST_RETRY_S:
        return False
    try:
        return not store.nudge_state(BROWSERS_STATE)
    except Exception:  # noqa: BLE001 - unknown: the incremental pass, as usual
        return False


def incremental(store: Any, *, now: float | None = None, log: LogFn | None = None) -> dict[str, Any]:
    """The last :data:`INCREMENTAL_S` of occasions rebuilt and mapped onto the learned skills already written (no new
    skill, no model call; the night pass makes those). An occasion whose key matches no written skill keeps the skill
    the last full pass gave its end moment (or its asked run): the hold joins, asked-occasion mappings and the
    ``none`` merges keep their skill until the next night (review r1 L8). On a database no full pass was ever written
    to (the first start after the install), this slot runs the first full pass instead, then the back-fill (design
    §9.3); the moments hub calls it only when the user is idle, with no game in front and no call. Content-free
    counts; never raises."""
    global _first_tried_at
    now = time.time() if now is None else float(now)
    if _first_pass_due(store, now):
        _first_tried_at = now
        res = run(store, now=now, name=True, log=log, write_rows=True)
        _backfill(store, res, log=log)
        return {"ok": res.ok, "mode": "first", "error": res.error or None, "cpu_ms": round(res.cpu_s * 1000.0, 1)}
    if not _RUN_LOCK.acquire(blocking=False):
        out = {"ok": True, "skipped": "full pass running"}
        _log(log, "skills_learned", mode="incremental", **out)
        return out
    try:
        return _incremental(store, now=now, log=log)
    finally:
        _RUN_LOCK.release()


def _incremental(store: Any, *, now: float, log: LogFn | None) -> dict[str, Any]:
    from yuki.skills.learn import bind as B
    from yuki.skills.learn import group as G
    from yuki.skills.learn import occasions as O

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
        built = O.build(store, now=now, since=lo, browsers=idx.browsers, index=idx)
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
        # the full pass's joins (a hold with no service, an asked run, a merged send) carried over by end moment / run
        by_moment: dict[int, tuple[int, Any]] = {}
        by_run: dict[str, tuple[int, Any]] = {}
        try:
            prev = store._query("SELECT end_moment_id, asked_run, skill_id, key_value_digest FROM occasions WHERE"
                                " skill_id IS NOT NULL AND at >= ? AND at <= ?", (lo, now + 1.0))
        except Exception:  # noqa: BLE001
            prev = []
        live = set(ids.values())
        for mid, run_key, sid, kvd in prev:
            if int(sid) not in live:
                continue
            if mid is not None:
                by_moment[int(mid)] = (int(sid), kvd)
            elif run_key:
                by_run[str(run_key)] = (int(sid), kvd)
        carried = 0
        for o in built.occasions:
            if o.group is not None:
                continue
            got = by_moment.get(o.end_moment_id) if o.end_moment_id is not None else by_run.get(o.asked_run or "")
            if got is not None:
                o.group = SimpleNamespace(skill_id=got[0], keys=())
                o.key_value_digest = got[1]
                carried += 1
        out["saved"] = O.save(store, built.occasions, since=lo, until=now, now=now)
        out["mapped"] = mapped
        out["carried"] = carried
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
    front or a call open (:func:`quiet`); ``None`` when it held back (logged). After a pass that went through, the
    one-time back-fill of stated usuals runs if it has not yet (:func:`_backfill`)."""
    now = time.time() if now is None else float(now)
    why = quiet(store, now)
    if why:
        _log(log, "skills_learned", mode="full", skipped=why)
        return None
    res = run(store, now=now, name=True, worker=worker, log=log, write_rows=True)
    _backfill(store, res, worker=worker, log=log)
    return res


def routine_inputs(store: Any, since: float, until: float, *, skill_ids: Sequence[int] | None = None) -> Any:
    """The routine miner's inputs keyed by learned skill (design §5, critique M1; phase L lWire wires
    ``routines.keyed_onsets`` to it): day-onsets per ``skill:<id>`` (``|<entity key>`` for a capability whose fill slot
    is a ``routine_key``, a playlist) from the user's own timed occasions (never Yuki's or asked ones, never a late
    send's, never a skill whose run commits: sends and joins), the seen ones apart from hold joins (D7). One skill per
    moment by construction: no ``ambiguous`` key. A usage record (no key value bound) gives onsets only through a
    ``routine_key`` fill slot (design §4.2)."""
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
        routine_slot = any(s.role == "fill" and s.routine_key for s in cap.slots)
        usage = not any(s.role == "key" and s.name in cap.bind for s in cap.slots)
        if usage and not routine_slot:
            # design section 4.2 (review r1 L1): a usage record (nothing bound: the volume record) makes a routine only
            # through a routine_key fill slot (a playlist), never as itself
            out.unmapped[f"{kind}|usage"] += 1
            continue
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


__all__ = ["INCREMENTAL_S", "BROWSERS_STATE", "FIRST_RETRY_S", "Result", "quiet", "run", "rebuild", "incremental",
           "nightly", "routine_inputs"]
