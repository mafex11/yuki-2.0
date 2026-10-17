"""``python -m yuki.skills.learn``: the skill learner's own pipeline command (phase L design §3.4, §9.2).

::

    python -m yuki.skills.learn rebuild [--db PATH] [--dry-run] [--report] [--show] [--no-name]
    python -m yuki.skills.learn backfill-usuals --db PATH [--dry-run] [--force]

* ``rebuild`` recomputes the occasions and the learned skills (the night pass, by hand).
* ``backfill-usuals`` is lName's one-time back-fill of the user's stated usuals (``yuki.skills.learn.backfill``; the
  learner runs it by itself after its first written pass).
* ``--dry-run`` copies the database first (``yuki.eval.dbcopy.copy_db``, source opened read-only), brings its moments
  to the current rule (``yuki.memory.moments.RULE_VERSION``) when they are older, runs the full pass and one
  incremental pass on the copy, prints, and deletes the copy. The source is never written. The naming step makes no
  model call in a dry run (lName's dry run): the report shows what a real night would ask (``name.plan``).
* ``--report`` prints the content-free report: counts, drops, browsers (app keys), groups (capability, key digests,
  counts, means app keys, rung, executable end, commit), groups no capability serves, the routine preview keyed by
  skill id, timings. No name, title, site, person or value.
* ``--show`` prints the learned skills with their names and bound values: on this PC's console only, never logged.
* ``--no-name`` skips the naming call (the skills stay nameless).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _date(t: float | None) -> str | None:
    return datetime.fromtimestamp(float(t)).strftime("%Y-%m-%d") if t else None


def _routines(store: Any, res: Any, now: float) -> dict[str, Any]:
    """A6: the routine miner's statistics on the learned skills' own onsets (``routine_inputs``): passed windows per
    skill id, and for those the first day the window passed (content-free)."""
    from yuki.memory import routines as R
    from yuki.skills.learn import routine_inputs

    ids = [p.row_id for p in res.planned if p.row_id is not None]
    if not ids:
        return {"keys": 0}
    inputs = routine_inputs(store, now - 60 * 86400.0, now, skill_ids=ids)
    mined = R.mine(inputs, now)
    out: dict[str, Any] = {"keys": len(inputs.onsets), "ambiguous": 0, "unmapped": dict(inputs.unmapped),
                           "passed": [], "near": []}
    passed_keys = set()
    for w in mined.passed:
        passed_keys.add(w.key)
        out["passed"].append({"key": w.key.split("|")[0], "dayset": w.dayset, "at": R.hm(R.clock_minute(w.median)),
                              "hits": w.hits, "seen_hits": w.seen_hits, "rate": round(w.rate, 2),
                              "first_hit": w.first_hit.isoformat() if w.first_hit else None})
    for key, w in mined.near.items():
        out["near"].append({"key": key.split("|")[0], "dayset": w.dayset, "at": R.hm(R.clock_minute(w.median)),
                            "hits": w.hits, "failed": list(w.failed or [])})
    first: dict[str, str] = {}
    for back in range(21, -1, -1):
        until = R.day_start(R.day_of(now)) - back * 86400.0 + 86399.0
        got = R.mine(inputs, min(until, now), keys=passed_keys - set(first))
        for w in got.passed:
            first.setdefault(w.key, R.day_of(min(until, now)).isoformat())
    out["first_active"] = {k.split("|")[0]: v for k, v in first.items()}
    return out


def report(store: Any, res: Any, *, now: float, inc: dict[str, Any] | None = None,
           moments: dict[str, Any] | None = None) -> dict[str, Any]:
    """The content-free report (A1, A3, A5, A6, A9)."""
    from yuki.skills.learn.bind import NONE

    built = res.built
    planned = {p.group.gid: p for p in res.planned}
    groups = []
    for g in res.groups:
        if g.capability == NONE:
            continue
        c = res.compiled.get(g.gid)
        p = planned.get(g.gid)
        groups.append({
            "capability": g.capability, "gid": g.gid, "keys": [s for s, _d in g.keys],
            "key_digests": [d[:12] for _s, d in g.keys], "usage": g.usage, "end": g.end,
            "n": g.bar.n, "days": g.bar.days, "asked": g.bar.asked, "asked_days": g.bar.asked_days,
            "first": _date(g.bar.first_at), "last": _date(g.bar.last_at),
            "means": c.means if c else {}, "rung": c.rung if c else None, "link_share": c.link_share if c else None,
            "exec_end": c.exec_end if c else None, "commit": c.commit if c else None,
            "end_commit": c.end_commit if c else None, "probe": c.probe if c else None,
            "covered": c.covered if c else 0,
            "state": p.state if p else ("refused" if g.bar.passes else "seen"),
            "offer_static": p.provenance.get("offer_static") if p else None,
            "late": sum(1 for o in g.occasions if o.by == "user" and not o.context_known),
            "verbs": p.provenance.get("verbs") if p else None, "row": p.row_id if p else None,
        })
    groups.sort(key=lambda d: (-d["n"] - d["asked"], d["capability"]))
    seen = sorted(({"capability": g.capability, "gid": g.gid, "end": g.end, "n": g.bar.n, "days": g.bar.days,
                    "asked": g.bar.asked, "why": g.bar.why, "app": (g.values.get("app") if g.capability == NONE
                                                                     else None)}
                   for g in res.seen), key=lambda d: (-d["n"], d["capability"]))
    c_only = [{"end": g.end, "app": g.values.get("app"), "n": g.bar.n, "days": g.bar.days, "passes": g.bar.passes}
              for g in res.groups if g.capability == NONE]
    pre_commit = [d for d in groups if d["exec_end"] and d["exec_end"] != d["end"]]
    occ = built.occasions if built else []
    catalog_check: dict[str, Any] = {}
    try:
        from yuki.skills.catalog import clear_cache, load

        clear_cache()
        loaded = load(store=store)
        catalog_check = {"capabilities": len(loaded.capabilities), "learned_loaded": len(loaded.learned),
                         "errors": [e.split(":")[0] + ":" + e.rpartition(": ")[2][:30] for e in loaded.errors]}
    except Exception as exc:  # noqa: BLE001
        catalog_check = {"error": type(exc).__name__}
    return {
        "catalog": catalog_check,
        "now": _date(now), "moments_rebuild": moments,
        "occasions": {"total": len(occ), "user": sum(1 for o in occ if o.by == "user"),
                      "asked": sum(1 for o in occ if o.by == "asked"),
                      "by_kind": dict(Counter(o.kind for o in occ)), "cover": dict(Counter(o.cover for o in occ)),
                      "late_sends": int(built.counts.get("late_send", 0)) if built else 0,
                      "asked_corrected": int(built.counts.get("asked_corrected", 0)) if built else 0,
                      "by_capability": dict(Counter(o.capability for o in occ))},
        "dropped": dict(sorted(built.dropped.items())) if built else {},
        "browsers": built.browsers if built else {},
        "groups": groups, "seen": seen, "c_only": c_only,
        "totals": {"named_skills": sum(1 for d in groups if d["state"] in ("candidate", "trusted", "listed")
                                       and not d["usage"]),
                   "usage_records": sum(1 for d in groups if d["state"] in ("candidate", "trusted", "listed")
                                        and d["usage"]),
                   "live": sum(1 for d in groups if d["state"] in ("candidate", "trusted")),
                   "listed": sum(1 for d in groups if d["state"] == "listed"),
                   "pre_commit_skills": len(pre_commit), **{k: int(v) for k, v in res.counts.items()}},
        "routines": _routines(store, res, now) if res.planned else {},
        "timing": {"full_cpu_s": res.cpu_s, "full_wall_s": res.wall_s, "incremental": inc,
                   "naming_called": bool((res.naming or {}).get("called"))},
        "naming": res.naming,
        "ok": res.ok, "error": res.error,
    }


def _text(rep: dict[str, Any]) -> str:
    lines = [f"learner report ({rep['now']}), ok={rep['ok']} {rep['error'] or ''}".rstrip()]
    o = rep["occasions"]
    lines.append(f"occasions: {o['total']} (user {o['user']}, asked {o['asked']}); late sends {o['late_sends']}; "
                 f"asked corrected {o['asked_corrected']}")
    lines.append(f"  by kind {o['by_kind']}; cover {o['cover']}")
    lines.append(f"  by capability {o['by_capability']}")
    lines.append(f"dropped: {rep['dropped']}")
    lines.append(f"browsers (app key: domains on >= 2 days): {rep['browsers']}")
    lines.append("groups that passed (capability | key slots | n/days asked | means | rung link | exec end commit | "
                 "state):")
    for d in rep["groups"]:
        lines.append(f"  {d['capability']:<18} {','.join(d['keys']) or '(usage)':<10} {d['gid']} "
                     f"n={d['n']}/{d['days']}d asked={d['asked']} last={d['last']} means={d['means']} "
                     f"rung={d['rung']} link={d['link_share']} exec={d['exec_end']}/{d['commit']} "
                     f"state={d['state']} offer_static={d['offer_static']} late={d['late']}"
                     + (f" verbs={d['verbs']}" if d['verbs'] else ""))
    lines.append("groups no capability serves (route C only, never written):")
    for d in rep["c_only"]:
        lines.append(f"  {d['end']:<14} app={d['app']} n={d['n']}/{d['days']}d passes={d['passes']}")
    lines.append(f"seen only (below the bar): {len(rep['seen'])}")
    for d in rep["seen"][:30]:
        lines.append(f"  {d['capability']:<18} {d['gid']} {d['end']} n={d['n']}/{d['days']}d asked={d['asked']} "
                     f"why={d['why']}" + (f" app={d['app']}" if d["app"] else ""))
    lines.append(f"totals: {rep['totals']}")
    lines.append(f"catalog (store-aware load): {rep['catalog']}")
    lines.append(f"routines (A6): {json.dumps(rep['routines'], default=str)}")
    lines.append(f"timing (A9): {json.dumps(rep['timing'], default=str)}")
    lines.append(f"naming (lName): {json.dumps(rep.get('naming') or {}, default=str)}")
    if rep.get("moments_rebuild"):
        lines.append(f"moments rebuild (RULE_VERSION): {json.dumps(rep['moments_rebuild'], default=str)}")
    return "\n".join(lines)


def _show(store: Any) -> str:
    """The learned skills with names and bound values (this PC's console only)."""
    from yuki.skills.catalog import clear_cache, load

    clear_cache()
    loaded = load(store=store)
    lines = []
    for s in loaded.learned:
        lines.append(f"{s.key}: {s.name or '(nameless)'} | bound {dict(s.bound)} | menu: {s.menu}")
    rows = store._query("SELECT key, state FROM skills WHERE origin = 'learned' ORDER BY id")
    lines.append("rows: " + ", ".join(f"{k}={st}" for k, st in rows))
    if loaded.errors:
        lines.append(f"catalog errors: {list(loaded.errors)}")
    return "\n".join(lines)


def _moments_current(store: Any) -> dict[str, Any] | None:
    """Bring a copy's derived moments to the current rule (as the memory service does on its next start)."""
    from yuki.memory.moments import RULE_VERSION, Extractor, default_page_entity

    if store.moment_cursor("rule") == str(RULE_VERSION):
        return None
    t0 = time.perf_counter()
    counts = Extractor(store, page_entity=default_page_entity()).rebuild()
    return {"rule": RULE_VERSION, "s": round(time.perf_counter() - t0, 2),
            "counts": {k: v for k, v in counts.items() if v}}


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "backfill-usuals":
        from yuki.skills.learn import backfill   # lName's module: its own arguments (--db, --dry-run, --force)

        return int(backfill.main(argv[1:]) or 0)
    parser = argparse.ArgumentParser(prog="python -m yuki.skills.learn", description=__doc__.split("\n")[0])
    parser.add_argument("cmd", nargs="?", default="rebuild", choices=["rebuild", "backfill-usuals"])
    parser.add_argument("--db", type=Path, default=None, help="database (default: Yuki's own)")
    parser.add_argument("--dry-run", action="store_true", help="run on a temporary copy and print what it gives")
    parser.add_argument("--report", action="store_true", help="print the content-free report")
    parser.add_argument("--json", action="store_true", help="the report as JSON")
    parser.add_argument("--show", action="store_true", help="print the learned skills (names: console only)")
    parser.add_argument("--no-name", action="store_true", help="skip the naming call")
    args = parser.parse_args(argv)
    from yuki.skills import learn

    copy_path = None
    if args.dry_run:
        from yuki.eval.dbcopy import copy_db, open_copy

        copy_path = copy_db(args.db)
        store = open_copy(copy_path)
    else:
        from yuki.memory.store import Store, default_db_path

        store = Store.open(Path(args.db) if args.db else default_db_path())
    try:
        moments = _moments_current(store) if args.dry_run else None
        now = time.time()
        res = learn.run(store, now=now, name=not args.no_name, write_rows=True, name_dry_run=args.dry_run)
        inc = learn.incremental(store, now=time.time()) if args.dry_run else None
        if args.report or args.json:
            rep = report(store, res, now=now, inc=inc, moments=moments)
            print(json.dumps(rep, indent=1, default=str) if args.json else _text(rep))
        else:
            print(json.dumps({"ok": res.ok, "error": res.error, "cpu_s": res.cpu_s,
                              "counts": {k: int(v) for k, v in res.counts.items()}}, default=str))
        if args.show:
            print(_show(store))
    finally:
        store.close()
        if copy_path is not None:
            from yuki.eval.dbcopy import remove_copy

            remove_copy(copy_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
