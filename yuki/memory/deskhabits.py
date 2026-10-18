"""Desk habits: where the user keeps each app, learned from the desk record (phase 3.3, contract D1).

The timeline's desk record (:class:`yuki.memory.timeline.DeskRecord`, table ``desk_stretches``, migration 48) holds
one row per stretch of the desk staying the same: for each window on a monitor its app key, monitor index, how it
shows (``max`` maximised, ``full`` full screen, ``win`` a plain window), whether a media session plays in it, and the
seconds it was in front and mostly visible. Content-free: app keys, numbers and code words; never a title.

Nightly, :func:`mine` reads the last :data:`LOOKBACK_DAYS` of the user's own present time (not Yuki acting, not a live
test, not away; the monitor setup he spent most present time on lately) and counts **occasions**: a run of desk
stretches no more than :data:`OCCASION_GAP_S` apart in which the thing holds. Two kinds of habit, each with its counts:

* ``keep``: app X sits on monitor N (maximised or not) in ``n`` of the ``m`` occasions X was on screen at all;
* ``while``: while app P plays (a media session in a visible window) or shows full screen on monitor A, app X sits on
  monitor B in ``n`` of the ``m`` occasions both were on screen. Kept only when the condition makes the difference:
  X's share on B beats its share there with nothing playing or full screen on A, on the same days, by
  :data:`WHILE_GAIN` (over at least :data:`MIN_N` such occasions), and chance would rarely give the difference (a
  one-sided Fisher exact test, held to a false-discovery rate of :data:`WHILE_FDR` over all the pairs tested that
  night). Occasions that split their time evenly between monitors count for neither.

A habit is kept only above the bar: at least :data:`MIN_N` occasions on at least :data:`MIN_DAYS` days, and a share of
at least :data:`MIN_SHARE`; and the newest :data:`RECENT_DAYS` must not disagree (after a rearrangement the habit is the
newest days' own, or none). Measured co-occurrence counts on stored app keys only: no word lists, nothing authored. An
app mostly seen full screen in front (a game, an F11 video) gets no ``keep`` habit: nothing ever places it.

The result is one small JSON in ``nudge_state`` (:data:`STATE_KEY`); :func:`lines` renders the strongest habits in
plain words for the cards, and :func:`place_for` answers "where does he keep this app, given what is on the desk now"
for the skills engine's place default (``yuki.skills.engine.place_for``). No model call, no OS read here (the caller
passes the situation). Logs are content-free.

Public API (contract D1)::

    mine(store, *, now) -> dict                                   # nightly; counts
    lines(store, *, purpose, max_chars) -> list[str]              # "when Chrome plays on monitor 0, he keeps ..."
    place_for(store, app, *, situation=None) -> dict | None       # {"monitor", "how", "when", "n", "m", "busy", ...}
"""

from __future__ import annotations

import json
import math
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from typing import Any

#: Where the mined habits live (``nudge_state``; JSON, content-free).
STATE_KEY = "desk:habits"
#: The desk record's table (migration 48).
TABLE = "desk_stretches"
#: How far back the miner reads (days), and when old desk rows are deleted (days).
LOOKBACK_DAYS = 28
PRUNE_DAYS = 60
#: Desk stretches closer than this (s) belong to one occasion.
OCCASION_GAP_S = 300.0
#: An occasion shorter than this (s, the stretches' own time) is not counted.
OCCASION_MIN_S = 60.0
#: The bar: occasions, distinct days among them, and share.
MIN_N = 5
MIN_DAYS = 3
MIN_SHARE = 0.7
#: Recency: when the newest RECENT_DAYS hold at least RECENT_MIN of an app's occasions, at least RECENT_AGREE of them
#: must sit on the habit's monitor (else he has rearranged: the habit is the newest days' own, or none).
RECENT_DAYS = 7
RECENT_MIN = 2
RECENT_AGREE = 0.5
#: A ``while`` habit is kept only when the app's share on that monitor under the condition beats its share there
#: with nothing playing or full screen on the condition's monitor, on the same days, by this much (it then says
#: something the plain habit does not) ...
WHILE_GAIN = 0.15
#: ... and not by chance: its one-sided Fisher exact test (n of m with the condition against k of m without) passes
#: the Benjamini-Hochberg cut at this false-discovery rate over every (condition, app) pair tested that night.
WHILE_FDR = 0.05
#: A window counts as seen (for "plays in a visible window") when it was mostly visible this share of its stretch.
SEEN_SHARE = 0.5
#: Habits kept in the state (strongest first).
MAX_HABITS = 24
#: Lines per purpose (the talker's card is small).
MAX_LINES = {"talker": 3}
DEFAULT_LINES = 6


