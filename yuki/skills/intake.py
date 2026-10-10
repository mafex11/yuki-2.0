"""Skill intake: seed the capabilities and turn old procedures into route evidence (PLAN.md B.2; map-skills S1.6;
phase L design §4.9 "Old procedures", §7).

Pipeline code, never a hand edit of the database (the user's rule: fix the pipeline, not the memory)::

    python -m yuki.skills.intake --db <path> --dry-run     # what it would do: a content-free report (default)
    python -m yuki.skills.intake --db <path> --apply       # do it (after Claude reviewed the dry run)

It does two things:

1. **Seed:** one ``skills`` row per capability of the shipped files and one ``skill_routes`` row per rung
   (:func:`yuki.skills.registry.seed`; a capability no file has any more is retired, never deleted; learned skills
   are the learner's and are never seeded).
2. **Old procedures onto rungs:** a live executable procedure (``playbooks`` kind ``procedure`` with a ``run``) maps
   onto a rung of a learned skill or of a capability (:func:`yuki.skills.catalog.load`: the learned skills first,
   being the more specific) by role-and-name identity: an app the rung itself acts on is one of the procedure's
   (``app_key`` identity: the rung's own ``app`` / ``window`` / ``process`` argument, a recorded path's step apps, a
   learned skill's bound value of an ``app`` slot), and the rung's last control is the procedure's last one, the same
   role and the same name (string identity, never meaning). A procedure maps onto the most specific tier that
   matches it (a learned skill's rungs before any capability's). Its verified runs (``n_ran_ok``) become the rung's
   ``seed_ok`` (rung B needs one verified run before it may run), and the procedure gets a ``superseded`` event with
   ``refs = skill:<key>`` through ``practice.add_events``, so the old learners stop offering it. Nothing else of the
   procedure is copied (its names stay where they are).

The report holds counts, catalog keys, route keys and procedure ids only. ``--prefs`` (one model batch proposing the
user's stated usuals) is the learner's install pass (``learn backfill-usuals``) and is not run here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from yuki.skills.catalog import load
from yuki.skills.connection import Loaded, Route, Skill


def _app(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


def _norm(text: Any) -> str:
    return " ".join(str(text or "").split()).casefold()


def route_target(route: Route) -> tuple[str, str] | None:
    """The control a rung acts on last, as ``(role, name)``: a ``uia_steps`` route's last click step, or a
    ``uia_invoke`` / ``slider`` route's own role and name. ``None`` when it names none (or names a slot)."""
    a = route.args
    if route.via == "uia_steps":
        steps = [s for s in a.get("steps") or [] if isinstance(s, dict)]
        last = next((s for s in reversed(steps) if s.get("do") in ("click", "pick")), None)
        if last is None:
            return None
        role, name = str(last.get("role") or ""), str(last.get("name") or "")
    elif route.via in ("uia_invoke", "slider"):
        role, name = str(a.get("role") or ""), str(a.get("name") or "")
    else:
        return None
    if not name or "{" in name:
        return None
    return _norm(role), _norm(name)


def route_apps(skill: Skill, route: Route) -> set[str]:
    """The apps (app keys) a rung itself acts on, from data only: its own ``app`` / ``window`` / ``process``
    argument (a literal, or a template a learned skill's bound value filled), a recorded path's step apps, and a
    learned skill's bound value of an ``app`` slot. Never the connection's own key alone (a capability's ``web`` or
    ``browser`` is no app)."""
    out: set[str] = set()
    a = route.args
    for k in ("app", "window", "process"):
        v = a.get(k)
        if isinstance(v, str) and v.strip() and "{" not in v:
            out.add(_app(v))
    for v in a.get("apps") or []:
        if isinstance(v, str) and v.strip() and "{" not in v:
            out.add(_app(v))
    for step in a.get("steps") or []:
        if isinstance(step, dict) and isinstance(step.get("app"), str) and "{" not in step["app"]:
            out.add(_app(step["app"]))
    for name, value in (getattr(skill, "bound", None) or {}).items():
        slot = skill.slot(name)
        if slot is not None and slot.type == "app" and isinstance(value, str):
            out.add(_app(value))
    out.discard("")
    return out


