"""Occasions: one user state change plus the visit that led to it (phase L design §3; D21, D22).

An **occasion** ends on a moment by the user of an end kind (``yuki.skills.connection.SERVE_ENDS``: a call joined, a
page in a browser, a message sent, a playlist started, a level moved; never a glance at a calling site, a new media
title or a game's voice chat). The visit is the timeline stretch of that app that holds the end; the steps are the
trace's identity tokens in that app from the anchor to the end; the context is the end's own weekday and minute,
whether a game was in front, whether a call was open, and the id of a request to Yuki in the 10 minutes before.

Segmentation (design §3.2; rebuildable, code only):

1. **Visits**: consecutive ``present`` timeline rows of one program with gaps of at most :data:`VISIT_GAP_S`.
2. **The visit of an end**: the app's visit holding the end (within :data:`VISIT_TOLERANCE_S`), else its latest visit
   that ended at most :data:`VISIT_BEFORE_S` before. None: cover ``gap`` (the end still counts).
3. **Anchor** = max(visit start - :data:`ANCHOR_LEAD_S`, end - :data:`ANCHOR_MAX_S`, the previous occasion's end in
   the same app).
4. **Steps**: the user's own trace rows of that app in [anchor, end + :data:`STEP_TAIL_S`] (no hover, no stale row,
   none during one of Yuki's runs) as identity tokens (``group.step_token``); unresolved clicks as a count only; the
   template pages passed on the way.
5. **Exclusions**: Yuki's own moments; any end inside an acting step of one of Yuki's runs of the same app (``runs``
   kind action / control, from the step's start to its end plus the kind's attribution lag) or inside a skill run's
   window (D18: such a moment is the asked occasion of that run when the run verified, never a user occasion); ends in
   a game's app; ends inside a task the user said was not theirs (kept in :data:`NOT_MINE_STATE`, so the verdicts
   outlive the trace tables); pages outside a browser; untemplated single pages (their host-scope visit stands for
   them).

Rules for the gaps and the live kinds (design §3.3): a message captured more than :data:`LATE_SEND_S` after its own
time label counts for demand and means only (no steps, no time context); level moves of two or more sessions within
:data:`DUCK_S` are one outside event (ducking), never an occasion; a playlist read counts only on a change (never the
first read, never the playlist already playing).

**Asked occasions** (design §4.8, D22): a request whose run reached a checked end with no correction within
:data:`CORRECTION_S` (skill runs: ``checked = 1`` and the re-check never disagreed; older thinker runs: an acting step
with ``verdict = 1`` of a verb in :data:`VERB_ENDS`, not followed by a ``user_corrected`` event on its run or by
another run on the same app with another verb of the same end kind within the minute: steered to another way). They count for demand and route trust only, never
preferences. A run whose step was corrected counts against that verb (``facts.corrected``). A request that was
cancelled or failed and followed within :data:`ASKED_THEN_DID_S` by the user's own occasion in an app it acted in
tags that occasion (``facts.after_ask``), so the request's words can become the skill's "use when".

Everything kept is content-free: kinds, app keys, keyed digests, times, cover words, counts. Values (a page's site,
a service's product name, a playlist) are read in RAM for binding and are written only encrypted, by the writer.
"""

from __future__ import annotations

import bisect
import json
import time
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from yuki.skills.learn.group import step_token

#: The end kinds (``yuki.skills.connection.SERVE_ENDS``).
END_KINDS: tuple[str, ...] = ("call_join", "page", "message_sent", "media_context", "volume_change")
VISIT_GAP_S = 10.0
VISIT_TOLERANCE_S = 5.0
VISIT_BEFORE_S = 120.0
ANCHOR_LEAD_S = 20.0
ANCHOR_MAX_S = 600.0
STEP_TAIL_S = 5.0
#: A message captured later than this after its own time label: demand and means only (critique M4).
LATE_SEND_S = 120.0
#: Level moves of two or more sessions this close together are one outside event (ducking: critique M5).
DUCK_S = 2.0
#: A request to Yuki this long before an end is its context (only the request's id is kept).
TURN_BEFORE_S = 600.0
#: D19's false-done window: a correction this soon after a checked step takes the asked occasion away.
CORRECTION_S = 60.0
ASKED_THEN_DID_S = 600.0
#: The moments' retention (``[moments] keep_days``): a rebuild reads no further back.
KEEP_DAYS = 90.0
#: The ``runs`` step kinds that act on the desktop (D18): reads (looking at a window, the situation) never do.
ACTING_KINDS: tuple[str, ...] = ("action", "control")
#: The closed verb -> end table for older thinker runs (design §4.8): the verbs whose checked step reached an end.
VERB_ENDS: dict[str, str] = {"set_slider": "volume_change", "app_volume": "volume_change", "open_url": "page",
                             "tab_select": "site_front"}
