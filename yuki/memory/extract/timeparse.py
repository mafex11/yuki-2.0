"""Time labels as chat and mail apps show them -> absolute times.

Parsing of structured labels (architecture rule 1 allows deterministic parsing
of structured data).  Handles the forms seen in Slack, Discord, WhatsApp,
Teams, Gmail, Outlook and LinkedIn, in English:

* clock times: ``18:12``, ``09:54:34``, ``9:04 PM``, ``9:04pm``, ``9.04 p.m.``
* relative days: ``Today``, ``Yesterday``, ``Tomorrow`` (+ ``at`` a time)
* weekdays: ``Saturday``, ``Sat``, ``Monday at 5:00 PM`` - the most recent past one
* dates: ``31 August``, ``Aug 31st``, ``Monday, September 22nd``,
  ``Sep 18, 2026, 6:12 PM``, ``18/09/2026``, ``2026-09-18``, ``18.09.26``
* ago: ``just now``, ``5 min ago``, ``2 hours ago``, ``3d``, ``(6 days ago)``

A label is read against the capture time ``now`` and, for a bare clock time,
the day separator above it (``Today`` / ``31 August``).  Anything that does not
parse is ``None`` - never a guess.  Numeric dates are day-first or month-first
per ``date_order`` ("dmy" | "mdy" | "ymd"), defaulting to the Windows user's
short-date pattern.
"""

from __future__ import annotations

import ctypes
import datetime as _dt
import re
from dataclasses import dataclass
from functools import lru_cache

_MONTHS = {
    "jan": 1, "january": 1, "feb": 2, "february": 2, "mar": 3, "march": 3,
    "apr": 4, "april": 4, "may": 5, "jun": 6, "june": 6, "jul": 7, "july": 7,
    "aug": 8, "august": 8, "sep": 9, "sept": 9, "september": 9, "oct": 10,
    "october": 10, "nov": 11, "november": 11, "dec": 12, "december": 12,
}
_WEEKDAYS = {
    "mon": 0, "monday": 0, "tue": 1, "tues": 1, "tuesday": 1, "wed": 2,
    "wednesday": 2, "thu": 3, "thur": 3, "thurs": 3, "thursday": 3, "fri": 4,
    "friday": 4, "sat": 5, "saturday": 5, "sun": 6, "sunday": 6,
}
_RELATIVE_DAYS = {"today": 0, "yesterday": -1, "tomorrow": 1}
_UNITS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
    "d": 86400, "day": 86400, "days": 86400,
    "w": 604800, "wk": 604800, "week": 604800, "weeks": 604800,
    "mo": 2592000, "month": 2592000, "months": 2592000,
    "y": 31536000, "yr": 31536000, "year": 31536000, "years": 31536000,
}

_MONTH_RE = "|".join(sorted(_MONTHS, key=len, reverse=True))
_WEEKDAY_RE = "|".join(sorted(_WEEKDAYS, key=len, reverse=True))

_CLOCK = re.compile(
    r"(?<![\d:])(\d{1,2})[:.](\d{2})(?::(\d{2}))?(?:\s*([ap])\.?\s?m\b\.?)?(?![\d:])",
    re.IGNORECASE,
)
_CLOCK_BARE_AMPM = re.compile(r"(?<![\d:])(\d{1,2})\s*([ap])\.?\s?m\b\.?", re.IGNORECASE)
_DAY_MONTH = re.compile(
    rf"\b(\d{{1,2}})(?:st|nd|rd|th)?(?:\s+of)?\s+({_MONTH_RE})\.?(?:,?\s+(\d{{4}}))?\b", re.IGNORECASE
)
_MONTH_DAY = re.compile(
    rf"\b({_MONTH_RE})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:,?\s+(\d{{4}}))?", re.IGNORECASE
)
_NUMERIC = re.compile(r"(?<![\d:])(\d{1,4})([/.\-])(\d{1,2})\2(\d{2,4})(?![\d:])")
_WEEKDAY = re.compile(rf"\b({_WEEKDAY_RE})\b\.?", re.IGNORECASE)
_RELDAY = re.compile(r"\b(today|yesterday|tomorrow)\b", re.IGNORECASE)
_AGO = re.compile(
    r"\b(\d+)\s*(" + "|".join(sorted(_UNITS, key=len, reverse=True)) + r")\b\.?(?:\s+ago)?",
    re.IGNORECASE,
)
_JUST_NOW = re.compile(r"\b(just now|now|moments? ago)\b", re.IGNORECASE)

#: Labels longer than this are text, not a time label.
MAX_LABEL = 80


