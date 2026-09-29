"""What the user was doing before a moment: the coach's welcome-back reconstruction, callable on demand.

The coach's welcome back (:func:`yuki.memory.nudges.build_resume_message`) has long rebuilt, well,
what the user was in the middle of before a long gap - time per site with titles, the sessions,
the last thing in front, the episodes and the journal of the stretch before it. Only the coach
could use it: "what was I doing before lunch yesterday", "continue where I left off" were guessed
by the agent from a time window it picked itself. The data assembly now lives here
(:func:`stretch_lines`, used by the coach unchanged) and :func:`before` serves the agent's
``before`` tool and :meth:`yuki.memory.api.MemoryClient.before`:

* ``event="break"`` (default): the break nearest ``at`` (a stretch of at least :data:`BREAK_MIN_S`
  with the user away from the PC - asleep, off, away, or nothing recorded - measured by the
  welcome back's own presence sessions), and the stretch before it; the day's other breaks are
  listed so the caller can pick another ("before lunch" is the model's reading of the user's words,
  never code's). With no ``at``: the latest break before now ("where I left off").
* ``event="time"``: the stretch that ended at ``at``.
* The threads of work touched in that stretch (:mod:`yuki.memory.workthreads`) come with it.

Plumbing on the timeline's own numbers; nothing here reads words or calls a model.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

#: A break is at least this long away from the PC (the welcome back's sessions settle at 5 min; a meal or an
#: errand is longer, so shorter pauses are left out of the list the caller picks from).
BREAK_MIN_S = 10 * 60.0
#: How far around ``at`` breaks are looked for.
SEARCH_H = 14.0
#: The stretch before a break that is described (the welcome back's ``resume_before_min``).
BEFORE_MIN = 90.0


def stretch_lines(store: Any, since: float, until: float, config: Any, safe: Callable[[Any, int], str],
                  ids: dict[str, float] | None = None) -> list[str]:
    """What the user was doing in ``[since, until)``, as the welcome back's BEFORE THE GAP lines: time use,
    SESSIONS, LAST THING IN FRONT, EPISODES and JOURNAL (oldest first). ``ids`` receives the journal
    evidence ids offered (``J<id>``). Never the stretches Yuki spent acting for the user."""
    from yuki.memory.nudges import JOURNAL_CHARS, ONGOING_S, _jline, _run_line, activity_sessions, session_line
    from yuki.memory.personality import usable
    from yuki.memory.timeline import _clip, _runs, aggregate, describe

    ids = ids if ids is not None else {}
    parts: list[str] = []
    rows = [r for r in store.timeline_between(since, until) if not r.by_yuki]
    agg = aggregate(rows, since, until, "site", limit=8)
    lines = describe(agg, max_items=8)
    parts.append("- totals: " + safe(lines[0], 400))
    parts += [safe(x, 600) for x in lines[1:] if not x.startswith("power: ")]
    parts.append("SESSIONS:")
    parts += [f"- {session_line(x, safe, until + ONGOING_S + 1)}"
              for x in activity_sessions(rows, since, until, config)] or ["(none)"]
    clips = _clip(rows, since, until)
    last_runs = [run for run, _ in _runs(clips, "site") if run.present_s > 0]
    if last_runs:
        parts.append(f"LAST THING IN FRONT: {_run_line(last_runs[-1], safe)}")
    parts.append("EPISODES:")
    parts += [f"- {safe(e.text, 700)}" for e in store.episodes_between(since, until)[-3:]] or ["(none)"]
    parts.append("JOURNAL (oldest first):")
    journal = [j for j in store.journal_between(since, until + 60.0) if not j.by_yuki and usable(j)]
    jl: list[str] = []
    used = 0
    for j in reversed(journal):
        line = _jline(j, safe)
        if used + len(line) > JOURNAL_CHARS:
            jl.insert(0, f"- ... {len(journal) - len(jl)} earlier facts not shown")
            break
        jl.insert(0, line)
        used += len(line)
        ids[f"J{j.id}"] = j.at
    parts += jl or ["(none)"]
    return parts


def breaks_near(store: Any, at: float, *, hours: float = SEARCH_H, min_s: float = BREAK_MIN_S,
                config: Any = None) -> list[dict[str, Any]]:
    """The user's breaks from the PC in ``at ± hours``: ``[{"start", "end", "seconds", "kinds": {...}}]``
    oldest first, each from the end of one settled stretch at the PC to the start of the next (the welcome
    back's presence sessions: a glance or a mouse bump never ends a break)."""
    from yuki.memory.nudges import NudgeConfig, presence_sessions
    from yuki.memory.timeline import _clip, is_power

    config = config or NudgeConfig()
    since, until = at - hours * 3600.0, min(at + hours * 3600.0, time.time())
    clips = _clip([r for r in store.timeline_states(since, until) if not r.by_yuki], since, until)
    sessions = [s for s in presence_sessions(clips, config) if s.settled]
    out = []
    for prev, nxt in zip(sessions, sessions[1:]):
        seconds = nxt.start - prev.end
        if seconds < min_s:
            continue
        kinds: dict[str, float] = {}
        for c in clips:
            overlap = min(c.end, nxt.start) - max(c.start, prev.end)
            if overlap > 0 and c.row.state != "present":
                key = c.row.state if is_power(c.row) else "away"
                kinds[key] = kinds.get(key, 0.0) + overlap
        covered = sum(kinds.values())
        if seconds - covered >= 60:
            kinds["nothing recorded"] = seconds - covered
        out.append({"start": prev.end, "end": nxt.start, "seconds": round(seconds, 1),
                    "kinds": {k: round(v, 1) for k, v in kinds.items()}})
    if sessions and until - sessions[-1].end >= min_s and until >= time.time() - 60:
        out.append({"start": sessions[-1].end, "end": None, "seconds": round(until - sessions[-1].end, 1),
                    "kinds": {}, "ongoing": True})
    return out


def _hm(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%a %d %b %H:%M") if at else "now"


def _break_words(b: dict[str, Any]) -> str:
    from yuki.memory.timeline import format_duration

    parts = [f"{k} {format_duration(v)}" for k, v in sorted(b["kinds"].items(), key=lambda kv: -kv[1]) if v >= 60]
    end = "still away" if b.get("end") is None else datetime.fromtimestamp(b["end"]).strftime("%H:%M")
    return (f"{_hm(b['start'])}-{end} ({format_duration(b['seconds'])}" + (f": {', '.join(parts)}" if parts else "")
            + ")")


def before(store: Any, at: float | None = None, *, event: str = "break", minutes: float = BEFORE_MIN,
           now: float | None = None) -> dict[str, Any]:
    """What the user was doing before ``at`` (see the module docstring). Never raises.

    Returns ``{"event", "at", "break": {...} | None, "since", "until", "text", "breaks": [...], "threads":
    [WorkThread], "ok"}``; ``text`` is the stretch as lines (the welcome back's own), ``breaks`` the other
    breaks near ``at`` (for the caller to pick another).
    """
    from yuki.memory.nudges import NudgeConfig

    now = time.time() if now is None else float(now)
    config = NudgeConfig()
    out: dict[str, Any] = {"event": event, "at": at, "break": None, "since": None, "until": None, "text": "",
                           "breaks": [], "threads": [], "ok": False}
    try:
        target = float(at) if at is not None else now
        chosen = None
        if event != "time":
            found = breaks_near(store, target, config=config)
            out["breaks"] = found
            done = [b for b in found if b.get("end") is not None or b.get("ongoing")]
            if at is None:   # the latest break before now: where they left off
                chosen = next((b for b in reversed(done) if b["start"] <= now), None)
            elif done:   # the break around the moment named: containing it, else the nearest one
                chosen = next((b for b in done if b["start"] <= target <= (b["end"] or now)), None) or min(
                    done, key=lambda b: min(abs(b["start"] - target), abs((b["end"] or now) - target)))
        until = chosen["start"] if chosen is not None else target
        since = until - float(minutes) * 60.0
        out.update({"break": chosen, "since": since, "until": until})

        def safe(value: Any, cap: int) -> str:
            text = " ".join(str(value or "").split())
            return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"

        lines = stretch_lines(store, since, until, config, safe)
        head = (f"Before the break {_break_words(chosen)}: the user's last {int(minutes)} min at the PC, "
                f"{_hm(since)}-{datetime.fromtimestamp(until):%H:%M}:" if chosen is not None else
                f"The {int(minutes)} min before {_hm(until)} ({_hm(since)}-{datetime.fromtimestamp(until):%H:%M}):")
        out["text"] = "\n".join([head, *lines])
        try:
            touched = store.latest_work_events(since=since - 3600.0, until=until + 60.0, limit=40)
            ids: list[int] = []
            for e in touched:
                tid = store.resolve_work_thread(e["work_thread_id"])
                if tid is not None and tid not in ids:
                    ids.append(tid)
            threads = {t.id: t for t in store.work_threads(None, ids=ids)} if ids else {}
            out["threads"] = [threads[i] for i in ids if i in threads][:3]
        except Exception:
            pass
        out["ok"] = True
    except Exception as exc:
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out


def breaks_text(breaks: list[dict[str, Any]]) -> str:
    """The breaks as lines, for the tool's text."""
    return "\n".join(f"- {_break_words(b)}" for b in breaks) or "(none)"
