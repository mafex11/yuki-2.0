"""The habits replay harness (PLAN.md Appendix B.1 and C.4; map-routines E1): offline suites on a **copy** of the
memory database (:mod:`yuki.eval.dbcopy`), never the live file, never the desktop.

Suites (:data:`SUITES`) and who fills them in:

* ``moments`` (phase F, $0, here): the moments backfill bars of B.1. Yuki's own pipeline command
  (``python -m yuki.memory.moments rebuild --db <copy>``, built by ``fMoments``) runs twice on the copy; then
  :func:`moments_bars` checks: the morning meeting (E1) joined in Zen on all 7 known weekdays within a minute, with
  the evidence the plan expects (seen or a corroborated mic hold); no join at all for FL Studio, VALORANT or Settings;
  a Discord join on the 15 evenings; YouTube ``page`` moments with an entity; the rebuild under 5 s and idempotent.
  :func:`source_reference` gives the same facts from the raw ``calls`` and ``timeline`` rows (independent of the
  moments code), and :func:`sample_moments` draws the 50 moments the reviewer checks by hand against independent
  sources (the calls row's ``via``, the timeline row's host kind, the memory log's ``call_detected``).
* ``requests`` (phase S): each labelled fixture (your own words, labelled by you) through the skill resolver, one
  cached talker call each, ``repeat`` times (:func:`run_requests`, scored by :func:`score_requests` against
  :data:`SLOT_SPEC`). The resolver is ``yuki.skills.resolve.eval_resolver(db_path=<copy>)`` once ``sCore`` /
  ``sVoice`` ship it (:func:`load_resolver` never builds it without a copy); until then the suite says so.
* ``cost`` (phase P): background model spend per day from the copy's own accounting (:func:`background_cost`).
* ``routines``, ``lane``, ``taste``: phase R and T, built in ``evalReplay`` on :func:`time_split`.

**Caps** (:class:`Caps`), enforced in code for every run: at most :data:`HARD_MINUTES` minutes and
:data:`HARD_BUDGET_USD` dollars (a larger ask is refused before anything starts); the expected cost, the worst case
(every call at its worst) and the expected time are checked up front (refused when over), and before every model call
its worst-case cost and the clock are checked, so no call can take a run past either cap (the c3 pattern,
``c3.py:36-40``). A call that reports no cost, or raises, is charged its worst case, so the cap never depends on the
resolver remembering to report. The moments rebuilds run under the same clock. ``dry_run`` makes no model call.

Everything printed or written by this module is content-free: ids, machine keys (skill keys, app keys, code words),
counts, times and levels. No words, names, titles, hosts, links or entities.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib
import inspect
import io
import json
import math
import random
import sqlite3
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from yuki.eval import dbcopy

# ---------------------------------------------------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------------------------------------------------

#: The user's caps for any eval run (PLAN §5, C.4): at most 55 minutes and $3. Asking for more is refused.
HARD_MINUTES = 55.0
HARD_BUDGET_USD = 3.0


class CapReached(Exception):
    """The next model call could pass the cost cap, or the time limit is reached."""


def reported_cost(value: Any) -> bool:
    """Whether a call's ``cost_usd`` is a cost at all: a finite number (not a bool, not text, not missing)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