# ---------------------------------------------------------------------------
# Rows (pure)
# ---------------------------------------------------------------------------

@dataclass
class DeskRow:
    """One desk stretch as the miner reads it: ``windows`` = ``[(app, monitor, how, play, front_s, seen_s), ...]``."""

    started_at: float
    ended_at: float
    screens: str
    windows: list[tuple[str, int, str, bool, float, float]] = field(default_factory=list)
    away: bool = False
    by_yuki: bool = False

    @property
    def seconds(self) -> float:
        return max(0.0, self.ended_at - self.started_at)


def parse_windows(text: Any) -> list[tuple[str, int, str, bool, float, float]]:
    """The ``windows`` column (JSON ``[[app, monitor, how, play, front_s, seen_s], ...]``) read leniently."""
    try:
        raw = json.loads(text) if isinstance(text, str) else (text or [])
    except (TypeError, ValueError):
        return []
    out: list[tuple[str, int, str, bool, float, float]] = []
    for item in raw if isinstance(raw, list) else []:
        try:
            app, mon, how = str(item[0]), int(item[1]), str(item[2])
            play = bool(item[3]) if len(item) > 3 else False
            front = float(item[4]) if len(item) > 4 else 0.0
            seen = float(item[5]) if len(item) > 5 else 0.0
        except (TypeError, ValueError, IndexError):
            continue
        if app:
            out.append((app, mon, how, play, front, seen))
    return out


