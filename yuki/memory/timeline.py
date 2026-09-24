"""The foreground timeline: which window and page was in front, and whether the user was at it.

Contract: ``docs/MEMORY.md`` (captures say *what* was on screen; the timeline
says *how long* and *in what order*, so episodes can connect the dots:
"reels in Chrome, then Claude, back and forth for two hours").

Two parts:

* :func:`aggregate`, :func:`sequence` and :func:`breaks` - pure functions over
  :class:`~yuki.memory.store.TimelineRow` lists (time per app / site / page,
  visits, longest uninterrupted stretch, switches, back-and-forth pairs,
  media time).  No Win32 here, so the API, the episode worker and the portrait
  import this module cheaply.
* :class:`TimelineRecorder` - one thread in ``yuki-memory`` that follows the
  foreground with the same OS window events the watcher uses
  (``EVENT_SYSTEM_FOREGROUND`` system-wide, ``EVENT_OBJECT_NAMECHANGE`` of the
  foreground process for title changes), and writes *stretches*:

  - start, end, process, app name, window title (encrypted);
  - for a browser page, the page's host and path (path encrypted), read with
    perception's page-only pass (:func:`yuki.perception.tree._read_pages`: the
    window frame and its Document elements, nothing inside the page) on a
    worker thread of its own, bounded to :data:`PAGE_PROBE_S`;
  - ``present`` vs ``away``: present while the last keyboard/mouse input
    (``GetLastInputInfo``, system idle time - no hooks, no input monitoring)
    is under :data:`AWAY_AFTER_S` old, or while the app in front plays media
    (``passive``: watching); otherwise ``away``;
  - media: the Windows media sessions (:func:`yuki.perception.system.media_sessions`,
    the media part of ``activity_facts()``), polled every
    :data:`MEDIA_EVERY_S`; a playing session is joined to the app in front by
    OS identity (AppUserModelID, the Start menu entry, the image name), other
    playing apps are kept as background media.

  Consecutive stretches of the same page (same host + path, or same title
  for a window without a page) in the same state are merged into one row.
  Privacy follows :mod:`yuki.memory.privacy`: a blocked app is recorded
  without app, title or page; a private window or a blocked address keeps the
  app name only.  Nothing is recorded while the user has paused memory or the
  session is locked - and then no hooks but the foreground one stay
  registered.

  - **Full screen** (a game, an F11 video, a presentation) is recorded as
    ``fullscreen`` rows.  While the privacy file pauses content in full
    screen (``[pause] when_fullscreen``, the default) such a row carries only
    the process and app name, start/end, present vs away and media - the
    window's title and page are not read, no title hook is kept.  When the
    same window was showing a site just before it went full screen, that
    host (not the path) is carried over, so "YouTube full screen" is known
    without reading the page.
  - **Meetings**: an app or page in the privacy file's ``[meetings]`` table
    (:class:`MeetingRules`) is recorded as a ``meeting`` row: app, host and
    service name ("Google Meet"), start/end - no title, no path.  Its content
    stays blocked by the ``[apps]``/``[web]`` rules.  While a meeting is in
    front the microphone users (ConsentStore, as
    :func:`yuki.perception.system.activity_facts`) are read every
    :data:`MEDIA_EVERY_S`; the app in front using the microphone counts as
    present without input (``passive_s``) and as ``mic_s``.

Nothing here sends input, changes focus, launches anything or takes pixels.
Logs are content-free.
"""

from __future__ import annotations

import ctypes
import threading
import time
import tomllib
import traceback
from collections import Counter
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from yuki.memory.store import Store, TimelineRow, app_key, url_host

# ---------------------------------------------------------------------------
# Budgets (plumbing, not behaviour)
# ---------------------------------------------------------------------------

#: No input for longer than this = away (unless the app in front plays media).
AWAY_AFTER_S = 60.0
#: Wake-up while the user is active; while idle past the threshold, every AWAY_TICK_S.
TICK_S = 15.0
AWAY_TICK_S = 2.0
#: Media sessions are read this often (a few ms each).
MEDIA_EVERY_S = 15.0
MEDIA_TIMEOUT_S = 0.2
#: The open stretch is written this often, so readers see it and a crash loses little.
FLUSH_EVERY_S = 30.0
#: A title change is acted on once the title has been quiet this long (or at the cap).
TITLE_SETTLE_S = 0.5
TITLE_SETTLE_CAP_S = 2.0
#: A stretch younger than this that is not yet written takes a title change in place
#: (a page loading: "New Tab" -> "Loading..." -> its title).
YOUNG_S = 5.0
#: Stretches of the same page closer than this merge into one row.
MERGE_GAP_S = 2.0
#: Stretches shorter than this are not written (alt-tab flicker).
MIN_ROW_S = 1.0
#: Budget of one page-identity read, and how long a stretch waits for it.
PAGE_PROBE_S = 1.0
PAGE_WAIT_S = 2.5
#: A page in front is read again this often (a single-page site changes address
#: without a title change: reels, feeds).
PAGE_REPROBE_S = 60.0
#: A probe still running after this is reported stuck; new probes are skipped meanwhile.
PAGE_STUCK_S = 5.0
#: A process whose last N probes found no page is probed at most once per this long.
NO_PAGE_STREAK = 3
NO_PAGE_BACKOFF_S = 300.0
#: A gap between two accounting passes longer than this (sleep, hibernation,
#: a stalled process) is left out of the timeline rather than counted.
MAX_TICK_GAP_S = 120.0

#: Aggregation: consecutive stretches of one activity closer than this are one visit.
RUN_GAP_S = 60.0
#: Away (or untracked) this long is a break.
BREAK_S = 600.0
#: Aggregation: stretches of one meeting service closer than this are one meeting
#: (the user looked at another window for a while during the call).
MEETING_GAP_S = 300.0


# ---------------------------------------------------------------------------
# Privacy-file tables this module and yuki.memory.warp read ([meetings], [terminal])
# ---------------------------------------------------------------------------


class PrivacySection:
    """One table of the privacy file that :class:`yuki.memory.privacy.PrivacyRules` does not parse.

    Re-read whenever the file's modification time or size moves (a ``stat``
    per call, like :class:`~yuki.memory.privacy.PrivacyConfig`).  A table the
    user's file lacks comes from the packaged defaults; a file that fails to
    parse keeps the last good value (or the defaults) and sets
    :attr:`last_error`, so a typo never opens a gate.

    Args:
        config: the service's ``PrivacyConfig`` (its ``path`` is read), or None for defaults only.
        name: the table name, e.g. ``"meetings"``.
        parse: ``parse(table: dict) -> value``.
    """

    def __init__(self, config: Any, name: str, parse: Callable[[dict], Any]) -> None:
        self._config = config
        self._name = name
        self._parse = parse
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._value: Any = None
        self.last_error = ""

    def _default_table(self) -> dict:
        from yuki.memory.privacy import DEFAULT_PATH

        with DEFAULT_PATH.open("rb") as fh:
            return tomllib.load(fh).get(self._name) or {}

    def get(self) -> Any:
        with self._lock:
            path = getattr(self._config, "path", None)
            stamp = None
            if path is not None:
                try:
                    st = Path(path).stat()
                    stamp = (st.st_mtime_ns, st.st_size)
                except OSError:
                    stamp = None
            if self._value is not None and stamp == self._stamp:
                return self._value
            self._stamp = stamp
            try:
                table = None
                if stamp is not None:
                    with Path(path).open("rb") as fh:
                        table = tomllib.load(fh).get(self._name)
                if not isinstance(table, dict):
                    table = self._default_table()
                self._value = self._parse(table)
                self.last_error = ""
            except Exception as exc:  # keep the last good value
                self.last_error = f"{type(exc).__name__}: {exc}"
                if self._value is None:
                    self._value = self._parse(self._default_table())
            return self._value