class Caps:
    """The run's limits. ``refusal`` is the up-front check; ``before_call`` runs before every model call."""

    def __init__(self, budget_usd: float = HARD_BUDGET_USD, minutes: float = HARD_MINUTES, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        budget_usd, minutes = float(budget_usd), float(minutes)
        if not (0.0 <= budget_usd <= HARD_BUDGET_USD):
            raise ValueError(f"the cost cap is at most ${HARD_BUDGET_USD:.2f} (asked ${budget_usd:.2f})")
        if not (0.0 < minutes <= HARD_MINUTES):
            raise ValueError(f"the time limit is at most {HARD_MINUTES:g} minutes (asked {minutes:g})")
        self.budget_usd = budget_usd
        self.limit_s = minutes * 60.0
        self.clock = clock
        self.started = clock()
        self.spent = 0.0
        self.calls = 0
        #: The most one call has been charged so far: the next call's check never assumes less.
        self.largest = 0.0

    def elapsed_s(self) -> float:
        return self.clock() - self.started

    def time_left_s(self) -> float:
        return self.limit_s - self.elapsed_s()

    def over_time(self) -> bool:
        return self.elapsed_s() > self.limit_s

    def refusal(self, expected_usd: float, expected_s: float, worst_usd: float | None = None) -> str | None:
        """Why the run is refused before anything is sent (``None``: it may start): its expected cost, its worst case
        (every call at its worst) or its expected time is over what is left of the caps."""
        left = self.budget_usd - self.spent
        if expected_usd > left:
            return f"the expected cost (${expected_usd:.2f}) is over the ${left:.2f} cap"
        if worst_usd is not None and worst_usd > left:
            return (f"the most it could cost (${worst_usd:.2f}, every call at its worst case) is over the ${left:.2f}"
                    " cap; use fewer fixtures or --repeat 1")
        if expected_s > self.time_left_s():
            return f"the expected time ({expected_s / 60:.0f} min) is over the {self.limit_s / 60:g} min limit"
        return None

    def before_call(self, worst_usd: float) -> None:
        if self.over_time():
            raise CapReached("the time limit")
        nxt = max(float(worst_usd), self.largest)
        if self.spent + nxt > self.budget_usd:
            raise CapReached(f"the cost cap (${self.spent:.3f} spent; the next call could cost ${nxt:.3f})")

    def add(self, cost_usd: Any, worst_usd: float) -> float:
        """Charge one model call; returns what was charged. The cost the call reported when it is a number (never
        below 0); its worst case when it reported none, or something that is not a finite number, or it raised (a
        billed call that failed still counts)."""
        charge = max(float(cost_usd), 0.0) if reported_cost(cost_usd) else float(worst_usd)
        self.spent += charge
        self.calls += 1
        self.largest = max(self.largest, charge)
        return charge

    def summary(self) -> dict[str, Any]:
        return {"budget_usd": self.budget_usd, "minutes": round(self.limit_s / 60.0, 1),
                "spent_usd": round(self.spent, 4), "calls": self.calls,
                "elapsed_min": round(self.elapsed_s() / 60.0, 2)}


# ---------------------------------------------------------------------------------------------------------------------
# Suites
# ---------------------------------------------------------------------------------------------------------------------

SUITES: dict[str, dict[str, str]] = {
    "moments": {"phase": "F", "cost": "$0", "what": "the moments backfill bars (B.1) on a copy"},
    "requests": {"phase": "S", "cost": "about $0.30", "what": "your labelled phrasings through the skill resolver",
                 "needs": "the skill resolver (sCore/sVoice: yuki.skills.resolve.eval_resolver)"},
    "cost": {"phase": "P", "cost": "$0", "what": "background model spend per day"},
    "routines": {"phase": "R", "cost": "$0", "what": "mining day by day, replay on day D (T1, T2, T6)",
                 "needs": "the routine miner (rMiner); built in evalReplay"},
    "lane": {"phase": "R", "cost": "$0", "what": "presence_replay --routine-only, weekdays 08:30-09:30",
             "needs": "the routine lane (rLane); built in evalReplay"},
    "taste": {"phase": "T", "cost": "$0", "what": "channel-in-top-3 for the tours (report only)",
              "needs": "taste (tTaste)"},
}


def suite_status(name: str) -> dict[str, Any]:
    """Whether a suite can run on this branch (its module is here), with what it needs otherwise."""
    info = dict(SUITES.get(name) or {})
    if name == "moments":
        info["available"] = moments_module() is not None
        if not info["available"]:
            info["needs"] = "yuki.memory.moments (fMoments)"
    elif name == "requests":
        info["available"] = _resolver_factory() is not None
    elif name == "cost":
        info["available"] = True
    else:
        info["available"] = False
    return info


# ---------------------------------------------------------------------------------------------------------------------
# Reading a copy
# ---------------------------------------------------------------------------------------------------------------------


def _ro(db_path: Path) -> sqlite3.Connection:
    """A read-only connection to a copy (the live database is refused)."""
    db_path = Path(db_path)
    if dbcopy.is_live(db_path):
        raise RuntimeError("evaluations read a copy of the memory database, never the live one")
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    try:
        return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    except sqlite3.Error:
        return []


def _local_ts(day: str, hms: str) -> float:
    return datetime.strptime(f"{day} {hms}", "%Y-%m-%d %H:%M:%S").timestamp()


def _hms(at: float | None) -> str | None:
    return None if at is None else datetime.fromtimestamp(float(at)).strftime("%Y-%m-%d %H:%M:%S")


def log_digest(value: Any) -> str | None:
    """A digest as logs carry it (12 hex characters, D20) through the one helper in ``yuki.log.events``."""
    if not value:
        return None
    try:
        from yuki.log.events import log_digest as helper   # fPrivacy (D20)
    except ImportError:
        return str(value)[:12]
    return helper(value)


#: The moments columns the bars read (migration 40, map-foundations §F1.2); a missing one reads as ``None``.
MOMENT_COLUMNS = ("id", "at", "ended_at", "seen_at", "kind", "app", "service", "entity_digest", "value", "value_from",
                  "means", "by", "request_id", "source", "source_id", "evidence")


def read_moments(db_path: Path) -> list[dict[str, Any]] | None:
    """Every moment of a copy as content-free dicts (``has_entity`` instead of the entity), or ``None`` without the
    table."""
    with contextlib.closing(_ro(db_path)) as conn:
        cols = _columns(conn, "moments")
        if not cols:
            return None
        sel = [c if c in cols else f"NULL AS {c}" for c in MOMENT_COLUMNS]
        has = "entity_ciphertext IS NOT NULL" if "entity_ciphertext" in cols else "0"
        rows = conn.execute(f"SELECT {', '.join(sel)}, {has} AS has_entity FROM moments ORDER BY at, id").fetchall()
    return [dict(r) for r in rows]


def fingerprint(rows: Sequence[dict[str, Any]]) -> str:
    """The moments as a rebuild must reproduce them: what, where, when and from which source row (never the row id or
    the time it was written)."""
    keys = sorted((str(r.get("kind")), str(r.get("app")), round(float(r.get("at") or 0.0), 3),
                   None if r.get("ended_at") is None else round(float(r["ended_at"]), 3), str(r.get("evidence")),
                   str(r.get("source")), r.get("source_id"), str(r.get("entity_digest")), str(r.get("by")))
                  for r in rows)
    return hashlib.sha256(json.dumps(keys, default=str).encode()).hexdigest()


# ---------------------------------------------------------------------------------------------------------------------
# Phase F: the moments backfill bars (PLAN B.1)
# ---------------------------------------------------------------------------------------------------------------------

#: E1, the weekday 09:00 meeting in Zen, on the exact sources of D7 (PLAN Appendix E): local day, onset, evidence.
E1_EXPECTED: tuple[tuple[str, str, str], ...] = (
    ("2026-09-25", "08:57:52", "seen"),
    ("2026-09-28", "09:01:01", "hold"),
    ("2026-09-29", "09:00:06", "seen"),
    ("2026-09-30", "09:00:35", "hold"),
    ("2026-10-05", "08:58:57", "seen"),
    ("2026-10-07", "08:55:59", "seen"),
    ("2026-10-08", "08:56:52", "seen"),
)
E1_APP = "zen"
E1_TOLERANCE_S = 60.0
#: Mic holders that were never meetings (B.1): FL Studio, VALORANT (its image key and the short form), Settings.
NOT_MEETINGS = ("fl64", "valorant-win64-shipping", "valorant", "systemsettings")
DISCORD_APP = "discord"
#: The round-2 evidence window (16 days, 24 Sep - 9 Oct; a day starts at 04:00): Discord joined on 15 evenings.
EVIDENCE_FROM = ("2026-09-24", "04:00:00")
EVIDENCE_TO = ("2026-10-10", "04:00:00")
DAY_START_H = 4
EVENING_FROM_H = 17
DISCORD_EVENINGS_NEED = 15
#: Documented host shapes (data, not meaning): YouTube's and Google Meet's.
YOUTUBE_HOSTS = ("www.youtube.com", "youtube.com", "m.youtube.com")
MEET_HOSTS = ("meet.google.com",)
RUNTIME_BAR_S = 5.0
#: How far around an onset the independent sources are searched (s).
REFERENCE_WINDOW_S = 120.0


def _evidence_class(evidence: Any) -> str:
    return "hold" if evidence == "hold" else "seen"


def _evening_day(at: float) -> str | None:
    t = datetime.fromtimestamp(float(at))
    if t.hour >= EVENING_FROM_H or t.hour < DAY_START_H:
        return (t - timedelta(hours=DAY_START_H)).date().isoformat()
    return None


def _window() -> tuple[float, float]:
    return _local_ts(*EVIDENCE_FROM), _local_ts(*EVIDENCE_TO)


def bar_e1(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """``call_join`` app=zen on each E1 weekday within a minute, with the expected evidence class."""
    joins = [r for r in rows if r.get("kind") == "call_join" and r.get("app") == E1_APP and r.get("at") is not None]
    days = []
    for day, hms, expected in E1_EXPECTED:
        t = _local_ts(day, hms)
        near = [r for r in joins if abs(float(r["at"]) - t) <= E1_TOLERANCE_S]
        best = min(near, key=lambda r: abs(float(r["at"]) - t)) if near else None
        days.append({"day": day, "expected": hms, "expected_evidence": expected, "found": best is not None,
                     "delta_s": None if best is None else round(float(best["at"]) - t, 1),
                     "evidence": None if best is None else best.get("evidence"),
                     "evidence_ok": best is not None and _evidence_class(best.get("evidence")) == expected})
    found = sum(d["found"] for d in days)
    ok_ev = sum(d["evidence_ok"] for d in days)
    weekend = sum(1 for r in joins if datetime.fromtimestamp(float(r["at"])).weekday() >= 5
                  and 8 <= datetime.fromtimestamp(float(r["at"])).hour < 10)
    return {"days": days, "found": found, "evidence_ok": ok_ev, "of": len(E1_EXPECTED),
            "weekend_morning_joins": weekend, "pass": found == len(E1_EXPECTED) and ok_ev == len(E1_EXPECTED)}


def bar_not_meetings(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """No ``call_join`` of any evidence for the apps that were never meetings."""
    counts = Counter(str(r.get("app")) for r in rows if r.get("kind") == "call_join" and r.get("app") in NOT_MEETINGS)
    return {"by_app": {a: counts.get(a, 0) for a in NOT_MEETINGS}, "total": sum(counts.values()),
            "pass": sum(counts.values()) == 0}


def bar_discord_evenings(rows: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """A Discord ``call_join`` on the evenings of the evidence window (15 of its 16 days)."""
    lo, hi = _window()
    days = sorted({d for r in rows if r.get("kind") == "call_join" and r.get("app") == DISCORD_APP
                   and r.get("at") is not None and lo <= float(r["at"]) < hi
                   for d in [_evening_day(float(r["at"]))] if d})
    n_days = int(round((hi - lo) / 86400.0))
    return {"evenings": len(days), "need": DISCORD_EVENINGS_NEED, "window_days": n_days,
            "pass": len(days) >= DISCORD_EVENINGS_NEED}


def _timeline_hosts(db_path: Path, ids: Sequence[int]) -> dict[int, str | None]:
    if not ids:
        return {}
    out: dict[int, str | None] = {}
    with contextlib.closing(_ro(db_path)) as conn:
        if "host" not in _columns(conn, "timeline"):
            return {}
        for i in range(0, len(ids), 500):
            chunk = list(ids[i:i + 500])
            q = f"SELECT id, host FROM timeline WHERE id IN ({','.join('?' * len(chunk))})"
            out.update({int(r[0]): r[1] for r in conn.execute(q, chunk)})
    return out


def _host_kind(host: Any) -> str | None:
    if not host:
        return None
    h = str(host).casefold()
    return "meet" if h in MEET_HOSTS else "youtube" if h in YOUTUBE_HOSTS else "other"


def bar_youtube_pages(rows: Sequence[dict[str, Any]], db_path: Path) -> dict[str, Any]:
    """YouTube ``page`` moments with a non-null entity (the template value: a video id, a handle, home)."""
    pages = [r for r in rows if r.get("kind") == "page"]

    def from_timeline(r: dict[str, Any]) -> bool:
        return r.get("source") == "timeline" and r.get("source_id") is not None

    hosts = _timeline_hosts(db_path, [int(r["source_id"]) for r in pages if from_timeline(r)])

    def youtube(r: dict[str, Any]) -> bool:
        host = hosts.get(int(r["source_id"])) if from_timeline(r) else None
        return _host_kind(host) == "youtube" or str(r.get("service") or "").casefold() == "youtube"

    yt = [r for r in pages if youtube(r)]
    with_entity = sum(1 for r in yt if r.get("has_entity"))
    return {"pages": len(pages), "youtube": len(yt), "youtube_with_entity": with_entity,
            "pages_with_entity": sum(1 for r in pages if r.get("has_entity")), "pass": with_entity > 0}


def per_kind_per_day(rows: Sequence[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Moments per local day and kind (the day starting at 04:00)."""
    out: dict[str, Counter] = defaultdict(Counter)
    for r in rows:
        if r.get("at") is None:
            continue
        day = (datetime.fromtimestamp(float(r["at"])) - timedelta(hours=DAY_START_H)).date().isoformat()
        out[day][str(r.get("kind"))] += 1
    return {d: dict(sorted(c.items())) for d, c in sorted(out.items())}


class MomentsUnavailable(RuntimeError):
    """Yuki's moments pipeline (``yuki.memory.moments``, fMoments) is not on this branch."""


def moments_module() -> Any:
    try:
        return importlib.import_module("yuki.memory.moments")
    except ImportError:
        return None


def _takes_argv(fn: Callable[..., Any]) -> bool:
    try:
        return len(inspect.signature(fn).parameters) >= 1
    except (TypeError, ValueError):
        return False


def _own_seconds(text: str) -> float | None:
    """The rebuild's own time from the pipeline's summary: ``yuki.memory.moments`` prints one JSON object whose ``s``
    is the rebuild alone (no opening, no migration, no printing). ``None`` when the output is not that shape."""
    try:
        d = json.loads((text or "").strip())
    except ValueError:
        return None
    s = d.get("s") if isinstance(d, dict) else None
    return float(s) if isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) else None


#: The longest one child-process rebuild may take (s), within the time left of the run's limit.
SUBPROCESS_TIMEOUT_S = 600.0


def cli_runner(db_path: Path, *, time_left: Callable[[], float] | None = None
               ) -> Callable[[list[str]], dict[str, Any]]:
    """Runs ``python -m yuki.memory.moments <args> --db <copy>``: in this process when the module's ``main`` takes an
    argv, else as a child process of the same Python, stopped at the time left of the run (``time_left``). Besides the
    wall time ``s``, ``own_s`` is the pipeline's own time for the rebuild, from its printed summary. Its output is kept
    out of the console (only its line count is reported)."""
    db_path = Path(db_path)
    if dbcopy.is_live(db_path):
        raise RuntimeError("the moments pipeline runs on a copy here, never the live database")

    def run(args: list[str]) -> dict[str, Any]:
        mod = moments_module()
        if mod is None:
            raise MomentsUnavailable("yuki.memory.moments is not on this branch (fMoments builds it)")
        argv = [*args, "--db", str(db_path)]
        main = getattr(mod, "main", None)
        t0 = time.perf_counter()
        if callable(main) and _takes_argv(main):
            buf, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
                try:
                    code = main(argv)
                except SystemExit as exc:
                    code = exc.code
            out = buf.getvalue()
            return {"code": int(code or 0) if not isinstance(code, str) else 1, "s": time.perf_counter() - t0,
                    "own_s": _own_seconds(out), "how": "in-process",
                    "lines": out.count("\n") + err.getvalue().count("\n")}
        root = Path(importlib.import_module("yuki").__file__).resolve().parents[1]
        limit = SUBPROCESS_TIMEOUT_S if time_left is None else max(1.0, min(SUBPROCESS_TIMEOUT_S, time_left()))
        try:
            got = subprocess.run([sys.executable, "-m", "yuki.memory.moments", *argv], cwd=root, capture_output=True,
                                 text=True, timeout=limit)
        except subprocess.TimeoutExpired:
            return {"code": 124, "s": time.perf_counter() - t0, "own_s": None, "how": "subprocess", "timed_out": True,
                    "lines": 0}
        return {"code": got.returncode, "s": time.perf_counter() - t0, "own_s": _own_seconds(got.stdout),
                "how": "subprocess (includes Python's start)",
                "lines": (got.stdout or "").count("\n") + (got.stderr or "").count("\n")}

    return run


def _rebuild_time(first: dict[str, Any], second: dict[str, Any]) -> tuple[float, str]:
    """The rebuild's time for the < 5 s bar: the pipeline's own time (the slower of the two rebuilds) when it printed
    it; else the second rebuild's wall time (the first also opens the copy and migrates it, which is not the
    rebuild)."""
    own = [r.get("own_s") for r in (first, second)]
    if all(isinstance(v, (int, float)) for v in own):
        return float(max(own)), "the pipeline's own timing, the slower rebuild"
    return float(second.get("s") or 0.0), f"the second rebuild, {second.get('how')}"


def moments_bars(db_path: Path, *, runner: Callable[[list[str]], dict[str, Any]] | None = None,
                 caps: Caps | None = None) -> dict[str, Any]:
    """Phase F's moments bars on a copy: ``rebuild`` twice through Yuki's own pipeline, then every B.1 bar. The copy
    is changed (its moments rebuilt); the live database is refused. With ``caps``, no rebuild starts after the run's
    time limit and a child-process rebuild is stopped at it (an in-process one can't be cut short; it is checked
    after)."""
    db_path = Path(db_path)
    if dbcopy.is_live(db_path):
        raise RuntimeError("the moments bars run on a copy, never the live database")
    runner = runner or cli_runner(db_path, time_left=caps.time_left_s if caps is not None else None)
    out: dict[str, Any] = {"suite": "moments", "phase": "F", "at": time.time(), "result": None}
    out["reference"] = source_reference(db_path)

    def stop_for_time(step: dict[str, Any] | None = None) -> bool:
        if (caps is None or not caps.over_time()) and not (step or {}).get("timed_out"):
            return False
        out["result"] = "stopped"
        out["why"] = f"the time limit ({caps.limit_s / 60:g} min)" if caps is not None else "the time limit"
        return True

    if stop_for_time():
        return out
    try:
        first = runner(["rebuild"])
    except MomentsUnavailable as exc:
        out["result"] = "not available"
        out["why"] = str(exc)
        return out
    out["rebuild"] = [{k: (round(v, 3) if isinstance(v, float) else v) for k, v in first.items()}]
    if stop_for_time(first):
        return out
    if first.get("code"):
        out["result"] = "error"
        out["why"] = f"the rebuild exited with code {first['code']}"
        return out
    rows = read_moments(db_path)
    if rows is None:
        out["result"] = "error"
        out["why"] = "the copy has no moments table after the rebuild"
        return out
    second = runner(["rebuild"])
    out["rebuild"].append({k: (round(v, 3) if isinstance(v, float) else v) for k, v in second.items()})
    if stop_for_time(second):
        return out
    again = read_moments(db_path) or []
    fp1, fp2 = fingerprint(rows), fingerprint(again)
    rebuild_s, rebuild_how = _rebuild_time(first, second)
    bars = {
        "e1": bar_e1(rows),
        "not_meetings": bar_not_meetings(rows),
        "discord_evenings": bar_discord_evenings(rows),
        "youtube_pages": bar_youtube_pages(rows, db_path),
        "runtime": {"s": round(rebuild_s, 3), "bar_s": RUNTIME_BAR_S, "how": rebuild_how,
                    "first_call_s": round(float(first.get("s") or 0.0), 3), "pass": rebuild_s < RUNTIME_BAR_S},
        "idempotent": {"rows": [len(rows), len(again)], "same": fp1 == fp2 and not second.get("code"),
                       "pass": fp1 == fp2 and not second.get("code")},
    }
    out["bars"] = bars
    out["moments"] = {"total": len(rows), "by_kind": dict(sorted(Counter(str(r.get("kind")) for r in rows).items())),
                      "by_evidence": dict(sorted(Counter(str(r.get("evidence")) for r in rows
                                                         if r.get("kind") == "call_join").items())),
                      "by_yuki": sum(1 for r in rows if r.get("by") == "yuki"),
                      "per_day": per_kind_per_day(rows)}
    out["result"] = "pass" if all(b["pass"] for b in bars.values()) else "fail"
    return out


def source_reference(db_path: Path) -> dict[str, Any]:
    """The bars' facts from the raw rows, independent of the moments code (content-free): for each E1 day, the call
    rows of Zen near the onset (their ``via``) and the timeline rows there (a Meet host? the mic held?); Discord call
    rows per evening of the evidence window; meeting rows and call rows of the apps that were never meetings."""
    from yuki.memory.store import app_key

    with contextlib.closing(_ro(db_path)) as conn:
        ccols, tcols = _columns(conn, "calls"), _columns(conn, "timeline")
        calls = []
        if ccols:
            kind = "kind" if "kind" in ccols else "'call'"
            hold = "hold_since" if "hold_since" in ccols else "NULL"
            calls = [dict(r) for r in conn.execute(
                f"SELECT id, process, app, via, {kind} AS kind, detected_at, {hold} AS hold_since FROM calls")]
        meetings = []
        if tcols and "meeting" in tcols:
            mic = "mic_s" if "mic_s" in tcols else "0"
            meetings = [dict(r) for r in conn.execute(
                f"SELECT id, process, host, started_at, {mic} AS mic_s FROM timeline WHERE meeting IS NOT NULL")]
    for c in calls:
        c["key"] = app_key(c.get("process")) or app_key(c.get("app")) or ""
        c["onset"] = float(c.get("hold_since") or c.get("detected_at") or 0.0)
    for m in meetings:
        m["key"] = app_key(m.get("process")) or ""
        m["host_kind"] = _host_kind(m.get("host"))
    e1 = []
    for day, hms, expected in E1_EXPECTED:
        t = _local_ts(day, hms)
        near_calls = [c for c in calls if c["key"] == E1_APP and abs(c["onset"] - t) <= REFERENCE_WINDOW_S]
        near_rows = [m for m in meetings if abs(float(m["started_at"]) - t) <= REFERENCE_WINDOW_S
                     and (m["key"] == E1_APP or m["host_kind"] == "meet")]
        e1.append({"day": day, "expected_evidence": expected, "calls_via": sorted({str(c["via"]) for c in near_calls}),
                   "meet_host_row": any(m["host_kind"] == "meet" for m in near_rows),
                   "zen_mic_row": any(m["key"] == E1_APP and not m["host_kind"] and float(m["mic_s"] or 0) > 0
                                      for m in near_rows)})
    lo, hi = _window()
    evenings = {d for c in calls if c["key"] == DISCORD_APP and c.get("kind") == "call" and lo <= c["onset"] < hi
                for d in [_evening_day(c["onset"])] if d}
    return {"e1": e1,
            "e1_days_with_a_source": sum(1 for d in e1 if d["calls_via"] or d["meet_host_row"] or d["zen_mic_row"]),
            "discord_call_evenings": len(evenings),
            "meeting_rows": {a: sum(1 for m in meetings if m["key"] == a)
                             for a in (*NOT_MEETINGS, E1_APP, DISCORD_APP)},
            "call_rows": {a: dict(sorted(Counter(f"{c['kind']}/{c['via']}" for c in calls if c["key"] == a).items()))
                          for a in (*NOT_MEETINGS, E1_APP, DISCORD_APP)}}


def _memory_log_calls(folder: Path | None) -> list[dict[str, Any]]:
    """``call_detected`` events of the memory service's logs (``logs/memory-*.jsonl``): time, call id, process and
    via only."""
    if folder is None:
        return []
    out = []
    for f in sorted(Path(folder).glob("memory-*.jsonl")):
        try:
            fh = f.open("r", encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"call_detected"' not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if isinstance(d, dict) and d.get("type") == "call_detected":
                    out.append({"ts": float(d.get("ts") or 0.0), "call_id": d.get("call_id"),
                                "process": d.get("process"), "via": d.get("via")})
    return out


def sample_moments(db_path: Path, n: int = 50, *, seed: int = 1009,
                   memory_logs: Path | None = None) -> list[dict[str, Any]]:
    """``n`` moments for the reviewer's hand check (B.1: ≥ 95% must agree), stratified by kind (at least one of each
    kind, the rest in proportion), each with what independent sources say: its calls row (``via``, kind, app), the
    timeline rows around it (meeting, mic held, host kind), the calls rows around it, and the memory log's
    ``call_detected``. Content-free."""
    from yuki.memory.store import app_key

    rows = read_moments(db_path) or []
    by_kind: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        by_kind[str(r.get("kind"))].append(r)
    rng = random.Random(seed)
    total = len(rows)
    picks: list[dict[str, Any]] = []
    if total and n > 0:
        kinds = sorted(by_kind)
        quota = {k: min(len(by_kind[k]), max(1, math.floor(n * len(by_kind[k]) / total))) for k in kinds}
        left = n - sum(quota.values())
        for k in sorted(kinds, key=lambda k: -len(by_kind[k])):
            if left <= 0:
                break
            add = min(left, len(by_kind[k]) - quota[k])
            quota[k] += add
            left -= add
        for k in kinds:
            picks += rng.sample(by_kind[k], quota[k])
    logs = _memory_log_calls(memory_logs)
    out = []
    with contextlib.closing(_ro(db_path)) as conn:
        has_calls, tcols = bool(_columns(conn, "calls")), _columns(conn, "timeline")
        for m in sorted(picks, key=lambda r: float(r.get("at") or 0.0))[:n]:
            at = float(m.get("at") or 0.0)
            item = {"id": m.get("id"), "kind": m.get("kind"), "app": m.get("app"), "evidence": m.get("evidence"),
                    "at": _hms(at), "by": m.get("by"), "source": m.get("source"), "source_id": m.get("source_id"),
                    "entity_digest": log_digest(m.get("entity_digest"))}
            if has_calls:
                if m.get("source") == "calls" and m.get("source_id") is not None:
                    c = conn.execute("SELECT * FROM calls WHERE id=?", (m["source_id"],)).fetchone()
                    if c is not None:
                        c = dict(c)
                        onset = float(c.get("hold_since") or c.get("detected_at") or 0.0)
                        item["calls_row"] = {"via": c.get("via"), "kind": c.get("kind"),
                                             "app": app_key(c.get("process")) or app_key(c.get("app")),
                                             "delta_s": round(at - onset, 1)}
                near = conn.execute("SELECT via, process, app FROM calls WHERE detected_at BETWEEN ? AND ?",
                                    (at - REFERENCE_WINDOW_S, at + REFERENCE_WINDOW_S)).fetchall()
                item["calls_near"] = sorted({f"{app_key(r[1]) or app_key(r[2])}/{r[0]}" for r in near})
            if tcols:
                meeting = "meeting IS NOT NULL" if "meeting" in tcols else "0"
                mic = "mic_s > 0" if "mic_s" in tcols else "0"
                near = conn.execute(
                    f"SELECT process, host, {meeting} AS meeting, {mic} AS mic FROM timeline"
                    " WHERE started_at <= ? AND ended_at >= ?", (at + REFERENCE_WINDOW_S, at - REFERENCE_WINDOW_S)
                ).fetchall()
                item["timeline_near"] = sorted({f"{app_key(r[0])}/{_host_kind(r[1]) or '-'}"
                                                f"{'/meeting' if r[2] else ''}{'/mic' if r[3] else ''}" for r in near})
            if logs:
                hits = [e for e in logs if (m.get("source") == "calls" and e["call_id"] == m.get("source_id"))
                        or abs(e["ts"] - at) <= REFERENCE_WINDOW_S]
                item["log_call_detected"] = sorted({f"{app_key(e['process'])}/{e['via']}" for e in hits})
            out.append(item)
    return out


def moments_text(out: dict[str, Any]) -> list[str]:
    """The moments suite in plain words (content-free)."""
    lines = ["Moments backfill (phase F), on a copy of your memory"]
    ref = out.get("reference") or {}
    if ref:
        lines.append(f"Independent sources: E1 has a call row or a meeting row on {ref.get('e1_days_with_a_source')} "
                     f"of {len(E1_EXPECTED)} weekdays; Discord call rows on {ref.get('discord_call_evenings')} "
                     f"evenings; meeting rows of apps that were never meetings: "
                     + ", ".join(f"{a} {ref['meeting_rows'][a]}" for a in NOT_MEETINGS) + ".")
    if out.get("result") in ("not available", "error", "stopped"):
        lines.append(f"{out['result'].capitalize()}: {out.get('why')}.")
        return lines
    b = out["bars"]
    e1 = b["e1"]
    lines.append(f"E1 (Zen, ±{E1_TOLERANCE_S:g} s): {e1['found']}/{e1['of']} weekdays found, evidence as expected on "
                 f"{e1['evidence_ok']}/{e1['of']}; weekend-morning joins {e1['weekend_morning_joins']}. "
                 f"{'PASS' if e1['pass'] else 'FAIL'}")
    for d in e1["days"]:
        lines.append(f"  {d['day']} {d['expected']} ({d['expected_evidence']}): "
                     + (f"found {d['delta_s']:+.1f} s, evidence {d['evidence']}" if d["found"] else "missing"))
    nm = b["not_meetings"]
    lines.append("Never meetings: " + ", ".join(f"{a} {n}" for a, n in nm["by_app"].items())
                 + f". {'PASS' if nm['pass'] else 'FAIL'}")
    de = b["discord_evenings"]
    lines.append(f"Discord evenings: {de['evenings']} of {de['window_days']} (need {de['need']}). "
                 f"{'PASS' if de['pass'] else 'FAIL'}")
    yt = b["youtube_pages"]
    lines.append(f"YouTube page moments: {yt['youtube']}, with an entity {yt['youtube_with_entity']} (all pages "
                 f"{yt['pages']}, with an entity {yt['pages_with_entity']}). {'PASS' if yt['pass'] else 'FAIL'}")
    rt, idem = b["runtime"], b["idempotent"]
    lines.append(f"Rebuild time: {rt['s']:.2f} s ({rt['how']}; bar < {rt['bar_s']:g} s). "
                 f"{'PASS' if rt['pass'] else 'FAIL'}")
    lines.append(f"Rebuild idempotent: rows {idem['rows'][0]} then {idem['rows'][1]}, same moments: "
                 f"{'yes' if idem['same'] else 'no'}. {'PASS' if idem['pass'] else 'FAIL'}")
    mo = out.get("moments") or {}
    lines.append(f"Moments: {mo.get('total')} ({', '.join(f'{k} {v}' for k, v in (mo.get('by_kind') or {}).items())}); "
                 f"by Yuki {mo.get('by_yuki')}.")
    lines.append(f"Result: {str(out['result']).upper()}")
    return lines


def sample_text(items: Sequence[dict[str, Any]]) -> list[str]:
    """The hand-check sheet: one line per moment, its independent sources, and a blank for the reviewer's verdict."""
    lines = [f"Hand check: {len(items)} moments (mark each agree / disagree against the independent sources)"]
    for i in items:
        parts = [f"#{i['id']} {i['kind']} {i['app']} ev={i['evidence']} {i['at']} by={i['by']} src={i['source']}"]
        if i.get("calls_row"):
            c = i["calls_row"]
            parts.append(f"calls row via={c['via']} kind={c['kind']} app={c['app']} d={c['delta_s']:+.1f}s")
        if i.get("calls_near") is not None:
            parts.append("calls near " + (",".join(i["calls_near"]) or "-"))
        if i.get("timeline_near") is not None:
            parts.append("timeline near " + (",".join(i["timeline_near"]) or "-"))
        if i.get("log_call_detected") is not None:
            parts.append("log " + (",".join(i["log_call_detected"]) or "-"))
        lines.append(" | ".join(parts) + " | agree? __")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# Time split (for the R and T suites, evalReplay)
# ---------------------------------------------------------------------------------------------------------------------

#: The tables a replay cuts back to "what memory knew on day D", and the time column tried first to last.
TIME_SPLIT_TABLES = ("moments", "routines", "routine_events", "tastes", "skill_runs")
TIME_SPLIT_COLUMNS = ("created_at", "seen_at", "at")


def time_split(db_path: Path, cutoff: float, tables: Sequence[str] = TIME_SPLIT_TABLES) -> dict[str, int]:
    """Delete, on a copy, every row of ``tables`` written at or after ``cutoff`` (by the table's ``created_at``, else
    ``seen_at``, else ``at``); returns rows removed per table. Tables not in the copy are skipped."""
    db_path = Path(db_path)
    if dbcopy.is_live(db_path):
        raise RuntimeError("a time split cuts a copy, never the live database")
    out: dict[str, int] = {}
    with contextlib.closing(sqlite3.connect(db_path, timeout=10)) as conn:
        for t in tables:
            cols = _columns(conn, t)
            col = next((c for c in TIME_SPLIT_COLUMNS if c in cols), None)
            if col is None:
                continue
            out[t] = conn.execute(f"DELETE FROM {t} WHERE {col} >= ?", (float(cutoff),)).rowcount
        conn.commit()
    return out


# ---------------------------------------------------------------------------------------------------------------------
# Phase S: the requests suite
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Fixture:
    """One labelled phrasing (from the user's fixtures file): the words, when said, the skill wanted (``None``: no
    skill) and the slots the user named."""
    id: str
    words: str
    at: float
    skill: str | None
    slots: dict[str, Any] = field(default_factory=dict)


#: Request text kept at most (characters).
WORDS_CHARS = 300


def user_words(text: Any) -> str:
    """The user's own words in a request Yuki composed (its own formats, never the user's words): a reply to a card
    starts with the card in brackets on its own line; Yuki's notes follow the words after a blank line, in brackets; a
    spoken request's note carries what speech recognition heard, which is the user's own words (kept over the voice's
    hand-off). ``""`` when the words are Yuki's own (a "Do it" on an offer, a card's suggestion sent unchanged).
    The same reading as ``yuki.eval.c3_data.request_words``, here so the skills eval outlives C3 (phase P)."""
    from yuki.memory.conversations import yuki_words_request

    t = str(text or "").strip()
    if yuki_words_request(t):
        return ""
    if t.startswith("[") and "]\n" in t:
        t = t.split("]\n", 1)[1]
    cut = t.find("\n\n[")
    head, notes = (t[:cut], t[cut:]) if cut >= 0 else (t, "")
    try:
        from yuki.agent.memory import VOICE_HEARD_MARK
    except Exception:  # noqa: BLE001
        VOICE_HEARD_MARK = "[Asked by voice. What the user said most recently"   # noqa: N806
    at = notes.find(VOICE_HEARD_MARK)
    if at >= 0:
        block = notes[at:]
        end = block.find("]\n")
        block = block[: end if end >= 0 else len(block)]
        colon = block.find("): ")
        if colon >= 0:
            quoted = block[colon + 3:].strip().rstrip("]").strip()
            heard = [h.strip().strip('"') for h in quoted.split('" / "') if h.strip().strip('"')]
            if heard:
                return " ".join(" / ".join(heard).split())[:WORDS_CHARS]
    return " ".join(head.split())[:WORDS_CHARS]


class Resolver(Protocol):
    """What the requests suite needs from the skill resolver (S): one talker call (the real prompt and menu, cached)
    plus code slot fill as of ``at``."""

    def resolve(self, words: str, at: float) -> dict[str, Any]:
        """``{"skill": key | None, "slots": {...}, "cost_usd": float}``."""
        ...


#: The talker's prompt (B.2: every state under 18k tokens), the fixture's own frame, and the answer.
TALKER_PROMPT_TOKENS = 18_000
FIXTURE_FRAME_TOKENS = 300
OUT_TYPICAL = 250
OUT_WORST = 1024
CHARS_PER_TOKEN = 4
UPPER_MARGIN = 1.5
SECONDS_PER_CALL = 4.0
#: The bars (PLAN B.2, map-routines E1.3).
RIGHT_BAR = 0.90
AGREE_WITHIN = 1


def _rates() -> dict[str, float]:
    try:
        from yuki.config import DEFAULT_PRICING, DEFAULT_VOICE_MOUTH_MODEL

        r = DEFAULT_PRICING.get(DEFAULT_VOICE_MOUTH_MODEL)
        if r:
            return r
    except Exception:  # noqa: BLE001 - the list price below
        pass
    return {"input": 1.0, "output": 5.0, "cache_write": 1.25, "cache_read": 0.1}


def default_estimate(words: str) -> tuple[float, float]:
    """``(typical, worst)`` dollars of one talker call on Haiku's list price: typical with the prompt from the cache,
    worst with the prompt written to it, a long answer and a margin."""
    r = _rates()
    own = len(words or "") // CHARS_PER_TOKEN + FIXTURE_FRAME_TOKENS
    typical = (TALKER_PROMPT_TOKENS * r["cache_read"] + own * r["input"] + OUT_TYPICAL * r["output"]) / 1e6
    worst = UPPER_MARGIN * (TALKER_PROMPT_TOKENS * r["cache_write"] + own * r["input"] + OUT_WORST * r["output"]) / 1e6
    return typical, worst


def _estimate(resolver: Any, words: str) -> tuple[float, float]:
    fn = getattr(resolver, "estimate_usd", None)
    if callable(fn):
        try:
            typical, worst = fn(words)
            return float(typical), float(worst)
        except Exception:  # noqa: BLE001 - the default estimate
            pass
    return default_estimate(words)


def plan_requests(fixtures: Sequence[Fixture], repeat: int, resolver: Any = None) -> dict[str, Any]:
    """The planned calls and their cost, for the up-front check and ``--dry-run``."""
    per = [_estimate(resolver, f.words) for f in fixtures]
    reps = max(1, int(repeat))
    calls = len(fixtures) * reps
    r = _rates()
    own_estimate = callable(getattr(resolver, "estimate_usd", None))
    # the default estimate reads the prompt from the cache: its first write is added once
    first_write = 0.0
    if fixtures and not own_estimate:
        first_write = TALKER_PROMPT_TOKENS * (r["cache_write"] - r["cache_read"]) / 1e6
    return {"calls": calls, "repeat": reps,
            "expected_usd": round(sum(t for t, _w in per) * reps + first_write, 4),
            "worst_usd": round(sum(w for _t, w in per) * reps, 4),
            "expected_s": calls * SECONDS_PER_CALL}


def _resolver_factory() -> Callable[..., Any] | None:
    try:
        mod = importlib.import_module("yuki.skills.resolve")
    except ImportError:
        return None
    fn = getattr(mod, "eval_resolver", None)
    return fn if callable(fn) else None


def load_resolver(db_path: Path) -> Any:
    """The skill resolver for evals, built on a **copy**: ``yuki.skills.resolve.eval_resolver(db_path=<copy>)`` (S),
    or ``None`` before it exists. Never built without the copy's path (it could fall back to Yuki's own database), and
    never on the live one; an error inside the factory is raised, never retried another way."""
    if db_path is None:
        raise ValueError("the resolver is only built on a copy of the memory database (no path given)")
    db_path = Path(db_path)
    if dbcopy.is_live(db_path):
        raise RuntimeError("the resolver is built on a copy of the memory database, never the live one")
    fn = _resolver_factory()
    if fn is None:
        return None
    return fn(db_path=db_path)


def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    s = " ".join(str(v).split()).casefold()
    try:
        return float(s)
    except ValueError:
        return s


#: Each skill's slots (D2; map-skills S2) and how a label is compared with the resolver's value, so a label is never
#: free words checked against a canonical value:
#: * ``choice``: a closed set (the label menu offers only these), compared exactly;
#: * ``points``: a signed number of points, or ``up`` / ``down`` when no amount was named (then only the sign counts);
#: * ``number``: a number (a level 0-100), compared as a number;
#: * ``name``: a name as the app shows it (your playlist's name in your Library, a channel, a person, an app key),
#:   compared with case and spacing folded;
#: * ``free``: kept with the fixture, never scored (its canonical form is the resolver's own, e.g. a video id).
#: ``volume.target``: ``spotify`` = Spotify's own volume; ``app`` = another app's (any app key other than the three);
#: ``system`` = the whole PC; ``own_voice`` = Yuki's voice.
VOLUME_TARGETS = ("spotify", "app", "system", "own_voice")
SLOT_SPEC: dict[str, dict[str, tuple[str, tuple[str, ...]]]] = {
    "meet.open": {},
    "youtube.home": {"browser": ("name", ())},
    "youtube.video": {"video": ("free", ()), "channel": ("name", ()), "browser": ("name", ())},
    "spotify.playlist": {"playlist": ("name", ())},
    "volume": {"target": ("choice", VOLUME_TARGETS), "change": ("points", ("up", "down")),
               "level": ("number", ()), "mute": ("choice", ("on", "off")),
               "control": ("choice", ("in_app", "mixer", "system", "own_voice"))},
    "message": {"app": ("name", ()), "person": ("name", ()), "text": ("free", ()),
                "send": ("choice", ("send", "draft"))},
    "discord.voice": {"channel": ("name", ())},
}


def slot_kind(skill: str | None, slot: str) -> tuple[str, tuple[str, ...]]:
    """How a slot is labelled and compared (an unknown slot is compared as a name)."""
    return SLOT_SPEC.get(str(skill), {}).get(slot, ("name", ()))


def _number(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _slot_right(skill: str | None, slot: str, want: Any, got: Any) -> bool | None:
    """One labelled slot against the resolver's value: ``True`` / ``False``, or ``None`` when it is not scored."""
    kind, options = slot_kind(skill, slot)
    if kind == "free":
        return None
    if skill == "volume" and slot == "target" and _norm(want) == "app":
        g = _norm(got)
        return bool(g) and g not in ("spotify", "system", "own_voice")
    if kind == "points" and _norm(want) in options:
        g = _number(got)
        return g is not None and (g > 0 if _norm(want) == "up" else g < 0)
    if kind in ("points", "number"):
        w, g = _number(want), _number(got)
        return w is not None and g is not None and abs(w - g) < 1e-9
    return _norm(want) == _norm(got)


def _slots_right(label: dict[str, Any], got: dict[str, Any], skill: str | None = None) -> bool:
    """Every scored slot the user named is in the answer with the right value (:data:`SLOT_SPEC`). Slots the user left
    blank, and free-text slots, are not compared."""
    have = got or {}
    checks = [_slot_right(skill, k, v, have.get(k)) for k, v in (label or {}).items() if v not in (None, "")]
    return all(c for c in checks if c is not None)


def _is_spotify_volume(f: Fixture) -> bool:
    """A fixture for Spotify's own volume: labelled ``volume`` with the closed choice ``target = spotify``."""
    return f.skill == "volume" and _norm((f.slots or {}).get("target")) == "spotify"


def score_requests(fixtures: Sequence[Fixture], runs: Sequence[dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Each run: right skill, right skill and slots, the Spotify-volume fixtures resolved to Spotify's own volume
    (``control`` = ``in_app``); across runs: fixtures whose answer differs. Content-free (ids and skill keys only)."""
    per_run = []
    rows = []
    for f in fixtures:
        rows.append({"id": f.id, "label": f.skill, "spotify_volume": _is_spotify_volume(f), "runs": []})
    for preds in runs:
        right = skill_right = errors = answered = 0
        sv_n = sv_in_app = 0
        for f, row in zip(fixtures, rows):
            p = preds.get(f.id)
            if p is None:
                row["runs"].append(None)
                continue
            answered += 1
            if p.get("error"):
                errors += 1
                row["runs"].append({"skill": None, "right": False, "error": p["error"]})
                continue
            ok_skill = (p.get("skill") or None) == (f.skill or None)
            ok = ok_skill and (f.skill is None or _slots_right(f.slots, p.get("slots") or {}, f.skill))
            skill_right += ok_skill
            right += ok
            entry = {"skill": p.get("skill"), "skill_right": ok_skill, "right": ok}
            if _is_spotify_volume(f):
                sv_n += 1
                in_app = ok_skill and _norm((p.get("slots") or {}).get("control")) == "in_app"
                sv_in_app += in_app
                entry["in_app"] = in_app
            row["runs"].append(entry)
        n = len(fixtures)
        per_run.append({"fixtures": n, "answered": answered, "errors": errors, "skill_right": skill_right,
                        "right": right, "right_rate": round(right / n, 3) if n else None,
                        "spotify_volume": sv_n, "spotify_in_app": sv_in_app})
    disagree = None
    if len(runs) >= 2:
        def key(p: dict[str, Any] | None) -> Any:
            if p is None:
                return None
            return (p.get("skill"), tuple(sorted((k, _norm(v)) for k, v in (p.get("slots") or {}).items())),
                    p.get("error"))
        disagree = max(sum(1 for f in fixtures if key(runs[i].get(f.id)) != key(runs[j].get(f.id)))
                       for i in range(len(runs)) for j in range(i + 1, len(runs)))
    complete = bool(runs) and all(r["answered"] == r["fixtures"] for r in per_run)
    n_sv = sum(1 for f in fixtures if _is_spotify_volume(f))
    bars = {
        "right": {"bar": RIGHT_BAR, "rates": [r["right_rate"] for r in per_run],
                  "pass": complete and all((r["right_rate"] or 0) >= RIGHT_BAR for r in per_run)},
        # never a pass with nothing to check: no Spotify-volume fixture leaves the bar (and the run) incomplete
        "spotify_in_app": {"fixtures": n_sv, "incomplete": n_sv == 0,
                           "pass": complete and n_sv > 0
                           and all(r["spotify_in_app"] == r["spotify_volume"] for r in per_run)},
        "agree": {"within": AGREE_WITHIN, "disagree": disagree,
                  "pass": complete and disagree is not None and disagree <= AGREE_WITHIN},
    }
    unscored = sum(1 for f in fixtures for k, v in (f.slots or {}).items()
                   if v not in (None, "") and slot_kind(f.skill, k)[0] == "free")
    whole = complete and not any(b.get("incomplete") for b in bars.values())
    why = None if whole else ("not every fixture was answered" if not complete else
                              "no Spotify-volume fixture (label one: volume, target spotify)")
    return {"per_run": per_run, "rows": rows, "disagree": disagree, "complete": complete, "bars": bars,
            "unscored_slots": unscored, "why_incomplete": why,
            "verdict": ("pass" if all(b["pass"] for b in bars.values()) else "fail") if whole else "incomplete"}


def run_requests(fixtures: Sequence[Fixture], resolver: Any, caps: Caps, *, repeat: int = 2, dry_run: bool = False,
                 say: Callable[[str], None] | None = None) -> dict[str, Any]:
    """The requests suite: refused up front when the expected cost or time is over the caps; before every call its
    worst case and the clock are checked, so no call can take the run past a cap (then it stops, incomplete)."""
    say = say or (lambda _s: None)
    plan = plan_requests(fixtures, repeat, resolver)
    out: dict[str, Any] = {"suite": "requests", "phase": "S", "at": time.time(), "fixtures": len(fixtures),
                           "plan": plan, "dry_run": bool(dry_run), "result": None}
    out["refusal"] = caps.refusal(plan["expected_usd"], plan["expected_s"], plan["worst_usd"])
    if dry_run:
        out["result"] = "dry run"
        out["resolver"] = resolver is not None or _resolver_factory() is not None
        out["estimate"] = "the resolver's own" if callable(getattr(resolver, "estimate_usd", None)) else "list price"
        out["caps"] = caps.summary()
        return out
    if not fixtures:
        out["result"] = "no fixtures"
        return out
    if out["refusal"]:
        out["result"] = "refused"
        return out
    if resolver is None:
        out["result"] = "not available"
        out["why"] = SUITES["requests"]["needs"]
        return out
    runs: list[dict[str, dict[str, Any]]] = []
    stopped = None
    unreported = 0
    for k in range(max(1, int(repeat))):
        preds: dict[str, dict[str, Any]] = {}
        runs.append(preds)
        for f in fixtures:
            worst = _estimate(resolver, f.words)[1]
            try:
                caps.before_call(worst)
            except CapReached as exc:
                stopped = str(exc)
                break
            try:
                res = resolver.resolve(f.words, f.at)
            except Exception as exc:  # noqa: BLE001 - one failed call is that fixture's error, never a crash
                caps.add(None, worst)                  # it may have been billed: its worst case counts
                preds[f.id] = {"error": type(exc).__name__}
                continue
            res = res if isinstance(res, dict) else {}
            caps.add(res.get("cost_usd"), worst)
            if not reported_cost(res.get("cost_usd")):
                unreported += 1                        # charged its worst case
            preds[f.id] = {"skill": res.get("skill"), "slots": dict(res.get("slots") or {})}
        say(f"Run {k + 1}: {len(preds)} of {len(fixtures)} fixtures (${caps.spent:.3f} spent).")
        if stopped:
            break
    out.update(score_requests(fixtures, runs))
    out["stopped"] = stopped
    out["unreported_cost_calls"] = unreported
    out["caps"] = caps.summary()
    out["result"] = out["verdict"]
    return out


def requests_text(out: dict[str, Any]) -> list[str]:
    lines = ["Requests (phase S): your labelled phrasings through the skill resolver"]
    p = out.get("plan") or {}
    lines.append(f"Planned: {p.get('calls', 0)} talker calls ({out.get('fixtures', 0)} fixtures x "
                 f"{p.get('repeat', 1)}); expected ${p.get('expected_usd', 0):.2f}, "
                 f"at most ${p.get('worst_usd', 0):.2f}; about "
                 f"{p.get('expected_s', 0) / 60:.1f} min.")
    res = out.get("result")
    if res == "dry run":
        lines.append("Dry run: no model was called"
                     + (" (estimated at Haiku's list price)." if out.get("estimate") == "list price" else ".")
                     + ("" if out.get("resolver") else " (The resolver is not on this branch yet.)")
                     + (f" A real run would be refused: {out['refusal']}." if out.get("refusal") else ""))
        return lines
    if res in ("refused", "not available", "no fixtures"):
        lines.append(f"{res.capitalize()}" + (f": {out.get('refusal') or out.get('why')}." if
                                               (out.get("refusal") or out.get("why")) else "."))
        return lines
    for k, r in enumerate(out.get("per_run") or [], 1):
        lines.append(f"Run {k}: right skill and slots {r['right']}/{r['fixtures']} ({(r['right_rate'] or 0):.0%}); "
                     f"right skill {r['skill_right']}; Spotify volume as Spotify's own {r['spotify_in_app']}/"
                     f"{r['spotify_volume']}; errors {r['errors']}.")
    if out.get("disagree") is not None:
        lines.append(f"The runs differ on {out['disagree']} fixtures (bar: at most {AGREE_WITHIN}).")
    c = out.get("caps") or {}
    lines.append(f"Spent ${c.get('spent_usd', 0):.3f} of ${c.get('budget_usd', 0):.2f} in {c.get('calls', 0)} calls, "
                 f"{c.get('elapsed_min', 0):g} min." + (f" Stopped at {out['stopped']}." if out.get("stopped") else "")
                 + (f" {out['unreported_cost_calls']} calls reported no cost: each was counted at its worst case."
                    if out.get("unreported_cost_calls") else ""))
    if out.get("why_incomplete") and res == "incomplete":
        lines.append(f"Incomplete: {out['why_incomplete']}.")
    if out.get("unscored_slots"):
        lines.append(f"{out['unscored_slots']} labelled free-text slots were kept but not scored.")
    lines.append(f"Result: {str(res).upper()}")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# Cost and the weekly scorecard
# ---------------------------------------------------------------------------------------------------------------------

#: Background model passes with their own accounting: table -> its time column. (``runs`` is your requests'
#: foreground spend, so it is left out.)
BACKGROUND_COST_TABLES = {
    "journal_batches": "at", "episode_runs": "at", "portrait_runs": "at", "conversation_batches": "at",
    "nudge_checkins": "at", "weekly_reviews": "at", "correction_passes": "started_at", "call_digests": "started_at",
    "day_digests": "created_at", "ui_task_runs": "at",
}


def background_cost(conn: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    """Background model spend between ``since`` and ``until``: per table and per day."""
    days = max((until - since) / 86400.0, 1e-9)
    per: dict[str, float] = {}
    for table, col in BACKGROUND_COST_TABLES.items():
        cols = _columns(conn, table)
        if "cost_usd" not in cols or col not in cols:
            continue
        got = conn.execute(f"SELECT coalesce(sum(cost_usd), 0) FROM {table} WHERE {col} >= ? AND {col} < ?",
                           (since, until)).fetchone()[0]
        per[table] = round(float(got or 0.0), 4)
    total = sum(per.values())
    return {"days": round(days, 2), "total_usd": round(total, 4), "usd_per_day": round(total / days, 4),
            "by_table": {k: v for k, v in sorted(per.items(), key=lambda kv: -kv[1])}}


def cost_report(db_path: Path, *, days: float = 7.0, now: float | None = None) -> dict[str, Any]:
    """The ``cost`` suite on a copy: background spend over the last ``days``."""
    now = time.time() if now is None else float(now)
    with contextlib.closing(_ro(db_path)) as conn:
        return {"suite": "cost", "phase": "P", "at": now, **background_cost(conn, now - float(days) * 86400.0, now)}


def _pct(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    v = sorted(values)
    i = max(0, min(len(v) - 1, math.ceil(q * len(v)) - 1))
    return round(v[i], 3)


#: Purpose test 1's bars (PLAN §5).
P50_BAR_S = 3.0
P90_BAR_S = 5.0
DONE_BAR = 0.9


def _purpose_fast_and_true(conn: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    """Per skill, from ``skill_runs`` (S): runs, done (the done-check said true), couldn't confirm, failed, time from
    your words to the done line (p50, p90), false done (the re-check disagreed or you corrected it)."""
    cols = _columns(conn, "skill_runs")
    if not cols:
        return {"available": False, "needs": "skill_runs (sCore, migration 41)"}
    t = "t0" if "t0" in cols else "at"
    key = ("(SELECT key FROM skills WHERE skills.id = skill_runs.skill_id)" if "skill_id" in cols and
           _columns(conn, "skills") else "skill" if "skill" in cols else "'?'")
    want = {c: c if c in cols else "NULL" for c in ("outcome", "checked", "ms", "t_done", "recheck", "false_done")}
    rows = conn.execute(f"SELECT {key} AS k, {t} AS t0, " + ", ".join(f"{v} AS {c}" for c, v in want.items())
                        + f" FROM skill_runs WHERE {t} >= ? AND {t} < ?", (since, until)).fetchall()
    per: dict[str, dict[str, Any]] = {}
    for r in rows:
        s = per.setdefault(str(r["k"]), {"runs": 0, "done": 0, "unconfirmed": 0, "failed": 0, "false_done": 0,
                                         "_s": []})
        s["runs"] += 1
        checked, outcome = r["checked"], r["outcome"]
        if checked == 1 or (checked is None and outcome == "done" and "checked" not in cols):
            s["done"] += 1
        elif checked == 0 or outcome == "failed":
            s["failed"] += 1
        elif outcome in ("done", "unverified"):
            s["unconfirmed"] += 1
        if r["false_done"] == 1 or r["recheck"] == 0:
            s["false_done"] += 1
        secs = (float(r["ms"]) / 1000.0 if r["ms"] is not None else
                float(r["t_done"]) - float(r["t0"]) if r["t_done"] is not None and r["t0"] is not None else None)
        if secs is not None and (checked == 1 or outcome == "done"):
            s["_s"].append(secs)
    for s in per.values():
        secs = s.pop("_s")
        s["done_rate"] = round(s["done"] / s["runs"], 3) if s["runs"] else None
        s["unconfirmed_rate"] = round(s["unconfirmed"] / s["runs"], 3) if s["runs"] else None
        s["p50_s"], s["p90_s"] = _pct(secs, 0.5), _pct(secs, 0.9)
        s["pass"] = (s["false_done"] == 0 and (s["done_rate"] or 0) >= DONE_BAR
                     and s["p50_s"] is not None and s["p50_s"] <= P50_BAR_S and s["p90_s"] <= P90_BAR_S)
    return {"available": True, "per_skill": dict(sorted(per.items())),
            "false_done": sum(s["false_done"] for s in per.values()),
            "bars": {"p50_s": P50_BAR_S, "p90_s": P90_BAR_S, "done_rate": DONE_BAR, "false_done": 0}}


def _counts_by(conn: sqlite3.Connection, table: str, col: str, tcol: str, since: float, until: float
               ) -> dict[str, int] | None:
    cols = _columns(conn, table)
    if not cols or col not in cols or tcol not in cols:
        return None
    return {str(r[0]): int(r[1]) for r in conn.execute(
        f"SELECT {col}, count(*) FROM {table} WHERE {tcol} >= ? AND {tcol} < ? GROUP BY {col} ORDER BY {col}",
        (since, until))}


def _purpose_picks_it_up(conn: sqlite3.Connection, since: float, until: float) -> dict[str, Any]:
    """Routine cards (R): events by kind; offers marked over a game in front or an open call (rLane writes
    ``game_front`` / ``in_call`` into the offer's detail); the bar is 0."""
    kinds = _counts_by(conn, "routine_events", "kind", "at", since, until)
    if kinds is None:
        return {"available": False, "needs": "routine_events (rMiner, migration 42; rLane)"}
    over = 0
    for (detail,) in conn.execute("SELECT detail FROM routine_events WHERE kind='offered' AND at >= ? AND at < ?",
                                  (since, until)):
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            continue
        over += bool(isinstance(d, dict) and (d.get("game_front") or d.get("in_call")))
    return {"available": True, "events": kinds, "over_game_or_call": over, "pass": over == 0}


def scorecard(db_path: Path, *, days: float = 7.0, now: float | None = None) -> dict[str, Any]:
    """The weekly scorecard (PLAN §5) on a copy, content-free: the three purpose tests as far as their tables exist on
    this branch, moments per day, background spend per day. Footprint comes from the live service's stats, not a
    copy."""
    now = time.time() if now is None else float(now)
    since = now - float(days) * 86400.0
    with contextlib.closing(_ro(db_path)) as conn:
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        moments = _counts_by(conn, "moments", "kind", "at", since, now)
        prefs = _counts_by(conn, "pref_events", "kind", "at", since, now)
        out = {
            "at": now, "days": float(days), "since": since, "db_version": int(version),
            "purpose_1_fast_and_true": _purpose_fast_and_true(conn, since, now),
            "purpose_2_shown_once": ({"available": True, "pref_events": prefs, "note": "counts only until sVoice "
                                      "defines the reuse measure"} if prefs is not None else
                                     {"available": False, "needs": "prefs, pref_events (sCore/sVoice, migration 41)"}),
            "purpose_3_picks_it_up": _purpose_picks_it_up(conn, since, now),
            "moments_per_day": ({k: round(v / float(days), 2) for k, v in moments.items()} if moments is not None
                                else {"available": False, "needs": "moments (fMoments, migration 40)"}),
            "background_cost": background_cost(conn, since, now),
            "footprint": {"available": False, "needs": "the memory service's stats lines (live, per-thread CPU)"},
        }
    return out


def scorecard_text(card: dict[str, Any]) -> list[str]:
    lines = [f"Scorecard, last {card['days']:g} days (database version {card['db_version']})"]
    p1 = card["purpose_1_fast_and_true"]
    if not p1.get("available"):
        lines.append(f"1. Fast and true: not yet ({p1['needs']}).")
    else:
        lines.append(f"1. Fast and true: false done {p1['false_done']} (bar 0).")
        for k, s in p1["per_skill"].items():
            lines.append(f"   {k}: runs {s['runs']}, done {s['done']} ({(s['done_rate'] or 0):.0%}), couldn't confirm "
                         f"{s['unconfirmed']}, failed {s['failed']}, p50 {s['p50_s']} s, p90 {s['p90_s']} s, "
                         f"false done {s['false_done']}. {'PASS' if s['pass'] else 'FAIL'}")
    p2 = card["purpose_2_shown_once"]
    lines.append("2. Shown once, reused: " + (f"preference events {p2['pref_events']}." if p2.get("available")
                                               else f"not yet ({p2['needs']})."))
    p3 = card["purpose_3_picks_it_up"]
    lines.append("3. Picks it up itself: " + (f"events {p3['events']}; over a game or call {p3['over_game_or_call']}."
                                               if p3.get("available") else f"not yet ({p3['needs']})."))
    mo = card["moments_per_day"]
    lines.append("Moments per day: " + (", ".join(f"{k} {v}" for k, v in mo.items()) if "available" not in mo
                                        else f"not yet ({mo['needs']})."))
    bc = card["background_cost"]
    lines.append(f"Background spend: ${bc['usd_per_day']:.2f} a day (${bc['total_usd']:.2f} in {bc['days']:g} days).")
    return lines


# ---------------------------------------------------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------------------------------------------------


def write_report(out: dict[str, Any], name: str, folder: Path | None = None) -> Path:
    """A content-free JSON report in ``%TEMP%`` (never the repo): ``yuki-skills-eval-<name>-<time>.json``."""
    folder = Path(folder) if folder is not None else dbcopy.report_dir()
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.fromtimestamp(float(out.get("at") or time.time())).strftime("%Y%m%d-%H%M%S")
    path = folder / f"yuki-skills-eval-{name}-{stamp}.json"
    path.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return path


__all__ = ["BACKGROUND_COST_TABLES", "Caps", "CapReached", "E1_EXPECTED", "Fixture", "HARD_BUDGET_USD", "HARD_MINUTES",
           "MomentsUnavailable", "NOT_MEETINGS", "SLOT_SPEC", "SUITES", "VOLUME_TARGETS", "background_cost", "bar_discord_evenings", "bar_e1",
           "bar_not_meetings", "bar_youtube_pages", "cli_runner", "cost_report", "default_estimate", "fingerprint",
           "load_resolver", "moments_bars", "moments_text", "plan_requests", "read_moments", "reported_cost",
           "requests_text", "run_requests", "sample_moments", "slot_kind", "sample_text", "score_requests", "scorecard", "scorecard_text",
           "source_reference", "suite_status", "time_split", "user_words", "write_report"]
