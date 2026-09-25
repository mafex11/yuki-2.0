"""Time arguments of the MCP tools: ISO strings (or ``now``), resolved to local time.

Argument parsing only (plumbing): every accepted form resolves to a span
``[start, end)`` in the PC's local time zone. A *point* (an ISO date-time,
"now") has ``start == end``; a *period* (an ISO date: that whole day) has an
extent. :func:`resolve_range` turns a tool's ``since``/``until`` into one window:

- ``since`` takes the start of its span, ``until`` the end of its span, so a day
  given as ``until`` is included whole (until="2026-09-20" runs through the end
  of that day).
- A day given alone as ``since`` is the whole window (since="2026-09-20" is that
  day only). A point given alone as ``since`` runs until now. ``until="now"``
  extends any ``since`` to the present.
- Windows never extend past now (nothing is recorded in the future).

Accepted forms: ``now``, an ISO date (``2026-09-20``), an ISO date-time
(``2026-09-20T14:00``, ``2026-09-20 14:00``, with or without an offset or
``Z``) and an ISO time today (``14:00``). Phrases ("yesterday", "last 7 days",
"last friday") are the caller's to turn into dates - a grammar of English time
words here would be code deciding what someone meant (docs/research/no-pattern-audit.md H).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

FORMS_HELP = (
    "use an ISO date or date-time in the PC's local time (2026-09-20, 2026-09-20T14:00) or 'now'; turn "
    "phrases like 'yesterday' or 'last 7 days' into dates first"
)


class TimeArgError(ValueError):
    """A time argument that is not one of the accepted forms."""


@dataclass(frozen=True)
class Span:
    start: datetime          # local, aware
    end: datetime            # local, aware; == start for a point
    period: bool             # True for a day/week/"last N days"; False for a point


def local_now() -> datetime:
    return datetime.now().astimezone()


def _local(d: datetime) -> datetime:
    """Aware local time (a naive value is taken as local)."""
    return d.astimezone() if d.tzinfo is None else d.astimezone()


def _midnight(d: date) -> datetime:
    return datetime.combine(d, time()).astimezone()


def _day_span(d: date) -> Span:
    return Span(_midnight(d), _midnight(d + timedelta(days=1)), True)


def resolve(value: str, now: datetime | None = None) -> Span:
    """The span ``value`` names. Raises :class:`TimeArgError` for anything else."""
    now = now or local_now()
    text = str(value).strip()
    if not text:
        raise TimeArgError("empty time argument")
    today = now.date()
    if text.lower() == "now":
        return Span(now, now, False)
    # ISO forms
    raw = text
    iso = raw[:-1] + "+00:00" if raw.endswith(("Z", "z")) else raw
    try:
        if len(iso) == 10 and iso[4] == "-" and iso[7] == "-":
            return _day_span(date.fromisoformat(iso))
        if ":" in iso and len(iso) <= 8 and "-" not in iso:
            at = datetime.combine(today, time.fromisoformat(iso)).astimezone()
            return Span(at, at, False)
        at = _local(datetime.fromisoformat(iso))
        return Span(at, at, False)
    except ValueError:
        pass
    raise TimeArgError(f"could not read the time {value!r}: {FORMS_HELP}")


@dataclass(frozen=True)
class Window:
    start: datetime | None   # None = no lower bound
    end: datetime | None     # None = no upper bound (== now)

    @property
    def since_ts(self) -> float | None:
        return self.start.timestamp() if self.start else None

    @property
    def until_ts(self) -> float | None:
        return self.end.timestamp() if self.end else None


def resolve_range(
    since: str | None, until: str | None, *, default_today: bool, now: datetime | None = None,
) -> Window:
    """The window for a tool's ``since``/``until`` (see the module doc).

    With neither given: today (local midnight until now) when ``default_today``,
    else unbounded. Raises :class:`TimeArgError` for unreadable values or an
    empty window.
    """
    now = now or local_now()
    s = resolve(since, now) if since and str(since).strip() else None
    u = resolve(until, now) if until and str(until).strip() else None
    if s is None and u is None:
        if default_today:
            return Window(_midnight(now.date()), now)
        return Window(None, None)
    start = s.start if s else None
    if u is not None:
        end = u.end
    elif s is not None and s.period:
        end = s.end
    else:
        end = now
    if end is not None and end > now:
        end = now
    if start is not None and end is not None and start >= end:
        if start > now:
            raise TimeArgError("the time range starts in the future")
        raise TimeArgError("since must be before until")
    return Window(start, end)


def fmt_at(d: datetime | None) -> str:
    """``Thu 2026-09-24 14:05`` (local)."""
    if d is None:
        return "?"
    d = _local(d)
    return d.strftime("%a %Y-%m-%d %H:%M")


def fmt_span(start: datetime | None, end: datetime | None) -> str:
    """``Thu 2026-09-24 09:00-11:30`` or across days ``Wed 2026-09-23 23:10 to Thu 2026-09-24 00:40``."""
    if start is None:
        return fmt_at(end)
    if end is None or end == start:
        return fmt_at(start)
    s, e = _local(start), _local(end)
    if s.date() == e.date():
        return f"{fmt_at(s)}-{e:%H:%M}"
    return f"{fmt_at(s)} to {fmt_at(e)}"


def utc_offset(now: datetime | None = None) -> str:
    """``UTC+05:30``."""
    off = (now or local_now()).utcoffset() or timedelta()
    minutes = int(off.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    minutes = abs(minutes)
    return f"UTC{sign}{minutes // 60:02d}:{minutes % 60:02d}"


def describe(window: Window, now: datetime | None = None) -> str:
    """The line every tool result starts with: what time range it covers."""
    now = now or local_now()
    tz = f"local time, {utc_offset(now)}"
    if window.start is None and window.end is None:
        return f"Covers: all of memory, up to now ({fmt_at(now)}; {tz})."
    if window.start is None:
        return f"Covers: everything before {fmt_at(window.end)} ({tz})."
    end = window.end or now
    suffix = "now; " if abs((end - now).total_seconds()) < 1 else ""
    return f"Covers: {fmt_at(window.start)} to {fmt_at(end)} ({suffix}{tz})."
