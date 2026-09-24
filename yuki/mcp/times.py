"""Time arguments of the MCP tools: ISO strings or plain phrases, resolved to local time.

Argument parsing only (plumbing): every accepted form resolves to a span
``[start, end)`` in the PC's local time zone. A *point* (an ISO date-time,
"now", "3 hours ago") has ``start == end``; a *period* (a day, a week, "last 7
days") has an extent. :func:`resolve_range` turns a tool's ``since``/``until``
into one window:

- ``since`` takes the start of its span, ``until`` the end of its span, so a day
  given as ``until`` is included whole (until="yesterday" runs through the end of
  yesterday; until="2026-09-20" through the end of that day).
- A period given alone as ``since`` is the whole window (since="yesterday" is
  yesterday only; since="last 7 days" ends now). A point given alone as ``since``
  runs until now. ``until="now"`` extends any ``since`` to the present.
- Windows never extend past now (nothing is recorded in the future).

Accepted forms (case-insensitive): ``now``, ``today``, ``yesterday``, ``day
before yesterday``, ``this week`` / ``last week`` (calendar weeks from Monday),
``this month`` / ``last month``, ``this year``, ``last|past N
minutes|hours|days|weeks|months`` (also ``last hour``, ``past day``), ``N
minutes|hours|days|weeks ago``, a weekday (``monday``: the latest Monday, today
included; ``last monday``: the latest before today), an ISO date
(``2026-09-20``), an ISO date-time (``2026-09-20T14:00``, ``2026-09-20 14:00``,
with or without an offset or ``Z``) and a time today (``14:00``).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

WEEKDAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_UNITS = {
    "minute": timedelta(minutes=1), "min": timedelta(minutes=1), "mins": timedelta(minutes=1),
    "hour": timedelta(hours=1), "hr": timedelta(hours=1), "hrs": timedelta(hours=1),
    "day": timedelta(days=1), "week": timedelta(weeks=1),
}
FORMS_HELP = (
    "use an ISO date or date-time (2026-09-20, 2026-09-20T14:00) or a phrase: now, today, yesterday, "
    "this week, last week, this month, last month, last 7 days, past 3 hours, 2 days ago, monday, last friday"
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


def _month_start(d: date, back: int = 0) -> date:
    y, m = d.year, d.month - back
    while m <= 0:
        y, m = y - 1, m + 12
    return date(y, m, 1)


def _count(word: str) -> int | None:
    words = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "fourteen": 14, "thirty": 30}
    if word.isdigit():
        return int(word)
    return words.get(word)


def _unit(word: str) -> str | None:
    w = word[:-1] if word.endswith("s") and word[:-1] in ("minute", "hour", "day", "week", "month", "year") else word
    if w in _UNITS or w in ("month", "year"):
        return w
    return None


def _minus(now: datetime, n: int, unit: str) -> datetime:
    if unit == "month":
        target = _month_start(now.date(), n)
        day = min(now.day, 28)
        return _local(datetime.combine(target.replace(day=day), now.time().replace(tzinfo=None)))
    if unit == "year":
        return _minus(now, 12 * n, "month")
    return now - _UNITS[unit] * n


def resolve(value: str, now: datetime | None = None) -> Span:
    """The span ``value`` names. Raises :class:`TimeArgError` for anything else."""
    now = now or local_now()
    text = " ".join(str(value).strip().lower().replace(",", " ").split())
    if not text:
        raise TimeArgError("empty time argument")
    today = now.date()
    if text == "now":
        return Span(now, now, False)
    if text == "today":
        return _day_span(today)
    if text == "yesterday":
        return _day_span(today - timedelta(days=1))
    if text in ("day before yesterday", "the day before yesterday"):
        return _day_span(today - timedelta(days=2))
    monday = today - timedelta(days=today.weekday())
    if text == "this week":
        return Span(_midnight(monday), _midnight(monday + timedelta(days=7)), True)
    if text == "last week":
        return Span(_midnight(monday - timedelta(days=7)), _midnight(monday), True)
    if text == "this month":
        start = _month_start(today)
        return Span(_midnight(start), _midnight(_month_start(start + timedelta(days=32))), True)
    if text == "last month":
        start = _month_start(today, 1)
        return Span(_midnight(start), _midnight(_month_start(today)), True)
    if text == "this year":
        return Span(_midnight(date(today.year, 1, 1)), _midnight(date(today.year + 1, 1, 1)), True)
    words = text.split()
    # weekday: "monday", "on monday", "last monday"
    if words and words[0] == "on":
        words = words[1:]
    if len(words) in (1, 2) and words[-1] in WEEKDAYS and (len(words) == 1 or words[0] in ("last", "past")):
        back = (today.weekday() - WEEKDAYS.index(words[-1])) % 7
        if len(words) == 2 and back == 0:
            back = 7
        return _day_span(today - timedelta(days=back))
    # "last 7 days", "past 3 hours", "last hour", "past day", "the last 2 weeks"
    if words and words[0] == "the":
        words = words[1:]
    if len(words) in (2, 3) and words[0] in ("last", "past"):
        n = _count(words[1]) if len(words) == 3 else 1
        unit = _unit(words[-1])
        if n is not None and unit and n > 0:
            return Span(_minus(now, n, unit), now, True)
    # "3 hours ago", "a week ago"
    if len(words) == 3 and words[2] == "ago":
        n, unit = _count(words[0]), _unit(words[1])
        if n is not None and unit:
            at = _minus(now, n, unit)
            return Span(at, at, False)
    # ISO forms
    raw = str(value).strip()
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