#: The trace's identity-less click kinds (counted, never compiled: CEF and unnamed list items).
OPAQUE_KINDS = frozenset({"click", "dclick", "rclick", "menu", "select", "invoke", "drag"})
#: Where the verdicts of tasks the user said were not theirs are kept (content-free spans and app keys), so they
#: outlive ``ui_tasks`` (phase P drops it): design §7's port of ``ui_tasks.user_verdict`` into tombstones.
NOT_MINE_STATE = "learn:not_mine"
DAY_S = 86400.0


@dataclass(eq=False)
class Occasion:
    """One occasion (a row of ``occasions``, plus what binding and grouping need in RAM)."""

    kind: str
    at: float
    app: str
    by: str = "user"                     # user | asked
    end_moment_id: int | None = None
    counts: bool = True                  # counts for the bar (demand)
    context_known: bool = True
    anchor_at: float | None = None
    cover: str = "gap"
    steps: list[str] = field(default_factory=list)
    opaque_n: int = 0
    entity_key: str | None = None
    asked_run: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    #: The bind sources' values (RAM only: a site, a product name, a decrypted entity).
    src: dict[str, Any] = field(default_factory=dict)
    capability: str = "none"
    forced_capability: str = ""
    attrs: dict[str, Any] = field(default_factory=dict)
    group: Any = None
    means: str | None = None
    key_value_digest: str | None = None

    @property
    def weekday(self) -> int | None:
        return datetime.fromtimestamp(self.at).weekday() if self.context_known else None

    @property
    def minute(self) -> float | None:
        if not self.context_known:
            return None
        t = datetime.fromtimestamp(self.at)
        return round(t.hour * 60 + t.minute + t.second / 60.0, 2)


@dataclass
class Built:
    """What one segmentation pass made: the occasions and its content-free counters."""

    occasions: list[Occasion]
    counts: Counter = field(default_factory=Counter)
    dropped: Counter = field(default_factory=Counter)
    since: float = 0.0
    until: float = 0.0
    browsers: dict[str, int] = field(default_factory=dict)
    trace_from: float | None = None


# ---------------------------------------------------------------------------------------------------------------------
# Indexes (bisect, never a scan per occasion: critique L9)
# ---------------------------------------------------------------------------------------------------------------------


class Spans:
    """Sorted spans with a payload; ``holding(t)`` and ``ended_before(t, within)`` by bisect."""

    def __init__(self, items: Iterable[tuple[float, float, Any]]) -> None:
        self.items = sorted(items, key=lambda x: (x[0], x[1]))
        self.starts = [x[0] for x in self.items]

    def holding(self, t: float, tol: float = 0.0) -> tuple[float, float, Any] | None:
        i = bisect.bisect_right(self.starts, t + tol) - 1
        best = None
        while i >= 0:
            a, b, p = self.items[i]
            if b + tol >= t:
                best = (a, b, p)
                break
            if t - a > 2 * DAY_S:
                break
            i -= 1
        return best

    def ended_before(self, t: float, within: float) -> tuple[float, float, Any] | None:
        i = bisect.bisect_right(self.starts, t) - 1
        best = None
        while i >= 0:
            a, b, p = self.items[i]
            if b <= t and t - b <= within and (best is None or b > best[1]):
                best = (a, b, p)
            if t - a > within + 2 * DAY_S:
                break
            i -= 1
        return best

    def overlapping(self, lo: float, hi: float, lag: float = 0.0) -> list[tuple[float, float, Any]]:
        """The spans with ``a <= hi`` and ``b + lag >= lo``."""
        i = bisect.bisect_right(self.starts, hi)
        out = []
        for a, b, p in reversed(self.items[:i]):
            if b + lag >= lo:
                out.append((a, b, p))
            if hi - a > 2 * DAY_S:
                break
        return out


