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
  - ``present`` vs ``away``: present while the user's last real input
    (:func:`yuki.memory.watcher.user_idle_s`: a key, a click, the wheel, the
    mouse travelling the takeover distance, read through Raw Input as counts
    and times only; a vibrating sensor's tiny moves and Yuki's own input left
    out, :mod:`yuki.memory.userpresence`)
    is under :data:`AWAY_AFTER_S` old, or while the app in front plays media;
    otherwise ``away``. Time while the app in front plays media is
    ``passive`` (watching or listening) whatever the input says: the idle
    timer is only as good as the devices behind it (on this PC a mouse whose
    sensor reports a one-count move every few seconds resets it with nobody
    there, measured 2026-09-24), so input cannot be what tells watching from
    doing;
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
  - **Meetings** are measured, not listed: the app in front has held the
    microphone (or the camera) for ``[meetings] hold_s`` seconds, per the
    ConsentStore (:mod:`yuki.memory.avsignals`, read every
    :data:`MEDIA_EVERY_S` - a cached read unless Windows changed it). For an
    app that shows web pages the meeting is the site that was in front when
    it took the device. Such a stretch is recorded as a ``meeting`` row: app,
    host and service name (the app's own name, or the site), start/end - no
    title, no path; it ends ``end_grace_s`` after the app lets go. Content
    stays blocked by the ``[apps]``/``[web]`` rules; the user's
    ``always_*``/``never_*`` lists (:class:`MeetingRules`) override. The
    meeting app using the microphone counts as present without input
    (``passive_s``) and as ``mic_s``.

The PC's own power history (:mod:`yuki.memory.power`) lives in the same table
as rows with state ``off`` / ``asleep`` / ``unknown`` (no app, no counters):
the helpers here show them as what they are - "PC off 23:10-08:05" - so a
hole in the timeline is either one of those or time the PC was on and memory
was not recording.

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

from yuki.memory.store import POWER_STATES, Store, TimelineRow, app_key, url_host

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
#: ...and this soon when the page's process has changed content since the last
#: read (name-change events: a reel scrolled, a feed moved on), so an address
#: that changes without a title change is seen within seconds. Each read is a
#: page-only pass (tens of ms, on the probe thread).
PAGE_ACTIVE_REPROBE_S = 2.5
#: A stretch that settled without a page, in a process that has shown pages
#: (fewer than NO_PAGE_STREAK empty probes in a row), is read again this often:
#: its first probe came while the page was loading. Seen live 2026-09-24: Chrome
#: stretches opened by a navigation kept host=None until the next title change.
PAGE_RETRY_S = 5.0
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


def _site_list(values: Any) -> tuple[str, ...]:
    return tuple(str(h).strip().lower() for h in (values or []) if str(h).strip())


def _app_set(values: Any) -> frozenset[str]:
    return frozenset(k for k in (app_key(str(p)) for p in (values or [])) if k)


@dataclass(frozen=True)
class MeetingRules:
    """The privacy file's ``[meetings]`` table.

    A meeting is **measured**, not listed: the app in front has held the
    microphone (or the camera, with ``camera``) for ``hold_s`` seconds, per
    the ConsentStore (:mod:`yuki.memory.avsignals`); it ends ``end_grace_s``
    after the app lets go. The rest is the user's own overrides, empty by
    default: ``always_apps`` / ``always_sites`` are meetings whenever they are
    in front, ``never_apps`` / ``never_sites`` never are, ``names`` renames a
    process or site for display. The keys ``processes``, ``host_suffixes`` and
    ``host_paths`` of files written before 2026-09-25 (a seeded list of
    meeting apps and sites) are not read.
    """

    hold_s: float = 20.0
    end_grace_s: float = 30.0
    camera: bool = True
    always_apps: frozenset[str] = frozenset()             # app_key form ("zoom")
    always_sites: tuple[str, ...] = ()
    never_apps: frozenset[str] = frozenset()
    never_sites: tuple[str, ...] = ()
    names: tuple[tuple[str, str], ...] = ()               # (process key or host, service name)

    @classmethod
    def from_dict(cls, table: dict) -> MeetingRules:
        table = table or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return max(0.0, float(table.get(key, default)))
            except (TypeError, ValueError):
                return default

        names = tuple((_name_key(k), str(v)) for k, v in (table.get("names", {}) or {}).items() if str(v).strip())
        return cls(
            hold_s=num("hold_s", d.hold_s), end_grace_s=num("end_grace_s", d.end_grace_s),
            camera=bool(table.get("camera", d.camera)),
            always_apps=_app_set(table.get("always_apps")), always_sites=_site_list(table.get("always_sites")),
            never_apps=_app_set(table.get("never_apps")), never_sites=_site_list(table.get("never_sites")),
            names=names,
        )

    def _name(self, key: str) -> str | None:
        return next((v for k, v in self.names if k == key), None)

    def name_for(self, key: str, fallback: str) -> str:
        """A meeting's display name: the user's ``names`` entry for the process key or host, else ``fallback``."""
        return self._name(key) or fallback

    def never(self, process_key: str, host: str | None) -> bool:
        """The user said this app or site is never a meeting."""
        return (bool(process_key) and process_key in self.never_apps) or (
            bool(host) and any(_host_matches(host or "", s) for s in self.never_sites))

    def match_process(self, process_name: str, app_name: str = "") -> str | None:
        """An ``always_apps`` override: the service name, or None (the measured signal decides the rest)."""
        key = app_key(process_name)
        if not key or key not in self.always_apps:
            return None
        return self._name(key) or (app_name or "").strip() or key

    def match_url(self, url: str) -> str | None:
        """An ``always_sites`` override for an http(s) page: the service name, or None."""
        try:
            parts = urlsplit((url or "").strip())
            host = (parts.hostname or "").lower()
        except ValueError:
            return None
        if parts.scheme.lower() not in ("http", "https") or not host:
            return None
        for suffix in self.always_sites:
            if _host_matches(host, suffix):
                return self._name(suffix) or site_of(host)
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


#: How a power row (yuki.memory.power) reads in a description.
POWER_WORDS: dict[str, str] = {
    "off": "PC off",
    "asleep": "PC asleep",
    "unknown": "PC stopped without a clean shutdown (a crash or power cut; not known exactly when)",
}
#: What an untracked gap is once the power history is known: the PC was on
#: (no shutdown or sleep recorded), memory was simply not recording.
GAP_WORDS = "nothing recorded (the PC was on; Yuki not running, memory paused or the PC locked)"


def is_power(row: TimelineRow) -> bool:
    """A power row: the PC off, asleep or stopped (not a stretch of the user's)."""
    return row.state in POWER_STATES


def _hm_span(start: float, end: float) -> str:
    """``23:10-08:05`` within a day, ``Thu 23:10-Fri 08:05`` across days."""
    a, b = datetime.fromtimestamp(start), datetime.fromtimestamp(end)
    if a.date() == b.date():
        return f"{a:%H:%M}-{b:%H:%M}"
    return f"{a:%a %H:%M}-{b:%a %H:%M}"


def power_list(clips: Sequence[_Clip]) -> list[dict[str, Any]]:
    """The power spans among clipped rows, oldest first: ``{"kind", "start", "end", "seconds", "source"}``."""
    return [
        {"kind": c.row.state, "start": c.start, "end": c.end, "seconds": round(c.end - c.start, 1),
         "source": c.row.source}
        for c in clips if is_power(c.row)
    ]


def power_spans(rows: Sequence[TimelineRow], since: float | None = None,
                until: float | None = None) -> list[dict[str, Any]]:
    """The PC's off / asleep / unknown spans in ``[since, until)`` (clipped), oldest first."""
    return power_list(_clip(rows, since, until))


def describe_power(span: dict[str, Any]) -> str:
    """``PC off 23:10-08:05 (8h55m)``."""
    return f"{POWER_WORDS.get(span['kind'], span['kind'])} {_hm_span(span['start'], span['end'])} " \
           f"({format_duration(span['seconds'])})"


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

    The PC's own power history (:mod:`yuki.memory.power`): ``power`` lists
    the spans it was off, asleep or stopped without a clean shutdown
    (``{"kind", "start", "end", "seconds", "source"}``), and
    ``totals.off_s`` / ``asleep_s`` / ``unknown_s`` add them up. They are
    breaks, not activity and not away time.
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
    power = power_list(clips)
    tracked = [c for c in clips if not is_power(c.row)]
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
        "first_at": tracked[0].start if tracked else None,
        "last_at": max(c.end for c in tracked) if tracked else None,
        # the PC's own power history (yuki.memory.power): off, asleep, stopped without a clean shutdown
        **{f"{kind}_s": round(sum(p["seconds"] for p in power if p["kind"] == kind), 1) for kind in POWER_STATES},
    }
    return {
        "since": since, "until": until, "group_by": group_by, "totals": totals, "items": out_items,
        "interleaving": [
            {"a": labels[a], "b": labels[b], "switches": n} for (a, b), n in pairs.most_common(8) if n >= 2
        ],
        "background_media": [{"app": a, "seconds": round(s, 1)} for a, s in background.most_common(8) if s >= 1],
        "meetings": meetings(clips),
        "power": power,
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
    untracked gaps of at least ``min_run_s`` appear as ``{"kind": "away" | "gap"}``,
    and the PC's own power spans (yuki.memory.power) as ``{"kind": "off" |
    "asleep" | "unknown"}`` - a gap is then time the PC was on but nothing was
    recorded. At most ``max_lines`` entries (the earliest are merged into one
    note when cut).
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
        last_end = c.end if last_end is None else max(last_end, c.end)
        if is_power(c.row):
            if away_start is not None:
                events.append((away_start, "away", (away_start, away_end)))
                away_start = away_end = None
            events.append((c.start, c.row.state, (c.start, c.end)))
            continue
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
        if kind in ("away", "gap") or kind in POWER_STATES:
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
    the PC asleep; full screen is recorded).  The PC's own power spans
    (yuki.memory.power: off, asleep, unknown) are breaks too, never missing
    data.  Each: ``{"start", "end", "seconds", "ongoing", "kind", "power",
    "away_s", "gap_s"}`` - ``kind`` is what most of it was (``off`` |
    ``asleep`` | ``unknown`` | ``away`` | ``gap``), ``power`` the seconds per
    power kind inside it.
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
            power: Counter = Counter()
            away = 0.0
            for c in clips:
                overlap = min(end, c.end) - max(start, c.start)
                if overlap <= 0 or c.row.state == "present":
                    continue
                if is_power(c.row):
                    power[c.row.state] += overlap
                else:
                    away += overlap
            gap = max(0.0, (end - start) - sum(power.values()) - away)
            parts = {**power, "away": away, "gap": gap}
            kind = max(parts, key=lambda k: parts[k])
            out.append({"start": start, "end": end, "seconds": round(end - start, 1), "ongoing": ongoing,
                        "kind": kind, "power": {k: round(v, 1) for k, v in power.items()},
                        "away_s": round(away, 1), "gap_s": round(gap, 1)})
    return out


