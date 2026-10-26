"""Calls as the user lived them: the ``calls`` rows merged into spans, with what was in front meanwhile.

Contract: ``docs/MEMORY.md`` -> "Calls: both sides" (calls in the activity output).

The call worker (:mod:`yuki.memory.calls`) writes one content-free ``calls`` row
per call it sees: an app holding the microphone, in front or not. Its rows are
cut where the call is not: a memory-service restart ends the open row
(``stop_reason = service_restart``) and the new service opens another for the
same call, and Google Meet drops the microphone whenever the user mutes, so one
meeting becomes many rows. Nothing downstream read the table, so the agent's
``activity`` tool, the coach and the request-origin line never knew a 13-hour
Discord call was going on (the design notes section 2).

This module is plumbing on those numbers (nothing decrypted, no model):

* :func:`call_spans` - the calls overlapping a window, merged: rows of one app
  are one call when they are in the same microphone hold (the same
  ``hold_since``, migration 26: one hold is one session whatever the gap, so a
  row the call worker cut is joined again), when the earlier one ended by a
  service restart and the next began within :data:`RESTART_GAP_S`, or when the
  next began within :data:`JOIN_GAP_S` whatever ended the earlier one. Measured on the real
  calls of 2026-09-24/26: every restart-split pair touches (gap 0 s, 9 of 9:
  the new service picks the call up the same second it closes the old row);
  the Meet meeting of 25 Sept 09:00-10:00 was 9 rows with gaps of 5 s to 9 min
  (muting); the Discord call of that night dropped once for 15 min (01:14-01:29)
  and went on. Each span has its real start and end (or ``ongoing``) even when
  they fall outside the window, the time in the window, the time the call app
  was in front (present time of its timeline rows, meeting rows included), what
  else was in front meanwhile, the drops and the PC's power spans inside it.
* :func:`merged_calls` - the same merge without the timeline (listing calls, finding a row's session).
* :func:`describe_call` - one plain line for a span.
* :func:`open_calls` - the calls still going (the request-origin line).
* :func:`coach_calls_text` - the coach's CALLS line.
"""

from __future__ import annotations

import time
from collections import Counter
from datetime import datetime
from typing import Any

from yuki.memory.store import CALL_OPEN_STATUSES, CallRecord, Store, app_key
from yuki.memory.timeline import POWER_WORDS, _clip, format_duration

#: A row ended by a service restart and the next row of the same app within this is one call (measured 0 s).
RESTART_GAP_S = 600.0
#: Two rows whose ``hold_since`` (the ConsentStore start of their microphone hold) differ by at most this are in
#: the same hold: one call, whatever the gap between them (yuki.memory.calls.HOLD_MATCH_S).
SAME_HOLD_S = 5.0
#: Any two rows of the same app this close are one call (Meet mute splits 5 s-9 min; a Discord drop 15 min).
JOIN_GAP_S = 1200.0
#: How far around a window rows are read to follow a call that began or ended outside it.
CONTEXT_S = 36 * 3600.0
#: A gap inside a call shorter than this is not worth naming as a drop.
DROP_MIN_S = 60.0
#: The coach hears about calls open now or ended at most this long ago.
COACH_RECENT_S = 1800.0
#: Other activities named per call ("meanwhile Google Chrome 2h49m, Warp 57m").
MEANWHILE = 3


def _key(c: CallRecord) -> str:
    return c.process or app_key(c.app) or c.app or ""


def _merge(rows: list[CallRecord], now: float) -> list[dict[str, Any]]:
    """Rows (oldest first) -> merged spans per app, oldest first."""
    by_key: dict[str, list[CallRecord]] = {}
    for r in sorted(rows, key=lambda r: (r.detected_at, r.id)):
        if r.detected_at <= now:   # a row from after ``now`` has not happened yet
            by_key.setdefault(_key(r), []).append(r)
    spans: list[dict[str, Any]] = []
    for key, items in by_key.items():
        current: dict[str, Any] | None = None
        for i, r in enumerate(items):
            later = items[i + 1].detected_at if i + 1 < len(items) else None
            # as of ``now``: a row that ended later was still going then (a replay of an earlier moment)
            ongoing = (r.ended_at is None and r.status in CALL_OPEN_STATUSES) or (r.ended_at or 0.0) > now
            end = min(r.ended_at, now) if r.ended_at is not None else now
            if later is not None and end > later:   # a row left open by a crashed service: the next one took over
                end, ongoing = later, False
            end = max(end, r.detected_at)
            if current is not None:
                gap = r.detected_at - current["end"]
                limit = RESTART_GAP_S if current["stop_reason"] == "service_restart" else JOIN_GAP_S
                hold = getattr(r, "hold_since", None)
                same_hold = (hold is not None and current["hold_since"] is not None
                             and abs(hold - current["hold_since"]) <= SAME_HOLD_S)
                if gap <= limit or same_hold:
                    current["pieces"].append((r.detected_at, end))
                    current["end"] = max(current["end"], end)
                    current["ongoing"] = ongoing
                    current["stop_reason"] = r.stop_reason
                    current["call_ids"].append(r.id)
                    current["service"] = current["service"] or r.service
                    current["hold_since"] = hold if hold is not None else current["hold_since"]
                    continue
            current = {"app": r.app or key, "process": key, "service": r.service, "start": r.detected_at,
                       "end": end, "ongoing": ongoing, "stop_reason": r.stop_reason, "pieces": [(r.detected_at, end)],
                       "call_ids": [r.id], "hold_since": getattr(r, "hold_since", None),
                       # call | game: a game's own voice chat is a voice session too (migration 34)
                       "kind": getattr(r, "kind", None) or "call"}
            spans.append(current)
    return sorted(spans, key=lambda s: s["start"])


