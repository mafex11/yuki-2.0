"""The skill registry: skills as data in the database, the trust ledger and the counters code computes from it.

PLAN.md Phase S, decisions D2, D3, D14, D18, D19; map-skills S1.3; phase L design §4.9. The connection files are the
capabilities (:mod:`yuki.skills.connection`); the tables of migration 41 hold what is the user's:

* ``skills``: one row per capability (``origin = 'capability'``, seeded from the files by :func:`seed`) and, from the
  learner (phase L), one per learned skill (``origin = 'learned'``, never seeded here): told values encrypted, the
  state code computes, uses;
* ``skill_routes``: one row per rung, with the connection hash it was seeded from and the verified runs intake
  imported from old procedures (``seed_ok``);
* ``skill_runs``: the trust ledger, content-free. A row is written at ``t0``, before the first step (D18: the moments
  of that app and kind inside ``[t0, t_done + lag]`` are Yuki's), and finished with the done-check's verdict:
  ``checked`` 1 (seen), 0 (contradicted), NULL (couldn't read); ``recheck`` 0 marks a false done (D19).

Trust is computed from the ledger, never stored (:func:`trust`):

* rung A is usable at once; rung B after one verified run (its own or imported evidence); rung C always (it is the
  thinker, gated by the engine);
* 2 contradicted done-checks in a row disable a rung (``Settings.skills_disable_after``); a "couldn't confirm"
  (``checked`` NULL) never counts; a disabled rung is retried after 24 h or as soon as its connection file changes;
* :func:`offer_ready` (D14): "open" skills need 2 verified runs, "change" skills 5 verified runs and no failure in the
  last 5. The routine lane (phase R) calls it and never re-implements it.

Everything here takes a :class:`yuki.memory.store.Store` (``store._query`` / ``store._write``) and never raises into a
request: a failure reads as "no trust yet".
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from yuki.skills.connection import Loaded, Route, Skill

#: Contradicted done-checks in a row that disable a rung (``Settings.skills_disable_after``).
DISABLE_AFTER = 2
#: A disabled rung is tried again after this long (or at once when its connection file changes).
RETRY_AFTER_S = 24 * 3600.0
#: D14.
OPEN_RUNS = 2
CHANGE_RUNS = 5
CHANGE_WINDOW = 5
#: How far back the counters read the ledger (old runs say little about a route today).
TRUST_DAYS = 120.0
#: ``skill_runs.outcome`` words (content-free code words).
OUTCOMES: tuple[str, ...] = ("running", "done", "already", "unverified", "failed", "held_game", "asked",
                             "handed_over", "refused", "cancelled", "other_device", "no_route", "error")
#: Outcomes where the run changed nothing on the PC: its window claims no moment (D18 attribution matches app + kind).
IDLE_OUTCOMES: tuple[str, ...] = ("already", "held_game", "asked", "refused", "cancelled", "other_device", "no_route")


def has_tables(store: Any) -> bool:
    """Whether migration 41 ran on this database."""
    try:
        return bool(store._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name='skill_runs'"))
    except Exception:  # noqa: BLE001
        return False


# ---------------------------------------------------------------------------------------------------------------------
# Seeding the tables from the connection files
# ---------------------------------------------------------------------------------------------------------------------


#: The ``skills.origin`` of a row seeded from a shipped file (phase L: a capability). ``seed`` is the old word (phase
#: S's hand-written skills): such rows are re-seeded as capabilities or retired, never left live. ``learned`` rows are
#: the learner's (phase L) and are never touched here.
CAPABILITY_ORIGIN = "capability"
SEEDED_ORIGINS: tuple[str, ...] = ("seed", CAPABILITY_ORIGIN)


def seed(store: Any, loaded: Loaded, *, now: float | None = None) -> dict[str, int]:
    """One ``skills`` row per capability (``origin = 'capability'``) and one ``skill_routes`` row per rung (upserted;
    a capability no file has any more is retired, never deleted). Learned skills (``origin = 'learned'``, written by
    the learner) are never seeded, retired or moved here, whatever ``loaded`` holds: the files seed capabilities, not
    skills (phase L design §4.9, §9.1). A changed connection file stamps ``hash_changed_at`` on its routes: the
    contradiction streak that disables a rung counts only runs after it (:func:`trust`), so each gets another try
    (``reenabled`` counts them). One transaction: seeded rows no file has any more (phase S's hand-written skills too)
    are retired first, so a renamed key or a new capability for the same (app, end) takes the place without breaking
    the one-live-row-per-(app, end) index. A row kept under the same key keeps its id (and its ledger). Returns counts
    (content-free). Never raises: ``{"error": 1}``."""
    now = time.time() if now is None else float(now)
    counts = {"skills": 0, "routes": 0, "retired": 0, "reenabled": 0}
    caps = [s for s in loaded.skills if getattr(s, "origin", CAPABILITY_ORIGIN) == CAPABILITY_ORIGIN]
    marks = ",".join("?" * len(SEEDED_ORIGINS))

    def write(conn: Any) -> None:
        live = {s.key for s in caps}
        # retire first (review M6): a key no file has any more frees its (app, end) before another takes it
        for sid, key in conn.execute(f"SELECT id, key FROM skills WHERE retired_at IS NULL AND origin IN ({marks})",
                                     SEEDED_ORIGINS).fetchall():
            if key not in live:
                conn.execute("UPDATE skills SET retired_at = ?, state = 'retired', updated_at = ? WHERE id = ?",
                             (now, now, sid))
                counts["retired"] += 1
        # a capability whose (app, end) moved steps aside too, so two moving rows never collide mid-way (the loop
        # below brings each back at its new place; connection.load keeps (app, end) unique among the capabilities)
        wanted = {s.key: (s.app, s.end) for s in caps}
        for sid, key, app, end_kind in conn.execute(
                f"SELECT id, key, app, end_kind FROM skills WHERE retired_at IS NULL AND origin IN ({marks})",
                SEEDED_ORIGINS).fetchall():
            if key in wanted and wanted[key] != (app, end_kind):
                conn.execute("UPDATE skills SET retired_at = ? WHERE id = ?", (now, sid))
        for s in caps:
            row = conn.execute("SELECT id, connection, retired_at, origin FROM skills WHERE key = ?",
                               (s.key,)).fetchone()
            if row is not None and str(row[3] or "") not in SEEDED_ORIGINS:
                continue    # a learned row holds this key: the learner's, never overwritten by a file
            if row is None:
                # a retired row of the same (app, end) keeps its row; this one takes the place
                conn.execute(
                    "INSERT INTO skills (key, app, end_kind, connection, origin, state, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'active', ?, ?)",
                    (s.key, s.app, s.end, s.connection, CAPABILITY_ORIGIN, now, now))
                skill_id = conn.execute("SELECT id FROM skills WHERE key = ?", (s.key,)).fetchone()[0]
            else:
                skill_id = int(row[0])
                conn.execute("UPDATE skills SET app = ?, end_kind = ?, connection = ?, origin = ?, retired_at = NULL, "
                             "state = CASE WHEN state = 'retired' THEN 'active' ELSE state END, updated_at = ? "
                             "WHERE id = ?", (s.app, s.end, s.connection, CAPABILITY_ORIGIN, now, skill_id))
            counts["skills"] += 1
            for r in s.routes:
                got = conn.execute(
                    "SELECT id, connection_hash, disabled_at FROM skill_routes WHERE skill_id = ? AND rung = ? AND "
                    "variant = ? AND via = ?", (skill_id, r.rung, r.variant, r.via)).fetchone()
                if got is None:
                    conn.execute(
                        "INSERT INTO skill_routes (skill_id, rung, variant, via, needs_focus, source, connection_hash, "
                        "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'connection', ?, ?, ?)",
                        (skill_id, r.rung, r.variant, r.via, int(r.needs_focus), s.hash, now, now))
                else:
                    changed = (got[1] or "") != s.hash
                    if changed:
                        counts["reenabled"] += 1
                    # B.2: a changed file retries a disabled rung: its streak restarts from now (review M1)
                    conn.execute(
                        "UPDATE skill_routes SET needs_focus = ?, connection_hash = ?, updated_at = ?, "
                        "hash_changed_at = CASE WHEN ? THEN ? ELSE hash_changed_at END, "
                        "disabled_at = CASE WHEN ? THEN NULL ELSE disabled_at END, "
                        "disabled_why = CASE WHEN ? THEN NULL ELSE disabled_why END WHERE id = ?",
                        (int(r.needs_focus), s.hash, now, int(changed), now, int(changed), int(changed),
                         int(got[0])))
                counts["routes"] += 1

    try:
        store._write(write)
    except Exception:  # noqa: BLE001 - the engine runs from the files alone
        return {"error": 1}
    return counts


def skill_ids(store: Any) -> dict[str, int]:
    """``{skill key: skills.id}`` of the live skills."""
    try:
        return {str(k): int(i) for i, k in store._query("SELECT id, key FROM skills WHERE retired_at IS NULL")}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------------------------------------------------


def new_run_key() -> str:
    return uuid.uuid4().hex[:16]


def start_run(store: Any, *, skill: Skill | str, origin: str, t0: float | None = None, run_key: str | None = None,
              request_id: str | None = None, session_id: str | None = None, app: str | None = None,
              end_kind: str | None = None, filled: Mapping[str, str] | None = None,
              slots_digest: str | None = None) -> str | None:
    """Write the run's row at ``t0``, before its first step (D18). ``app``: the app its first rung acts on (an app
    key; default the connection's app). Returns the run key (``None`` when it could not be written: the run still
    goes on)."""
    key = skill.key if isinstance(skill, Skill) else str(skill)
    app = _app_key(app) if app else (skill.app if isinstance(skill, Skill) else None)
    end_kind = end_kind if end_kind is not None else (skill.end if isinstance(skill, Skill) else None)
    t0 = time.time() if t0 is None else float(t0)
    run_key = run_key or new_run_key()

    def write(conn: Any) -> None:
        row = conn.execute("SELECT id FROM skills WHERE key = ? AND retired_at IS NULL", (key,)).fetchone()
        conn.execute(
            "INSERT INTO skill_runs (at, skill_id, skill, origin, session_id, request_id, run_key, app, end_kind, t0, "
            "slots_digest, filled, outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running')",
            (t0, int(row[0]) if row else None, key, str(origin), session_id, request_id, run_key, app, end_kind, t0,
             slots_digest, json.dumps(dict(filled or {}), sort_keys=True)))

    try:
        store._write(write)
    except Exception:  # noqa: BLE001
        return None
    return run_key


def finish_run(store: Any, run_key: str, *, outcome: str, checked: bool | None, route: Route | None = None,
               t_done: float | None = None, ms: float | None = None, value: float | None = None,
               entity_digest: str | None = None, evidence: Mapping[str, Any] | None = None,
               error: str | None = None, app: str | None = None, acted: bool = False) -> bool:
    """Finish the run's row: the route that ran last, the outcome word, the done-check's verdict (D19: 1 / 0 / NULL),
    when it was done, and the D18 attribution facts (a level set, the entity started, and ``app``: the app the route
    acted on, as an app key, when it is not the connection's own: a mixer level set on Spotify is Spotify's moment).
    ``acted``: a rung of this run acted before it ended (its end kind stays even on an idle outcome, review r2 L6).
    Content-free."""
    if outcome not in OUTCOMES:
        outcome = "error"
    acted_app = _app_key(app)

    def write(conn: Any) -> None:
        route_id = None
        if route is not None:
            got = conn.execute(
                "SELECT r.id FROM skill_routes r JOIN skill_runs u ON u.skill_id = r.skill_id WHERE u.run_key = ? "
                "AND r.rung = ? AND r.variant = ? AND r.via = ?", (run_key, route.rung, route.variant, route.via)
            ).fetchone()
            route_id = int(got[0]) if got else None
        # D18: a run that changed nothing (asked, held, refused, playing elsewhere, already so) claims no moment; one
        # whose earlier rung acted keeps it (that rung's moment is Yuki's, review r2 L6)
        idle = outcome in IDLE_OUTCOMES and not acted
        conn.execute(
            "UPDATE skill_runs SET outcome = ?, checked = ?, route_id = ?, rung = ?, variant = ?, via = ?, t_done = ?, "
            "ms = ?, value = ?, entity_digest = ?, evidence = ?, error = ?, app = coalesce(?, app), "
            + ("end_kind = NULL " if idle else "end_kind = end_kind ") + "WHERE run_key = ?",
            (outcome, None if checked is None else int(bool(checked)), route_id,
             route.rung if route else None, route.variant if route else None, route.via if route else None,
             time.time() if t_done is None else float(t_done), ms, value, entity_digest,
             json.dumps(dict(evidence or {}), sort_keys=True, default=str), error, acted_app, run_key))
        if outcome in ("done", "already"):
            conn.execute("UPDATE skills SET uses = uses + 1, last_used_at = ? WHERE id = (SELECT skill_id FROM "
                         "skill_runs WHERE run_key = ?)", (time.time(), run_key))

    try:
        store._write(write)
        return True
    except Exception:  # noqa: BLE001
        return False


def _app_key(app: Any) -> str | None:
    if not app:
        return None
    from yuki.memory.store import app_key

    return app_key(str(app))


def set_run_app(store: Any, run_key: str, app: str | None) -> bool:
    """D18: the app the next rung acts on, written before it acts (a moment that rung causes is matched on it while the
    run is still going: ``yuki.memory.moments.attribute`` compares the run's app with the moment's)."""
    key = _app_key(app)
    if not key or not run_key:
        return False
    try:
        store._write(lambda conn: conn.execute("UPDATE skill_runs SET app = ? WHERE run_key = ?", (key, run_key)))
        return True
    except Exception:  # noqa: BLE001
        return False


def record_route_try(store: Any, run_key: str, route: Route, checked: bool | None, *, outcome: str,
                     ms: float | None = None) -> bool:
    """A rung that ran (or could not) and was not the last one of its run: its verdict in ``skill_route_tries``, so
    the counters see every rung while ``skill_runs`` keeps one row per run."""
    def write(conn: Any) -> None:
        base = conn.execute("SELECT skill, t0 FROM skill_runs WHERE run_key = ?", (run_key,)).fetchone()
        if base is None:
            return
        conn.execute(
            "INSERT INTO skill_route_tries (run_key, skill, rung, variant, via, at, outcome, checked, ms) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (run_key, base[0], route.rung, route.variant, route.via, time.time(),
             outcome if outcome in OUTCOMES else "error", None if checked is None else int(bool(checked)), ms))

    try:
        store._write(write)
        return True
    except Exception:  # noqa: BLE001
        return False


def set_recheck(store: Any, run_key: str, agrees: bool | None) -> bool:
    """The second probe's verdict on a "done" (D19): 1 agrees, 0 disagrees (a false done), NULL couldn't read."""
    try:
        store._write(lambda conn: conn.execute("UPDATE skill_runs SET recheck = ? WHERE run_key = ?",
                                               (None if agrees is None else int(bool(agrees)), run_key)))
        return True
    except Exception:  # noqa: BLE001
        return False


def mark_false_done(store: Any, run_key: str) -> bool:
    """The user corrected or repeated the skill within 60 s of its "done" (D19's other false-done source)."""
    return set_recheck(store, run_key, False)


#: D19: a correction, or the same skill asked again with the same values, this soon after its "done" is a false done.
FALSE_DONE_S = 60.0


def values_digest(store: Any, skill_key: str, values: Mapping[str, Any] | None) -> str | None:
    """A keyed digest of a run's slot values (``skill_runs.slots_digest``: "the same values as last time", never the
    values in clear). ``None`` with no values given (a relative change has none: the engine passes ``None``) or no
    key to digest with."""
    if values is None:
        return None
    try:
        canon = {str(k): (v.get("key") or v.get("name")) if isinstance(v, Mapping) else v
                 for k, v in values.items() if v is not None}
        blob = json.dumps(canon, sort_keys=True, ensure_ascii=False, default=str)
        return str(store.cipher.digest("skill-slots", str(skill_key), blob))[:32]
    except Exception:  # noqa: BLE001 - no digest: no repeat can be matched (never a false mark)
        return None


def false_done_after(store: Any, skill_key: str, *, now: float | None = None, digest: str | None = None,
                     corrects: bool = False, within_s: float = FALSE_DONE_S,
                     request_id: str | None = None) -> str | None:
    """D19's other false-done source: the newest run of ``skill_key`` that ended "done" within ``within_s`` of
    ``now`` is marked a false done (:func:`mark_false_done`) when the user corrected it (``corrects``) or asked the
    same skill again with the same absolute values (``digest`` equal to its ``slots_digest``) in another request
    (``request_id``: a model calling the skill twice inside one request is not the user repeating it). Returns the run
    key marked, else ``None``. Only the newest finished run counts (an older done was already followed by another
    run)."""
    now = time.time() if now is None else float(now)
    if not corrects and not digest:
        return None
    rows = last_runs(store, skill_key, since=now - within_s - 30.0, limit=3)
    if not rows:
        return None
    row = rows[0]
    if row.get("outcome") != "done" or row.get("checked") != 1 or row.get("recheck") == 0:
        return None
    t_done = row.get("t_done") if row.get("t_done") is not None else row.get("t0")
    if t_done is None or now - float(t_done) > within_s:
        return None
    if not corrects and (row.get("slots_digest") != digest
                         or (request_id is not None and row.get("request_id") == request_id)):
        return None
    key = str(row.get("run_key") or "")
    return key if key and mark_false_done(store, key) else None


# ---------------------------------------------------------------------------------------------------------------------
# Trust: computed from the ledger
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class RouteTrust:
    """What the ledger says of one rung (computed, never stored)."""

    key: str
    ok: int = 0                  # verified runs (checked 1 and no recheck disagreeing)
    failed: int = 0              # contradicted done-checks and false dones
    unverified: int = 0          # "couldn't confirm" (never counts against it)
    streak: int = 0              # contradicted in a row, newest first (unverified runs skipped)
    last5: list[int] = field(default_factory=list)   # 1 ok / 0 failed, newest first (decided runs only)
    seed_ok: int = 0
    last_ok_at: float | None = None
    last_failed_at: float | None = None
    disabled_at: float | None = None
    disabled_why: str | None = None
    connection_hash: str | None = None
    #: When the connection file last changed for this rung: the streak counts only runs after it (B.2).
    hash_changed_at: float | None = None
    ms: list[float] = field(default_factory=list)

    @property
    def verified(self) -> int:
        return self.ok + self.seed_ok

    @property
    def p50_ms(self) -> float | None:
        if not self.ms:
            return None
        xs = sorted(self.ms)
        return xs[len(xs) // 2]


def _route_key(rung: Any, variant: Any, via: Any) -> str:
    return f"{rung or ''}:{variant or ''}:{via or ''}"


def trust(store: Any, skill_key: str, *, now: float | None = None, until: float | None = None,
          disable_after: int = DISABLE_AFTER) -> dict[str, RouteTrust]:
    """``{route key: RouteTrust}`` for one skill, from its ledger rows (``until``: only runs before it, for replays and
    evals "as of that time"). A rung whose newest decided runs are ``disable_after`` contradictions in a row is
    disabled (from the last one) unless it was retried since (24 h) or its connection file changed since: the streak
    counts only runs after ``skill_routes.hash_changed_at`` (review M1)."""
    now = time.time() if now is None else float(now)
    out: dict[str, RouteTrust] = {}
    try:
        routes = store._query(
            "SELECT r.rung, r.variant, r.via, r.seed_ok, r.disabled_at, r.disabled_why, r.connection_hash, "
            "r.hash_changed_at FROM skill_routes r JOIN skills s ON s.id = r.skill_id WHERE s.key = ?", (skill_key,))
        for rung, variant, via, seed_ok, dis_at, dis_why, chash, changed_at in routes:
            k = _route_key(rung, variant, via)
            if until is not None and changed_at is not None and float(changed_at) >= float(until):
                changed_at = None      # as of a time before the change: the file had not changed yet
            out[k] = RouteTrust(key=k, seed_ok=int(seed_ok or 0), disabled_at=dis_at, disabled_why=dis_why,
                                connection_hash=chash,
                                hash_changed_at=float(changed_at) if changed_at is not None else None)
        hi = float(until) if until is not None else now + 1.0
        lo = now - TRUST_DAYS * 86400.0
        rows = store._query(
            "SELECT rung, variant, via, checked, recheck, t, ms FROM ("
            "SELECT rung, variant, via, checked, recheck, coalesce(t_done, t0) AS t, ms, id FROM skill_runs "
            "WHERE skill = ? AND rung IS NOT NULL AND t0 >= ? AND t0 < ? AND outcome NOT IN ('running', 'held_game', "
            "'asked', 'refused', 'cancelled', 'other_device') "
            "UNION ALL SELECT rung, variant, via, checked, NULL, at, ms, id FROM skill_route_tries "
            "WHERE skill = ? AND at >= ? AND at < ?) ORDER BY t DESC, id DESC",
            (skill_key, lo, hi, skill_key, lo, hi))
    except Exception:  # noqa: BLE001 - no trust yet
        return out
    streak_open: dict[str, bool] = {}
    for rung, variant, via, checked, recheck, t0, ms in rows:
        k = _route_key(rung, variant, via)
        t = out.setdefault(k, RouteTrust(key=k))
        if checked is None:
            t.unverified += 1
            continue
        good = int(checked) == 1 and recheck != 0
        if good:
            t.ok += 1
            t.last_ok_at = max(t.last_ok_at or 0.0, float(t0))
            if ms is not None:
                t.ms.append(float(ms))
        else:
            t.failed += 1
            t.last_failed_at = max(t.last_failed_at or 0.0, float(t0))
        if len(t.last5) < CHANGE_WINDOW:
            t.last5.append(1 if good else 0)
        if t.hash_changed_at is not None and float(t0) < t.hash_changed_at:
            streak_open[k] = False      # runs before the file changed never count towards a disable
        if streak_open.get(k, True):
            if good:
                streak_open[k] = False
            else:
                t.streak += 1
    for t in out.values():
        if t.disabled_at is None and t.streak >= max(1, int(disable_after)) and t.last_failed_at:
            t.disabled_at, t.disabled_why = t.last_failed_at, "contradicted"
    return out


def usable(route: Route, t: RouteTrust | None, *, now: float | None = None) -> tuple[bool, str]:
    """Whether a rung may run now: ``(ok, why not)``. Rung A at once; rung B after one verified run; a disabled rung
    after :data:`RETRY_AFTER_S` (one try)."""
    now = time.time() if now is None else float(now)
    t = t or RouteTrust(key=route.key)
    if t.disabled_at is not None and now - float(t.disabled_at) < RETRY_AFTER_S:
        return False, "disabled"
    if route.rung == "B" and t.verified < 1:
        return False, "untrusted"
    return True, ""


def offer_ready(store: Any, skill: Skill | str, route: Route | str | None = None, *,
                kind: str | None = None, now: float | None = None) -> bool:
    """D14, the one place it is decided: a skill (or one of its routes) may be offered on a card when its ledger holds
    2 verified runs ("open" skills) or 5 verified runs with none failed in the last 5 ("change" skills)."""
    key = skill.key if isinstance(skill, Skill) else str(skill)
    kind = kind or (skill.kind if isinstance(skill, Skill) else "change")
    table = trust(store, key, now=now)
    if route is not None:
        rkey = route.key if isinstance(route, Route) else str(route)
        items = [table[rkey]] if rkey in table else []
    else:
        items = list(table.values())
    if not items:
        return False
    total_ok = sum(t.ok for t in items)      # live verified runs only (imported evidence never makes an offer)
    if kind == "open":
        return total_ok >= OPEN_RUNS
    newest = items[0].last5 if route is not None else _merged_last5(store, key)
    return total_ok >= CHANGE_RUNS and len(newest) >= CHANGE_WINDOW and 0 not in newest[:CHANGE_WINDOW]


def _merged_last5(store: Any, key: str) -> list[int]:
    try:
        rows = store._query(
            "SELECT checked, recheck FROM (SELECT checked, recheck, coalesce(t_done, t0) AS t, id FROM skill_runs "
            "WHERE skill = ? AND checked IS NOT NULL AND rung IS NOT NULL UNION ALL SELECT checked, NULL, at, id FROM "
            "skill_route_tries WHERE skill = ? AND checked IS NOT NULL) ORDER BY t DESC, id DESC LIMIT ?",
            (key, key, CHANGE_WINDOW))
    except Exception:  # noqa: BLE001
        return []
    return [1 if int(c) == 1 and r != 0 else 0 for c, r in rows]


def menu_version(store: Any, loaded: Loaded) -> str:
    """What the menu depends on: the loaded files and which rungs are usable now (it changes only when the registry
    does: nightly, or after a rung is disabled or re-enabled)."""
    parts = [loaded.version]
    for s in loaded.skills:
        table = trust(store, s.key)
        parts.append(s.key + ":" + "".join("1" if usable(r, table.get(r.key))[0] else "0" for r in s.routes))
    import hashlib

    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:12]


def last_runs(store: Any, skill_key: str, *, since: float, limit: int = 5) -> list[dict[str, Any]]:
    """The skill's latest finished runs since ``since`` (newest first): outcome, verdict, times, route (content-free;
    the resolver's "again" and the voice's "a bit more")."""
    try:
        rows = store._query(
            "SELECT run_key, origin, outcome, checked, rung, variant, via, t0, t_done, value, slots_digest, recheck, "
            "request_id FROM skill_runs WHERE skill = ? AND t0 >= ? AND outcome != 'running' "
            "ORDER BY t0 DESC LIMIT ?", (skill_key, float(since), int(limit)))
    except Exception:  # noqa: BLE001
        return []
    return [dict(zip(("run_key", "origin", "outcome", "checked", "rung", "variant", "via", "t0", "t_done", "value",
                      "slots_digest", "recheck", "request_id"), tuple(r))) for r in rows]


def import_evidence(store: Any, skill_key: str, route: Route, *, ok: int, source: str,
                    now: float | None = None) -> bool:
    """Intake: verified runs of an old procedure counted as this rung's evidence (``seed_ok``), its source kept
    (``procedure:<id>``). Pipeline code only (``python -m yuki.skills.intake --apply``), never a hand edit."""
    now = time.time() if now is None else float(now)

    def write(conn: Any) -> None:
        conn.execute(
            "UPDATE skill_routes SET seed_ok = max(seed_ok, ?), source = CASE WHEN source = 'connection' THEN ? ELSE "
            "source END, updated_at = ? WHERE skill_id = (SELECT id FROM skills WHERE key = ?) AND rung = ? AND "
            "variant = ? AND via = ?", (int(ok), str(source), now, skill_key, route.rung, route.variant, route.via))

    try:
        store._write(write)
        return True
    except Exception:  # noqa: BLE001
        return False


def table_counts(store: Any) -> dict[str, int]:
    """Rows per skill table (content-free, for doctor and the report)."""
    out: dict[str, int] = {}
    for table in ("skills", "skill_routes", "skill_runs", "skill_route_tries", "prefs", "pref_events", "tastes"):
        try:
            out[table] = int(store._query(f"SELECT count(*) FROM {table}")[0][0])
        except Exception:  # noqa: BLE001
            out[table] = -1
    return out


def runs_between(store: Any, since: float, until: float) -> list[dict[str, Any]]:
    """D18 run windows overlapping ``[since, until]``: request id, app, end kind, t0, t_done, value, entity digest
    (``yuki.memory.moments.skill_runs_between`` reads the same columns)."""
    try:
        rows = store._query(
            "SELECT request_id, app, end_kind, t0, t_done, value, entity_digest FROM skill_runs WHERE t0 <= ? AND "
            "coalesce(t_done, t0) >= ?", (float(until), float(since)))
    except Exception:  # noqa: BLE001
        return []
    return [dict(zip(("request_id", "app", "end_kind", "t0", "t_done", "value", "entity_digest"), tuple(r)))
            for r in rows]


__all__ = ["DISABLE_AFTER", "RETRY_AFTER_S", "OPEN_RUNS", "CHANGE_RUNS", "OUTCOMES", "RouteTrust", "has_tables", "seed",
           "CAPABILITY_ORIGIN", "SEEDED_ORIGINS",
           "skill_ids", "new_run_key", "start_run", "finish_run", "set_run_app", "record_route_try", "set_recheck",
           "mark_false_done", "trust", "usable", "offer_ready", "menu_version", "last_runs", "import_evidence",
           "table_counts", "runs_between"]
