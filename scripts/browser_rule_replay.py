"""sWeb offline bar: youtube.home's browser rule (PLAN.md rev 4, Appendix B.2: ">= 94% of held-out YouTube sessions";
review r1 M3), replayed on a COPY of the memory database.

Copies the database read-only (``yuki.eval.dbcopy.copy_db``: the sqlite backup API, source opened ``?mode=ro``), opens
the copy, replays, and deletes the copy. Only the ``timeline`` table is read (and ``prefs`` when the copy has it).

* **A YouTube session** starts at a user's own present timeline row on a YouTube host (``yuki/connect/sites/
  youtube.toml``) with no YouTube row of any program ending in the 30 minutes before it. **The truth** is the program
  that row is in: the browser the user actually used.
* **The rule** is the shipped one (``yuki.skills.web_route``: ``measured_browsers``, ``last_in_front``,
  ``browser_rule``), run as of one second before each session start: "in front" is the program of the user's own row
  in front within the 60 s before the start; "recent" the last measured browser in front at most 30 minutes before;
  the usual is the ``youtube.home:browser`` preference as of then (none on a copy without the skills tables). Nobody
  names a browser in a replay.
* **The definition of "a browser"** has three knobs: the minimum distinct hosts in 14 days (1-5), whether a program
  that showed YouTube counts too (``by_site``), and whether "the browser YouTube was last shown in" comes before "the
  one used most" (``site_first``). The grid (20 definitions) is scored on the sessions **before** the cut (the choosing
  half); the best one there (ties: the earlier in the grid) is then scored on the sessions **after** the cut (the
  held-out half), beside the shipped definition (3 hosts, by_site, site_first). The cut defaults to the median session
  start (half the sessions each side); ``--cut YYYY-MM-DD`` sets it.
* **Refinements of the "last in front <= 30 min" step** (review r2 M3: all the held-out misses come from it), inside
  the plan's rule: skip glance rows (the user's time on the row under 5-120 s), count only rows with input, count only
  rows that showed a page (``web_route.last_in_front``'s knobs). Run with the shipped definition, chosen on the
  choosing half the same way (ties: the earlier in the grid, the shipped step first) and scored on the held-out half.

Content-free output only: counts, shares and the knobs (no app names, no hosts, no titles).

    python scripts/browser_rule_replay.py [--src PATH] [--since 2026-09-24] [--cut YYYY-MM-DD] [--gap 30] [--json]
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

#: A YouTube row starts a session when no YouTube row ended in this gap before it (s).
SESSION_GAP_S = 30 * 60.0
#: "In front" before the session: the user's own row in front within this long before its start (s).
FRONT_S = 60.0
#: B.2's bar.
BAR = 0.94
#: The shipped definition (web_route's defaults).
SHIPPED = (3, True, True)
GRID = [(h, by_site, site_first) for h in (1, 2, 3, 4, 5) for by_site in (False, True) for site_first in (False, True)]
#: The "recent" step's refinements: (min seconds on the row, rows with input only, rows with a page only); the shipped
#: step (no refinement) first.
RECENT_GRID = [(m, active, page) for m in (0, 5, 10, 15, 20, 30, 60, 120) for active in (False, True)
               for page in (False, True)]


def _ts(day: str) -> float:
    return dt.datetime.strptime(day, "%Y-%m-%d").timestamp()


def sessions(store: Any, hosts: list[str], since: float, gap_s: float = SESSION_GAP_S) -> list[tuple[float, str]]:
    """``[(start, app key of the browser used)]`` of the YouTube sessions starting at or after ``since``."""
    from yuki.memory.store import app_key

    marks = ",".join("?" * len(hosts))
    rows = store._query(
        f"SELECT started_at, ended_at, process FROM timeline WHERE host IN ({marks}) AND by_yuki = 0 AND state = "
        "'present' AND process IS NOT NULL ORDER BY started_at", hosts)
    out: list[tuple[float, str]] = []
    last_end = float("-inf")
    for started, ended, process in rows:
        started, ended = float(started), float(ended or started)
        if started - last_end >= gap_s and started >= since:
            key = app_key(str(process))
            if key:
                out.append((started, key))
        last_end = max(last_end, ended)
    return out


def front_before(store: Any, start: float) -> str | None:
    """The program of the user's own row in front within :data:`FRONT_S` before ``start``."""
    from yuki.memory.store import app_key

    rows = store._query(
        "SELECT process FROM timeline WHERE by_yuki = 0 AND state = 'present' AND process IS NOT NULL AND "
        "started_at < ? AND ended_at >= ? ORDER BY ended_at DESC, started_at DESC LIMIT 1",
        (start, start - FRONT_S))
    return (app_key(str(rows[0][0])) or None) if rows else None


def usual_then(store: Any, now: float) -> str | None:
    try:
        from yuki.skills import prefs

        got = prefs.choose(store, "youtube.home:browser", now=now, until=now)
        return got.option if got is not None else None
    except Exception:  # noqa: BLE001 - a copy without the skills tables
        return None