def _overlap(pieces: list[tuple[float, float]], since: float, until: float) -> float:
    return sum(max(0.0, min(e, until) - max(s, since)) for s, e in pieces)


def call_spans(store: Store, since: float, until: float, *, now: float | None = None) -> list[dict[str, Any]]:
    """The calls overlapping ``[since, until)``, merged across restarts and short drops, oldest first.

    Each (times are epoch seconds): ``{"app", "process", "service", "start", "end", "ongoing",
    "seconds" (time on the call: its rows added up), "window_s" (of it inside the window),
    "in_front_s" (present time of the call app's own timeline rows during the call, meetings
    included), "window_in_front_s", "background_s" / "window_background_s" (call time the app was
    not in front), "meanwhile": [{"app", "seconds"}] (the other apps in front during the call, most
    first), "drops": [{"start", "end", "seconds"}], "power": [{"kind", "start", "end", "seconds",
    "source"}] (the PC off or asleep inside it), "call_ids"}``. Content-free; never decrypts.
    """
    now = time.time() if now is None else float(now)
    rows = store.calls_overlapping(since - CONTEXT_S, until + CONTEXT_S)
    spans = [s for s in _merge(rows, now) if s["start"] < until and s["end"] > since]
    if not spans:
        return []
    lo, hi = min(s["start"] for s in spans), max(s["end"] for s in spans)
    timeline = sorted((r for r in store.timeline_states(lo, hi) if r.state == "present"), key=lambda r: r.started_at)
    power_rows = store.power_spans(lo, hi)
    out = []
    for s in spans:
        pieces = s["pieces"]
        seconds = sum(e - b for b, e in pieces)
        # what was in front over the whole call, its short drops included ("Discord in front 1h01m of it")
        in_front = win_front = 0.0
        others: Counter = Counter()
        for c in _clip([r for r in timeline if r.ended_at > s["start"] and r.started_at < s["end"]],
                       s["start"], s["end"]):
            present = c.present_s
            if (c.row.process or app_key(c.row.app) or "") == s["process"]:
                in_front += present
                if c.end > c.start:
                    win_front += present * _overlap([(c.start, c.end)], since, until) / (c.end - c.start)
            else:
                others[c.row.app or c.row.process or "(unknown app)"] += present
        window_s = _overlap(pieces, since, until)
        drops = [{"start": a_end, "end": b_start, "seconds": round(b_start - a_end, 1)}
                 for (_, a_end), (b_start, _) in zip(pieces, pieces[1:]) if b_start - a_end >= DROP_MIN_S]
        power = [{"kind": p.state, "start": max(p.started_at, s["start"]), "end": min(p.ended_at, s["end"]),
                  "seconds": round(min(p.ended_at, s["end"]) - max(p.started_at, s["start"]), 1), "source": p.source}
                 for p in power_rows if p.ended_at > s["start"] and p.started_at < s["end"]]
        out.append({
            "app": s["app"], "process": s["process"], "service": s["service"], "start": s["start"],
            "end": s["end"], "ongoing": s["ongoing"], "seconds": round(seconds, 1), "window_s": round(window_s, 1),
            "in_front_s": round(in_front, 1), "window_in_front_s": round(win_front, 1),
            "background_s": round(max(0.0, seconds - in_front), 1),
            "window_background_s": round(max(0.0, window_s - win_front), 1),
            "meanwhile": [{"app": a, "seconds": round(v, 1)} for a, v in others.most_common(MEANWHILE) if v >= 60],
            "drops": drops, "power": [p for p in power if p["seconds"] >= 1], "call_ids": s["call_ids"],
            "kind": s.get("kind", "call"),
        })
    return out