def describe_break(span: dict[str, Any]) -> str:
    """What a break was, from :func:`breaks`: ``8h55m (PC off 5h53m, away 12m, nothing recorded 2m)``."""
    parts = [f"{POWER_WORDS.get(k, k).split(' (')[0]} {format_duration(s)}"
             for k, s in sorted((span.get("power") or {}).items(), key=lambda kv: -kv[1]) if s >= 30]
    if span.get("away_s", 0) >= 30:
        parts.append(f"away from the PC {format_duration(span['away_s'])}")
    if span.get("gap_s", 0) >= 30:
        parts.append(f"PC on but not recorded {format_duration(span['gap_s'])}")
    return f"{format_duration(span['seconds'])}" + (f" ({', '.join(parts)})" if parts else "")


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
    for kind in POWER_STATES:
        if t.get(f"{kind}_s", 0) >= 60:
            special += f", {POWER_WORDS[kind].split(' (')[0]} {format_duration(t[f'{kind}_s'])}"
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
    for p in [p for p in agg.get("power", []) if p["seconds"] >= 60][-8:]:
        lines.append(f"power: {describe_power(p)}")
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
WM_APP_ACTING = _WM_APP + 23
_PM_REMOVE = 0x0001
_PM_NOREMOVE = 0x0000
_QS_ALLINPUT = 0x04FF
_MWMO_INPUTAVAILABLE = 0x0004
_EVENT_SYSTEM_FOREGROUND = 0x0003
_EVENT_OBJECT_NAMECHANGE = 0x800C
#: Hooked together with name changes (one range): an address bar's or a page
#: Document's value changing is the address changing.
_EVENT_OBJECT_VALUECHANGE = 0x800E
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
    #: Yuki was acting for the user when this stretch began (yuki.memory.acting).
    by_yuki: bool = False
    yuki_request: str | None = None
    #: In memory only, never written unless the stretch becomes a meeting: the process behind the
    #: window (also when privacy keeps it out of the row), and the page's host and path (also when
    #: its address is blocked). The meeting signal is matched against these.
    proc: str = ""
    proc_app: str = ""
    exe: str = ""
    own: bool = False
    seen_host: str | None = None
    seen_path: str | None = None
    #: The meeting comes from the measured signal (an app holding the microphone or camera), not from
    #: an ``always_*`` override: it ends when the app lets go.
    signal_meeting: bool = False


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

    def __init__(
        self, store: Store, *, privacy: Any = None, log: Callable[..., Any] | None = None,
        on_page_change: Callable[[int], None] | None = None, signals: Any = None, evidence: Any = None,
        on_media_change: Callable[..., None] | None = None, on_front_change: Callable[..., None] | None = None,
    ) -> None:
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
        #: Who holds the microphone and camera (yuki.memory.avsignals.AvSignals; shared with the call
        #: worker in the service), read every MEDIA_EVERY_S - a cached read unless Windows changed them.
        self._signals = signals
        #: The screen's call verdicts (yuki.memory.callevidence.CallEvidence, the call worker's own; None: not
        #: running): a microphone hold is a meeting only when a screen of its app was judged in a call during it
        #: (revamp A9: VALORANT's voice chat and FL Studio recording were 1 h 35 m of "meetings" on 2 Oct).
        self._evidence = evidence
        #: App keys whose hold was seen without call evidence (logged once per hold).
        self._mic_only: dict[str, float] = {}
        self._av: Any = None                      # the last AvSnapshot
        self._mic_errors = 0
        #: Per app key with a measured meeting on: the site it is on ("" for an app without pages), and
        #: the last time (monotonic) the app was seen holding the device.
        self._meeting_sites: dict[str, str] = {}
        self._hold_seen: dict[str, float] = {}
        #: Gaming with voice chat (yuki.memory.avsignals.gaming_with_voice): the last judgement for the
        #: stretch in front, ``(id(stretch), when, gaming)``, and the GPU reader (tests pass a stub).
        self._gaming_cache: tuple[int, float, bool] | None = None
        self._gpu_fn: Callable[..., Any] | None = None
        #: The last stretches (start, end, app key, page host), to find the page that was in front when
        #: an app took the microphone.
        self._recent: list[tuple[float, float, str, str | None]] = []
        self._current: _Stretch | None = None
        self._last_row: TimelineRow | None = None
        self._acc_at = time.time()
        self._input_at = self._acc_at
        self._seq = 0
        self._title_first: float | None = None
        self._title_last: float | None = None
        self._next_flush = time.monotonic() + FLUSH_EVERY_S
        self._next_media = 0.0
        self._playing: list[tuple[str, str, str]] = []   # (app id casefolded, display label, media title)
        self._media_errors = 0
        self._no_page: dict[int, tuple[int, float]] = {}   # pid -> (streak, last probe wall time)
        self._page_results: list[tuple] = []
        self._page_lock = threading.Lock()
        self._probe: _PageProbe | None = None
        self._due = 0.0
        self.counters: Counter = Counter()
        #: Called (on this thread) with the window whose page changed address
        #: under the same title, so the watcher can read it (a reel, a feed).
        self.on_page_change = on_page_change
        #: Wall time of the last content (non-window) name change from the
        #: process in front: the page may have moved on.
        self._content_at = 0.0
        #: What Yuki is acting on (yuki.memory.acting.Acting), applied on this thread.
        self._acting: Any = None
        self._acting_next: Any = None
        #: Moments (yuki.memory.moments, plan rev 4 F4; no new polling): called on this thread with the media sessions
        #: that started or stopped playing at a media read, ``on_media_change(started, stopped, at=, by_yuki=,
        #: request_id=, initial=)`` (each ``{"app_id", "app", "title"}``; the title in RAM only), and when another app
        #: or window comes to the front, ``on_front_change(prev_key, new_key, at=, hwnd=, prev_hwnd=, by_yuki=,
        #: request_id=)`` (app keys as rows store them; "" for an app the privacy rules keep out). Never raise into here.
        self.on_media_change = on_media_change
        self.on_front_change = on_front_change
        #: The playing sessions of the last media read that answered, ``{(app_id, title)}``: the baseline media changes
        #: are told against (a read that failed changes nothing, so a recovered read is no "start").
        self._media_seen: set[tuple[str, str]] | None = None
        #: A media session's app id -> the app key of the process behind it, learned from the stretches' identities
        #: (image key, AUMID, Start entry) and, for a packaged app never in front yet, from its running process.
        self._app_ids: dict[str, str] = {}
        self._app_ids_missing: dict[str, float] = {}
        #: The app key and window last told as in front (``on_front_change``).
        self._front: tuple[str, int] | None = None

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

    def set_acting(self, acting: Any) -> None:
        """Yuki started (an :class:`~yuki.memory.acting.Acting`) or stopped (``None``)
        acting for the user; safe from any thread. The stretch in front is cut
        there: what follows is recorded ``by_yuki`` (or no longer)."""
        self._acting_next = acting
        if self._thread_id and self.alive:
            self._user32.PostThreadMessageW(self._thread_id, WM_APP_ACTING, 0, 0)
        else:
            self._acting = acting

    @property
    def alive(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def current_meeting(self) -> dict[str, Any] | None:
        """The meeting row in front now, content-free: ``{"process", "app", "service"}``, or None.

        Read by the call memory (:mod:`yuki.memory.calls`) to tell a call; safe
        from any thread (one attribute read of the open stretch).
        """
        st = self._current
        if st is None or not st.meeting or self._blocked:
            return None
        return {"process": st.process, "app": st.app, "service": st.meeting}

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
            "meetings_on": len(self._meeting_sites),
            "av": self._signals.stats() if self._signals is not None and hasattr(self._signals, "stats") else None,
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
            presence = getattr(self._w, "presence_facts", None)
            self._log("timeline_start", away_after_s=AWAY_AFTER_S, media_every_s=MEDIA_EVERY_S,
                      flush_every_s=FLUSH_EVERY_S, paused=self._paused,
                      presence=presence() if callable(presence) else None)
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
                    if not msg.hWnd and msg.message == WM_APP_ACTING:
                        try:
                            self._apply_acting(time.time())
                        except Exception as exc:
                            self._log_error("acting", exc)
                        continue
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
        elif st is not None and st.host and not st.hidden and self._content_at > st.page_checked_at:
            delay = min(delay, max(st.page_checked_at + PAGE_ACTIVE_REPROBE_S - now, 0.0))
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
            if event != _EVENT_OBJECT_NAMECHANGE or id_object != _OBJID_WINDOW or id_child != _CHILDID_SELF:
                if hwnd and self._fg and not self._blocked:
                    self._content_at = time.time()  # the page may have moved on (see PAGE_ACTIVE_REPROBE_S)
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
                _EVENT_OBJECT_NAMECHANGE, _EVENT_OBJECT_VALUECHANGE, None, self._callback, p, 0,
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

    def _apply_acting(self, now: float) -> None:
        """Take the acting state set by :meth:`set_acting`; cut the stretch in front where it changed."""
        acting = self._acting_next
        was = self._acting is not None
        self._acting = acting
        if (acting is not None) == was:
            return
        self.counters["acting_on" if acting is not None else "acting_off"] += 1
        st = self._current
        if st is None or self._blocked:
            return
        self._advance(now)
        st = self._current
        if st is None:
            return
        self._close(now)
        self._open(st.hwnd, now, like=st, state=st.state)

    def _mark_acting(self, st: _Stretch) -> None:
        acting = self._acting
        st.by_yuki = acting is not None
        st.yuki_request = (getattr(acting, "request", "") or None) if acting is not None else None

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
            self._mark_acting(st)
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
        # appOpen second review (V1): the seen ledger - this window was on screen, so a launch may show it again once
        # its app hides it in the tray (content-free: the handle, its process and that process's start)
        seen = getattr(self._windows, "note_on_screen", None)
        if seen is not None:
            try:
                seen([hwnd], persist=True)
            except Exception:  # noqa: BLE001 - never breaks the timeline
                pass
        pid = self._w._window_pid(hwnd)
        facts = self._procs.get(pid)
        rules = self.privacy.rules()
        fullscreen = self._fullscreen(hwnd)
        hidden = fullscreen and rules.pause_when_fullscreen
        app, withheld, title, host = facts.app_name, None, "", None
        meeting = None
        seen_host, signal = None, False
        if facts.is_own and rules.skip_own_windows:
            app, withheld = "Yuki", "own_window"
        elif meeting := self._meetings.get().match_process(facts.process_name, facts.app_name):
            # the user's always_apps override: meeting hours only, nothing from its windows
            withheld, hidden = rules.check_app(facts.process_name, facts.app_name) or "meeting", True
        elif reason := rules.check_app(facts.process_name, facts.app_name):
            app, withheld = "", reason
        elif hidden:
            if carry is not None and carry.hwnd == hwnd and carry.pid == pid and not carry.hidden:
                host, withheld, meeting = carry.host, carry.withheld, carry.meeting
                seen_host, signal = carry.seen_host, carry.signal_meeting
        else:
            title = self._w._window_title(hwnd)
            if reason := rules.check_window(facts.process_name, title):
                withheld, title = reason, ""
        ids, entry = self._identity(pid, facts.exe, facts.process_name)
        own_key = app_key(facts.process_name) or ""
        if own_key and not facts.is_own:
            for i in ids:   # media sessions name apps by these (in memory only)
                if i != own_key:
                    self._app_ids[i] = own_key
        st = _Stretch(start=now, end=now, hwnd=hwnd, pid=pid, process=facts.process_name if app else "",
                      app=app, title=title, withheld=withheld, host=host, ids=ids, entry=entry,
                      fullscreen=fullscreen, hidden=hidden, meeting=meeting,
                      proc=facts.process_name, proc_app=facts.app_name, exe=facts.exe, own=facts.is_own,
                      seen_host=seen_host, signal_meeting=signal)
        self._mark_acting(st)
        self._current = st
        self._front_changed(st, now)
        if app and not facts.is_own:   # never for an app the user's privacy rules keep out (its app is "")
            # appOpen F3: which program started this one (content-free, in memory only, once per process), so the
            # practice learner can read what the user did right after one of Yuki's openings
            try:
                from yuki.memory import lineage

                lineage.note(pid, now, by_yuki=st.by_yuki)
            except Exception:  # noqa: BLE001 - never breaks the timeline
                pass
        if hidden:
            self._unhook_names()
        else:
            self._hook_names(hwnd)
        idle = now - self._input_at
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
            host = path = meeting = seen_host = seen_path = None
            withheld = st.withheld
            title = st.title
            if page and page[1]:
                url = page[1]
                reason = self.privacy.rules().check_url(url)
                meeting = self._meetings.get().match_url(url)
                try:
                    parts = urlsplit(url)
                except ValueError:
                    parts = None
                if parts is not None and parts.scheme.lower() in ("http", "https"):
                    seen_host, seen_path = url_host(url), parts.path or "/"   # in memory only
                if meeting:
                    # the user's always_sites override: the service and host, never the title or the path
                    host, withheld, title = url_host(url), reason, ""
                elif reason:
                    withheld, title = reason, ""
                elif parts is not None:
                    if parts.scheme.lower() in ("http", "https"):
                        host = url_host(url)
                    path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
                    if not host:
                        path = f"{parts.scheme}:{path}"
            if not st.settled:
                st.host, st.path, st.withheld, st.title, st.meeting = host, path, withheld, title, meeting
                st.seen_host, st.seen_path = seen_host, seen_path
                if meeting:
                    self.counters["meeting_stretches"] += 1
                self._settle(st)
                continue
            if st.signal_meeting and seen_host is not None and seen_host == st.seen_host:
                continue  # still the meeting's site (its path and title are not recorded)
            if (host, path, withheld, meeting) != (st.host, st.path, st.withheld, st.meeting):
                # the page changed under the same title (a feed, a single-page site)
                self._advance(now)
                self._close(now)
                if st.meeting and not meeting:
                    self._open(st.hwnd, now)  # left the meeting page: read the window afresh
                else:
                    new = replace(st, host=host, path=path, withheld=withheld, title=title, meeting=meeting,
                                  seen_host=seen_host, seen_path=seen_path, signal_meeting=False)
                    self._open(st.hwnd, now, like=new, state=st.state)
                self.counters["page_rotations"] += 1
                if self.on_page_change is not None and host and not withheld and not meeting:
                    try:
                        self.on_page_change(st.hwnd)
                        self.counters["page_change_signals"] += 1
                    except Exception as exc:
                        self._log_error("on_page_change", exc)

    def _settle(self, st: _Stretch) -> None:
        """Decide the stretch's identity; continue the previous row when it is the same page."""
        st.settled = True
        if not st.meeting and self._current is st:
            now = max(st.start, time.time())
            self._poll_av(now)
            service = self._signal_service(st, now)
            if service:
                self._as_meeting(st, service)
        key = self.store.timeline_key(st.process, st.host, st.path, st.title, st.withheld,
                                      fullscreen=st.fullscreen, meeting=st.meeting)
        last = self._last_row
        if (
            last is not None and last.page_key == key and last.state == st.state
            and bool(last.by_yuki) == st.by_yuki
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
            by_yuki=st.by_yuki, yuki_request=st.yuki_request,
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
        self._remember(st, now)
        row = self._write(st, now)
        if row is not None:
            self._last_row = row

    def _remember(self, st: _Stretch, end: float) -> None:
        self._recent.append((st.start, end, app_key(st.proc) or "", st.seen_host))
        if len(self._recent) > 64:
            del self._recent[:32]

    # -- meetings: measured (an app in front holding the microphone or camera) --------------------

    def _poll_av(self, now: float) -> None:
        """The microphone and camera holders now (a cached read unless Windows changed them)."""
        if self._signals is None:
            try:
                from yuki.memory.avsignals import AvSignals

                self._signals = AvSignals()
            except Exception:
                self.counters["mic_errors"] += 1
                return
        try:
            self._av = self._signals.read()
            self._mic_errors = 0
        except Exception:
            self._mic_errors += 1
            self.counters["mic_errors"] += 1
            if self._mic_errors >= 3:
                self._av = None
            return
        rules = self._meetings.get()
        held = {h.key for h in self._devices(rules.camera)}
        for key in held:
            self._hold_seen[key] = now
        for key in list(self._meeting_sites):
            if key not in held and now - self._hold_seen.get(key, 0.0) >= rules.end_grace_s:
                del self._meeting_sites[key]   # the app let go: its meeting is over
                self.counters["meetings_ended"] += 1

    def _devices(self, camera: bool = True) -> tuple:
        av = self._av
        if av is None:
            return ()
        return tuple(av.mic) + (tuple(av.camera) if camera else ())

    def _holder(self, st: _Stretch, *, camera: bool = True) -> Any:
        """The microphone (or camera) holder that is the app of ``st``, or None."""
        if not st.proc or st.own:
            return None
        key = app_key(st.proc) or ""
        for h in self._devices(camera):
            if h.matches(process_key=key, exe=st.exe, ids=st.ids):
                return h
        return None

    def _site_at(self, key: str, since: float, st: _Stretch) -> str | None:
        """The page host in front, in the app ``key``, when it took the device at ``since`` (None: not known)."""
        if st.start <= since:
            return st.seen_host
        for start, end, k, host in reversed(self._recent):
            if k == key and start <= since <= end:
                return host
        return None

    def _signal_service(self, st: _Stretch, now: float) -> str | None:
        """The meeting ``st`` is by the measured signal, or None.

        The app in front has held the microphone (or the camera) for ``hold_s``. For an app that shows
        web pages the meeting is the site that was in front when the app took the device (a browser
        holds the microphone for one of its tabs; another site in front is not the meeting).
        """
        rules = self._meetings.get()
        h = self._holder(st, camera=rules.camera)
        if h is None:
            return None
        key = app_key(st.proc) or ""
        if rules.never(key, st.seen_host):
            return None
        if st.fullscreen and self._gaming_voice(st, h, now):
            return None   # a full-screen game's own voice chat: gaming, not a meeting
        site = self._meeting_sites.get(key)
        if site is None:
            if now - h.since < rules.hold_s:
                return None
            if self._evidence is not None and st.seen_host is None and not self._call_seen(h, key, now):
                # an app (not a site in a browser, whose meeting is the site that took the device) holds the
                # microphone, and no screen of it was seen in a call: a game's voice chat, a recording - no meeting
                return None
            site = ""
            if st.seen_host is not None:
                site = self._site_at(key, h.since, st) or ""
                if not site:
                    return None   # an app showing pages, but which page took the device is not known
            self._meeting_sites[key] = site
            self._hold_seen[key] = now
            self.counters["meetings_started"] += 1
        if site:
            if st.seen_host != site:
                return None
            return rules.name_for(site, site_of(site) or site)
        if st.seen_host is not None:
            return None   # the meeting is an app without pages; this is one of its pages? not the meeting
        return rules.name_for(key, st.proc_app or key)

    def _call_seen(self, holder: Any, key: str, now: float) -> bool:
        """Whether the screen judge saw this app in a call during its microphone hold (the call worker's measured
        verdicts, :meth:`yuki.memory.callevidence.CallEvidence.state`); a hold without it is logged once
        (``timeline_mic_no_call``, content-free)."""
        try:
            state, _service = self._evidence.state(holder, holder.since, now)
        except Exception:
            return True   # the verdicts cannot be read: the measured hold decides, as before
        if state is not None:
            self._mic_only.pop(key, None)
            return True
        if self._mic_only.get(key) != holder.since:
            self._mic_only[key] = holder.since
            self.counters["mic_no_call"] += 1
            self._log("timeline_mic_no_call", app=key, held_s=round(now - holder.since, 1))
        return False

    def _gaming_voice(self, st: _Stretch, holder: Any, now: float) -> bool:
        """The microphone holder is this full-screen stretch's app drawing like a game (measured).

        Judged at most every 30 s per stretch; the first "gaming" of a stretch is logged
        (``timeline_gaming_voice``, content-free) and counted (``gaming_voice``).
        """
        cached = self._gaming_cache
        if cached is not None and cached[0] == id(st) and now - cached[1] < 30.0:
            return cached[2]
        from yuki.memory.avsignals import gaming_with_voice, gpu_share

        pids: list[int] = []
        try:
            pid = int(self._w._window_pid(st.hwnd) or 0)
            if pid:
                pids.append(pid)
        except Exception:
            pass
        camera = tuple(self._av.camera) if self._av is not None else ()
        gaming, facts = gaming_with_voice(holder, fullscreen=st.fullscreen, shows_page=st.seen_host is not None,
                                          camera=camera, pids=pids, gpu_fn=self._gpu_fn or gpu_share)
        first = gaming and not (cached is not None and cached[0] == id(st) and cached[2])
        self._gaming_cache = (id(st), now, gaming)
        if first:
            self.counters["gaming_voice"] += 1
            self._log("timeline_gaming_voice", app=app_key(st.proc) or "", **facts)
        return gaming

    def _as_meeting(self, st: _Stretch, service: str) -> None:
        """Turn ``st`` into a meeting row: the app and service, the site's host - never a title or path."""
        st.meeting, st.signal_meeting = service, True
        st.title, st.path = "", None
        st.host = st.seen_host
        st.process, st.app = st.proc, st.proc_app or st.app or st.proc
        if st.seen_host is None:
            st.withheld = st.withheld or "meeting"
            if not st.hidden:
                st.hidden = True   # nothing about the window is read while it is a meeting
                if self._current is st:
                    self._unhook_names()
        self.counters["meeting_stretches"] += 1

    def _check_meeting(self, now: float) -> None:
        """At each signal read: the stretch in front starts or ends a measured meeting."""
        st = self._current
        if st is None or self._blocked or not st.settled:
            return
        if st.signal_meeting:
            if (app_key(st.proc) or "") not in self._meeting_sites:
                # the app let go of the microphone and camera: the window is read afresh
                self._close(now)
                self._open(st.hwnd, now)
            return
        if st.meeting:
            return   # an always_* override
        service = self._signal_service(st, now)
        if service:
            self._close(now)
            meeting = replace(st)
            self._as_meeting(meeting, service)
            self._open(st.hwnd, now, like=meeting, state=st.state)
            if self._current is not None and self._current.hidden:
                self._unhook_names()

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
        """The app in front is playing media (any of its windows or tabs)."""
        return any(self._matches(st, app_id) for app_id, _, _ in self._playing)

    def _watching(self, st: _Stretch) -> bool:
        """The stretch in front is what plays: its app's media, and for a web page the page naming it."""
        return any(self._matches(st, app_id) and self._shows(st, title) for app_id, _, title in self._playing)

    @staticmethod
    def _shows(st: _Stretch, media_title: str) -> bool:
        """Whether the stretch in front can be the one playing: for a web page, its
        title names the media when both have one (a browser's other tab playing
        is not the page in front: Wikipedia read while a YouTube tab plays is not
        watching). Two titles the OS and the app wrote, compared as text."""
        if not st.host or not media_title or not st.title:
            return True
        return " ".join(media_title.casefold().split()) in " ".join(st.title.casefold().split())

    def _mic_on(self, st: _Stretch) -> bool:
        """A meeting's app is using the microphone (by executable, image key or package family)."""
        return bool(st.meeting) and self._holder(st, camera=False) is not None

    def _engaged(self, st: _Stretch) -> bool:
        """At the app without input: its media plays, or it is a meeting using the microphone."""
        return self._fg_media(st) or self._mic_on(st)

    def _credit(self, st: _Stretch, a: float, b: float, kind: str) -> None:
        if b <= a:
            return
        d = b - a
        setattr(st, kind, getattr(st, kind) + d)
        if self._mic_on(st):
            st.mic_s += d
        for app_id, label, _ in self._playing:
            if self._matches(st, app_id):
                if kind != "away_s":
                    st.media_s += d
            else:
                st.media_other[label] = st.media_other.get(label, 0.0) + d
        st.end = b

    def _switch(self, st: _Stretch, at: float, state: str) -> _Stretch:
        """End ``st`` at ``at`` and continue the same window in ``state``."""
        self._current = st
        self._remember(st, at)
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
        if new_input:
            # appOpen final check (L-T3/L-T8): when the user gave input (a time only, in memory), so the practice
            # learner tells a program the user started from one Yuki's own slow launch started meanwhile
            try:
                from yuki.memory import lineage

                lineage.note_input(l1)
            except Exception:  # noqa: BLE001 - never breaks the timeline
                pass
        if st.state == "present" and self._watching(st):
            # The app in front is playing media: watching or listening, whatever
            # the idle timer says (see the module docstring).
            self._credit(st, t0, now, "passive_s")
            return
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
        playing: list[tuple[str, str, str]] = []
        entries: dict[str, Any] = {}
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
            playing.append((app_id, label, str(s.get("title") or "")))
            entries[app_id] = entry
        self._playing = playing
        self._media_changed(playing, entries)

    def _media_changed(self, playing: list[tuple[str, str, str]], entries: dict[str, Any]) -> None:
        """Tell ``on_media_change`` which sessions started or stopped playing since the last read that answered (by
        app id and title: a new track is a start). The first read tells what plays then, marked ``initial``."""
        now_set = {(app_id, title) for app_id, _label, title in playing}
        before, self._media_seen = self._media_seen, now_set
        if self.on_media_change is None:
            return
        old = before or set()
        started = sorted(now_set - old)
        stopped = sorted(old - now_set)
        if not started and not stopped:
            return
        try:
            acting = self._acting
            self.on_media_change(
                [{"app_id": a, "app": self._media_app(a, entries.get(a)), "title": t} for a, t in started],
                [{"app_id": a, "app": self._media_app(a, entries.get(a)), "title": t} for a, t in stopped],
                at=time.time(), by_yuki=acting is not None,
                request_id=getattr(acting, "request_id", None) if acting is not None else None,
                initial=before is None)
            self.counters["media_changes"] += 1
        except Exception as exc:   # never breaks the timeline
            self._log_error("on_media_change", exc)

    def _media_app(self, app_id: str, entry: Any = None) -> str:
        """The app key (as rows store it) of the process behind a media session: learned from the stretches in front;
        else the Start entry's program; else, for a packaged app (an AUMID, ``Family!App``), its running process (once
        per app id, the processes' own package identity); else the id as a key."""
        import os

        known = self._app_ids.get(app_id)
        if known:
            return known
        target = getattr(entry, "target", "") if entry is not None else ""
        if target:
            key = app_key(os.path.basename(target)) or ""
            if key:
                self._app_ids[app_id] = key
                return key
        if "!" in app_id and time.monotonic() - self._app_ids_missing.get(app_id, -1e9) > 600.0:
            try:
                import psutil

                facts_for = getattr(self._windows, "_facts_for", None)
                for pid in psutil.pids() if facts_for is not None else ():
                    aumid, exe = facts_for(pid)
                    if aumid == app_id and exe:
                        key = app_key(os.path.basename(exe)) or ""
                        if key:
                            self._app_ids[app_id] = key
                            return key
            except Exception:   # noqa: BLE001 - then the id itself
                pass
            self._app_ids_missing[app_id] = time.monotonic()
        return app_key(app_id) or app_id

    def _front_changed(self, st: _Stretch, now: float) -> None:
        """Tell ``on_front_change`` when the app or window in front is another than the last told (a stretch of the
        same window in another state, or after a title change, is no front change)."""
        key, hwnd = app_key(st.process) or "", st.hwnd
        prev, self._front = self._front, (key, hwnd)
        if self.on_front_change is None or prev == (key, hwnd):
            return
        try:
            self.on_front_change(prev[0] if prev else "", key, at=now, hwnd=hwnd, prev_hwnd=prev[1] if prev else 0,
                                 by_yuki=st.by_yuki,
                                 request_id=getattr(self._acting, "request_id", None) if st.by_yuki else None)
            self.counters["front_changes"] += 1
        except Exception as exc:   # never breaks the timeline
            self._log_error("on_front_change", exc)

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
            self._poll_av(now)
            self._check_meeting(now)
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
            st is not None and st.settled and not st.hidden and st.state == "present"
            and now - self._input_at < AWAY_AFTER_S
        ):
            if st.host:
                due = PAGE_ACTIVE_REPROBE_S if self._content_at > st.page_checked_at else PAGE_REPROBE_S
            elif not st.withheld and not st.meeting and self._no_page.get(st.pid, (0, 0.0))[0] < NO_PAGE_STREAK:
                due = PAGE_RETRY_S
            else:
                due = None
            if due is not None and now - st.page_checked_at >= due:
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
    "POWER_STATES", "POWER_WORDS", "GAP_WORDS", "is_power", "power_spans", "describe_power", "describe_break",
]