def _merge_visits(rows: Iterable[tuple[float, float]]) -> list[tuple[float, float, None]]:
    out: list[list[float]] = []
    for a, b in sorted(rows):
        if out and a - out[-1][1] <= VISIT_GAP_S:
            out[-1][1] = max(out[-1][1], b)
        else:
            out.append([a, b])
    return [(a, b, None) for a, b in out]


def _app(name: Any) -> str:
    from yuki.memory.store import app_key

    return app_key(str(name or "")) or ""


def _facts(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        got = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def _q(store: Any, sql: str, params: Sequence[Any] = ()) -> list[Any]:
    try:
        return list(store._query(sql, params))
    except Exception:  # noqa: BLE001 - a table a database doesn't have yet: nothing from it
        return []


# ---------------------------------------------------------------------------------------------------------------------
# Not-mine verdicts (the ui_tasks port)
# ---------------------------------------------------------------------------------------------------------------------


def not_mine_spans(store: Any) -> list[tuple[float, float, list[str]]]:
    """The spans (and app keys) of tasks the user said were not theirs: the kept list (:data:`NOT_MINE_STATE`) merged
    with any ``ui_tasks`` verdicts still on disk, written back so they outlive that table. Content-free."""
    try:
        kept = json.loads(store.nudge_state(NOT_MINE_STATE) or "[]")
    except Exception:  # noqa: BLE001
        kept = []
    spans = {(float(a), float(b), tuple(sorted(apps))) for a, b, apps in kept if isinstance(apps, list)} \
        if isinstance(kept, list) else set()
    before = len(spans)
    for r in _q(store, "SELECT started_at, ended_at, apps FROM ui_tasks WHERE user_verdict = 'not_mine'"):
        try:
            apps = tuple(sorted(_app(a) for a in json.loads(r[2] or "[]") if a))
        except (TypeError, ValueError):
            apps = ()
        spans.add((float(r[0]), float(r[1]), apps))
    if len(spans) != before:
        try:
            store.set_nudge_state(NOT_MINE_STATE, json.dumps(sorted([a, b, list(p)] for a, b, p in spans)))
        except Exception:  # noqa: BLE001 - kept next time
            pass
    return [(a, b, list(p)) for a, b, p in sorted(spans)]


# ---------------------------------------------------------------------------------------------------------------------
# The pass
# ---------------------------------------------------------------------------------------------------------------------


class Builder:
    """One segmentation pass over ``[since, until]`` on a store (a rebuild: the moments' retention; incremental: the
    last hour). Every source is read once, indexed, and never written; :func:`save` writes the occasions."""

    def __init__(self, store: Any, *, until: float, since: float, browsers: dict[str, int],
                 is_game: Any = None) -> None:
        self.store = store
        self.until = float(until)
        self.since = float(since)
        self.browsers = dict(browsers)
        if is_game is None:
            from yuki.memory.moments import game_judge

            is_game = game_judge(store)
        self.is_game = is_game
        self.counts: Counter = Counter()
        self.dropped: Counter = Counter()

    # -- loading -------------------------------------------------------------------------------------------------------

    def _moments(self) -> list[dict[str, Any]]:
        kinds = ",".join("?" * len(END_KINDS))
        cols = ("id, at, ended_at, kind, app, service, entity_digest, entity_ciphertext, entity_key, value, means, by,"
                " request_id, source, source_id, evidence, facts")
        rows = _q(self.store, f"SELECT {cols} FROM moments WHERE kind IN ({kinds}) AND at >= ? AND at <= ?"
                              " ORDER BY at, id", (*END_KINDS, self.since, self.until))
        out = []
        for r in rows:
            d = dict(r)
            d["facts"] = _facts(d.get("facts"))
            d["app"] = _app(d.get("app"))
            out.append(d)
        return out

    def _visits(self) -> tuple[dict[str, Spans], Spans]:
        per: dict[str, list[tuple[float, float]]] = defaultdict(list)
        front: list[tuple[float, float, str]] = []
        for process, a, b in _q(self.store, "SELECT process, started_at, ended_at FROM timeline WHERE state ="
                                            " 'present' AND ended_at >= ? AND started_at <= ? AND process IS NOT NULL",
                                (self.since - DAY_S, self.until + 60.0)):
            key = _app(process)
            if key:
                per[key].append((float(a), float(b)))
                front.append((float(a), float(b), key))
        return {k: Spans(_merge_visits(v)) for k, v in per.items()}, Spans(front)

    def _trace(self) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[float]], float | None]:
        per: dict[str, list[dict[str, Any]]] = defaultdict(list)
        first = _q(self.store, "SELECT min(at) FROM ui_actions")
        trace_from = float(first[0][0]) if first and first[0][0] is not None else None
        for r in _q(self.store, "SELECT id, at, process, kind, source, during_run, resolved, element_key, role,"
                                " position, chord, file_ext FROM ui_actions WHERE at >= ? AND at <= ? ORDER BY at, id",
                    (self.since - ANCHOR_MAX_S, self.until + STEP_TAIL_S)):
            d = dict(r)
            if d.get("source") not in ("user", "user_injected") or d.get("kind") == "hover" \
                    or d.get("resolved") == "stale" or d.get("during_run"):
                continue
            d["process"] = _app(d.get("process"))
            per[d["process"]].append(d)
        return per, {k: [float(x["at"]) for x in v] for k, v in per.items()}, trace_from

    def _acting(self) -> tuple[dict[str, Spans], list[dict[str, Any]]]:
        """Yuki's acting steps per app (D18): ``runs`` steps of kind action / control, from their start to their end
        (start + duration when no end was kept), and the skill runs' windows."""
        per: dict[str, list[tuple[float, float, Any]]] = defaultdict(list)
        steps: list[dict[str, Any]] = []
        acting = ",".join("?" * len(ACTING_KINDS))
        for r in _q(self.store, f"SELECT id, run_key, at, ended_at, ms, tool, kind, app, ok, verdict, input_ciphertext"
                                f" FROM runs WHERE step > 0 AND kind IN ({acting}) AND at >= ? AND at <= ?"
                                f" ORDER BY at, id", (*ACTING_KINDS, self.since - 3600.0, self.until)):
            d = dict(r)
            d["app"] = _app(d.get("app"))
            a = float(d["at"])
            b = float(d["ended_at"]) if d.get("ended_at") else a + max(0.0, float(d.get("ms") or 0.0)) / 1000.0
            d["t1"] = b
            steps.append(d)
            if d["app"]:
                per[d["app"]].append((a, b, d))
        for r in _q(self.store, "SELECT id, run_key, skill, app, end_kind, t0, t_done, outcome, checked, recheck,"
                                " request_id, rung, variant, via FROM skill_runs WHERE t0 >= ? AND t0 <= ?",
                    (self.since - 3600.0, self.until)):
            d = dict(r)
            d["app"] = _app(d.get("app"))
            d["skill_run"] = True
            a = float(d["t0"])
            b = float(d["t_done"]) if d.get("t_done") is not None else a
            d["t1"] = b
            if d["app"]:
                per[d["app"]].append((a, b, d))
        return {k: Spans(v) for k, v in per.items()}, steps

    # -- the pass ------------------------------------------------------------------------------------------------------

    def build(self) -> Built:
        from yuki.memory.moments import ATTRIBUTION_LAG_S

        moments = self._moments()
        visits, front = self._visits()
        trace, trace_at, trace_from = self._trace()
        acting, run_steps = self._acting()
        not_mine = not_mine_spans(self.store)
        calls = Spans((float(a), float(b or c or self.until), None) for a, b, c in _q(
            self.store, "SELECT detected_at, ended_at, last_alive_at FROM calls WHERE kind = 'call' AND detected_at <= ?",
            (self.until,)))
        turns = sorted((float(at), int(i)) for i, at in _q(
            self.store, "SELECT id, at FROM conversation_turns WHERE at >= ? AND at <= ?",
            (self.since - TURN_BEFORE_S, self.until)))
        turn_at = [t for t, _i in turns]
        threads = {int(r[0]): (str(r[1] or "").casefold(), r[2]) for r in _q(
            self.store, "SELECT id, host, url_ciphertext FROM threads")}
        pages_by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for m in moments:
            if m["kind"] == "page":
                pages_by_app[m["app"]].append(m)
        page_at = {k: [float(m["at"]) for m in v] for k, v in pages_by_app.items()}
        self.counts["moments"] = len(moments)

        # --- the live-kind rules and the drops that need neighbours
        duck: set[int] = set()
        vols = [m for m in moments if m["kind"] == "volume_change"]
        for i, m in enumerate(vols):
            for n in vols[i + 1:]:
                if float(n["at"]) - float(m["at"]) > DUCK_S:
                    break
                if n["app"] != m["app"]:
                    duck.update({int(m["id"]), int(n["id"])})
        # each app's playlist read just before the window (an incremental pass compares its first read with it)
        last_context: dict[str, str | None] = {_app(a): d for a, d, _t in _q(
            self.store, "SELECT app, entity_digest, max(at) FROM moments WHERE kind = 'media_context' AND at < ?"
                        " GROUP BY app", (self.since,))}
        # the moment each of Yuki's runs made: "r:<runs step id>", "s:<skill run id>", "q:<request id>" -> moment id
        acted_moments: dict[str, int] = {}
        occasions: list[Occasion] = []
        last_end: dict[str, float] = {}
        for m in moments:
            kind, app, at, mid = m["kind"], m["app"], float(m["at"]), int(m["id"])
            facts = m["facts"]
            self.counts[f"end_{kind}"] += 1
            if kind == "media_context":
                prev = last_context.get(app)
                last_context[app] = m.get("entity_digest")
                if "since" not in facts:
                    self.dropped["context_first_read"] += 1
                    continue
                if prev is not None and prev == m.get("entity_digest"):
                    self.dropped["context_unchanged"] += 1
                    continue
            if (m.get("by") or "user") == "yuki":
                self.dropped["yuki_moment"] += 1
                if m.get("request_id"):
                    acted_moments.setdefault("q:" + str(m["request_id"]), mid)
                continue
            if kind == "volume_change" and mid in duck:
                self.dropped["ducking"] += 1
                continue
            if app and self.is_game(app):
                self.dropped["game_app"] += 1
                continue
            if any(a <= at <= b and (not apps or app in apps) for a, b, apps in not_mine):
                self.dropped["not_mine"] += 1
                continue
            lag = float(ATTRIBUTION_LAG_S.get(kind, 20.0))
            hit = None
            spans = acting.get(app)
            for a, b, d in (spans.overlapping(at, at, lag) if spans is not None else ()):
                if a <= at <= b + lag:
                    hit = d
                    break
            if hit is not None:
                # D18 (critique M2): inside one of Yuki's acting steps of this app: that run's, never the user's
                self.dropped["yuki_acting"] += 1
                self.dropped[f"yuki_acting:{kind}"] += 1
                self.dropped[f"yuki_acting:{kind}:{app}"] += 1
                acted_moments.setdefault(("s:" if hit.get("skill_run") else "r:") + str(hit["id"]), mid)
                continue
            src: dict[str, Any] = {"app": app, "service": m.get("service"), "value": m.get("value"),
                                   "means": m.get("means")}
            if kind == "page":
                if app not in self.browsers:
                    self.dropped["page_not_browser"] += 1
                    continue
                if facts.get("scope") == "host":
                    src["site"] = facts.get("host")
                    src["page"] = None
                    src["top_host"] = facts.get("top_host")
                    src["host"] = facts.get("host")
                elif facts.get("template"):
                    site, _, page = str(facts["template"]).partition(".")
                    src["site"], src["page"], src["host"] = site, page or None, facts.get("host")
                else:
                    self.dropped["page_untemplated"] += 1     # its host-scope visit stands for it
                    continue
            if kind == "message_sent":
                tid = facts.get("thread_id")
                host, url_ct = threads.get(int(tid), ("", None)) if isinstance(tid, int) else ("", None)
                src["host"] = host[4:] if host.startswith("www.") else host
                src["thread_id"] = tid
                src["url_ct"] = url_ct
                src["contact"] = facts.get("contact_id")
            try:
                src["entity"] = self.store._dec(m["entity_ciphertext"]) if m.get("entity_ciphertext") else None
            except Exception:  # noqa: BLE001 - an entity that can't be read: its digest only
                src["entity"] = None
            src["entity_digest"] = m.get("entity_key") or m.get("entity_digest")
            o = Occasion(kind=kind, at=at, app=app, end_moment_id=mid, entity_key=m.get("entity_key"), src=src)
            if m.get("evidence") == "hold":
                o.facts["hold"] = True
            # late chat capture (critique M4): the label time when on time, else demand and means only
            if kind == "message_sent":
                lag_s = facts.get("label_lag_s")
                if isinstance(lag_s, (int, float)) and 0.0 <= float(lag_s) <= LATE_SEND_S:
                    o.at = at - float(lag_s)
                else:
                    o.context_known = False
                    o.facts["late"] = True
                    self.counts["late_send"] += 1
            self._context(o, visits, front, trace, trace_at, trace_from, calls, turns, turn_at, last_end,
                          pages_by_app, page_at)
            occasions.append(o)
        asked = self._asked(run_steps, acted_moments, occasions)
        occasions.extend(asked)
        self.counts["occasions_user"] = sum(1 for o in occasions if o.by == "user")
        self.counts["occasions_asked"] = len(asked)
        return Built(occasions=occasions, counts=self.counts, dropped=self.dropped, since=self.since,
                     until=self.until, browsers=self.browsers, trace_from=trace_from)

    def _context(self, o: Occasion, visits: dict[str, Spans], front: Spans, trace: dict[str, list[dict[str, Any]]],
                 trace_at: dict[str, list[float]], trace_from: float | None, calls: Spans,
                 turns: list[tuple[float, int]], turn_at: list[float], last_end: dict[str, float],
                 pages_by_app: dict[str, list[dict[str, Any]]], page_at: dict[str, list[float]]) -> None:
        """The visit, anchor, steps, cover and context of one user occasion."""
        at, app = o.at, o.app
        if not o.context_known:
            o.cover = "gap"     # a late send: demand and means only, no visit, steps or time context
            return
        held = front.holding(at)
        o.facts["game_front"] = bool(held and held[2] != app and self.is_game(held[2]))
        o.facts["call_open"] = calls.holding(at) is not None
        j = bisect.bisect_right(turn_at, at) - 1       # the latest request to Yuki in the 10 minutes before
        if j >= 0 and turns[j][0] >= at - TURN_BEFORE_S:
            o.facts["turn_id"] = turns[j][1]
        spans = visits.get(app)
        visit = spans.holding(at, VISIT_TOLERANCE_S) if spans else None
        if visit is None and spans:
            visit = spans.ended_before(at, VISIT_BEFORE_S)
        if visit is None:
            o.cover = "gap"
            o.anchor_at = at
            last_end[app] = at
            return
        anchor = max(visit[0] - ANCHOR_LEAD_S, at - ANCHOR_MAX_S, last_end.get(app, float("-inf")))
        o.anchor_at = anchor
        last_end[app] = at
        if trace_from is None or at < trace_from:
            o.cover = "before_trace"
            return
        rows = trace.get(app) or []
        times = trace_at.get(app) or []
        lo, hi = bisect.bisect_left(times, anchor), bisect.bisect_right(times, at + STEP_TAIL_S)
        tokens: list[str] = []
        identity = opaque = switches = 0
        for r in rows[lo:hi]:
            got = step_token(r)
            if got is None:
                if r.get("kind") in OPAQUE_KINDS:
                    opaque += 1
                continue
            tok = got[0]
            if tokens and tokens[-1] == tok:
                continue
            tokens.append(tok)
            if tok.endswith("|switch"):
                switches += 1
            else:
                identity += 1
        if not any(t.endswith("|switch") for t in tokens) and anchor <= visit[0] <= at:
            tokens.insert(0, f"{app}|front")     # the switch in, from the timeline's boundary
        plist = pages_by_app.get(app) or []
        ptimes = page_at.get(app) or []
        for m in plist[bisect.bisect_left(ptimes, anchor):bisect.bisect_left(ptimes, at)]:
            if int(m["id"]) == o.end_moment_id:
                continue
            f = m["facts"]
            tokens.append("page:" + str(f.get("template") or (f.get("host") and "host") or "page"))
        o.steps = tokens[:80]
        o.opaque_n = opaque
        if identity and not opaque:
            o.cover = "full"
        elif identity or opaque:
            o.cover = "partial"
        elif switches:
            o.cover = "switch_only"
        else:
            o.cover = "gap"

    def _asked(self, steps: list[dict[str, Any]], acted: dict[str, int], user: list[Occasion]) -> list[Occasion]:
        """Asked occasions (design §4.8, D22) from older thinker runs and from skill runs."""
        out: list[Occasion] = []
        corrected_runs = {str(r[0]) for r in _q(self.store, "SELECT DISTINCT run_key FROM playbook_events WHERE"
                                                            " type = 'user_corrected' AND run_key IS NOT NULL")}
        by_app: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for d in steps:
            by_app[d["app"]].append(d)
        used_moments: set[int] = {o.end_moment_id for o in user if o.end_moment_id is not None}
        for d in steps:
            end = VERB_ENDS.get(str(d.get("tool") or ""))
            if end is None or d.get("kind") != "action":
                continue
            verdict = d.get("verdict")
            if verdict == 0 or not d.get("ok"):
                continue
            at = float(d["at"])
            corrected = str(d.get("run_key")) in corrected_runs
            if not corrected:
                # steered to another way of reaching the same end within the minute (another verb of the same end
                # kind, in a later run on the same app): the first way was corrected
                for n in by_app.get(d["app"], ()):
                    tool = str(n.get("tool") or "")
                    if n["run_key"] != d["run_key"] and at < float(n["at"]) <= at + CORRECTION_S and \
                            tool != str(d.get("tool") or "") and VERB_ENDS.get(tool) == end:
                        corrected = True
                        break
            o = Occasion(kind=end, at=at, app=d["app"], by="asked", asked_run=str(d.get("run_key") or ""))
            o.facts.update(verb=str(d.get("tool") or ""), verified=verdict == 1, corrected=corrected)
            o.counts = not corrected
            o.context_known = True
            o.cover = "asked"
            o.src = {"app": d["app"]}
            if end == "page":
                url = None
                try:
                    inp = json.loads(self.store._dec(d["input_ciphertext"]) or "{}") if d.get("input_ciphertext") \
                        else {}
                    url = inp.get("url") if isinstance(inp, dict) else None
                except Exception:  # noqa: BLE001
                    url = None
                self._page_src(o, url)
            mid = acted.get("r:" + str(d["id"]))
            if mid is not None and mid not in used_moments:
                o.end_moment_id = mid
                used_moments.add(mid)
            if corrected:
                self.counts["asked_corrected"] += 1
            out.append(o)
        for d in self._skill_runs():
            checked, recheck = d.get("checked"), d.get("recheck")
            if checked == 0 or d.get("outcome") not in ("done", "unverified", "already"):
                continue
            corrected = recheck == 0
            end = str(d.get("end_kind") or "")
            if not end:
                continue
            o = Occasion(kind=end, at=float(d["t_done"] or d["t0"]), app=d["app"], by="asked",
                         asked_run=str(d.get("run_key") or ""))
            o.facts.update(verb=f"{d.get('rung') or ''}:{d.get('variant') or ''}:{d.get('via') or ''}",
                           verified=checked == 1, corrected=corrected, skill=str(d.get("skill") or ""))
            o.counts = not corrected
            o.cover = "asked"
            o.src = {"app": d["app"]}
            o.forced_capability = str(d.get("capability") or "")
            mid = acted.get("s:" + str(d["id"]))
            if mid is None and d.get("request_id"):
                mid = acted.get("q:" + str(d["request_id"]))
            if mid is not None and mid not in used_moments:
                o.end_moment_id = mid
                used_moments.add(mid)
            if corrected:
                self.counts["asked_corrected"] += 1
            out.append(o)
        # asked, then did it himself: a cancelled or failed request, then the user's own occasion in an app it acted
        heads = {str(r[0]): str(r[1] or "") for r in _q(self.store, "SELECT run_key, outcome FROM runs WHERE step = 0"
                                                                    " AND at >= ?", (self.since - 3600.0,))}
        failed_apps: list[tuple[float, str, str]] = sorted(
            (float(d["t1"]), d["app"], str(d["run_key"])) for d in steps
            if d["app"] and heads.get(str(d["run_key"])) in ("cancelled", "error", "withdrawn"))
        ends = [t for t, _a, _k in failed_apps]
        for o in user:
            i = bisect.bisect_left(ends, o.at - ASKED_THEN_DID_S)
            for t, app, key in failed_apps[i:]:
                if t > o.at:
                    break
                if app == o.app:
                    o.facts["after_ask"] = key
        return out

    def _skill_runs(self) -> list[dict[str, Any]]:
        rows = _q(self.store, "SELECT u.id, u.run_key, u.skill, u.app, u.end_kind, u.t0, u.t_done, u.outcome, u.checked,"
                              " u.recheck, u.rung, u.variant, u.via, u.request_id, c.key AS capability FROM skill_runs u"
                              " LEFT JOIN"
                              " skills s ON s.id = u.skill_id LEFT JOIN skills c ON c.id = coalesce(s.capability_id,"
                              " s.id) WHERE u.t0 >= ? AND u.t0 <= ?", (self.since, self.until))
        out = []
        for r in rows:
            d = dict(r)
            d["app"] = _app(d.get("app"))
            out.append(d)
        return out

    @staticmethod
    def _page_src(o: Occasion, url: str | None) -> None:
        """A page an asked run opened, as the bind sources a page moment gives (its site file's page kind, else its
        registrable domain: a host-scope page)."""
        if not url:
            return
        try:
            from urllib.parse import urlsplit

            from yuki.connect.templates import page_of
            from yuki.memory.moments import registrable_domain, templated_hosts

            got = page_of(url)
            if got is not None:
                o.src.update(site=got.site, page=got.kind, entity=got.entity)
                return
            host = (urlsplit(url if "//" in url else "https://" + url).hostname or "").casefold()
            bare = host[4:] if host.startswith("www.") else host
            if bare and bare not in templated_hosts():
                o.src.update(site=registrable_domain(bare), page=None, top_host=bare, host=bare)
        except Exception:  # noqa: BLE001
            return