def matches(store: Any, loaded: Loaded) -> list[dict[str, Any]]:
    """Every (procedure, catalog entry, rung) pairing by the rule above, each procedure on its most specific tier
    only: ``{"procedure", "skill", "route", "ok", "tier"}`` (``tier``: ``learned`` or ``capability``)."""
    from yuki.memory import practice

    try:
        procs = practice.live(store, ["procedure"])
    except Exception:  # noqa: BLE001
        return []
    tiers = (("learned", list(loaded.learned)), ("capability", list(loaded.capabilities)))
    out: list[dict[str, Any]] = []
    for pb in procs:
        try:
            run = pb.run
        except Exception:  # noqa: BLE001
            continue
        target = procedure_target(run)
        if target is None:
            continue
        apps = {_app(a) for a in pb.apps or [] if a} | {_app(s.get("app")) for s in run
                                                        if isinstance(s, dict) and s.get("app")}
        apps.discard("")
        for tier, skills in tiers:
            found = []
            for skill in skills:
                for route in skill.routes:
                    rt = route_target(route)
                    if rt is None or not (route_apps(skill, route) & apps):
                        continue
                    role_ok = not rt[0] or not target[0] or rt[0] == target[0]
                    if role_ok and rt[1] == target[1]:
                        found.append({"procedure": int(pb.id), "skill": skill.key, "route": route.key,
                                      "ok": int(getattr(pb, "n_ran_ok", 0) or 0), "tier": tier})
            if found:
                out.extend(found)
                break
    return out


def procedure_target(run: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The control a procedure's last acting step names, as ``(role, name)`` (its executable ``run``)."""
    last = next((s for s in reversed(run or []) if isinstance(s, dict) and s.get("do") in ("click", "pick", "slider")
                 and s.get("name")), None)
    if last is None:
        return None
    return _norm(last.get("role")), _norm(last.get("name"))


def run_intake(store: Any, *, apply: bool = False, loaded: Loaded | None = None,
               now: float | None = None) -> dict[str, Any]:
    """Seed and map (see the module docstring). ``apply=False`` writes nothing. Returns the content-free report."""
    from yuki.skills import registry

    now = time.time() if now is None else float(now)
    loaded = loaded if loaded is not None else load(store=store)
    report: dict[str, Any] = {"at": now, "applied": bool(apply), "capabilities": [s.key for s in loaded.capabilities],
                              "learned": len(loaded.learned), "errors": len(loaded.errors),
                              "tables": registry.has_tables(store)}
    if not report["tables"]:
        report["result"] = "no_tables"
        return report
    if apply:
        report["seed"] = registry.seed(store, loaded, now=now)
    found = matches(store, loaded)
    report["matches"] = [{k: m[k] for k in ("procedure", "skill", "route", "ok", "tier")} for m in found]
    if apply and found:
        from yuki.memory import practice

        by_route: dict[tuple[str, str], tuple[int, int]] = {}
        for m in found:
            key = (m["skill"], m["route"])
            ok, pid = by_route.get(key, (0, m["procedure"]))
            by_route[key] = (max(ok, m["ok"]), pid)
        imported = 0
        for (skill_key, route_key), (ok, pid) in by_route.items():
            skill: Skill | None = loaded.get(skill_key)
            route = next((r for r in (skill.routes if skill else ()) if r.key == route_key), None)
            if route is not None and registry.import_evidence(store, skill_key, route, ok=ok,
                                                              source=f"procedure:{pid}", now=now):
                imported += 1
        events = [{"playbook_id": m["procedure"], "type": "superseded", "run_key": f"skill:{m['skill']}",
                   "refs": [f"skill:{m['skill']}"], "at": now} for m in found]
        try:
            report["superseded"] = practice.add_events(store, events, now=now)
        except Exception as exc:  # noqa: BLE001
            report["superseded_error"] = type(exc).__name__
        report["imported"] = imported
    report["result"] = "applied" if apply else "dry_run"
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.skills.intake", description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True, help="the memory database (a dry run reads a temporary copy of it)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="report only (the default)")
    mode.add_argument("--apply", action="store_true", help="seed and import (after the dry run was reviewed)")
    parser.add_argument("--connections", action="append", help="a folder of connection files (default: bundled)")
    args = parser.parse_args(argv)
    if not args.apply:
        # a dry run writes nothing anywhere: it reads a temporary copy (yuki.eval.dbcopy; source opened ?mode=ro), so
        # even the migrations Store.open applies touch only the copy, which is deleted afterwards (review L6)
        from pathlib import Path

        from yuki.eval.dbcopy import working_copy

        with working_copy(Path(args.db)) as (store, _path):
            loaded = load(args.connections, store=store)
            report = run_intake(store, apply=False, loaded=loaded)
        report["read"] = "copy"
    else:
        from yuki.memory.store import Store

        store = Store.open(args.db)
        try:
            loaded = load(args.connections, store=store)
            report = run_intake(store, apply=True, loaded=loaded)
        finally:
            store.close()
    report["warnings"] = len(loaded.warnings)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["route_target", "route_apps", "procedure_target", "matches", "run_intake", "main"]