def site_first_rule(*, front: str | None, recent: str | None, pref: str | None,
                    measured: list[dict[str, Any]]) -> tuple[str, str]:
    """NOT shipped, reported beside the plan's order for Claude and the user: in front > the browser YouTube was last
    shown in > recent > usual > most used (the plan's order puts recent and the usual before YouTube's own browser)."""
    keys = [str(m.get("key") or "") for m in measured if m.get("key")]
    if front and front in keys:
        return front, "front"
    shown = [m for m in measured if m.get("key") and m.get("site_last") is not None]
    if shown:
        return str(max(shown, key=lambda m: float(m["site_last"]))["key"]), "site"
    if recent and recent in keys:
        return recent, "recent"
    if pref:
        return pref, "pref"
    return (keys[0], "most") if keys else ("", "default")


def replay(store: Any, *, since: float, cut: float | None, gap_s: float = SESSION_GAP_S) -> dict[str, Any]:
    from yuki.skills import web_route as W

    hosts = [h.casefold() for h in W.site_hosts("youtube")]
    found = sessions(store, hosts, since, gap_s)
    if not found:
        return {"sessions": 0}
    if cut is None:
        cut = found[len(found) // 2][0]
    scores: dict[tuple[int, bool, bool], dict[str, Counter]] = {g: {"choose": Counter(), "held": Counter()}
                                                                for g in GRID}
    sources: Counter = Counter()
    misses: Counter = Counter()
    alt: dict[str, Counter] = {"choose": Counter(), "held": Counter()}
    rec: dict[tuple[float, bool, bool], dict[str, Counter]] = {v: {"choose": Counter(), "held": Counter()}
                                                               for v in RECENT_GRID}
    t0 = time.perf_counter()
    for start, truth in found:
        now = start - 1.0
        part = "choose" if start < cut else "held"
        front = front_before(store, start)
        pref = usual_then(store, now)
        # every program with a page row, once per session (the knobs only filter it, in the function's own order)
        programs = W.measured_browsers(store, now=now, site_hosts=hosts, min_hosts=1, by_site=False)
        shipped = W.measured_browsers(store, now=now, site_hosts=hosts)
        recents: dict[tuple[str, ...], str | None] = {}
        for h, by_site, site_first in GRID:
            measured = [m for m in programs if m["hosts"] >= h or (by_site and m["site_last"] is not None)]
            if (h, by_site, site_first) == SHIPPED:
                assert measured == shipped, "the replay's filter drifted from web_route.measured_browsers"
            keys = tuple(m["key"] for m in measured)
            if keys not in recents:
                recents[keys] = W.last_in_front(store, list(keys), now=now) if keys else None
            key, source = W.browser_rule(named=None, said=False, front=front, recent=recents[keys], pref=pref,
                                         measured=measured, site_first=site_first)
            hit = key == truth
            scores[(h, by_site, site_first)][part]["n"] += 1
            scores[(h, by_site, site_first)][part]["hit"] += int(hit)
            if (h, by_site, site_first) == SHIPPED:
                if part == "held":
                    sources[source] += 1
                    misses[source] += int(not hit)
                alt_key, _alt = site_first_rule(front=front, recent=recents[keys], pref=pref, measured=measured)
                alt[part]["n"] += 1
                alt[part]["hit"] += int(alt_key == truth)
                for v in RECENT_GRID:
                    recent = W.last_in_front(store, list(keys), now=now, min_s=v[0], active_only=v[1],
                                             page_only=v[2]) if keys else None
                    if v == RECENT_GRID[0]:
                        assert recent == recents[keys], "the shipped recent step drifted"
                    key_v, _src = W.browser_rule(named=None, said=False, front=front, recent=recent, pref=pref,
                                                 measured=measured, site_first=site_first)
                    rec[v][part]["n"] += 1
                    rec[v][part]["hit"] += int(key_v == truth)
    ms = round((time.perf_counter() - t0) * 1000.0)

    def share(c: Counter) -> float | None:
        return round(c["hit"] / c["n"], 4) if c["n"] else None

    best = max(GRID, key=lambda g: (share(scores[g]["choose"]) or 0.0, -GRID.index(g)))
    table = [{"min_hosts": g[0], "by_site": g[1], "site_first": g[2],
              "choose": [scores[g]["choose"]["hit"], scores[g]["choose"]["n"], share(scores[g]["choose"])],
              "held": [scores[g]["held"]["hit"], scores[g]["held"]["n"], share(scores[g]["held"])]} for g in GRID]
    held_best = scores[best]["held"]
    held_shipped = scores[SHIPPED]["held"]
    rbest = max(RECENT_GRID, key=lambda v: (share(rec[v]["choose"]) or 0.0, -RECENT_GRID.index(v)))
    recent_table = [{"min_s": v[0], "active_only": v[1], "page_only": v[2],
                     "choose": [rec[v]["choose"]["hit"], rec[v]["choose"]["n"], share(rec[v]["choose"])],
                     "held": [rec[v]["held"]["hit"], rec[v]["held"]["n"], share(rec[v]["held"])]} for v in RECENT_GRID]
    return {
        "sessions": len(found), "since": dt.datetime.fromtimestamp(since).strftime("%Y-%m-%d"),
        "cut": dt.datetime.fromtimestamp(cut).strftime("%Y-%m-%d %H:%M"),
        "choose_n": scores[best]["choose"]["n"], "held_n": held_best["n"],
        "chosen": {"min_hosts": best[0], "by_site": best[1], "site_first": best[2],
                   "choose_share": share(scores[best]["choose"]), "held_share": share(held_best),
                   "held_hits": held_best["hit"],
                   "margin_sessions": held_best["hit"] - math.ceil(BAR * held_best["n"])},
        "shipped": {"min_hosts": SHIPPED[0], "by_site": SHIPPED[1], "site_first": SHIPPED[2],
                    "held_share": share(held_shipped), "held_hits": held_shipped["hit"],
                    "margin_sessions": held_shipped["hit"] - math.ceil(BAR * held_shipped["n"]),
                    "by_source": {k: [v, misses[k]] for k, v in sorted(sources.items())}},
        "not_shipped_site_first_order": {"choose_share": share(alt["choose"]), "held_share": share(alt["held"]),
                                         "held_hits": alt["held"]["hit"]},
        "recent_step": {"chosen": {"min_s": rbest[0], "active_only": rbest[1], "page_only": rbest[2],
                                   "choose_share": share(rec[rbest]["choose"]), "held_share": share(rec[rbest]["held"]),
                                   "held_hits": rec[rbest]["held"]["hit"],
                                   "margin_sessions": rec[rbest]["held"]["hit"]
                                   - math.ceil(BAR * rec[rbest]["held"]["n"])},
                        "best_held_any": max((r["held"][2] or 0.0) for r in recent_table),
                        "grid": recent_table},
        "gap_min": round(gap_s / 60.0, 1), "bar": BAR, "grid": table, "ms": ms,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--src", type=Path, default=None, help="the database to copy (default: the live one, read-only)")
    ap.add_argument("--since", default="2026-09-24", help="first session day (YYYY-MM-DD)")
    ap.add_argument("--cut", default=None, help="the split day (YYYY-MM-DD; default: the median session start)")
    ap.add_argument("--gap", type=float, default=SESSION_GAP_S / 60.0,
                    help="minutes with no YouTube row that start a new session (default 30)")
    ap.add_argument("--json", action="store_true", help="print the whole result as JSON")
    a = ap.parse_args(argv)
    from yuki.eval.dbcopy import copy_db, open_copy, remove_copy

    path = copy_db(a.src)
    store = open_copy(path)
    try:
        out = replay(store, since=_ts(a.since), cut=_ts(a.cut) if a.cut else None, gap_s=float(a.gap) * 60.0)
    finally:
        try:
            store.close()
        finally:
            remove_copy(path)
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    if not out.get("sessions"):
        print("no YouTube sessions on the copy")
        return 0
    c, s = out["chosen"], out["shipped"]
    print(f"YouTube sessions (a {out['gap_min']} min gap) since {out['since']}: {out['sessions']}; cut {out['cut']}: "
          f"choose {out['choose_n']}, held out {out['held_n']}")
    print(f"chosen on the first half: min_hosts={c['min_hosts']} by_site={c['by_site']} site_first={c['site_first']} "
          f"({c['choose_share']:.1%} there) -> held out {c['held_share']:.1%} ({c['held_hits']}/{out['held_n']}, "
          f"{c['margin_sessions']:+d} sessions vs the {out['bar']:.0%} bar)")
    print(f"shipped definition: held out {s['held_share']:.1%} ({s['held_hits']}/{out['held_n']}, "
          f"{s['margin_sessions']:+d} vs the bar); by source [n, wrong]: {s['by_source']}")
    alt = out["not_shipped_site_first_order"]
    print(f"not shipped, for comparison: front > YouTube's own browser > recent > usual: choose "
          f"{alt['choose_share']:.1%}, held out {alt['held_share']:.1%} ({alt['held_hits']}/{out['held_n']})")
    for row in out["grid"]:
        print(f"  hosts>={row['min_hosts']} by_site={row['by_site']!s:5} site_first={row['site_first']!s:5} "
              f"choose {row['choose'][2]:.1%}  held {row['held'][2]:.1%}")
    r = out["recent_step"]
    rc = r["chosen"]
    print(f"recent-step refinements (shipped definition), chosen on the first half: min_s={rc['min_s']} "
          f"active_only={rc['active_only']} page_only={rc['page_only']} ({rc['choose_share']:.1%} there) -> held out "
          f"{rc['held_share']:.1%} ({rc['held_hits']}/{out['held_n']}, {rc['margin_sessions']:+d} vs the bar); "
          f"the best held-out share of any refinement (not a fair pick: chosen on the held-out half) "
          f"{r['best_held_any']:.1%}")
    for row in r["grid"]:
        print(f"  min_s={row['min_s']:>3} active_only={row['active_only']!s:5} page_only={row['page_only']!s:5} "
              f"choose {row['choose'][0]}/{row['choose'][1]}  held {row['held'][0]}/{row['held'][1]}")
    print(f"replay {out['ms']} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