def _day(at: float) -> str:
    """The local day of ``at`` (``YYYY-MM-DD``), cached per quarter hour (every UTC offset is a multiple of 15 min, so
    local midnight never falls inside a bucket)."""
    return _quarter_day(int(at // 900))


@lru_cache(maxsize=8192)
def _quarter_day(quarter: int) -> str:
    return datetime.fromtimestamp(quarter * 900).strftime("%Y-%m-%d")


def _sits(row: DeskRow) -> dict[str, dict[int, str]]:
    """Per app: the monitors it has a window on in this row, with how (``max`` wins over ``win`` on one monitor;
    ``full`` over both)."""
    rank = {"full": 2, "max": 1, "win": 0}
    out: dict[str, dict[int, str]] = defaultdict(dict)
    for app, mon, how, _play, _front, _seen in row.windows:
        have = out[app].get(mon)
        if have is None or rank.get(how, 0) > rank.get(have, 0):
            out[app][mon] = how
    return out


def _conditions(row: DeskRow) -> set[tuple[str, str, int]]:
    """What holds the desk in this row, as ``(app, state, monitor)``: ``plays`` (a media session in a window that was
    mostly visible) and ``full`` (a full-screen window)."""
    out: set[tuple[str, str, int]] = set()
    secs = row.seconds or 1.0
    for app, mon, how, play, _front, seen in row.windows:
        if how == "full":
            out.add((app, "full", mon))
        if play and seen >= SEEN_SHARE * secs:
            out.add((app, "plays", mon))
    return out


class _Occasion:
    """One run of an app's stretches: when it started (and the local day), its seconds, and per monitor the seconds
    it sat there, maximised there, full screen there. Read with :meth:`where` once complete."""

    __slots__ = ("start", "day", "seconds", "on", "maxed", "full", "_where")

    def __init__(self, start: float) -> None:
        self.start = start
        self.day = _day(start)
        self.seconds = 0.0
        self.on: dict[int, float] = {}
        self.maxed: dict[int, float] = {}
        self.full: dict[int, float] = {}
        self._where: tuple[int, bool, bool] | None | bool = False

    def where(self) -> tuple[int, bool, bool] | None:
        """``(monitor it sat on most, mostly maximised there, mostly full screen there)``; ``None`` when it split its
        time evenly (a window on each monitor says nothing about where it sits; never broken towards monitor 0);
        cached."""
        if self._where is False:
            ranked = sorted(self.on.items(), key=lambda kv: -kv[1])
            if not ranked or (len(ranked) > 1 and ranked[1][1] >= ranked[0][1] - 1e-9):
                self._where = None
            else:
                mon, secs = ranked[0][0], ranked[0][1] or 1.0
                self._where = (mon, self.maxed.get(mon, 0.0) >= 0.5 * secs, self.full.get(mon, 0.0) >= 0.5 * secs)
        return self._where


#: A desk row with its per-app sits read once (:func:`_sits`): the miner's unit.
_Item = tuple[DeskRow, dict[str, dict[int, str]]]


def _occasions(items: Sequence[_Item], apps: set[str] | None = None) -> dict[str, list[_Occasion]]:
    """Per app (only ``apps`` when given): its occasions over ``items`` (already filtered to the stretches that
    count, oldest first), in one pass over the rows."""
    out: dict[str, list[_Occasion]] = defaultdict(list)
    last_end: dict[str, float] = {}
    for row, sits in items:
        secs = row.seconds
        for app, mons in sits.items():
            if not mons or (apps is not None and app not in apps):
                continue
            occs = out[app]
            if not occs or row.started_at - last_end[app] > OCCASION_GAP_S:
                occs.append(_Occasion(start=row.started_at))
            occ = occs[-1]
            occ.seconds += secs
            share = secs / len(mons)
            for mon, how in mons.items():
                occ.on[mon] = occ.on.get(mon, 0.0) + share
                if how == "max":
                    occ.maxed[mon] = occ.maxed.get(mon, 0.0) + share
                elif how == "full":
                    occ.full[mon] = occ.full.get(mon, 0.0) + share
            last_end[app] = row.ended_at
    return {app: [o for o in occs if o.seconds >= OCCASION_MIN_S] for app, occs in out.items()}


def _bar(wheres: Sequence[tuple[_Occasion, tuple[int, bool, bool]]]) -> dict[str, Any] | None:
    """The modal monitor over occasions with its counts, or None below the bar."""
    m = len(wheres)
    if m < MIN_N:
        return None
    per = Counter(w[0] for _o, w in wheres)
    mon, n = max(sorted(per.items()), key=lambda kv: kv[1])
    support = [(o, w) for o, w in wheres if w[0] == mon]
    days = {o.day for o, _w in support}
    share = n / m
    if n < MIN_N or len(days) < MIN_DAYS or share < MIN_SHARE:
        return None
    maxed = sum(1 for _o, w in support if w[1])
    full = sum(1 for _o, w in support if w[2])
    how = "full" if full >= 0.5 * n else ("max" if maxed >= MIN_SHARE * n else "keep")
    return {"monitor": mon, "how": how, "n": n, "m": m, "days": len(days), "share": round(share, 3)}


def _verdict(occs: Sequence[_Occasion], *, now: float, counts: Counter | None = None) -> dict[str, Any] | None:
    """The habit over ``occs`` (:func:`_bar`), unless the newest :data:`RECENT_DAYS` disagree: with at least
    :data:`RECENT_MIN` occasions there and fewer than :data:`RECENT_AGREE` of them on the modal monitor, he has
    rearranged; the habit is then the newest days' own (which must pass the bar alone), else none."""
    wheres = [(o, w) for o in occs if (w := o.where()) is not None]
    v = _bar(wheres)
    recent = [(o, w) for o, w in wheres if o.start >= now - RECENT_DAYS * 86400.0]
    if len(recent) < RECENT_MIN:
        return v
    if v is not None and sum(1 for _o, w in recent if w[0] == v["monitor"]) >= RECENT_AGREE * len(recent):
        return v
    if v is not None and counts is not None:
        counts["stale"] += 1
    return _bar(recent)


def _per_day(occs: Sequence[_Occasion]) -> dict[str, Counter]:
    """Per local day: how many of the app's occasions sat on each monitor."""
    out: dict[str, Counter] = defaultdict(Counter)
    for o in occs:
        w = o.where()
        if w is not None:
            out[o.day][w[0]] += 1
    return out


def _on(per_day: Mapping[str, Counter], days: Iterable[str], monitor: int) -> tuple[int, int]:
    """``(k, m)``: on ``days``, of ``m`` occasions ``k`` had the app on ``monitor``."""
    k = m = 0
    for day in days:
        got = per_day.get(day)
        if got:
            k += got.get(monitor, 0)
            m += sum(got.values())
    return k, m


def _fisher(n: int, m: int, k0: int, m0: int) -> float:
    """One-sided Fisher exact test: the chance that ``n`` or more of the ``m`` occasions with the condition sit on the
    monitor, when ``k0`` of the ``m0`` without it do and the condition makes no difference (both counts measured, so a
    thin "without" side widens it). Log space: large counts never overflow."""
    total, on = m + m0, n + k0

    def log_c(a: int, b: int) -> float:
        return math.lgamma(a + 1) - math.lgamma(b + 1) - math.lgamma(a - b + 1)

    whole = log_c(total, m)
    p = sum(math.exp(log_c(on, x) + log_c(total - on, m - x) - whole) for x in range(n, min(m, on) + 1))
    return min(1.0, p)


def _setup(rows: Sequence[DeskRow], now: float) -> str:
    """The monitor setup to mine: the one with the most present (not away) seconds in the newest
    :data:`RECENT_DAYS` (else the whole window), so a last stretch on a passing setup (a monitor off, a TV on) never
    decides the month."""
    for since in (now - RECENT_DAYS * 86400.0, float("-inf")):
        secs: Counter = Counter()
        last: dict[str, float] = {}
        for r in rows:
            if r.away or not r.screens or r.started_at < since:
                continue
            secs[r.screens] += r.seconds
            last[r.screens] = r.started_at
        if secs:
            return max(secs, key=lambda s: (secs[s], last[s]))
    return rows[-1].screens if rows else ""


def mine_rows(rows: Iterable[DeskRow], *, now: float, screens: str | None = None,
              skip: Sequence[tuple[float, float]] = ()) -> dict[str, Any]:
    """Habits from desk rows (pure). ``screens``: the monitor setup to mine (default :func:`_setup`); ``skip``: time
    spans to leave out (the live test's). Returns ``{"screens", "habits": [...], "counts": {...}}``."""
    since = now - LOOKBACK_DAYS * 86400.0
    all_rows = sorted((r for r in rows if r.ended_at > since and r.started_at <= now), key=lambda r: r.started_at)
    counts: Counter = Counter(rows_read=len(all_rows))
    if screens is None:
        screens = _setup(all_rows, now)
    use: list[DeskRow] = []
    for r in all_rows:
        if r.screens != screens:
            counts["other_setup"] += 1
        elif r.away:
            counts["away"] += 1
        elif r.by_yuki:
            counts["by_yuki"] += 1
        elif any(r.started_at <= b and r.ended_at >= a for a, b in skip):
            counts["test"] += 1
        else:
            use.append(r)
    counts["rows_used"] = len(use)
    items: list[_Item] = [(r, _sits(r)) for r in use]
    day_of = [_day(r.started_at) for r in use]
    counts["days"] = len(set(day_of))
    app_days: dict[str, set[str]] = defaultdict(set)
    for (_r, sits), day in zip(items, day_of):
        for app in sits:
            app_days[app].add(day)
    counts["apps"] = len(app_days)
    apps = {app for app, days in app_days.items() if len(days) >= MIN_DAYS}
    occs = _occasions(items, apps)
    habits: list[dict[str, Any]] = []
    fullish: set[str] = set()
    for app in sorted(apps):
        v = _verdict(occs.get(app, []), now=now, counts=counts)
        if v is None:
            continue
        if v["how"] == "full":
            fullish.add(app)   # a game or an F11 video: nothing places it
            counts["full_apps"] += 1
            continue
        habits.append({"kind": "keep", "app": app, **v})
    # while P plays / is full screen on A, X sits on B: only when it beats where X sits with A free on the same days
    # (lift), and not by chance (Fisher exact, Benjamini-Hochberg over the night's pairs)
    by_cond: dict[tuple[str, str, int], list[int]] = defaultdict(list)
    busy: list[set[int]] = []
    for i, r in enumerate(use):
        conds = _conditions(r)
        busy.append({c[2] for c in conds})
        for cond in conds:
            by_cond[cond].append(i)
    # the comparison, per monitor: the occasions with nothing playing or full screen there (another player on that
    # monitor would blur it), built once and cut to each condition's days
    placeable = apps - fullish
    free: dict[int, dict[str, dict[str, Counter]]] = {}
    tested: list[tuple[float, dict[str, Any]]] = []
    for cond, idx in sorted(by_cond.items()):
        days = {day_of[i] for i in idx}
        if len(idx) < MIN_N or len(days) < MIN_DAYS:
            continue
        sub_days: dict[str, set[str]] = defaultdict(set)
        for i in idx:
            for app in items[i][1]:
                sub_days[app].add(day_of[i])
        present = {app for app, d in sub_days.items() if len(d) >= MIN_DAYS and app != cond[0] and app in placeable}
        if not present:
            continue
        with_occs = _occasions([items[i] for i in idx], present)
        if cond[2] not in free:
            free[cond[2]] = {app: _per_day(o) for app, o in _occasions(
                [it for it, b in zip(items, busy) if cond[2] not in b], placeable).items()}
        for app in sorted(present):
            v = _verdict(with_occs.get(app, []), now=now)
            if v is None or v["how"] == "full":
                continue
            k0, m0 = _on(free[cond[2]].get(app, {}), days, v["monitor"])
            if m0 < MIN_N:
                counts["while_thin"] += 1      # too little of the same days without it to compare with
                continue
            base = k0 / m0
            tested.append((_fisher(v["n"], v["m"], k0, m0),
                           {"kind": "while", "app": app, "player": cond[0], "state": cond[1], "at": cond[2], **v,
                            "without": round(base, 3), "m_without": m0}))
    # Benjamini-Hochberg over every pair tested: the largest rank whose p fits under rank / N * WHILE_FDR, and all
    # stronger ones; then the gain must be real too (the condition changes where it sits, not just how sure we are)
    tested.sort(key=lambda t: t[0])
    cut = max((i for i, (p, _h) in enumerate(tested, 1) if p <= i / len(tested) * WHILE_FDR), default=0)
    counts["while_tested"] = len(tested)
    counts["while_chance"] = len(tested) - cut
    for _p, h in tested[:cut]:
        if h["share"] - h["without"] < WHILE_GAIN:
            counts["while_no_lift"] += 1   # he keeps it there anyway: the condition adds nothing
            continue
        habits.append(h)
    habits.sort(key=lambda h: (-h["n"], -h["share"], h["kind"], h["app"]))
    counts["keep"] = sum(1 for h in habits if h["kind"] == "keep")
    counts["while"] = sum(1 for h in habits if h["kind"] == "while")
    return {"screens": screens, "habits": habits[:MAX_HABITS], "counts": dict(counts)}


# ---------------------------------------------------------------------------
# Store (nightly mine, reads)
# ---------------------------------------------------------------------------

def _rows(store: Any, since: float) -> list[DeskRow]:
    got = store._query(f"SELECT started_at, ended_at, screens, windows, away, by_yuki FROM {TABLE}"
                       " WHERE ended_at >= ? ORDER BY started_at", (since,))
    return [DeskRow(started_at=float(r["started_at"]), ended_at=float(r["ended_at"]), screens=str(r["screens"] or ""),
                    windows=parse_windows(r["windows"]), away=bool(r["away"]), by_yuki=bool(r["by_yuki"]))
            for r in got]


def _names(store: Any, keys: Iterable[str]) -> dict[str, str]:
    """App key -> the display name the timeline last stored for it (the app's own name; the key when none)."""
    out: dict[str, str] = {}
    for key in sorted(set(keys)):
        try:
            row = store._query("SELECT app FROM timeline WHERE process=? AND coalesce(app, '') != ''"
                               " ORDER BY started_at DESC LIMIT 1", (key,))
            out[key] = str(row[0]["app"]) if row else key
        except Exception:  # noqa: BLE001
            out[key] = key
    return out


def _test_spans(store: Any) -> list[tuple[float, float]]:
    try:
        from yuki.memory.testtag import test_spans

        with store._lock:
            return list(test_spans(store._conn))
    except Exception:  # noqa: BLE001 - no test sessions known: nothing skipped
        return []


def mine(store: Any, *, now: float | None = None) -> dict[str, Any]:
    """The nightly pass: mine the desk record into habits (``nudge_state`` :data:`STATE_KEY`) and delete desk rows
    older than :data:`PRUNE_DAYS`. Returns content-free counts; never raises (``{"error": ...}`` instead)."""
    now = time.time() if now is None else float(now)
    try:
        rows = _rows(store, now - LOOKBACK_DAYS * 86400.0)
    except Exception as exc:  # noqa: BLE001 - no table yet (migration 48 not run)
        return {"error": type(exc).__name__}
    result = mine_rows(rows, now=now, skip=_test_spans(store))
    keys = {h["app"] for h in result["habits"]} | {h["player"] for h in result["habits"] if h.get("player")}
    result["names"] = _names(store, keys)
    result["mined_at"] = now
    counts = dict(result["counts"])
    try:
        store.set_nudge_state(STATE_KEY, json.dumps(result, separators=(",", ":")))
    except Exception as exc:  # noqa: BLE001
        counts["error"] = type(exc).__name__
    try:
        cut = now - PRUNE_DAYS * 86400.0
        counts["pruned"] = int(store._write(lambda conn: conn.execute(
            f"DELETE FROM {TABLE} WHERE ended_at < ?", (cut,)).rowcount) or 0)
    except Exception:  # noqa: BLE001
        pass
    return counts


def load(store: Any) -> dict[str, Any]:
    """The mined habits (``{}`` before the first night)."""
    try:
        text = store.nudge_state(STATE_KEY)
        got = json.loads(text) if text else {}
        return got if isinstance(got, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


# ---------------------------------------------------------------------------
# Words (pure)
# ---------------------------------------------------------------------------

def _mon(index: Any) -> str:
    return f"monitor {index}"


def _counts(h: Mapping[str, Any]) -> str:
    return f"{h['n']} of {h['m']}"


def _cond_words(h: Mapping[str, Any], names: Mapping[str, str]) -> str:
    player = names.get(h["player"], h["player"])
    if h.get("state") == "full":
        return f"when {player} is full screen on {_mon(h['at'])}"
    return f"when {player} plays on {_mon(h['at'])}"


def _app_words(h: Mapping[str, Any], names: Mapping[str, str]) -> str:
    return f"{names.get(h['app'], h['app'])}{' maximised' if h.get('how') == 'max' else ''} ({_counts(h)})"


def _and(parts: Sequence[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def render(state: Mapping[str, Any], *, purpose: str = "card", max_chars: int = 600) -> list[str]:
    """The habits in plain words, whole lines within ``max_chars`` in total. The situational ones lead (they connect
    what is on the desk with where he keeps things), one line per condition and monitor: "when Google Chrome plays on
    monitor 0, he keeps Discord (9 of 11) and WhatsApp (7 of 8) on monitor 1"; then one line per monitor for the
    plain ones: "on monitor 1 he keeps T3 Code (Alpha) maximised (11 of 11) and WhatsApp (22 of 22)". Strongest
    first within each."""
    habits = [h for h in state.get("habits") or [] if isinstance(h, Mapping)]
    names = state.get("names") or {}
    if not habits:
        return []
    groups: dict[tuple, list[Mapping[str, Any]]] = defaultdict(list)
    for h in sorted(habits, key=lambda h: (-int(h.get("n") or 0), -float(h.get("share") or 0.0))):
        if h.get("kind") == "while":
            groups[("while", h.get("player"), h.get("state"), h.get("at"), h.get("monitor"))].append(h)
        else:
            groups[("keep", h.get("monitor"))].append(h)
    order = sorted(groups, key=lambda k: (k[0] != "while", -sum(int(h.get("n") or 0) for h in groups[k])))
    out: list[str] = []
    used = 0
    limit = MAX_LINES.get(purpose, DEFAULT_LINES)
    for key in order:
        hs = groups[key]
        said = _and([_app_words(h, names) for h in hs])
        if key[0] == "keep":
            line = f"on {_mon(key[1])} he keeps {said}"
        else:
            line = f"{_cond_words(hs[0], names)}, he keeps {said} on {_mon(hs[0]['monitor'])}"
        cost = len(line) + (2 if out else 0)
        if used + cost > max_chars:
            continue
        out.append(line)
        used += cost
        if len(out) >= limit:
            break
    return out


def lines(store: Any, *, purpose: str = "card", max_chars: int = 600) -> list[str]:
    """Contract D1: the mined habits as plain lines for a card (``[]`` before the first night or with none)."""
    try:
        return render(load(store), purpose=purpose, max_chars=max_chars)
    except Exception:  # noqa: BLE001
        return []


# ---------------------------------------------------------------------------
# Placement (pure decision over the mined state and the desk now)
# ---------------------------------------------------------------------------

def _key(name: Any) -> str:
    part = str(name or "").strip().casefold()
    return part[:-4].rstrip() if part.endswith(".exe") else part


def screens_key(monitors: Iterable[Any]) -> str:
    """A monitor setup as text (``"1920x1080@0,0;2560x1440@1920,0"``, in ``list_monitors`` order): the desk record
    and the habits mined from it carry it, so a habit is never applied to another setup."""
    parts: list[str] = []
    for m in monitors or []:
        rect = m.get("rect") if isinstance(m, Mapping) else getattr(m, "rect", m)
        try:
            left, top, right, bottom = (int(v) for v in rect)
        except (TypeError, ValueError):
            continue
        parts.append(f"{right - left}x{bottom - top}@{left},{top}")
    return ";".join(parts)


def desk_now(situation: Any) -> dict[str, Any]:
    """What holds the desk now, from a :class:`yuki.perception.situation.DeskState` (or its dict): ``{"screens",
    "plays": {(app, monitor)}, "full": {(app, monitor)}}``. A playing session not tied to one window counts on the
    monitor all its app's shown windows share (else nowhere)."""
    if situation is None:
        return {"screens": None, "plays": set(), "full": set()}
    get = situation.get if hasattr(situation, "get") else (lambda k, d=None: getattr(situation, k, d))
    monitors = get("monitors") or []
    windows = [w for w in get("windows") or [] if (w.get("kind") if hasattr(w, "get") else "window") == "window"]
    plays: set[tuple[str, int]] = set()
    full: set[tuple[str, int]] = set()
    for w in windows:
        g = w.get if hasattr(w, "get") else (lambda k, d=None, _w=w: getattr(_w, k, d))
        mon = g("monitor")
        if mon is None or g("minimized"):
            continue
        if g("playing"):
            plays.add((_key(g("app")), int(mon)))
        if g("fullscreen"):
            full.add((_key(g("app")), int(mon)))
    for m in get("media") or []:
        g = m.get if hasattr(m, "get") else (lambda k, d=None, _m=m: getattr(_m, k, d))
        if not g("playing") or g("hwnd"):
            continue
        key = _key(g("app"))
        mons = set()
        for w in windows:
            wg = w.get if hasattr(w, "get") else (lambda k, d=None, _w=w: getattr(_w, k, d))
            if _key(wg("app")) == key and wg("monitor") is not None and not wg("minimized"):
                mons.add(int(wg("monitor")))
        if len(mons) == 1:
            plays.add((key, mons.pop()))
    return {"screens": screens_key(monitors) if monitors else None, "plays": plays, "full": full}


def _resolve(state: Mapping[str, Any], app: str) -> str:
    """The app key the habits know ``app`` by: its own key (``"Discord.exe"`` -> ``discord``), else the key whose
    display name it is (``"T3 Code (Alpha)"``)."""
    key = _key(app)
    if not key or any(isinstance(h, Mapping) and h.get("app") == key for h in state.get("habits") or []):
        return key
    return {_key(v): k for k, v in (state.get("names") or {}).items()}.get(key, key)


def pick(state: Mapping[str, Any], app: str, desk: Mapping[str, Any] | None) -> dict[str, Any] | None:
    """The user's habit for ``app`` on the desk now (pure): the strongest ``while`` habit whose condition holds now,
    else the app's ``keep`` habit; ``None`` with no habit, or when the setup differs from the one mined. The answer
    also says ``busy``: the monitors where another app plays or shows full screen now (a window coming forward there
    covers it)."""
    key = _resolve(state, app)
    names = state.get("names") or {}
    mine = [h for h in state.get("habits") or [] if isinstance(h, Mapping) and h.get("app") == key]
    if not mine:
        return None
    desk = desk or {}
    if desk.get("screens") and state.get("screens") and desk["screens"] != state["screens"]:
        return None
    plays, full = set(desk.get("plays") or ()), set(desk.get("full") or ())
    held = [h for h in mine if h.get("kind") == "while"
            and (h.get("player"), int(h.get("at", -1))) in (full if h.get("state") == "full" else plays)]
    best = max(held, key=lambda h: (h["share"], h["n"])) if held else next(
        (h for h in mine if h.get("kind") == "keep"), None)
    if best is None:
        return None
    busy = sorted({mon for app_key, mon in plays | full if app_key != key})
    line = render({"habits": [best], "names": names}, max_chars=400)
    return {"monitor": int(best["monitor"]), "how": "maximize" if best.get("how") == "max" else "keep",
            "when": best["kind"], "n": best["n"], "m": best["m"], "busy": busy,
            "covers": int(best["at"]) if best["kind"] == "while" else None, "line": line[0] if line else ""}


def place_for(store: Any, app: str, *, situation: Any = None) -> dict[str, Any] | None:
    """Contract D1: the user's habitual monitor and how for ``app`` given the desk now (``situation``: a
    ``DeskState``, its dict, or a callable returning one, read only when the app has a habit), or ``None`` (no
    habit). ``{"monitor": index, "how": "maximize" | "keep", "when": "while" | "keep", "n", "m", "busy", "covers",
    "line"}``. Deciding whether to move a window is the caller's (the engine moves only a window that would cover
    what plays, :func:`yuki.skills.engine.place_for`)."""
    try:
        state = load(store)
        key = _resolve(state, app)
        if not key or not any(isinstance(h, Mapping) and h.get("app") == key for h in state.get("habits") or []):
            return None
        if callable(situation):
            situation = situation()
        return pick(state, app, desk_now(situation))
    except Exception:  # noqa: BLE001 - no habit known
        return None


__all__ = ["STATE_KEY", "TABLE", "LOOKBACK_DAYS", "PRUNE_DAYS", "OCCASION_GAP_S", "OCCASION_MIN_S", "MIN_N",
           "MIN_DAYS", "MIN_SHARE", "RECENT_DAYS", "RECENT_MIN", "RECENT_AGREE", "WHILE_GAIN", "WHILE_FDR",
           "SEEN_SHARE", "DeskRow", "parse_windows", "mine_rows", "mine", "load", "render", "lines", "screens_key",
           "desk_now", "pick", "place_for"]