def build(store: Any, *, now: float | None = None, since: float | None = None, browsers: dict[str, int] | None = None,
          is_game: Any = None) -> Built:
    """One segmentation pass (see the module docstring); ``since`` defaults to the moments' retention."""
    from yuki.skills.learn.bind import measured_browsers

    now = time.time() if now is None else float(now)
    since = now - KEEP_DAYS * DAY_S if since is None else float(since)
    if browsers is None:
        browsers = measured_browsers(store, now)
    return Builder(store, until=now, since=since, browsers=browsers, is_game=is_game).build()


def save(store: Any, occasions: Sequence[Occasion], *, since: float, until: float, now: float | None = None) -> int:
    """Replace the occasions in ``[since, until]`` with these, one transaction; returns rows written."""
    now = time.time() if now is None else float(now)
    rows = []
    for o in occasions:
        if not since <= o.at <= until + 1.0:
            continue
        rows.append((o.end_moment_id, o.kind, o.capability, o.key_value_digest, o.app or None, o.means, float(o.at),
                     o.anchor_at, o.weekday, o.minute, int(bool(o.context_known)), o.cover,
                     json.dumps(o.steps, separators=(",", ":")) if o.steps else None, int(o.opaque_n), o.entity_key,
                     o.asked_run, o.by, getattr(o.group, "skill_id", None) if o.group is not None else None,
                     json.dumps({k: v for k, v in o.facts.items() if v not in (None, False, "")}, sort_keys=True,
                                separators=(",", ":")) or None, now))

    def write(conn: Any) -> int:
        conn.execute("DELETE FROM occasions WHERE at >= ? AND at <= ?", (float(since), float(until) + 1.0))
        conn.executemany(
            "INSERT OR REPLACE INTO occasions (end_moment_id, kind, capability, key_value_digest, app, means, at,"
            " anchor_at, weekday, minute, context_known, cover, steps, opaque_n, entity_key, asked_run, by, skill_id,"
            " facts, built_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        return len(rows)

    return int(store._write(write))


__all__ = ["END_KINDS", "LATE_SEND_S", "DUCK_S", "CORRECTION_S", "KEEP_DAYS", "VERB_ENDS", "NOT_MINE_STATE", "Occasion",
           "Built", "Spans", "Builder", "not_mine_spans", "build", "save"]