@dataclass(frozen=True)
class Parsed:
    """What a label says.  ``date`` fields are absolute when known."""

    #: "clock" (a time only), "day" (a day only), "datetime", "ago"
    kind: str
    year: int | None = None
    month: int | None = None
    day: int | None = None
    #: days relative to today (Today 0, Yesterday -1)
    rel_days: int | None = None
    weekday: int | None = None
    hour: int | None = None
    minute: int | None = None
    second: int | None = None
    ago_s: int | None = None

    @property
    def has_time(self) -> bool:
        return self.hour is not None

    @property
    def has_day(self) -> bool:
        return self.month is not None or self.rel_days is not None or self.weekday is not None


@lru_cache(maxsize=1)
def default_date_order() -> str:
    """"dmy" / "mdy" / "ymd" from the Windows user's short date pattern."""
    try:
        buf = ctypes.create_unicode_buffer(80)
        # LOCALE_NAME_USER_DEFAULT = None, LOCALE_SSHORTDATE = 0x1F
        if ctypes.windll.kernel32.GetLocaleInfoEx(None, 0x1F, buf, 80):
            pattern = buf.value.lower()
            positions = {k: pattern.find(k) for k in "dmy" if pattern.find(k) >= 0}
            order = "".join(sorted(positions, key=positions.get))
            if order in ("dmy", "mdy", "ymd"):
                return order
    except Exception:
        pass
    return "mdy"


def _year(raw: str | None) -> int | None:
    if not raw:
        return None
    value = int(raw)
    return value + 2000 if value < 100 else value


def _clock(label: str) -> tuple[int, int, int | None] | None:
    match = _CLOCK.search(label)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        second = int(match.group(3)) if match.group(3) else None
        ampm = (match.group(4) or "").lower()
    else:
        match = _CLOCK_BARE_AMPM.search(label)
        if not match:
            return None
        hour, minute, second, ampm = int(match.group(1)), 0, None, match.group(2).lower()
    if ampm:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if ampm == "p" else 0)
    if hour > 23 or minute > 59 or (second is not None and second > 59):
        return None
    return hour, minute, second


def parse(label: str | None, *, date_order: str | None = None) -> Parsed | None:
    """What ``label`` says about when, or None when it is not a time label."""
    if not label:
        return None
    text = " ".join(label.split())
    if not text or len(text) > MAX_LABEL:
        return None
    lowered = text.lower()
    order = date_order or default_date_order()
    fields: dict[str, int | None] = {}

    if _JUST_NOW.search(lowered) and not any(ch.isdigit() for ch in lowered):
        return Parsed(kind="ago", ago_s=0)

    date_found = False
    numeric = _NUMERIC.search(text)
    if numeric:
        a, b, c = numeric.group(1), numeric.group(3), numeric.group(4)
        if len(a) == 4:
            y, m, d = int(a), int(b), int(c)
        elif order == "dmy":
            d, m, y = int(a), int(b), _year(c)
        elif order == "ymd":
            y, m, d = _year(a), int(b), int(c)
        else:
            m, d, y = int(a), int(b), _year(c)
        if len(a) != 4 and order != "ymd" and m > 12 >= d:
            m, d = d, m  # "18/09/2026" under a month-first locale: only one reading exists
        if y and 1 <= (m or 0) <= 12 and 1 <= (d or 0) <= 31:
            fields.update(year=y, month=m, day=d)
            date_found = True
    if not date_found:
        dm = _DAY_MONTH.search(text)
        md = _MONTH_DAY.search(text)
        hit = dm or md
        if dm and md:
            hit = dm if dm.start() <= md.start() else md
        if hit is dm and dm:
            fields.update(day=int(dm.group(1)), month=_MONTHS[dm.group(2).lower()], year=_year(dm.group(3)))
            date_found = 1 <= int(dm.group(1)) <= 31
        elif hit is md and md:
            fields.update(day=int(md.group(2)), month=_MONTHS[md.group(1).lower()], year=_year(md.group(3)))
            date_found = 1 <= int(md.group(2)) <= 31
        if not date_found:
            fields.clear()
    rel = _RELDAY.search(text)
    if not date_found and rel:
        fields["rel_days"] = _RELATIVE_DAYS[rel.group(1).lower()]
        date_found = True
    weekday = _WEEKDAY.search(text)
    if weekday and not date_found:
        fields["weekday"] = _WEEKDAYS[weekday.group(1).lower()]
        date_found = True

    # The clock must not be read out of the date itself ("18.09.26").
    clock_text = text[: numeric.start()] + " " + text[numeric.end() :] if (numeric and fields.get("year")) else text
    clock = _clock(clock_text)
    if clock is not None:
        fields.update(hour=clock[0], minute=clock[1], second=clock[2])

    if clock is None and not date_found:
        ago = _AGO.search(lowered)
        # "3d" or "5 min ago": the whole label is the amount (plus "ago" / parentheses).
        if ago and len(re.sub(r"[()\s]", "", lowered)) <= len(ago.group(0).replace(" ", "")) + 3:
            return Parsed(kind="ago", ago_s=int(ago.group(1)) * _UNITS[ago.group(2).lower()])
        return None
    kind = "datetime" if (clock is not None and date_found) else ("clock" if clock is not None else "day")
    return Parsed(kind=kind, **fields)  # type: ignore[arg-type]