def merged_calls(store: Store, since: float, until: float, *, now: float | None = None) -> list[dict[str, Any]]:
    """The calls overlapping ``[since, until)`` merged as in :func:`call_spans`, without the timeline work:
    ``{"app", "process", "service", "start", "end", "ongoing", "pieces", "call_ids"}`` each, oldest first
    (epoch seconds). Content-free: for listing calls and finding the session a call row belongs to."""
    now = time.time() if now is None else float(now)
    rows = store.calls_overlapping(since - CONTEXT_S, until + CONTEXT_S)
    return [dict(s) for s in _merge(rows, now) if s["start"] < until and s["end"] > since]


def session_start(store: Store, call_id: int, *, now: float | None = None) -> float | None:
    """When the call session that row ``call_id`` belongs to began (epoch s): its first row's start, merged as in
    :func:`call_spans` (rows of the same microphone hold, across restarts and short drops); None when the row is
    unknown. The call pill counts from here, so a rare real reconnect does not restart the timer (05 §7.4)."""
    now = time.time() if now is None else float(now)
    row = store.call_record(int(call_id))
    if row is None:
        return None
    for s in _merge(store.calls_overlapping(row.detected_at - CONTEXT_S, now + 1.0), now):
        if row.id in s["call_ids"]:
            return float(s["start"])
    return float(row.detected_at)


def open_calls(store: Store, now: float | None = None) -> list[dict[str, Any]]:
    """The calls going on now, merged: ``{"app", "process", "service", "since", "seconds"}`` each (epoch)."""
    now = time.time() if now is None else float(now)
    # no timeline needed: the calls table alone (a few rows), on the path of every request
    return [{"app": s["app"], "process": s["process"], "service": s["service"], "since": s["start"],
             "seconds": round(now - s["start"], 1), "kind": s.get("kind", "call")}
            for s in _merge(store.calls_overlapping(now - CONTEXT_S, now + 1.0), now) if s["ongoing"]]


def _when(at: float, ref: float) -> str:
    """``13:20`` on the day of ``ref``, ``Thu 13:20`` on another day."""
    a = datetime.fromtimestamp(at)
    return f"{a:%H:%M}" if a.date() == datetime.fromtimestamp(ref).date() else f"{a:%a %H:%M}"


def describe_call(span: dict[str, Any], *, now: float | None = None, window: bool = False) -> str:
    """One plain line: ``Discord call since 13:20, still going: 8h16m (...). Discord was in front 33m of it; ...``.

    ``window``: also say how much of it fell inside the window asked about, when not all of it did.
    """
    now = time.time() if now is None else float(now)
    app, start = span["app"], span["start"]
    noun = "game voice chat" if span.get("kind") == "game" else "call"   # a game's own voice chat (migration 34)
    if span["ongoing"]:
        head = f"{app} {noun} since {_when(start, now)}, still going: {format_duration(span['seconds'])}"
    else:
        head = (f"{app} {noun} {_when(start, now)}-{_when(span['end'], start)}: "
                f"{format_duration(span['seconds'])}")
    drops = span.get("drops") or []
    if len(drops) <= 2:   # the app let go of the microphone: muted (Meet), or left and rejoined
        notes = [f"no microphone {_when(d['start'], start)}-{_when(d['end'], d['start'])}" for d in drops]
    else:
        notes = [f"microphone free {len(drops)} times, {format_duration(sum(d['seconds'] for d in drops))} in all"]
    notes += [f"{POWER_WORDS.get(p['kind'], p['kind']).split(' (')[0]} {_when(p['start'], start)}-"
              f"{_when(p['end'], p['start'])}" for p in span.get("power") or []]
    if window and span.get("window_s") is not None and span["window_s"] < span["seconds"] - 60:
        notes.append(f"{format_duration(span['window_s'])} of it in this time range")
    line = head + (f" ({'; '.join(notes)})" if notes else "") + "."
    line += f" {app} was in front {format_duration(span['in_front_s'])} of it"
    if span.get("meanwhile"):
        line += "; meanwhile " + ", ".join(f"{m['app']} {format_duration(m['seconds'])}" for m in span["meanwhile"])
    return line + "."


def coach_calls_text(store: Store, now: float) -> str | None:
    """The coach's CALLS line: calls open now or ended within :data:`COACH_RECENT_S`; ``None`` when none."""
    try:
        spans = call_spans(store, now - COACH_RECENT_S, now + 1.0, now=now)
    except Exception:
        return None
    lines = []
    for s in spans:
        if s["ongoing"]:
            lines.append("on a " + describe_call(s, now=now))
        elif now - s["end"] <= COACH_RECENT_S:
            lines.append(f"was on a {describe_call(s, now=now)} It ended {format_duration(now - s['end'])} ago.")
    return "\n".join(lines) or None


__all__ = ["RESTART_GAP_S", "JOIN_GAP_S", "SAME_HOLD_S", "call_spans", "merged_calls", "open_calls", "session_start",
           "describe_call", "coach_calls_text"]
