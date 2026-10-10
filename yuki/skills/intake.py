"""Skill intake: seed the registry and turn old procedures into route evidence (PLAN.md B.2; map-skills S1.6).

Pipeline code, never a hand edit of the database (the user's rule: fix the pipeline, not the memory)::

    python -m yuki.skills.intake --db <path> --dry-run     # what it would do: a content-free report (default)
    python -m yuki.skills.intake --db <path> --apply       # do it (after Claude reviewed the dry run)

It does two things:

1. **Seed:** one ``skills`` row per loaded connection skill and one ``skill_routes`` row per rung
   (:func:`yuki.skills.registry.seed`; a skill no file has any more is retired, never deleted).
2. **Old procedures as route evidence:** a live executable procedure (``playbooks`` kind ``procedure`` with a ``run``)
   maps onto a rung when its app is the skill's app (``app_key`` identity) and its last step acts on the control the
   rung names: the same role and the same name (string identity, never meaning). Its verified runs (``n_ran_ok``)
   become the rung's ``seed_ok`` (rung B needs one verified run before it may run), and the procedure gets a
   ``superseded`` event with ``refs = skill:<key>`` through ``practice.add_events``, so the old learners stop offering
   it. Nothing else of the procedure is copied (its names stay where they are).

The report holds counts, skill keys, route keys and procedure ids only. ``--prefs`` (one model batch proposing the
user's stated usuals) is the voice builder's (sVoice) and is not run here.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from yuki.skills.connection import Loaded, Route, Skill, load


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


def procedure_target(run: list[dict[str, Any]]) -> tuple[str, str] | None:
    """The control a procedure's last acting step names, as ``(role, name)`` (its executable ``run``)."""
    last = next((s for s in reversed(run or []) if isinstance(s, dict) and s.get("do") in ("click", "pick", "slider")
                 and s.get("name")), None)
    if last is None:
        return None
    return _norm(last.get("role")), _norm(last.get("name"))


def matches(store: Any, loaded: Loaded) -> list[dict[str, Any]]:
    """Every (procedure, skill, rung) pairing by the rule above: ``{"procedure", "skill", "route", "ok"}``."""
    from yuki.memory import practice

    try:
        procs = practice.live(store, ["procedure"])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict[str, Any]] = []
    for pb in procs:
        try:
            run = pb.run
        except Exception:  # noqa: BLE001
            continue
        target = procedure_target(run)
        if target is None:
            continue
        apps = {_app(a) for a in pb.apps or [] if a}
        for skill in loaded.skills:
            if _app(skill.app) not in apps and not any(_app(skill.app) == _app(s.get("app")) for s in run
                                                        if isinstance(s, dict)):
                continue
            for route in skill.routes:
                rt = route_target(route)
                if rt is None:
                    continue
                role_ok = not rt[0] or not target[0] or rt[0] == target[0]
                if role_ok and rt[1] == target[1]:
                    out.append({"procedure": int(pb.id), "skill": skill.key, "route": route.key,
                                "ok": int(getattr(pb, "n_ran_ok", 0) or 0)})
    return out


def run_intake(store: Any, *, apply: bool = False, loaded: Loaded | None = None,
               now: float | None = None) -> dict[str, Any]:
    """Seed and map (see the module docstring). ``apply=False`` writes nothing. Returns the content-free report."""
    from yuki.skills import registry

    now = time.time() if now is None else float(now)
    loaded = loaded if loaded is not None else load()
    report: dict[str, Any] = {"at": now, "applied": bool(apply), "skills": list(loaded.keys),
                              "errors": len(loaded.errors), "tables": registry.has_tables(store)}
    if not report["tables"]:
        report["result"] = "no_tables"
        return report
    if apply:
        report["seed"] = registry.seed(store, loaded, now=now)
    found = matches(store, loaded)
    report["matches"] = [{k: m[k] for k in ("procedure", "skill", "route", "ok")} for m in found]
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
    loaded = load(args.connections) if args.connections else load()
    if not args.apply:
        # a dry run writes nothing anywhere: it reads a temporary copy (yuki.eval.dbcopy; source opened ?mode=ro), so
        # even the migrations Store.open applies touch only the copy, which is deleted afterwards (review L6)
        from pathlib import Path

        from yuki.eval.dbcopy import working_copy

        with working_copy(Path(args.db)) as (store, _path):
            report = run_intake(store, apply=False, loaded=loaded)
        report["read"] = "copy"
    else:
        from yuki.memory.store import Store

        store = Store.open(args.db)
        try:
            report = run_intake(store, apply=True, loaded=loaded)
        finally:
            store.close()
    report["warnings"] = len(loaded.warnings)
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = ["route_target", "procedure_target", "matches", "run_intake", "main"]