def is_day_label(label: str | None, *, date_order: str | None = None) -> bool:
    """Whether ``label`` names a day only ("Today", "Saturday", "31 August")."""
    parsed = parse(label, date_order=date_order)
    return parsed is not None and parsed.kind == "day"


def _resolve_day(parsed: Parsed, now: _dt.datetime) -> _dt.date | None:
    today = now.date()
    if parsed.month is not None and parsed.day is not None:
        year = parsed.year or today.year
        try:
            day = _dt.date(year, parsed.month, parsed.day)
        except ValueError:
            return None
        if parsed.year is None and day > today + _dt.timedelta(days=1):
            try:
                day = _dt.date(year - 1, parsed.month, parsed.day)
            except ValueError:
                return None
        return day
    if parsed.rel_days is not None:
        return today + _dt.timedelta(days=parsed.rel_days)
    if parsed.weekday is not None:
        back = (today.weekday() - parsed.weekday) % 7 or 7
        return today - _dt.timedelta(days=back)
    return None


def resolve(
    label: str | None,
    *,
    now: float,
    day_label: str | None = None,
    date_order: str | None = None,
) -> float | None:
    """Best epoch for ``label`` seen at ``now``, under the day separator ``day_label``."""
    parsed = parse(label, date_order=date_order)
    if parsed is None:
        return None
    local_now = _dt.datetime.fromtimestamp(now).astimezone()
    if parsed.kind == "ago":
        return now - float(parsed.ago_s or 0)
    day: _dt.date | None = _resolve_day(parsed, local_now) if parsed.has_day else None
    if day is None and day_label:
        context = parse(day_label, date_order=date_order)
        if context is not None and context.has_day:
            day = _resolve_day(context, local_now)
    if not parsed.has_time:
        if day is None:
            return None
        moment = _dt.datetime(day.year, day.month, day.day, tzinfo=local_now.tzinfo)
        return moment.timestamp()
    if day is None:
        # A bare clock time without a day separator: the latest such time not
        # after now (a minute of slack for clocks that run ahead).
        day = local_now.date()
        candidate = _local(day, parsed, local_now)
        if candidate.timestamp() > now + 60:
            candidate = _local(day - _dt.timedelta(days=1), parsed, local_now)
        return candidate.timestamp()
    return _local(day, parsed, local_now).timestamp()


def _local(day: _dt.date, parsed: Parsed, reference: _dt.datetime) -> _dt.datetime:
    naive = _dt.datetime(day.year, day.month, day.day, parsed.hour or 0, parsed.minute or 0, parsed.second or 0)
    # Local wall time -> the zone in force on that day (DST-safe via mktime).
    import time as _time

    return _dt.datetime.fromtimestamp(_time.mktime(naive.timetuple())).astimezone()


def time_of_day(label: str | None, *, date_order: str | None = None) -> str:
    """``"HH:MM"`` (24 h) the label shows, "" when it shows no clock time.

    The one part of a label that stays the same while an app re-words it
    ("Today at 6:12 PM" -> "Yesterday at 6:12 PM" -> "18/09/2026 18:12"), so it
    is what goes into a message fingerprint.  Seconds are dropped: one view of
    an app shows them and another does not.
    """
    parsed = parse(label, date_order=date_order)
    if parsed is None or parsed.hour is None:
        return ""
    return f"{parsed.hour:02d}:{parsed.minute or 0:02d}"


_LABEL_WORDS = frozenset(
    {"at", "am", "pm", "a", "p", "m", "ago", "of", "on", "the", "last", "sent", "edited", "received", "(edited)"}
    | set(_MONTHS) | set(_WEEKDAYS) | set(_RELATIVE_DAYS) | set(_UNITS) | {"just", "now", "moment", "moments"}
)


def is_time_label(text: str | None, *, date_order: str | None = None, max_len: int = 48) -> bool:
    """Whether ``text`` is a time label and nothing else (for unanchored detection).

    A message saying "see you on 5 June at 10:00" parses as a time too; a label
    is short and made of time words, with at most one other word.
    """
    if not text or len(text) > max_len:
        return False
    if not any(ch.isdigit() for ch in text):
        words = re.findall(r"[a-z]+", text.lower())
        if not words or not any(w in _WEEKDAYS or w in _RELATIVE_DAYS or w in ("now", "just") for w in words):
            return False
    if parse(text, date_order=date_order) is None:
        return False
    words = re.findall(r"[A-Za-z]+", text.lower())
    others = [w for w in words if w not in _LABEL_WORDS and not re.fullmatch(r"(st|nd|rd|th)", w)]
    return len(others) <= 1