def _host_matches(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def _name_key(value: str) -> str:
    """A ``[meetings.names]`` key: a process as :func:`app_key`, a host lower-cased."""
    value = str(value).strip().lower()
    return (app_key(value) or "") if value.endswith(".exe") else value


@dataclass(frozen=True)
class MeetingRules:
    """The privacy file's ``[meetings]`` table: apps and pages recorded as meeting hours only."""

    processes: frozenset[str] = frozenset()               # app_key form ("zoom")
    host_suffixes: tuple[str, ...] = ()
    host_paths: tuple[tuple[str, tuple[str, ...]], ...] = ()
    names: tuple[tuple[str, str], ...] = ()               # (process key or host, service name)

    @classmethod
    def from_dict(cls, table: dict) -> MeetingRules:
        table = table or {}
        processes = frozenset(k for k in (app_key(str(p)) for p in table.get("processes", []) or []) if k)
        hosts = tuple(str(h).strip().lower() for h in table.get("host_suffixes", []) or [] if str(h).strip())
        paths = tuple(
            (str(h).strip().lower(), tuple(str(f).lower() for f in (frags or []) if str(f)))
            for h, frags in (table.get("host_paths", {}) or {}).items()
        )
        names = tuple((_name_key(k), str(v)) for k, v in (table.get("names", {}) or {}).items() if str(v).strip())
        return cls(processes=processes, host_suffixes=hosts, host_paths=paths, names=names)

    def _name(self, key: str) -> str | None:
        return next((v for k, v in self.names if k == key), None)

    def match_process(self, process_name: str, app_name: str = "") -> str | None:
        """The meeting service a process is (its name from ``names``, else the app name), or None."""
        key = app_key(process_name)
        if not key or key not in self.processes:
            return None
        return self._name(key) or (app_name or "").strip() or key

    def match_url(self, url: str) -> str | None:
        """The meeting service an http(s) page is, or None."""
        try:
            parts = urlsplit((url or "").strip())
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https") or not host:
            return None
        for suffix in self.host_suffixes:
            if _host_matches(host, suffix):
                return self._name(suffix) or site_of(host)
        path = (parts.path or "").lower()
        for rule_host, fragments in self.host_paths:
            if _host_matches(host, rule_host) and any(f in path for f in fragments):
                return self._name(rule_host) or site_of(host)
        return None


# ---------------------------------------------------------------------------
# Aggregation (pure)
# ---------------------------------------------------------------------------


def format_duration(seconds: float) -> str:
    """``40s``, ``25m``, ``1h50m``."""
    s = max(0.0, float(seconds or 0.0))
    if s < 60:
        return f"{int(round(s))}s"
    minutes = int(round(s / 60.0))
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h{minutes % 60:02d}m"


def site_of(host: str | None) -> str | None:
    """A page host as a site: lower-case, without a leading ``www.``."""
    if not host:
        return None
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


@dataclass
class _Clip:
    row: TimelineRow
    start: float
    end: float
    f: float

    def val(self, name: str) -> float:
        return float(getattr(self.row, name) or 0.0) * self.f

    @property
    def present_s(self) -> float:
        return self.val("active_s") + self.val("passive_s")


def _clip(rows: Sequence[TimelineRow], since: float | None, until: float | None) -> list[_Clip]:
    out: list[_Clip] = []
    for row in sorted(rows, key=lambda r: (r.started_at, r.id or 0)):
        start = max(row.started_at, since) if since is not None else row.started_at
        end = min(row.ended_at, until) if until is not None else row.ended_at
        if end <= start:
            continue
        span = row.ended_at - row.started_at
        out.append(_Clip(row, start, end, (end - start) / span if span > 0 else 1.0))
    return out


def _app_label(row: TimelineRow) -> str:
    return row.app or row.process or "(unknown app)"


def group_of(row: TimelineRow, group_by: str) -> tuple[str, str]:
    """``(key, label)`` of the activity a row belongs to at this grouping.

    A meeting is its own activity at every grouping ("in a meeting (Google
    Meet)"); a full-screen row belongs to its app or site like any other
    (its full-screen time is counted separately).
    """
    if row.meeting:
        return f"meeting:{row.meeting}", f"in a meeting ({row.meeting})"
    if row.withheld:
        if not row.app:
            return "withheld", "(private)"
        return f"withheld:{row.process or row.app}", f"(private) in {row.app}"
    if group_by == "app":
        return f"app:{row.process or row.app}", _app_label(row)
    if group_by == "page":
        if row.host or row.path:
            label = (row.title or "").strip() or f"{site_of(row.host) or ''}{row.path or ''}"
        else:
            label = (row.title or "").strip() or _app_label(row)
        if row.fullscreen and not (row.title or "").strip():
            label += " (full screen)"
        return f"page:{row.page_key}", label
    site = site_of(row.host)
    if site:
        return f"site:{site}", site
    return f"app:{row.process or row.app}", _app_label(row)


@dataclass
class Run:
    """An uninterrupted stretch of one activity: consecutive present rows of one group."""

    key: str
    label: str
    start: float
    end: float
    active_s: float = 0.0
    passive_s: float = 0.0
    media_s: float = 0.0
    fullscreen_s: float = 0.0
    mic_s: float = 0.0
    apps: Counter = field(default_factory=Counter)
    hosts: Counter = field(default_factory=Counter)
    titles: Counter = field(default_factory=Counter)

    @property
    def present_s(self) -> float:
        return self.active_s + self.passive_s


def _runs(clips: Sequence[_Clip], group_by: str) -> list[tuple[Run, bool]]:
    """Runs in time order, each with whether it directly follows the previous one (a switch)."""
    runs: list[tuple[Run, bool]] = []
    current: Run | None = None
    last_end: float | None = None
    broken = True  # an away row or a gap since the current run
    for c in clips:
        row = c.row
        if row.state != "present":
            broken = True
            last_end = c.end
            continue
        key, label = group_of(row, group_by)
        gap = (c.start - last_end) if last_end is not None else None
        interrupted = broken or gap is None or gap > RUN_GAP_S
        if current is not None and current.key == key and not interrupted:
            current.end = c.end
        else:
            adjacent = current is not None and not interrupted
            current = Run(key=key, label=label, start=c.start, end=c.end)
            runs.append((current, adjacent))
        current.active_s += c.val("active_s")
        current.passive_s += c.val("passive_s")
        current.media_s += c.val("media_s")
        current.mic_s += c.val("mic_s")
        if row.fullscreen:
            current.fullscreen_s += c.present_s
        current.apps[_app_label(row)] += c.present_s
        if row.host:
            current.hosts[site_of(row.host)] += c.present_s
        if row.title and not row.withheld:
            current.titles[row.title.strip()] += c.present_s
        broken = False
        last_end = c.end
    return runs


def aggregate(
    rows: Sequence[TimelineRow], since: float | None = None, until: float | None = None,
    group_by: str = "site", *, limit: int | None = None, titles: int = 3,
) -> dict[str, Any]:
    """Time use in ``[since, until)`` per activity (``group_by``: app | site | page).

    Rows are clipped to the window (their counters scaled by the overlap).
    Per activity: present time (active = with input, passive = no input
    while that app played media), media time, visits (uninterrupted runs:
    another activity, an away stretch or a gap over :data:`RUN_GAP_S` ends
    one), the longest run, and the titles shown longest.  Totals add away
    time, switches (one activity directly followed by another) and
    background media.  ``interleaving`` lists pairs of activities the user
    went back and forth between, with the number of switches between them.

    Full screen: ``fullscreen_s`` per activity and in the totals is present
    time with a full-screen window in front.  Meetings are their own
    activities ("in a meeting (Zoom)"); ``meetings`` lists each meeting as one
    span - stretches of one service less than :data:`MEETING_GAP_S` apart
    are one meeting - with ``in_front_s`` (time it was the window in front),
    ``present_s`` and ``mic_s`` (the app in front using the microphone);
    ``totals.meeting_s`` is the present time in meetings.
    """
    if group_by not in ("app", "site", "page"):
        raise ValueError(f"group_by must be app, site or page, not {group_by!r}")
    clips = _clip(rows, since, until)
    runs = _runs(clips, group_by)
    items: dict[str, dict[str, Any]] = {}
    pairs: Counter = Counter()
    switches = 0
    previous: Run | None = None
    for run, adjacent in runs:
        item = items.get(run.key)
        if item is None:
            item = items[run.key] = {
                "key": run.key, "label": run.label, "apps": Counter(), "hosts": Counter(), "titles": Counter(),
                "present_s": 0.0, "active_s": 0.0, "passive_s": 0.0, "media_s": 0.0, "fullscreen_s": 0.0,
                "mic_s": 0.0, "meeting": run.key.startswith("meeting:"), "visits": 0,
                "longest_s": 0.0, "longest_start": None, "longest_end": None,
                "first_at": run.start, "last_at": run.end,
            }
        item["present_s"] += run.present_s
        item["active_s"] += run.active_s
        item["passive_s"] += run.passive_s
        item["media_s"] += run.media_s
        item["fullscreen_s"] += run.fullscreen_s
        item["mic_s"] += run.mic_s
        item["visits"] += 1
        item["apps"].update(run.apps)
        item["hosts"].update(run.hosts)
        item["titles"].update(run.titles)
        item["last_at"] = max(item["last_at"], run.end)
        if run.present_s > item["longest_s"]:
            item["longest_s"], item["longest_start"], item["longest_end"] = run.present_s, run.start, run.end
        if adjacent and previous is not None and previous.key != run.key:
            switches += 1
            pairs[tuple(sorted((previous.key, run.key)))] += 1
        previous = run
    labels = {k: v["label"] for k, v in items.items()}
    ordered = sorted(items.values(), key=lambda i: -i["present_s"])
    total_items = len(ordered)
    if limit is not None:
        ordered = ordered[: max(0, int(limit))]
    out_items = []
    for item in ordered:
        apps = item.pop("apps")
        hosts = item.pop("hosts")
        top_titles = item.pop("titles")
        item["app"] = apps.most_common(1)[0][0] if apps else None
        item["host"] = hosts.most_common(1)[0][0] if hosts else None
        item["titles"] = [
            {"title": t, "present_s": round(s, 1)} for t, s in top_titles.most_common(titles) if t
        ] if titles else []
        for k in ("present_s", "active_s", "passive_s", "media_s", "fullscreen_s", "mic_s", "longest_s"):
            item[k] = round(item[k], 1)
        out_items.append(item)
    background: Counter = Counter()
    for c in clips:
        for app, seconds in (c.row.media_other or {}).items():
            background[app] += float(seconds) * c.f
    present = [c for c in clips if c.row.state == "present"]
    totals = {
        "present_s": round(sum(c.present_s for c in present), 1),
        "active_s": round(sum(c.val("active_s") for c in present), 1),
        "passive_s": round(sum(c.val("passive_s") for c in present), 1),
        "away_s": round(sum(c.val("away_s") for c in clips), 1),
        "media_s": round(sum(c.val("media_s") for c in clips), 1),
        "background_media_s": round(sum(background.values()), 1),
        "fullscreen_s": round(sum(c.present_s for c in present if c.row.fullscreen), 1),
        "meeting_s": round(sum(c.present_s for c in present if c.row.meeting), 1),
        "switches": switches,
        "activities": total_items,
        "first_at": clips[0].start if clips else None,
        "last_at": clips[-1].end if clips else None,
    }
    return {
        "since": since, "until": until, "group_by": group_by, "totals": totals, "items": out_items,
        "interleaving": [
            {"a": labels[a], "b": labels[b], "switches": n} for (a, b), n in pairs.most_common(8) if n >= 2
        ],
        "background_media": [{"app": a, "seconds": round(s, 1)} for a, s in background.most_common(8) if s >= 1],
        "meetings": meetings(clips),
    }


def meetings(clips: Sequence[_Clip]) -> list[dict[str, Any]]:
    """Meetings as spans: stretches of one service closer than :data:`MEETING_GAP_S` joined, oldest first."""
    spans: list[dict[str, Any]] = []
    open_by: dict[str, dict[str, Any]] = {}
    for c in clips:
        label = c.row.meeting
        if not label:
            continue
        span = open_by.get(label)
        if span is None or c.start - span["end"] > MEETING_GAP_S:
            span = {"label": label, "apps": Counter(), "hosts": Counter(), "start": c.start, "end": c.end,
                    "in_front_s": 0.0, "present_s": 0.0, "mic_s": 0.0, "fullscreen_s": 0.0}
            open_by[label] = span
            spans.append(span)
        span["end"] = max(span["end"], c.end)
        span["in_front_s"] += c.end - c.start
        span["present_s"] += c.present_s if c.row.state == "present" else 0.0
        span["mic_s"] += c.val("mic_s")
        if c.row.fullscreen:
            span["fullscreen_s"] += c.end - c.start
        span["apps"][_app_label(c.row)] += c.end - c.start
        if c.row.host:
            span["hosts"][site_of(c.row.host)] += c.end - c.start
    out = []
    for span in sorted(spans, key=lambda s: s["start"]):
        apps, hosts = span.pop("apps"), span.pop("hosts")
        span["app"] = apps.most_common(1)[0][0] if apps else None
        span["host"] = hosts.most_common(1)[0][0] if hosts else None
        span["seconds"] = round(span["end"] - span["start"], 1)
        for k in ("in_front_s", "present_s", "mic_s", "fullscreen_s"):
            span[k] = round(span[k], 1)
        out.append(span)
    return out


def sequence(
    rows: Sequence[TimelineRow], since: float | None = None, until: float | None = None,
    group_by: str = "site", *, min_run_s: float = 60.0, max_lines: int = 80,
) -> list[dict[str, Any]]:
    """The window as an ordered list of runs and away spans, for a reader to follow the flow.

    Runs shorter than ``min_run_s`` are folded into ``{"kind": "short", "count",
    "present_s", "labels"}`` entries between the longer ones; away spans and
    untracked gaps of at least ``min_run_s`` appear as ``{"kind": "away" | "gap"}``.
    At most ``max_lines`` entries (the earliest are merged into one note when cut).
    """
    clips = _clip(rows, since, until)
    out: list[dict[str, Any]] = []
    short: dict[str, Any] | None = None

    def flush_short() -> None:
        nonlocal short
        if short is not None:
            short["labels"] = [label for label, _ in short.pop("_labels").most_common(4)]
            out.append(short)
            short = None

    last_end: float | None = None
    away_start: float | None = None
    away_end: float | None = None
    events: list[tuple[float, str, Any]] = []
    for c in clips:
        if last_end is not None and c.start - last_end >= min_run_s:
            events.append((last_end, "gap", (last_end, c.start)))
        last_end = c.end
        if c.row.state != "present":
            if away_start is not None and away_end is not None and c.start - away_end <= RUN_GAP_S:
                away_end = c.end
            else:
                if away_start is not None:
                    events.append((away_start, "away", (away_start, away_end)))
                away_start, away_end = c.start, c.end
        elif away_start is not None:
            events.append((away_start, "away", (away_start, away_end)))
            away_start = away_end = None
    if away_start is not None:
        events.append((away_start, "away", (away_start, away_end)))
    for run, _ in _runs(clips, group_by):
        events.append((run.start, "run", run))
    events.sort(key=lambda e: e[0])
    for _, kind, value in events:
        if kind in ("away", "gap"):
            start, end = value
            if end - start < min_run_s:
                continue
            flush_short()
            out.append({"kind": kind, "start": start, "end": end, "seconds": round(end - start, 1)})
            continue
        run: Run = value
        if run.present_s < min_run_s:
            if short is None:
                short = {"kind": "short", "start": run.start, "end": run.end, "count": 0, "present_s": 0.0,
                         "_labels": Counter()}
            short["count"] += 1
            short["end"] = run.end
            short["present_s"] = round(short["present_s"] + run.present_s, 1)
            short["_labels"][run.label] += 1
            continue
        flush_short()
        out.append({
            "kind": "run", "label": run.label, "start": run.start, "end": run.end,
            "present_s": round(run.present_s, 1), "active_s": round(run.active_s, 1),
            "passive_s": round(run.passive_s, 1), "media_s": round(run.media_s, 1),
            "fullscreen_s": round(run.fullscreen_s, 1), "mic_s": round(run.mic_s, 1),
            "meeting": run.key.startswith("meeting:"),
            "app": run.apps.most_common(1)[0][0] if run.apps else None,
            "title": run.titles.most_common(1)[0][0] if run.titles else None,
        })
    flush_short()
    if len(out) > max_lines:
        cut = len(out) - max_lines + 1
        head = out[:cut]
        out = [{"kind": "earlier", "start": head[0]["start"], "end": head[-1]["end"], "count": cut}] + out[cut:]
    return out


def breaks(
    rows: Sequence[TimelineRow], since: float | None = None, until: float | None = None,
    *, min_s: float = BREAK_S, now: float | None = None,
) -> list[dict[str, Any]]:
    """Spans of at least ``min_s`` with nobody at the PC: away stretches and untracked gaps, merged.

    With ``now``, the time after the last row up to ``now`` counts as a gap
    (the recorder writes the open stretch every :data:`FLUSH_EVERY_S`, so a
    silence longer than that is time nothing was recorded: paused, locked,
    the PC asleep; full screen is recorded).  Each: ``{"start", "end", "seconds", "ongoing"}``.
    """
    clips = _clip(rows, since, until)
    spans: list[list[float]] = []

    def add(start: float, end: float) -> None:
        if end <= start:
            return
        if spans and start - spans[-1][1] <= RUN_GAP_S:
            spans[-1][1] = max(spans[-1][1], end)
        else:
            spans.append([start, end])

    limit = None if now is None else (min(now, until) if until is not None else now)
    last_end = since if since is not None else (clips[0].start if clips else None)
    for c in clips:
        if last_end is not None and c.start > last_end:
            add(last_end, c.start)
        if c.row.state != "present":
            add(c.start, c.end)
        last_end = c.end if last_end is None else max(last_end, c.end)
    if limit is not None and last_end is not None and limit - last_end > FLUSH_EVERY_S * 2:
        add(last_end, limit)
    out = []
    for start, end in spans:
        if end - start >= min_s:
            # the open stretch is written every FLUSH_EVERY_S, so "reaches now" allows for that
            ongoing = limit is not None and end >= limit - FLUSH_EVERY_S * 2
            out.append({"start": start, "end": end, "seconds": round(end - start, 1), "ongoing": ongoing})
    return out


def _hm(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M") if at else "?"


def describe(agg: dict[str, Any], *, max_items: int = 12, titles: bool = True) -> list[str]:
    """Plain lines for an aggregate (the API tool text, prompts)."""
    t = agg["totals"]
    special = ""
    if t.get("fullscreen_s", 0) >= 30:
        special += f", full screen {format_duration(t['fullscreen_s'])}"
    if t.get("meeting_s", 0) >= 30:
        special += f", in meetings {format_duration(t['meeting_s'])}"
    lines = [
        f"present {format_duration(t['present_s'])} (active {format_duration(t['active_s'])}, watching or "
        f"listening with no input {format_duration(t['passive_s'])}), away {format_duration(t['away_s'])}, "
        f"media playing in the app in front {format_duration(t['media_s'])}{special}, {t['switches']} switches "
        f"between {t['activities']} activities"
    ]
    for item in agg["items"][:max_items]:
        where = f" in {item['app']}" if item.get("app") and item["app"] != item["label"] else ""
        extra = []
        if item["passive_s"] >= 30:
            what = "no input, microphone on" if item.get("meeting") else "watching"
            extra.append(f"{what} {format_duration(item['passive_s'])}")
        if item["media_s"] >= 30:
            extra.append(f"media {format_duration(item['media_s'])}")
        if item.get("fullscreen_s", 0) >= 30:
            extra.append(f"full screen {format_duration(item['fullscreen_s'])}")
        longest = (
            f"longest {format_duration(item['longest_s'])} at {_hm(item['longest_start'])}"
            if item["longest_s"] else "longest -"
        )
        line = (
            f"- {item['label']}{where}: {format_duration(item['present_s'])} "
            f"(active {format_duration(item['active_s'])}{', ' + ', '.join(extra) if extra else ''}), "
            f"{item['visits']} visit{'' if item['visits'] == 1 else 's'}, {longest}"
        )
        if titles and item.get("titles") and agg["group_by"] != "page":
            shown = "; ".join(f"\"{x['title'][:80]}\" {format_duration(x['present_s'])}" for x in item["titles"])
            line += f"; titles: {shown}"
        lines.append(line)
    if len(agg["items"]) > max_items:
        lines.append(f"- ... {len(agg['items']) - max_items} more")
    for pair in agg.get("interleaving", [])[:5]:
        lines.append(f"back and forth: {pair['a']} <-> {pair['b']}, {pair['switches']} switches")
    for media in agg.get("background_media", [])[:4]:
        lines.append(f"background media: {media['app']} {format_duration(media['seconds'])}")
    for m in agg.get("meetings", [])[:8]:
        lines.append(describe_meeting(m))
    return lines


def describe_meeting(m: dict[str, Any]) -> str:
    """``meeting: in a meeting 15:00-15:45 (Google Meet in Google Chrome), in front 40m, microphone on 38m``."""
    where = m["label"] + (f" in {m['app']}" if m.get("app") and m["app"] != m["label"] else "")
    line = (f"meeting: in a meeting {_hm(m['start'])}-{_hm(m['end'])} ({where}), "
            f"in front {format_duration(m['in_front_s'])}")
    if m.get("mic_s", 0) >= 30:
        line += f", microphone on {format_duration(m['mic_s'])}"
    return line


# ---------------------------------------------------------------------------
# Recorder (Win32; imported lazily so the functions above stay cheap)
# ---------------------------------------------------------------------------

_WM_USER = 0x0400
_WM_APP = 0x8000
_WM_QUIT = 0x0012
WM_APP_PAUSE = _WM_APP + 21
WM_APP_PAGE = _WM_APP + 22
_PM_REMOVE = 0x0001
_PM_NOREMOVE = 0x0000
_QS_ALLINPUT = 0x04FF
_MWMO_INPUTAVAILABLE = 0x0004
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_NAMECHANGE = 0x800C
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_OBJID_WINDOW = 0
_CHILDID_SELF = 0
_GA_ROOT = 2


@dataclass
class _Stretch:
    """The stretch being recorded (in memory until written)."""

    start: float
    hwnd: int
    pid: int
    process: str
    app: str
    title: str
    state: str = "present"
    host: str | None = None
    path: str | None = None
    withheld: str | None = None
    ids: frozenset = frozenset()
    entry: Any = None
    seq: int = 0
    probe_at: float = 0.0
    page_checked_at: float = 0.0
    settled: bool = False
    row_id: int | None = None
    base: TimelineRow | None = None
    segments: int = 1
    active_s: float = 0.0
    passive_s: float = 0.0
    away_s: float = 0.0
    media_s: float = 0.0
    media_other: dict[str, float] = field(default_factory=dict)
    end: float = 0.0
    fullscreen: bool = False
    #: Nothing about the window is read but its process and app (full screen
    #: under the privacy pause, a meeting app): no title, no page, no title hook.
    hidden: bool = False
    meeting: str | None = None
    mic_s: float = 0.0


def _mic_users() -> frozenset[str]:
    """Apps using the microphone now (ConsentStore), as app keys / package families, casefolded.

    The same read-only registry facts as ``activity_facts()["microphone"]``
    (:func:`yuki.perception.system._device_users`), without its media and
    foreground parts.
    """
    import psutil

    from yuki.perception import system as _system

    processes = _system._process_entries()
    boot_ft = int(psutil.boot_time() * 1e7) + _system._FILETIME_UNIX_EPOCH
    try:
        users = _system._device_users("microphone", processes, boot_ft)
    except FileNotFoundError:
        return frozenset()
    return frozenset(k for k in ((app_key(u.get("app")) or "") for u in users) if k)


class _PageProbe:
    """The page a window shows, read on a thread of its own (COM MTA), latest request wins."""

    def __init__(self, on_result: Callable[[tuple], None]) -> None:
        self._on_result = on_result
        self._cond = threading.Condition()
        self._request: tuple[int, int] | None = None
        self._stopping = False
        self.busy_since: float | None = None
        self.probes = 0
        self.ms: list[float] = []
        self.errors = 0
        self._thread = threading.Thread(target=self._main, name="yuki-timeline-page", daemon=True)
        self._thread.start()

    def submit(self, seq: int, hwnd: int) -> bool:
        """Queue a read; False when the reader is stuck in a provider (nothing queued)."""
        busy = self.busy_since
        if busy is not None and time.monotonic() - busy > PAGE_STUCK_S:
            return False
        with self._cond:
            self._request = (seq, hwnd)
            self._cond.notify()
        return True

    def stop(self) -> None:
        with self._cond:
            self._stopping = True
            self._cond.notify()

    def _main(self) -> None:
        import comtypes
        import comtypes.client

        from yuki.perception import tree as _tree

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:
            pass
        automation = None
        try:
            while True:
                with self._cond:
                    while self._request is None and not self._stopping:
                        self._cond.wait()
                    if self._stopping:
                        return
                    (seq, hwnd), self._request = self._request, None
                self.busy_since = time.monotonic()
                t0 = time.perf_counter()
                page, error = None, None
                try:
                    if automation is None:
                        module = _tree._uia_core()
                        automation = comtypes.client.CreateObject(
                            _tree._CUIAUTOMATION_CLSID, interface=module.IUIAutomation
                        )
                    pages = _tree._read_pages(automation, hwnd, time.monotonic() + PAGE_PROBE_S, threading.Event())
                    page = (pages[0].title, pages[0].url) if pages else None
                except Exception as exc:  # not answering, or gone: no page known
                    error = f"{type(exc).__name__}: {exc}"
                    self.errors += 1
                ms = (time.perf_counter() - t0) * 1000.0
                self.probes += 1
                self.ms.append(ms)
                if len(self.ms) > 500:
                    del self.ms[:250]
                self.busy_since = None
                try:
                    self._on_result((seq, hwnd, page, ms, error))
                except Exception:
                    pass
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass


class TimelineRecorder:
    """Follows the foreground window and writes stretches to the store (one thread).

    Args:
        store: the store to write to (shared with the service; never closed here).
        privacy: the service's :class:`yuki.memory.privacy.PrivacyConfig`.
        log: ``log(type, **fields)``, content-free.
    """

    def __init__(self, store: Store, *, privacy: Any = None, log: Callable[..., Any] | None = None) -> None:
        from yuki.memory import watcher as _w
        from yuki.memory.privacy import PrivacyConfig
        from yuki.perception import windows as _windows

        self.store = store
        self.privacy = privacy or PrivacyConfig()
        self._log_fn = log
        self._w = _w
        self._windows = _windows
        self._procs = _w._ProcessCache()
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        u = self._user32
        u.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, _w._WINEVENTPROC,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.DWORD]
        u.SetWinEventHook.restype = wintypes.HANDLE
        u.UnhookWinEvent.argtypes = [wintypes.HANDLE]
        u.MsgWaitForMultipleObjectsEx.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                                  wintypes.DWORD, wintypes.DWORD]
        u.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
        u.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT,
                                   wintypes.UINT]
        u.PeekMessageW.restype = wintypes.BOOL
        u.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        u.PostThreadMessageW.restype = wintypes.BOOL
        u.GetForegroundWindow.restype = wintypes.HWND
        u.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u.GetAncestor.restype = wintypes.HWND
        u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self._kernel32.GetCurrentThreadId.restype = wintypes.DWORD
        self._callback = _w._WINEVENTPROC(self._on_winevent)

        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._start_error: BaseException | None = None
        self._fg_hook = None
        self._name_hooks: list[int] = []
        self._hooked: tuple[int, ...] = ()
        self._fg = 0
        self._paused = False
        self._blocked: str | None = None     # paused | locked
        self._meetings = PrivacySection(self.privacy, "meetings", MeetingRules.from_dict)
        self._mic: frozenset[str] = frozenset()
        self._mic_at = 0.0
        self._mic_errors = 0
        self._current: _Stretch | None = None
        self._last_row: TimelineRow | None = None
        self._acc_at = time.time()
        self._input_at = self._acc_at
        self._seq = 0
        self._title_first: float | None = None
        self._title_last: float | None = None
        self._next_flush = time.monotonic() + FLUSH_EVERY_S
        self._next_media = 0.0
        self._playing: list[tuple[str, str]] = []   # (app id casefolded, display label)
        self._media_errors = 0
        self._no_page: dict[int, tuple[int, float]] = {}   # pid -> (streak, last probe wall time)
        self._page_results: list[tuple] = []
        self._page_lock = threading.Lock()
        self._probe: _PageProbe | None = None
        self._due = 0.0
        self.counters: Counter = Counter()

    # -- public ------------------------------------------------------------

    def start(self, timeout_s: float = 10.0) -> None:
        self._thread = threading.Thread(target=self._main, name="yuki-memory-timeline", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise RuntimeError("timeline thread did not start")
        if self._start_error is not None:
            raise RuntimeError(f"timeline failed to start: {self._start_error}")

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout_s)

    def set_paused(self, paused: bool) -> None:
        """Pause or resume (the user's ``paused`` flag); safe from any thread."""
        if self._thread_id and self.alive:
            self._user32.PostThreadMessageW(self._thread_id, WM_APP_PAUSE, int(bool(paused)), 0)
        else:
            self._paused = bool(paused)

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def stats(self) -> dict[str, Any]:
        """Counters (content-free)."""
        probe = self._probe
        ms = sorted(probe.ms) if probe else []

        def pct(p: float) -> float | None:
            return round(ms[min(len(ms) - 1, int(p * len(ms)))], 1) if ms else None

        counters: dict[str, int] = {}
        for _ in range(3):  # the timeline thread may add a key mid-copy
            try:
                counters = dict(self.counters)
                break
            except RuntimeError:
                continue
        return {
            **counters, "blocked": self._blocked, "state": self._current.state if self._current else None,
            "page_probes": probe.probes if probe else 0, "page_errors": probe.errors if probe else 0,
            "page_ms": {"p50": pct(0.5), "p90": pct(0.9), "max": pct(1.0)},
            "media_errors": self._media_errors, "playing": len(self._playing),
            "fullscreen": bool(self._current.fullscreen) if self._current else None,
            "meeting": bool(self._current.meeting) if self._current else None,
            "meetings_config_error": self._meetings.last_error or None,
        }

    # -- logging -------------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:
            pass

    def _log_error(self, where: str, exc: BaseException) -> None:
        self.counters["errors"] += 1
        self._log("error", where=f"timeline.{where}", error=f"{type(exc).__name__}: {exc}",
                  traceback="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))

    # -- thread ----------------------------------------------------------------

    def _main(self) -> None:
        u = self._user32
        msg = wintypes.MSG()
        try:
            self._thread_id = int(self._kernel32.GetCurrentThreadId())
            u.PeekMessageW(ctypes.byref(msg), None, _WM_USER, _WM_USER, _PM_NOREMOVE)  # create the queue
            self._probe = _PageProbe(self._page_done)
            try:
                from yuki.perception.system import warm_activity

                warm_activity()
            except Exception:
                pass
            try:
                self._windows.warm_start_menu_index()
            except Exception:
                pass
            self._fg_hook = u.SetWinEventHook(
                _EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None, self._callback, 0, 0,
                _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS,
            )
            if not self._fg_hook:
                raise OSError(ctypes.get_last_error(), "SetWinEventHook(EVENT_SYSTEM_FOREGROUND) failed")
            now = time.time()
            self._acc_at = now
            self._input_at = now - self._w.user_idle_s()
            self._log("timeline_start", away_after_s=AWAY_AFTER_S, media_every_s=MEDIA_EVERY_S,
                      flush_every_s=FLUSH_EVERY_S, paused=self._paused)
            self._foreground(self._root(int(u.GetForegroundWindow() or 0)), now)
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            self._cleanup()
            return
        self._ready.set()
        try:
            while True:
                u.MsgWaitForMultipleObjectsEx(0, None, self._timeout_ms(), _QS_ALLINPUT, _MWMO_INPUTAVAILABLE)
                quit_seen = False
                while u.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                    if msg.message == _WM_QUIT:
                        quit_seen = True
                        break
                    if not msg.hWnd and msg.message == WM_APP_PAUSE:
                        self._set_paused(bool(msg.wParam))
                        continue
                    if not msg.hWnd and msg.message == WM_APP_PAGE:
                        continue  # results are taken in _tick
                    u.TranslateMessage(ctypes.byref(msg))
                    u.DispatchMessageW(ctypes.byref(msg))
                if quit_seen:
                    break
                # Name-change events of a busy page wake this loop often; the
                # tick itself runs only when something is due.
                if time.monotonic() >= self._due or self._page_results:
                    try:
                        self._tick(time.time())
                    except Exception as exc:
                        self._log_error("tick", exc)
        finally:
            try:
                now = time.time()
                self._advance(now)
                self._close(now)
            except Exception as exc:
                self._log_error("final_close", exc)
            self._cleanup()
            self._log("timeline_stop", stats=self.stats())

    def _cleanup(self) -> None:
        self._unhook_names()
        if self._fg_hook:
            self._user32.UnhookWinEvent(self._fg_hook)
            self._fg_hook = None
        if self._probe is not None:
            self._probe.stop()

    def _timeout_ms(self) -> int:
        """Milliseconds to the next thing due; also sets :attr:`_due` (monotonic)."""
        now = time.time()
        mono = time.monotonic()
        idle = now - self._input_at
        st = self._current
        if st is None or self._blocked:
            delay = TICK_S
        elif st.state == "away" or idle >= AWAY_AFTER_S - 0.5:
            delay = AWAY_TICK_S
        else:
            delay = min(TICK_S, AWAY_AFTER_S - idle + 0.25)
        if self._title_first is not None:
            due = min((self._title_last or mono) + TITLE_SETTLE_S, self._title_first + TITLE_SETTLE_CAP_S)
            delay = min(delay, max(due - mono, 0.0))
        if st is not None and not st.settled:
            delay = min(delay, max(st.probe_at + PAGE_WAIT_S - now, 0.0))
        delay = max(delay, 0.0)
        self._due = mono + delay
        return max(int(delay * 1000) + 1, 1)

    # -- window events -----------------------------------------------------------

    def _root(self, hwnd: int) -> int:
        if not hwnd:
            return 0
        return int(self._user32.GetAncestor(hwnd, _GA_ROOT) or hwnd)

    def _on_winevent(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:
        try:
            if event == _EVENT_SYSTEM_FOREGROUND:
                self.counters["foreground_events"] += 1
                fg = self._root(int(hwnd or 0) or int(self._user32.GetForegroundWindow() or 0))
                self._foreground(fg, time.time())
                return
            if id_object != _OBJID_WINDOW or id_child != _CHILDID_SELF:
                return
            if not hwnd or int(hwnd) != self._fg or self._blocked:
                return
            now = time.monotonic()
            self.counters["title_events"] += 1
            if self._title_first is None:
                self._title_first = now
            self._title_last = now
        except Exception as exc:  # never let an exception escape a ctypes callback
            self._log_error("winevent", exc)

    def _hook_names(self, hwnd: int) -> None:
        """Title-change hooks for the processes behind ``hwnd`` (its own and, for a UWP frame, the app's)."""
        import os

        pid = self._w._window_pid(hwnd)
        raw = wintypes.DWORD(0)
        self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(raw))
        pids = tuple(p for p in dict.fromkeys((pid, int(raw.value))) if p and p != os.getpid())
        if pids == self._hooked:
            return
        self._unhook_names()
        for p in pids:
            handle = self._user32.SetWinEventHook(
                _EVENT_OBJECT_NAMECHANGE, _EVENT_OBJECT_NAMECHANGE, None, self._callback, p, 0,
                _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS,
            )
            if handle:
                self._name_hooks.append(handle)
        self._hooked = pids

    def _unhook_names(self) -> None:
        for handle in self._name_hooks:
            self._user32.UnhookWinEvent(handle)
        self._name_hooks = []
        self._hooked = ()

    def _foreground(self, hwnd: int, now: float) -> None:
        self._fg = hwnd
        self._title_first = self._title_last = None
        self._advance(now)
        if self._gate(now):
            return
        st = self._current
        if st is not None and st.hwnd == hwnd and self._fullscreen(hwnd) == st.fullscreen and (
            st.hidden or self._w._window_title(hwnd) == st.title
        ):
            return  # the same window came back (a menu or dialog of it closed)
        self._close(now)
        self._open(hwnd, now, carry=st)

    def _fullscreen(self, hwnd: int) -> bool:
        try:
            return bool(hwnd) and bool(self._w.is_fullscreen_front(hwnd))
        except Exception:
            return False

    def _check_fullscreen(self, now: float) -> None:
        """The window in front went into or out of full screen (F11, a game's mode switch): a new stretch."""
        st = self._current
        if st is None or st.hwnd != self._fg:
            return
        fullscreen = self._fullscreen(st.hwnd)
        if fullscreen == st.fullscreen:
            return
        self.counters["fullscreen_on" if fullscreen else "fullscreen_off"] += 1
        self._log("timeline_fullscreen", fullscreen=fullscreen)
        self._close(now)
        self._open(st.hwnd, now, carry=st)

    # -- gates ---------------------------------------------------------------------

    def _set_paused(self, paused: bool) -> None:
        if paused == self._paused:
            return
        self._paused = paused
        self._gate(time.time())

    def _gate(self, now: float) -> bool:
        """Apply pause / lock; True while recording is blocked (full screen is recorded, see :meth:`_open`)."""
        rules = self.privacy.rules()
        reason = None
        if self._paused:
            reason = "paused"
        elif rules.pause_when_locked and self._w.session_locked():
            reason = "locked"
        if reason == self._blocked:
            return reason is not None
        previous, self._blocked = self._blocked, reason
        self._log("timeline_state", blocked=reason, was=previous)
        if reason is not None:
            self._advance(now)
            self._close(now)
            self._unhook_names()
            return True
        self._acc_at = now
        self._input_at = now - self._w.user_idle_s()
        self._open(self._fg, now)
        return False

    # -- stretches -------------------------------------------------------------------

    def _identity(self, pid: int, exe: str, process: str) -> tuple[frozenset, Any]:
        """OS identities of an app for joining media sessions: image key, AUMID, Start menu entry."""
        import os

        ids = {app_key(process) or ""}
        aumid = ""
        try:
            aumid, _ = self._windows.process_package_ids(pid)
        except Exception:
            pass
        if aumid:
            ids.add(aumid.casefold())
        entry = None
        try:
            index = self._windows.start_menu_index()
            if index is not None:
                entry = index.entry_for(aumid.casefold(), os.path.normcase(exe) if exe else "")
                if entry is not None:
                    ids.add(entry.appid.casefold())
        except Exception:
            pass
        ids.discard("")
        return frozenset(ids), entry

    def _open(
        self, hwnd: int, now: float, *, like: _Stretch | None = None, state: str = "present",
        carry: _Stretch | None = None,
    ) -> None:
        """Start a stretch for ``hwnd`` (``like``: same window and page, another state).

        ``carry``: the stretch that just ended; when it was the same window
        and this one is full screen under the privacy pause, its host,
        privacy reason and meeting carry over (nothing new is read).
        """
        self._current = None
        if self._blocked or not hwnd:
            return
        if like is not None:
            st = replace(like, start=now, end=now, state=state, settled=False, row_id=None, base=None, segments=1,
                         active_s=0.0, passive_s=0.0, away_s=0.0, media_s=0.0, media_other={}, mic_s=0.0)
            self._current = st
            self._settle(st)
            return
        try:
            if not self._windows.is_user_window(hwnd):
                self._unhook_names()
                self.counters["not_user_window"] += 1
                return
        except Exception:
            return
        pid = self._w._window_pid(hwnd)
        facts = self._procs.get(pid)
        rules = self.privacy.rules()
        fullscreen = self._fullscreen(hwnd)
        hidden = fullscreen and rules.pause_when_fullscreen
        app, withheld, title, host = facts.app_name, None, "", None
        meeting = None
        if facts.is_own and rules.skip_own_windows:
            app, withheld = "Yuki", "own_window"
        elif meeting := self._meetings.get().match_process(facts.process_name, facts.app_name):
            # meeting hours only: the app's name and the time, nothing from its windows
            withheld, hidden = rules.check_app(facts.process_name, facts.app_name) or "meeting", True
        elif reason := rules.check_app(facts.process_name, facts.app_name):
            app, withheld = "", reason
        elif hidden:
            if carry is not None and carry.hwnd == hwnd and carry.pid == pid and not carry.hidden:
                host, withheld, meeting = carry.host, carry.withheld, carry.meeting
        else:
            title = self._w._window_title(hwnd)
            if reason := rules.check_window(facts.process_name, title):
                withheld, title = reason, ""
        ids, entry = self._identity(pid, facts.exe, facts.process_name)
        st = _Stretch(start=now, end=now, hwnd=hwnd, pid=pid, process=facts.process_name if app else "",
                      app=app, title=title, withheld=withheld, host=host, ids=ids, entry=entry,
                      fullscreen=fullscreen, hidden=hidden, meeting=meeting)
        self._current = st
        if hidden:
            self._unhook_names()
        else:
            self._hook_names(hwnd)
        idle = now - self._input_at
        if meeting:
            self._poll_mic()
        if idle >= AWAY_AFTER_S and not self._engaged(st):
            st.state = "away"
        if withheld or hidden or not self._probe_page(st, now):
            self._settle(st)
        self.counters["stretches"] += 1
        if fullscreen:
            self.counters["fullscreen_stretches"] += 1
        if meeting:
            self.counters["meeting_stretches"] += 1

    def _probe_page(self, st: _Stretch, now: float) -> bool:
        """Ask for the page ``st`` shows; False when no read was queued."""
        if self._probe is None:
            return False
        streak, last = self._no_page.get(st.pid, (0, 0.0))
        if streak >= NO_PAGE_STREAK and now - last < NO_PAGE_BACKOFF_S:
            self.counters["page_backoff"] += 1
            return False
        self._seq += 1
        st.seq = self._seq
        st.probe_at = now
        if not self._probe.submit(st.seq, st.hwnd):
            self.counters["page_stuck"] += 1
            return False
        return True

    def _page_done(self, result: tuple) -> None:
        """Called on the probe thread: hand the result to the timeline thread."""
        with self._page_lock:
            self._page_results.append(result)
        if self._thread_id:
            self._user32.PostThreadMessageW(self._thread_id, WM_APP_PAGE, 0, 0)

    def _take_pages(self, now: float) -> None:
        with self._page_lock:
            results, self._page_results = self._page_results, []
        for seq, hwnd, page, ms, error in results:
            st = self._current
            pid = st.pid if st is not None and st.seq == seq else None
            if pid is not None:
                streak, _ = self._no_page.get(pid, (0, 0.0))
                self._no_page[pid] = (0 if page else streak + 1, now)
                if len(self._no_page) > 512:
                    self._no_page.clear()
            if st is None or st.seq != seq or st.hwnd != hwnd:
                continue
            st.page_checked_at = now
            if st.settled and not page:
                continue  # a re-read that found nothing (loading, not answering) changes nothing
            host = path = meeting = None
            withheld = st.withheld
            title = st.title
            if page and page[1]:
                url = page[1]
                reason = self.privacy.rules().check_url(url)
                meeting = self._meetings.get().match_url(url)
                if meeting:
                    # meeting hours only: the service and host, never the title or the path
                    host, withheld, title = url_host(url), reason, ""
                elif reason:
                    withheld, title = reason, ""
                else:
                    try:
                        parts = urlsplit(url)
                    except ValueError:
                        parts = None
                    if parts is not None:
                        if parts.scheme.lower() in ("http", "https"):
                            host = url_host(url)
                        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                        if not host:
                            path = f"{parts.scheme}:{path}"
            if not st.settled:
                st.host, st.path, st.withheld, st.title, st.meeting = host, path, withheld, title, meeting
                if meeting:
                    self._poll_mic()
                    self.counters["meeting_stretches"] += 1
                self._settle(st)
                continue
            if (host, path, withheld, meeting) != (st.host, st.path, st.withheld, st.meeting):
                # the page changed under the same title (a feed, a single-page site)
                self._advance(now)
                self._close(now)
                if st.meeting and not meeting:
                    self._open(st.hwnd, now)  # left the meeting page: read the window afresh
                else:
                    new = replace(st, host=host, path=path, withheld=withheld, title=title, meeting=meeting)
                    self._open(st.hwnd, now, like=new, state=st.state)
                self.counters["page_rotations"] += 1

    def _settle(self, st: _Stretch) -> None:
        """Decide the stretch's identity; continue the previous row when it is the same page."""
        st.settled = True
        key = self.store.timeline_key(st.process, st.host, st.path, st.title, st.withheld,
                                      fullscreen=st.fullscreen, meeting=st.meeting)
        last = self._last_row
        if (
            last is not None and last.page_key == key and last.state == st.state
            and st.start - last.ended_at <= MERGE_GAP_S
        ):
            st.row_id, st.base, st.start = last.id, last, last.started_at
            st.segments = last.segments + 1
            self.counters["merged"] += 1

    def _row(self, st: _Stretch, end: float) -> TimelineRow:
        base = st.base
        other = dict(base.media_other) if base else {}
        for k, v in st.media_other.items():
            other[k] = other.get(k, 0.0) + v
        return TimelineRow(
            id=st.row_id, started_at=st.start, ended_at=end, state=st.state, process=st.process, app=st.app,
            title=st.title or None, host=st.host, path=st.path, page_key=None,
            active_s=st.active_s + (base.active_s if base else 0.0),
            passive_s=st.passive_s + (base.passive_s if base else 0.0),
            away_s=st.away_s + (base.away_s if base else 0.0),
            media_s=st.media_s + (base.media_s if base else 0.0),
            media_other=other, withheld=st.withheld, segments=st.segments,
            fullscreen=st.fullscreen, meeting=st.meeting, mic_s=st.mic_s + (base.mic_s if base else 0.0),
        )

    def _write(self, st: _Stretch, end: float) -> TimelineRow | None:
        if not st.settled:
            self._settle(st)
        row = self._row(st, end)
        if row.id is None and end - st.start < MIN_ROW_S:
            return None
        try:
            self.store.save_timeline(row)
        except Exception as exc:
            self._log_error("save", exc)
            return None
        st.row_id = row.id
        self.counters["writes"] += 1
        return row

    def _close(self, now: float) -> None:
        st, self._current = self._current, None
        if st is None:
            return
        row = self._write(st, now)
        if row is not None:
            self._last_row = row

    # -- time accounting ---------------------------------------------------------------

    def _matches(self, st: _Stretch, app_id: str) -> bool:
        if app_id in st.ids or (app_key(app_id) or "") in st.ids:
            return True
        if st.entry is not None:
            try:
                index = self._windows.start_menu_index()
                return index is not None and index.by_appid.get(app_id) is st.entry
            except Exception:
                return False
        return False

    def _fg_media(self, st: _Stretch) -> bool:
        return any(self._matches(st, app_id) for app_id, _ in self._playing)

    def _mic_on(self, st: _Stretch) -> bool:
        """A meeting's app is using the microphone (by image key, or package family for a packaged app)."""
        if not st.meeting or not self._mic:
            return False
        key = app_key(st.process) or ""
        return key in self._mic or any(i.startswith(m + "_") or i.startswith(m + "!") for m in self._mic
                                       for i in st.ids)

    def _engaged(self, st: _Stretch) -> bool:
        """At the app without input: its media plays, or it is a meeting using the microphone."""
        return self._fg_media(st) or self._mic_on(st)

    def _poll_mic(self) -> None:
        self._mic_at = time.monotonic()
        try:
            self._mic = _mic_users()
            self._mic_errors = 0
        except Exception:
            self._mic_errors += 1
            self.counters["mic_errors"] += 1
            if self._mic_errors >= 3:
                self._mic = frozenset()

    def _credit(self, st: _Stretch, a: float, b: float, kind: str) -> None:
        if b <= a:
            return
        d = b - a
        setattr(st, kind, getattr(st, kind) + d)
        if self._mic_on(st):
            st.mic_s += d
        for app_id, label in self._playing:
            if self._matches(st, app_id):
                if kind != "away_s":
                    st.media_s += d
            else:
                st.media_other[label] = st.media_other.get(label, 0.0) + d
        st.end = b

    def _switch(self, st: _Stretch, at: float, state: str) -> _Stretch:
        """End ``st`` at ``at`` and continue the same window in ``state``."""
        self._current = st
        row = self._write(st, at)
        if row is not None:
            self._last_row = row
        self._open(st.hwnd, at, like=st, state=state)
        self.counters[f"to_{state}"] += 1
        return self._current or st

    def _advance(self, now: float) -> None:
        """Account the time since the last pass to the current stretch (present / passive / away)."""
        t0 = self._acc_at
        idle = self._w.user_idle_s()
        l1 = now - idle
        l0 = self._input_at
        self._acc_at, self._input_at = now, l1
        st = self._current
        if st is None or self._blocked or now <= t0:
            return
        if now - t0 > MAX_TICK_GAP_S:
            # the process did not run (sleep, hibernation): leave the gap out
            self.counters["tick_gaps"] += 1
            self._close(t0)
            self._open(st.hwnd, now, like=st, state="present" if idle < AWAY_AFTER_S else "away")
            return
        new_input = l1 > l0 + 0.5
        if st.state == "present":
            a = max(t0, l0 + AWAY_AFTER_S)
            b = min(now, l1 if new_input else now)
            if b <= a:
                self._credit(st, t0, now, "active_s")
                return
            self._credit(st, t0, a, "active_s")
            if self._engaged(st):
                self._credit(st, a, b, "passive_s")
                self._credit(st, b, now, "active_s")
                return
            away = self._switch(st, a, "away")
            self._credit(away, a, b, "away_s")
            if new_input:
                back = self._switch(away, b, "present")
                self._credit(back, b, now, "active_s")
            return
        if new_input:
            back_at = min(max(t0, l1), now)
            self._credit(st, t0, back_at, "away_s")
            back = self._switch(st, back_at, "present")
            self._credit(back, back_at, now, "active_s")
        elif self._engaged(st):
            back = self._switch(st, t0, "present")
            self._credit(back, t0, now, "passive_s")
        else:
            self._credit(st, t0, now, "away_s")

    def _poll_media(self) -> None:
        try:
            from yuki.perception.system import media_sessions

            sessions = media_sessions(timeout_s=MEDIA_TIMEOUT_S)
        except Exception:
            self._media_errors += 1
            self.counters["media_errors"] += 1
            if self._media_errors >= 3:
                self._playing = []
            return
        self._media_errors = 0
        rules = self.privacy.rules()
        index = None
        try:
            index = self._windows.start_menu_index()
        except Exception:
            pass
        playing: list[tuple[str, str]] = []
        for s in sessions:
            if s.get("status") != "playing":
                continue
            app_id = str(s.get("app_id") or "").casefold()
            if not app_id:
                continue
            entry = index.by_appid.get(app_id) if index is not None else None
            label = entry.name if entry is not None else (app_key(app_id) or app_id)
            if rules.check_app(label, label):
                continue  # a blocked app's media is not recorded either
            playing.append((app_id, label))
        self._playing = playing

    # -- tick --------------------------------------------------------------------------

    def _tick(self, now: float) -> None:
        self.counters["ticks"] += 1
        if self._gate(now):
            return
        mono = time.monotonic()
        self._advance(now)
        self._check_fullscreen(now)
        if mono >= self._next_media:
            self._next_media = mono + MEDIA_EVERY_S
            self._poll_media()
            if self._current is not None and self._current.meeting:
                self._poll_mic()
            else:
                self._mic = frozenset()
        self._take_pages(now)
        st = self._current
        if self._title_first is not None:
            due = min((self._title_last or mono) + TITLE_SETTLE_S, self._title_first + TITLE_SETTLE_CAP_S)
            if mono >= due:
                self._title_first = self._title_last = None
                self._on_title(now)
                st = self._current
        if st is not None and not st.settled and now - st.probe_at >= PAGE_WAIT_S:
            self.counters["page_timeouts"] += 1
            self._settle(st)
        if (
            st is not None and st.settled and st.host and not st.hidden and st.state == "present"
            and now - st.page_checked_at >= PAGE_REPROBE_S and now - self._input_at < AWAY_AFTER_S
        ):
            st.page_checked_at = now
            self._probe_page(st, now)
        if mono >= self._next_flush:
            self._next_flush = mono + FLUSH_EVERY_S
            # an unsettled stretch waits for its page (at most PAGE_WAIT_S): its privacy is not known yet
            if st is not None and self._current is st and st.settled:
                self._write(st, now)

    def _on_title(self, now: float) -> None:
        st = self._current
        if st is None or st.hwnd != self._fg or st.withheld == "own_window":
            return
        if st.hidden:
            return  # full screen under the privacy pause, or a meeting app: nothing about the window is read
        if st.meeting:
            # a meeting page: its title is not read; the page is read again in case the tab was left
            if st.settled:
                self._probe_page(st, now)
            return
        title = self._w._window_title(st.hwnd)
        if st.withheld and not st.app:
            return  # a blocked app: nothing about it is read
        rules = self.privacy.rules()
        reason = rules.check_window(st.process, title)
        if (not reason and title == st.title) or (reason and st.withheld == reason):
            return
        self.counters["title_changes"] += 1
        if st.row_id is None and now - st.start < YOUNG_S:
            st.title = "" if reason else title
            st.withheld = reason
            st.host = st.path = None
            st.settled = False
            if reason or not self._probe_page(st, now):
                self._settle(st)
            return
        self._advance(now)
        self._close(now)
        self._open(st.hwnd, now)


__all__ = [
    "TimelineRecorder", "aggregate", "sequence", "breaks", "describe", "describe_meeting", "meetings",
    "format_duration", "group_of", "site_of", "MeetingRules", "PrivacySection",
    "AWAY_AFTER_S", "BREAK_S", "RUN_GAP_S", "MEETING_GAP_S",
]
