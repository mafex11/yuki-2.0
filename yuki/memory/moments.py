"""Moments: one table of state changes, rebuildable from what memory already keeps (plan rev 4, Phase F item 4).

``docs/research/audit-2026-10-09/PLAN.md`` decisions D1 (the vocabulary), D7 (where a meeting join comes from), D18
(Yuki's own runs are Yuki's); spec ``round3/map-foundations.md`` F1 (its Spotify Web API parts are void: zero setup).

A *moment* is *that* something changed: a call joined, a page reached, a playlist started, a level moved. Nothing
here keeps what was said or shown. The vocabulary is closed (:data:`MOMENT_KINDS`):

* ``call_join`` - a meeting **or** a voice channel joined (``app`` = the calling app's key, ``service`` = a calling
  site's product name when one is known), from :func:`is_call_join` (D7, below);
* ``game_voice`` - a game's own voice chat (a ``calls`` row of kind ``game`` whose program is a game by machine
  facts, :func:`yuki.memory.gamegate.judge_program`); never a ``call_join``;
* ``site_front`` - a calling site's page came to the front (the watcher's blocked-site sighting, app + service only);
* ``media_play`` - a media session started playing something new (the timeline's existing 15 s media read, on
  change only); the title is **never kept**, only its keyed digest;
* ``media_context`` - which Spotify playlist plays (fed by the Spotify window reader, ``yuki.connect``): its name
  encrypted, a Spotify-owned one digest-only;
* ``page`` - a page visit that reached ``[moments] page_dwell_s`` or matched a site's URL template (the template's
  value, a video id or a channel handle, encrypted; else the timeline's own page key as the digest);
* ``volume_change`` - a level moved and stayed (fed by the volume watcher and the Spotify slider read);
* ``message_sent`` - a message by the user appeared in a chat (the ``messages`` rows, ``is_me``, first seen);
* ``display_change`` - a monitor's brightness or screen power changed (morning21 B9: the end Yuki's display runs
  reach, read back over DDC/CI; nothing writes the user's own display changes yet, so it is neither live nor derived).

Two classes. **Derived** kinds (``call_join``, ``game_voice``, ``page``, ``message_sent``) come from rows that already
exist (``calls``, ``timeline``, ``messages``) through :class:`Extractor`, which reads past a cursor per source; they
point back to their source row and ``python -m yuki.memory.moments rebuild`` recomputes them, so a rule change is a
code change, never an edit of memory. **Live** kinds (``site_front``, ``media_play``, ``media_context``,
``volume_change``) are their own source and are written once, through :class:`MomentWriter`, when the change is seen.
No new polling: the timeline's media read and foreground events, the call worker's own events, and the watcher's
reads feed :class:`MomentHub`; the service's 1 s loop calls :meth:`MomentHub.beat`.

**Where a meeting join comes from (D7).** *Seen* joins: ``calls`` rows of kind ``call`` with ``via`` screen /
screen_stale / meeting whose program is not a game, and timeline meeting rows on a page whose host is a calling site
(the user's ``[meetings]`` word, the call evidence's persisted verdicts, or a seen call of the same app that named the
same service). *Hold* joins (``evidence='hold'``): a ``calls`` row ``via='mic'``, or a host-less timeline meeting row
with ``mic_s > 0``, only for an app with at least one seen call on record (or in the user's ``[calls] apps``) and never
a game. So FL Studio, Settings and VALORANT never become meetings. Candidates of one app closer than
:data:`JOIN_GAP_S` are one join (``at`` = the earliest, ``evidence`` = the earliest seen one's, else ``hold``;
``facts.seen_at`` = the earliest seen candidate). A hold join is never a done-check and never makes a routine by
itself: the routine miner (phase R) counts hold days only inside a routine that already has >= 3 seen hit days of its
own. ``mic_spans`` is never a source.

**Yuki's own runs (D18).** :func:`attribute` stamps ``by='yuki'`` and the run's ``request_id`` on a moment of the run's
app and kind inside the run's window plus a per-kind lag (:data:`ATTRIBUTION_LAG_S`). The windows come from
``skill_runs`` (phase S, migration 41) once it exists; until then live moments take Yuki's acting marker at the time
they are seen, and page visits and call joins the timeline's own ``by_yuki`` (a join whose earliest candidate is a
stretch begun while Yuki acted is Yuki's: marked, never dropped).

Logs are content-free: kinds, app keys, code words, counts, times and 12-hex digests (``moment``, ``moment_source``,
``site_front``), never a title, a name, a link or a host. The new table never appears in the MCP.

CLI (Yuki's own pipeline command, like ``scrub_secrets``)::

    python -m yuki.memory.moments rebuild [--db PATH] [--dry-run]
    python -m yuki.memory.moments backfill --since 2026-09-25 [--db PATH] [--dry-run]
    python -m yuki.memory.moments stats [--db PATH]
    python -m yuki.memory.moments bench [--db PATH] [--hours 1]      # always on a temporary copy
"""

from __future__ import annotations

import argparse
import json
import os
import threading
import time
from collections import Counter, defaultdict, deque
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from yuki.memory.store import app_key

# ---------------------------------------------------------------------------
# Vocabulary (D1) and rules
# ---------------------------------------------------------------------------

#: The closed vocabulary of state changes (D1). ``sit_down`` and ``app_front`` are deferred (rebuildable later).
MOMENT_KINDS: tuple[str, ...] = (
    "call_join", "game_voice", "site_front", "media_play", "media_context", "page", "volume_change", "message_sent",
    "display_change",
)
#: Kinds computed from existing rows (rebuildable) and the sources they come from. ``timeline_host``: a host-scope page
#: visit (RULE_VERSION 3, phase L design §3.3), apart from ``timeline`` so a page and its host visit can share a first row.
DERIVED_KINDS: tuple[str, ...] = ("call_join", "game_voice", "page", "message_sent")
DERIVED_SOURCES: tuple[str, ...] = ("calls", "timeline", "messages", "timeline_host")
#: Kinds written once, live, when the change is seen.
LIVE_KINDS: tuple[str, ...] = ("site_front", "media_play", "media_context", "volume_change")
LIVE_SOURCES: tuple[str, ...] = ("site", "media", "spotify", "volume")
#: ``calls.via`` values that are a seen call (a screen or site judged in a call; the old timeline meeting route).
SEEN_VIAS: tuple[str, ...] = ("screen", "screen_stale", "meeting")
#: ``evidence`` values of a seen join (``hold`` is the other one).
SEEN_EVIDENCE: tuple[str, ...] = ("screen", "meeting", "site")
#: Bumped whenever a derived rule changes: the service rebuilds the derived moments once on its next start. 2 (fWire,
#: 10 Oct): page moments take their entity from the sites' URL templates (``yuki.connect.templates.page_entity``). 3
#: (lCore, 10 Oct; phase L design §3.3, D23): every moment with an entity carries ``entity_key`` (one identity across
#: apps: the same page in two browsers is one entity), and a browser's stretch on one registrable domain whose dwell
#: reaches ``[moments] host_dwell_s`` is a host-scope ``page`` moment (source ``timeline_host``, ``facts.scope =
#: "host"``, entity = the domain) for hosts no site file covers.
RULE_VERSION = 3
#: Join candidates of one app closer than this are one join (the timeline's own "one meeting" gap,
#: ``yuki.memory.timeline.MEETING_GAP_S``).
JOIN_GAP_S = 300.0
#: The extractor re-reads rows written up to this long before its cursor (two writers' clocks and transactions).
CURSOR_OVERLAP_S = 30.0
#: Rows of one page closer than this are one visit (a present/away switch, a merge gap).
VISIT_GAP_S = 5.0
#: D18: a moment of a run's app and kind counts as Yuki's up to this long after the run finished (s).
ATTRIBUTION_LAG_S: dict[str, float] = {
    "media_play": 20.0, "media_context": 20.0, "volume_change": 3.0, "site_front": 3.0, "call_join": 60.0,
    "message_sent": 120.0, "page": 20.0, "display_change": 3.0,
}
#: The derived refresh runs at most this often (s) unless nudged; the retention pass this often.
PRUNE_EVERY_S = 6 * 3600.0
#: A rebuild that failed (a busy database) is tried again this long after, off the loop (s).
REBUILD_RETRY_S = 600.0
#: The checkpoint names (``source_checkpoints``, prefix ``moments.``).
CURSORS = ("calls", "timeline", "messages", "rule", "context")
#: Phase L (design §9.1, critique L9): after a refresh that wrote occasion-end moments, the skill learner's incremental
#: pass runs off the loop, at most this often (s), and only while the user is idle (:data:`LEARN_IDLE_S` without input),
#: with no game in front and no call (``yuki.skills.learn.quiet``). Never polls: it rides on the existing beat.
LEARN_EVERY_S = 600.0
LEARN_IDLE_S = 120.0
#: The moment kinds an occasion can end on (``yuki.skills.connection.SERVE_ENDS``): a refresh or a flush that wrote one
#: makes the learner's incremental pass due.
LEARN_END_KINDS: tuple[str, ...] = ("call_join", "page", "message_sent", "media_context", "volume_change",
                                     "display_change")


@dataclass(frozen=True)
class MomentsConfig:
    """The privacy file's ``[moments]`` table (defaults when the file has none)."""

    enabled: bool = True
    volume_events: bool = True
    media_titles: bool = False     # True: a media title is kept encrypted (unless its app was private lately)
    refresh_s: float = 60.0
    page_dwell_s: float = 300.0
    #: A host-scope page visit counts from this dwell (s; design §3.3, Q14: 60 s, tuned after the Playbook review).
    host_dwell_s: float = 60.0
    keep_days: float = 90.0

    @classmethod
    def from_dict(cls, table: dict | None) -> MomentsConfig:
        table = table or {}
        d = cls()

        def num(key: str, default: float, lo: float) -> float:
            try:
                return max(lo, float(table.get(key, default)))
            except (TypeError, ValueError):
                return default

        return cls(enabled=bool(table.get("enabled", d.enabled)),
                   volume_events=bool(table.get("volume_events", d.volume_events)),
                   media_titles=bool(table.get("media_titles", d.media_titles)),
                   refresh_s=num("refresh_s", d.refresh_s, 5.0), page_dwell_s=num("page_dwell_s", d.page_dwell_s, 0.0),
                   host_dwell_s=num("host_dwell_s", d.host_dwell_s, 10.0), keep_days=num("keep_days", d.keep_days, 1.0))


def log_digest(digest: str | None) -> str | None:
    """A digest as logs carry it (D20: 12 hex characters, one helper for every emitter)."""
    if not digest:
        return None
    try:
        from yuki.log.events import log_digest as _shared   # fPrivacy's helper, once merged
    except ImportError:
        return str(digest)[:12]
    return _shared(digest)


@dataclass
class Moment:
    """One state change (a row of ``moments``). ``entity`` is plaintext in RAM only: it is digested, and encrypted
    only when ``keep_entity`` is set (never logged)."""

    kind: str
    at: float
    source: str
    source_id: int | None = None
    app: str | None = None
    service: str | None = None
    ended_at: float | None = None
    entity: str | None = None
    keep_entity: bool = False
    entity_digest: str | None = None
    value: float | None = None
    value_from: float | None = None
    means: str | None = None
    by: str = "user"
    request_id: str | None = None
    evidence: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    partial: bool = False          # only ended_at / value / facts of an existing row
    seen_at: float | None = None
    #: Encrypted extras (a learned ``spotify:`` link), written into ``facts`` as ciphertext at flush.
    secret_facts: dict[str, str] = field(default_factory=dict)
    #: RAM only: ``service`` is a product name a call or a verdict gave, so the ``moment`` log line may carry it (a
    #: user's own label for a site never goes to a log).
    log_service: bool = True
    #: D23 (RULE_VERSION 3): the entity's identity across apps, ``digest("entity", place, entity)``; computed at
    #: :meth:`row` when the entity is in RAM (:func:`entity_key`), or set by the writer that had it (a media title).
    entity_key: str | None = None

    def key(self) -> tuple[str, int | None, str]:
        return (self.source, self.source_id, self.kind)

    def digest_with(self, cipher: Any) -> str | None:
        if self.entity_digest is None and self.entity:
            self.entity_digest = cipher.digest("moment", self.kind, self.app or "", self.entity)
        return self.entity_digest

    def place(self) -> str:
        """Where the entity lives, for :func:`entity_key`: a site's or service's name (a page's template site, a
        host-scope page's domain, a chat's thread space), else the app (a player's playlist, a title)."""
        facts = self.facts if isinstance(self.facts, dict) else {}
        if self.kind == "page":
            return str(facts.get("host") if facts.get("scope") == "host" else (self.service or facts.get("host") or ""))
        if self.kind == "message_sent":
            return "thread"
        return str(self.service or self.app or "")

    def row(self, cipher: Any) -> dict[str, Any]:
        """The store row (``Store.save_moments``): the entity encrypted only when kept; never plaintext."""
        if self.partial:
            return {"partial": True, "source": self.source, "source_id": self.source_id, "kind": self.kind,
                    "ended_at": self.ended_at, "value": self.value, "facts": dict(self.facts) or None}
        digest = self.digest_with(cipher)
        if self.entity_key is None and self.entity:
            self.entity_key = entity_key(cipher, self.place(), self.entity)
        facts = dict(self.facts)
        for k, v in self.secret_facts.items():
            if v:
                facts[f"{k}_ciphertext"] = cipher.encrypt(v)
        return {
            "at": float(self.at), "ended_at": self.ended_at, "seen_at": self.seen_at, "kind": self.kind,
            "app": self.app, "service": self.service, "entity_digest": digest, "entity_key": self.entity_key,
            "entity_ciphertext": cipher.encrypt(self.entity) if (self.keep_entity and self.entity) else None,
            "value": self.value, "value_from": self.value_from, "means": self.means, "by": self.by or "user",
            "request_id": self.request_id, "source": self.source, "source_id": self.source_id,
            "evidence": self.evidence, "facts": facts or None,
        }


def entity_key(cipher: Any, place: str | None, entity: str | None) -> str | None:
    """D23: one entity's identity across apps (``moments.entity_key``): the keyed digest of where it lives and what it
    is, case-folded and spaced once (the same page in two browsers, one playlist read twice, is one key). ``None``
    with no entity."""
    if not entity:
        return None
    norm = " ".join(str(entity).split()).casefold()
    return str(cipher.digest("entity", " ".join(str(place or "").split()).casefold(), norm))


def registrable_domain(host: str | None) -> str:
    """A host's registrable domain as a fixed format (phase L design §3.3, critique L6): lower case, no leading
    ``www.``, its last two labels, or its last three when the last is a two-letter country label and the one before it
    is a second-level label of three letters or fewer (``a.b.co.uk`` -> ``b.co.uk``). An address of digits stays
    whole. ``""`` for no host. A machine-format rule, never a list of sites."""
    host = str(host or "").strip().strip(".").casefold()
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    labels = host.split(".")
    if len(labels) <= 2 or all(part.isdigit() for part in labels):
        return host
    if len(labels[-1]) == 2 and len(labels[-2]) <= 3:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


_templated_lock = threading.Lock()
_templated: frozenset[str] | None = None


def templated_hosts() -> frozenset[str]:
    """The hosts a site file covers (``yuki/connect/sites/*.toml`` ``[site] hosts``), lower case, no ``www.``: their
    pages are template pages, never host-scope ones (cached; ``frozenset()`` when there is no site data)."""
    global _templated
    with _templated_lock:
        if _templated is not None:
            return _templated
    out: set[str] = set()
    try:
        from yuki.connect.templates import site, sites

        for name in sites():
            for h in (site(name).get("site") or {}).get("hosts") or []:
                h = str(h).strip().casefold()
                out.add(h[4:] if h.startswith("www.") else h)
    except Exception:  # noqa: BLE001 - no site data: every host is untemplated
        out = set()
    with _templated_lock:
        _templated = frozenset(out)
    return _templated


# ---------------------------------------------------------------------------
# D18: Yuki's own runs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunWindow:
    """One of Yuki's runs, as the skill engine records it (``skill_runs``, phase S): which request, which app and end
    kind, when it started and when it was done. ``value`` / ``entity_digest``: the level or the playlist it set."""

    request_id: str | None
    app: str | None
    end_kind: str | None
    t0: float
    t_done: float | None = None
    value: float | None = None
    entity_digest: str | None = None


def attribute(m: Moment, runs: Sequence[RunWindow], *, now: float | None = None) -> Moment:
    """Stamp ``by='yuki'`` and the run's ``request_id`` on ``m`` when one of Yuki's runs made it (D18).

    A run matches a moment of its own app and end kind whose ``at`` lies in ``[t0, t_done + lag]``
    (:data:`ATTRIBUTION_LAG_S`). Spotify's own reads are matched by what the run set, since the read lags: a slider
    read (``volume_change`` with ``means='in_app'``) is Yuki's when its level equals the level a run set since the
    previous read (``facts['since']``); a playlist read (``media_context``) when it is the playlist a run started since
    then. A moment already marked Yuki's keeps its mark."""
    if m.by == "yuki" or not runs:
        return m
    lag = ATTRIBUTION_LAG_S.get(m.kind)
    if lag is None:
        return m
    now = time.time() if now is None else now
    since = m.facts.get("since") if isinstance(m.facts, dict) else None
    lo = float(since) if isinstance(since, (int, float)) else m.at - 3600.0
    for run in runs:
        if not run.app or run.app != m.app or run.end_kind != m.kind:
            continue
        if m.kind == "volume_change" and m.means == "in_app":
            hit = (run.value is not None and m.value is not None and abs(float(run.value) - float(m.value)) < 0.5
                   and lo <= run.t0 <= m.at)
        elif m.kind == "media_context":
            hit = bool(run.entity_digest) and run.entity_digest == m.entity_digest and lo <= run.t0 <= m.at
        else:
            end = run.t_done if run.t_done is not None else now
            hit = run.t0 <= m.at <= end + lag
        if hit:
            return replace(m, by="yuki", request_id=run.request_id)
    return m


def skill_runs_between(store: Any, since: float, until: float) -> list[RunWindow]:
    """Yuki's run windows from ``skill_runs`` (phase S, migration 41) overlapping ``[since, until]``; ``[]`` before
    that table exists or when it lacks the D18 columns (``request_id, app, end_kind, t0, t_done``)."""
    try:
        cols = {r[1] for r in store._query("PRAGMA table_info(skill_runs)")}
    except Exception:  # noqa: BLE001
        return []
    need = {"request_id", "app", "end_kind", "t0", "t_done"}
    if not need <= cols:
        return []
    extra = [c for c in ("value", "entity_digest") if c in cols]
    try:
        rows = store._query(
            "SELECT request_id, app, end_kind, t0, t_done%s FROM skill_runs WHERE t0 <= ? AND coalesce(t_done, t0) >= ?"
            % "".join(", " + c for c in extra), (float(until), float(since) - max(ATTRIBUTION_LAG_S.values())))
    except Exception:  # noqa: BLE001
        return []
    out = []
    for r in rows:
        d = dict(r)
        try:
            out.append(RunWindow(request_id=d.get("request_id"), app=app_key(d.get("app")) or None,
                                 end_kind=d.get("end_kind"), t0=float(d["t0"]),
                                 t_done=float(d["t_done"]) if d.get("t_done") is not None else None,
                                 value=d.get("value"), entity_digest=d.get("entity_digest")))
        except (TypeError, ValueError):
            continue
    return out


# ---------------------------------------------------------------------------
# D7: is this a call join?
# ---------------------------------------------------------------------------


def call_at(detected_at: float, hold_since: float | None) -> float:
    """When a call row's join happened: the microphone hold's start (``hold_since``) when it came first, else when the
    call was detected (the rule Appendix E's E1 replay used)."""
    return float(hold_since) if hold_since is not None and hold_since <= detected_at else float(detected_at)


def is_call_join(row: Any, *, app: str, seen_apps: Iterable[str], is_game: Callable[[str], bool],
                 calls_apps: Iterable[str] = ()) -> tuple[bool, str | None]:
    """D7's one rule for a ``calls`` row: ``(True, evidence)`` when it is a call join, else ``(False, None)``.

    ``evidence``: ``screen`` (via screen / screen_stale), ``meeting`` (the old timeline route), ``hold`` (``via='mic'``
    of an app with a seen call on record or in the user's ``[calls] apps``). A game's program (machine facts) is never a
    call join; a game voice row (``kind='game'``) is not one either (:func:`is_game_voice`). ``app``: the row's app key
    as the timeline writes it (:class:`AppIdentity`)."""
    kind = _get(row, "kind") or "call"
    if kind != "call":
        return False, None
    process = _get(row, "process") or ""
    if is_game(process) or (app and app != app_key(process) and is_game(app)):
        return False, None
    via = _get(row, "via") or ""
    if via in SEEN_VIAS:
        return True, "meeting" if via == "meeting" else "screen"
    if via == "mic" and app and (app in set(seen_apps) or app in set(calls_apps)):
        return True, "hold"
    return False, None


def seen_call_record(store: Any, *, is_game: Callable[[str], bool] | None = None,
                     identity: AppIdentity | None = None) -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    """D7's "seen call on record": the app keys with at least one seen call (``calls`` kind ``call``, ``via`` in
    :data:`SEEN_VIAS`, not a game), and the ``(app, service)`` pairs those calls named (casefolded). A hold of an app
    outside the first set is never a meeting; the timeline's display proof can share this with :func:`is_call_join`."""
    if is_game is None:
        is_game = game_judge(store)
    if identity is None:
        identity = AppIdentity(store)
        identity.refresh(time.time(), force=True)
    apps: set[str] = set()
    services: set[tuple[str, str]] = set()
    try:
        rows = store._query("SELECT process, app, service, via FROM calls WHERE kind = 'call'")
    except Exception:  # noqa: BLE001
        rows = []
    for r in rows:
        if r["via"] not in SEEN_VIAS or is_game(r["process"] or ""):
            continue
        app = identity.key(r["process"], r["app"])
        if app:
            apps.add(app)
            if r["service"]:
                services.add((app, " ".join(str(r["service"]).split()).casefold()))
    return frozenset(apps), frozenset(services)


def is_game_voice(row: Any, *, is_game: Callable[[str], bool]) -> bool:
    """A ``calls`` row of kind ``game`` whose program is still a game by machine facts (FL Studio's two old ``game``
    rows from before capQ2b drop out here)."""
    return (_get(row, "kind") or "call") == "game" and is_game(_get(row, "process") or "")


def _get(row: Any, name: str) -> Any:
    if isinstance(row, dict):
        return row.get(name)
    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return getattr(row, name, None)


def game_judge(store: Any = None) -> Callable[[str], bool]:
    """``is_game(process key)`` from machine facts only (``gamegate.judge_program``): the live gate's game set when the
    gate runs in this process, else the packaged list, the user's additions, Windows' detected games and the learned set
    kept in the database. The Direct3D reading of *now* is never used: a stored row is judged by what its program is."""
    from yuki.memory import gamegate

    games = None
    live = getattr(gamegate, "_LIVE", None)
    try:
        if live is not None and live.alive:
            games = live.games()
    except Exception:  # noqa: BLE001
        games = None
    if games is None:
        try:
            base = gamegate._fallback_games()
        except Exception:  # noqa: BLE001
            base = gamegate.GameSet.load(None, None, ())
        learned: dict[str, dict[str, Any]] = {}
        if store is not None:
            try:
                from yuki.memory.trace_store import load_game_evidence

                kept, _dropped = gamegate.recheck_evidence(load_game_evidence(store))
                learned = {gamegate.stem(k): v for k, v in gamegate.confirmed(kept).items() if gamegate.stem(k)}
            except Exception:  # noqa: BLE001
                learned = {}
        games = gamegate.GameSet(fixed=base.fixed, learned=learned, windows=base.windows)
    cache: dict[str, bool] = {}

    def is_game(process: str) -> bool:
        key = app_key(process) or ""
        if not key:
            return False
        if key not in cache:
            cache[key] = bool(gamegate.judge_program(key, "", games=games, d3d_fn=lambda: False)[0])
        return cache[key]

    return is_game


class AppIdentity:
    """A ``calls`` row's app as the timeline writes it: its process key when the timeline knows that key, else the
    timeline process whose display name equals the call's app name (a packaged app's call row is keyed by its package,
    ``com.tinyspeck.slackdesktop``, where the timeline has the image, ``slack``; the same idea as
    ``Store._meetings_proven``)."""

    REFRESH_S = 3600.0

    def __init__(self, store: Any) -> None:
        self._store = store
        self._procs: set[str] = set()
        self._names: dict[str, str] = {}
        self._at = float("-inf")

    def refresh(self, now: float, *, force: bool = False) -> None:
        if not force and now - self._at < self.REFRESH_S:
            return
        self._at = now
        counts: dict[str, Counter] = defaultdict(Counter)
        procs: set[str] = set()
        try:
            rows = self._store._query("SELECT process, app, count(*) AS n FROM timeline WHERE process IS NOT NULL"
                                      " AND process != '' GROUP BY process, app")
        except Exception:  # noqa: BLE001
            rows = []
        for r in rows:
            key = app_key(r["process"]) or ""
            if not key:
                continue
            procs.add(key)
            name = " ".join(str(r["app"] or "").split()).casefold()
            if name:
                counts[name][key] += int(r["n"] or 0)
        self._procs = procs
        self._names = {name: c.most_common(1)[0][0] for name, c in counts.items() if c}

    def key(self, process: str | None, app: str | None = None) -> str:
        k = app_key(process) or ""
        if k and k in self._procs:
            return k
        name = " ".join(str(app or "").split()).casefold()
        return self._names.get(name) or k


# ---------------------------------------------------------------------------
# Calling sites (timeline meeting rows on a page)
# ---------------------------------------------------------------------------

#: Where the call evidence keeps its verdicts on the shipped list of blocked sites (``callevidence``, phase F, fLive):
#: ``{entry: [calling, service]}``, optionally under ``"verdicts"`` next to the list's digest.
SITE_VERDICTS_STATE = "call_evidence:sites"


def _host_under(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith("." + suffix)


def persisted_site_verdicts(store: Any) -> dict[str, tuple[bool, str]]:
    """The call evidence's persisted verdicts on the shipped list (entry -> (calling, service)); ``{}`` when none."""
    try:
        raw = store.nudge_state(SITE_VERDICTS_STATE)
        data = json.loads(raw) if raw else {}
    except Exception:  # noqa: BLE001
        return {}
    if isinstance(data, dict) and isinstance(data.get("verdicts"), dict):
        data = data["verdicts"]
    out: dict[str, tuple[bool, str]] = {}
    if isinstance(data, dict):
        for entry, v in data.items():
            if isinstance(v, (list, tuple)) and v:
                out[str(entry).casefold()] = (v[0] is True, str(v[1]) if len(v) > 1 and v[1] else "")
            elif isinstance(v, dict):
                out[str(entry).casefold()] = (v.get("calling") is True, str(v.get("service") or ""))
    return out


class CallingSites:
    """Whether a page host a timeline meeting row was on is a calling site, and its service (None: not one).

    In order: the user's ``[meetings]`` word (``never_sites``, ``always_sites``); the call evidence's persisted verdict
    on the shipped list entry the host falls under; else a seen call of the same app that named the same service the
    meeting row names (two fields the call judge and the timeline wrote, compared exactly)."""

    def __init__(self, *, meeting_rules: Callable[[], Any] | None, verdicts: dict[str, tuple[bool, str]],
                 seen_services: set[tuple[str, str]]) -> None:
        self._rules = meeting_rules
        self._verdicts = verdicts
        self._seen = seen_services

    def judge(self, app: str, host: str, meeting: str | None) -> tuple[bool, str | None, str | None]:
        """``(calling, service, named_by)``: whether the host is a calling site, its name when one is known, and who
        named it: ``verdict`` (the call evidence's service), ``call`` (the service a seen call of the app named, equal
        to the row's meeting label) or ``rule`` (the user's own ``[meetings] names`` entry). Never the host, and never
        the timeline's meeting label on its own (for a site row that label is often the host): a calling site with no
        name known gives ``(True, None, None)``. Only ``verdict`` and ``call`` names are product names fit for a log."""
        host = (host or "").casefold()
        if not host:
            return False, None, None
        rules = None
        if self._rules is not None:
            try:
                rules = self._rules()
            except Exception:  # noqa: BLE001
                rules = None
        if rules is not None:
            if rules.never(app, host):
                return False, None, None
            named = rules.match_url(f"https://{host}/")
            if named:   # match_url falls back to the site itself when the user gave no name
                from yuki.memory.timeline import site_of

                return (True, None, None) if named == site_of(host) else (True, named, "rule")
        found = [e for e in self._verdicts if _host_under(host, e)]
        if found:
            calling, service = self._verdicts[max(found, key=len)]
            if not calling:
                return False, None, None
            return (True, service, "verdict") if service else (True, None, None)
        if meeting and (app, " ".join(meeting.split()).casefold()) in self._seen:
            return True, meeting, "call"
        return False, None, None

    def service(self, app: str, host: str, meeting: str | None) -> str | None:
        """The name of a calling site (None: not one, or no name known)."""
        return self.judge(app, host, meeting)[1]


# ---------------------------------------------------------------------------
# The derived kinds
# ---------------------------------------------------------------------------


@dataclass
class _Cand:
    at: float
    end: float | None          # None: still open
    chain_end: float           # for chaining (an open row: its last sign of life)
    evidence: str
    source: str
    source_id: int
    service: str | None
    by_yuki: bool = False      # a timeline stretch begun while Yuki acted
    named_by: str | None = None   # who named the service: "call", "verdict", "rule" (CallingSites.judge)

    @property
    def seen(self) -> bool:
        return self.evidence in SEEN_EVIDENCE


_SOURCE_ORDER = {"calls": 0, "timeline": 1}


def cluster_joins(cands: Sequence[_Cand], gap_s: float = JOIN_GAP_S) -> list[list[_Cand]]:
    """Candidates of one app chained while each starts within ``gap_s`` of the chain's end so far."""
    out: list[list[_Cand]] = []
    end = float("-inf")
    for c in sorted(cands, key=lambda c: (c.at, _SOURCE_ORDER.get(c.source, 9), c.source_id)):
        if out and c.at <= end + gap_s:
            out[-1].append(c)
            end = max(end, c.chain_end)
        else:
            out.append([c])
            end = c.chain_end
    return out


def join_moment(app: str, group: Sequence[_Cand]) -> Moment:
    """One ``call_join`` from a chain of candidates: ``at`` and the source of the earliest; the evidence of the earliest
    seen candidate (else ``hold``); the first service a seen candidate named (else any). Yuki's (D18) when the earliest
    is a stretch begun while Yuki acted: the join happened inside one of Yuki's runs (``request_id`` then comes from the
    run windows, :func:`attribute`)."""
    first = min(group, key=lambda c: (c.at, _SOURCE_ORDER.get(c.source, 9), c.source_id))
    seen = sorted((c for c in group if c.seen), key=lambda c: c.at)
    named = [c for c in seen if c.service] or [c for c in sorted(group, key=lambda c: c.at) if c.service]
    ended = None if any(c.end is None for c in group) else max(c.end for c in group if c.end is not None)
    facts = {"calls": sum(1 for c in group if c.source == "calls"),
             "timeline": sum(1 for c in group if c.source == "timeline")}
    if seen:
        facts["seen_at"] = round(seen[0].at, 3)
    return Moment(kind="call_join", at=first.at, ended_at=ended, source=first.source, source_id=first.source_id,
                  app=app, service=named[0].service if named else None, evidence=seen[0].evidence if seen else "hold",
                  by="yuki" if first.by_yuki else "user", facts=facts,
                  log_service=bool(named) and named[0].named_by in ("call", "verdict"))


#: The columns the rules read, and nothing more (never a title, a path or another ciphertext): a ``calls`` row ...
_CALL_COLS = "id, updated_at, app, process, kind, detected_at, ended_at, hold_since, last_alive_at"
#: ... a timeline row of a page visit ...
_VISIT_COLS = ("id, started_at, ended_at, state, process, host, page_key, active_s, passive_s, withheld, meeting,"
               " by_yuki")
#: ... and a changed timeline row.
_TL_COLS = "updated_at, " + _VISIT_COLS


class Extractor:
    """The derived moments, incremental: one :meth:`refresh` reads each source past its cursor (in
    ``source_checkpoints``: ``moments.calls`` / ``.timeline`` / ``.messages``) and writes what changed.

    Every outside fact is a replaceable function, so it runs offline on a temp database or a copy."""

    def __init__(
        self, store: Any, *, config: Callable[[], MomentsConfig] | None = None,
        meeting_rules: Callable[[], Any] | None = None, calls_apps: Callable[[], Iterable[str]] | None = None,
        is_game: Callable[[str], bool] | None = None,
        page_entity: Callable[[str, str], tuple[str, str] | None] | None = None,
        runs_fn: Callable[[float, float], list[RunWindow]] | None = None,
        log: Callable[..., Any] | None = None, clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self._config = config or (lambda: MomentsConfig())
        self._meeting_rules = meeting_rules
        self._calls_apps = calls_apps or (lambda: ())
        self._is_game_fn = is_game
        self._is_game: Callable[[str], bool] | None = is_game
        self._judge_at = float("-inf")
        self.page_entity = page_entity
        self._runs_fn = runs_fn or (lambda lo, hi: skill_runs_between(store, lo, hi))
        self._log_fn = log
        self._clock = clock
        self.identity = AppIdentity(store)
        self._seen_apps: frozenset[str] = frozenset()
        self._seen_services: set[tuple[str, str]] = set()
        self._calls_apps_now: frozenset[str] = frozenset()
        self._sig: tuple | None = None
        self._sites: CallingSites | None = None
        #: (source -> {row id: updated_at}) of rows already taken inside the cursor overlap.
        self._done: dict[str, dict[int, float]] = {"calls": {}, "timeline": {}}
        self._quiet = False
        self.counters: Counter = Counter()

    # -- plumbing ----------------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _cutoff(self, now: float) -> float:
        return now - self._config().keep_days * 86400.0

    def _cursor(self, name: str, default: float = 0.0) -> float:
        try:
            value = self.store.moment_cursor(name)
            return float(value) if value is not None else default
        except (TypeError, ValueError):
            return default

    def _context(self, now: float, *, force: bool = False) -> set[str]:
        """Refresh what the rules read (the seen-call apps and services, the identity map, the game judge, the user's
        rules); returns the apps whose whole join history must be recomputed because one of those changed."""
        if self._is_game_fn is None and (self._is_game is None or force or now - self._judge_at >= 3600.0):
            self._is_game, self._judge_at = game_judge(self.store), now   # a newly learned game counts within an hour
        self.identity.refresh(now, force=force)
        seen_apps, seen_services = seen_call_record(self.store, is_game=self._is_game, identity=self.identity)
        try:
            calls_apps = frozenset(k for k in (app_key(a) for a in self._calls_apps()) if k)
        except Exception:  # noqa: BLE001
            calls_apps = frozenset()
        verdicts = persisted_site_verdicts(self.store)
        rules = None
        if self._meeting_rules is not None:
            try:
                rules = self._meeting_rules()
            except Exception:  # noqa: BLE001
                rules = None
        import hashlib

        rest = hashlib.sha256(repr((sorted(verdicts.items()), rules)).encode("utf-8")).hexdigest()[:16]
        sig = (frozenset(seen_apps), frozenset(seen_services), calls_apps, rest)
        seen_apps, seen_services = set(seen_apps), set(seen_services)
        old_sig = self._sig
        if old_sig is None and not force:   # a new process: what the rules read when the last one wrote
            old_sig = self._stored_context()
        changed: set[str] = set()
        if old_sig is not None and sig != old_sig:
            old_apps, old_services, old_calls = old_sig[0], old_sig[1], old_sig[2]
            changed |= set(seen_apps) ^ set(old_apps)
            changed |= {a for a, _s in set(seen_services) ^ set(old_services)}
            changed |= set(calls_apps) ^ set(old_calls)
            if sig[3] != old_sig[3]:   # the user's [meetings] word or the site verdicts: every app on a page
                changed |= {r[0] for r in self._meeting_apps()}
        if sig != self._sig:
            try:
                self.store.set_moment_cursor("context", json.dumps(
                    {"apps": sorted(sig[0]), "services": sorted(sig[1]), "calls_apps": sorted(sig[2]), "rest": rest}))
            except Exception:  # noqa: BLE001
                pass
        self._sig = sig
        self._seen_apps, self._seen_services, self._calls_apps_now = frozenset(seen_apps), seen_services, calls_apps
        self._sites = CallingSites(meeting_rules=self._meeting_rules, verdicts=verdicts, seen_services=seen_services)
        return changed

    def _stored_context(self) -> tuple | None:
        try:
            data = json.loads(self.store.moment_cursor("context") or "null")
            return (frozenset(data["apps"]), frozenset(tuple(x) for x in data["services"]),
                    frozenset(data["calls_apps"]), str(data["rest"]))
        except (TypeError, ValueError, KeyError):
            return None

    def _meeting_apps(self) -> list[tuple[str]]:
        try:
            return [(app_key(r[0]) or "",) for r in self.store._query(
                "SELECT DISTINCT process FROM timeline WHERE meeting IS NOT NULL AND process IS NOT NULL")]
        except Exception:  # noqa: BLE001
            return []

    #: Rows read per query. A rebuild walks its window chunk by chunk (the store's lock is held per chunk only), so its
    #: memory is bounded by the chunk, never by the length of the history.
    READ_CHUNK = 1000

    def _changed(self, source: str, floor: float, now: float, *,
                 full: bool = False) -> Iterator[tuple[list[Any], float]]:
        """Rows of ``calls`` / ``timeline`` written past the cursor (with the overlap; ``full``: from the start, the
        stored cursor untouched) that end at or after ``floor``, not yet taken.

        Yields ``(rows, top)`` per chunk of :attr:`READ_CHUNK` rows in ``(updated_at, id)`` order (``top``: the cursor
        top so far). Only the columns the rules read (:data:`_CALL_COLS`, :data:`_TL_COLS`): never a title or a path."""
        if source == "calls":
            cols, end, end_args = _CALL_COLS, "coalesce(ended_at, last_alive_at, ?)", (now,)
        else:   # "+": the scan follows timeline(updated_at) (its order), never timeline(ended_at)
            cols, end, end_args = _TL_COLS, "+ended_at", ()
        sql = (f"SELECT {cols} FROM {source} WHERE updated_at >= ? AND (updated_at > ? OR id > ?) AND {end} >= ?"
               " ORDER BY updated_at, id LIMIT ?")
        if full:   # every row up to the newest is read (or ends before the window): the newest is the cursor top
            cursor = 0.0
            top = float(self.store._query(f"SELECT max(updated_at) FROM {source}")[0][0] or 0.0)
        else:
            cursor = top = self._cursor(source)
        done = self._done[source]
        u, last_id = cursor - CURSOR_OVERLAP_S, 2**62   # the first chunk: updated_at > u
        while True:
            rows = self.store._query(sql, (u, u, last_id, *end_args, floor, self.READ_CHUNK))
            if not rows:
                return
            fresh = []
            for r in rows:
                ru = float(r["updated_at"] or 0.0)
                top = max(top, ru)
                if done.get(int(r["id"])) == ru:
                    continue
                done[int(r["id"])] = ru
                fresh.append(r)
            u, last_id = float(rows[-1]["updated_at"] or 0.0), int(rows[-1]["id"])
            low = top - CURSOR_OVERLAP_S   # only rows inside the next pass's overlap are remembered
            for k in [k for k, v in done.items() if v < low]:
                del done[k]
            yield fresh, top
            if len(rows) < self.READ_CHUNK:
                return

    # -- refresh -----------------------------------------------------------------

    def refresh(self, now: float | None = None, *, full: bool = False, since: float | None = None) -> dict[str, int]:
        """Read every source past its cursor and write the derived moments that changed; returns counts. On an error
        nothing read in this pass counts as taken (the next pass reads it again) and the stored cursors stay.

        ``full`` (a rebuild): every source from the start of the keep window (or ``since``, when later), chunk by
        chunk; the stored cursors move only when the whole pass succeeded."""
        done = {k: dict(v) for k, v in self._done.items()}
        sig = self._sig
        try:
            return self._refresh(now, full=full, since=since)
        except BaseException:
            self._done, self._sig = done, sig
            raise

    def _refresh(self, now: float | None, *, full: bool, since: float | None = None) -> dict[str, int]:
        now = self._clock() if now is None else float(now)
        cutoff = self._cutoff(now)
        floor = max(cutoff, float(since)) if (full and since is not None) else cutoff   # rows ending before: unread
        counts: Counter = Counter()
        rebuild_apps = self._context(now, force=full)
        windows: dict[str, list[tuple[float, float]]] = defaultdict(list)
        calls_top = None
        for rows, calls_top in self._changed("calls", floor, now, full=full):
            for r in rows:
                counts["calls_rows"] += 1
                at = call_at(float(r["detected_at"]), r["hold_since"])
                end = r["ended_at"] if r["ended_at"] is not None else (r["last_alive_at"] or now)
                if (r["kind"] or "call") == "game":
                    counts["game_voice"] += self._game_voice(r, now)
                    continue
                app = self.identity.key(r["process"], r["app"])
                if app and float(end) >= cutoff:
                    windows[app].append((at - JOIN_GAP_S, float(end) + JOIN_GAP_S))
        tl_top = None
        visits_before: dict[int, tuple] = {}
        hosts_before: dict[int, tuple] = {}
        counts["page"] += 0
        for rows, tl_top in self._changed("timeline", floor, now, full=full):
            pages: list[Any] = []
            for r in rows:
                counts["timeline_rows"] += 1
                if r["meeting"] and r["process"]:
                    windows[app_key(r["process"]) or ""].append((float(r["started_at"]) - JOIN_GAP_S,
                                                                 float(r["ended_at"]) + JOIN_GAP_S))
                elif r["host"] and r["state"] in ("present", "away"):
                    pages.append(r)
            written, visits_before = self._pages(pages, now, skip=visits_before)   # this chunk's pages, then gone
            counts["page"] += written
            written, hosts_before = self._host_pages(pages, now, skip=hosts_before)   # RULE_VERSION 3
            counts["page"] += written
            counts["page_host"] += written
            for app in list(windows):
                windows[app] = _merge_spans(windows[app])
        for app in rebuild_apps:
            if app:
                windows[app] = [(cutoff, now + JOIN_GAP_S)]
        for app, spans in windows.items():
            if not app:
                continue
            for lo, hi in _merge_spans(spans):
                written, deleted = self._rebuild_joins(app, max(lo, cutoff), hi, now)
                counts["call_join"] += written
                counts["call_join_deleted"] += deleted
        counts["message_sent"] += self._messages(now, cutoff, full=full)
        for name, top in (("calls", calls_top), ("timeline", tl_top)):
            if top is not None and (full or top > self._cursor(name)):
                self.store.set_moment_cursor(name, repr(top))
        self.counters.update(counts)
        return dict(counts)

    def rebuild(self, now: float | None = None, *, since: float | None = None) -> dict[str, int]:
        """Recompute every derived moment from the sources (those with ``at >= since``, default the keep window).

        The sources are read from ``since`` (or the keep window's start) on, chunk by chunk. The stored cursors move
        only when it succeeded; after a failure the rule checkpoint stays old, so the hub rebuilds again."""
        now = self._clock() if now is None else float(now)
        since = self._cutoff(now) if since is None else float(since)
        self._done = {"calls": {}, "timeline": {}}
        self._sig = None
        self._quiet = True   # a rebuild logs its counts once, not each moment
        try:
            removed = self.store.clear_moments(sources=DERIVED_SOURCES, kinds=DERIVED_KINDS, since=since)
            counts = self.refresh(now, full=True, since=since)
        finally:
            self._quiet = False
        self.store.set_moment_cursor("rule", str(RULE_VERSION))
        counts["removed"] = removed
        return counts

    # -- call joins and game voice -------------------------------------------------

    def _join_candidates(self, app: str, lo: float, hi: float, now: float) -> list[_Cand]:
        is_game = self._is_game or (lambda p: False)
        out: list[_Cand] = []
        if is_game(app):
            return out
        for r in self.store._query(
                "SELECT id, app, process, service, via, kind, detected_at, ended_at, hold_since, last_alive_at"
                " FROM calls WHERE kind = 'call' AND detected_at < ? AND coalesce(ended_at, last_alive_at, ?) > ?",
                (hi, now, lo - JOIN_GAP_S)):
            if self.identity.key(r["process"], r["app"]) != app:
                continue
            ok, evidence = is_call_join(r, app=app, seen_apps=self._seen_apps, is_game=is_game,
                                        calls_apps=self._calls_apps_now)
            if not ok:
                continue
            at = call_at(float(r["detected_at"]), r["hold_since"])
            end = float(r["ended_at"]) if r["ended_at"] is not None else None
            chain = end if end is not None else float(r["last_alive_at"] or now)
            out.append(_Cand(at=at, end=end, chain_end=max(chain, at), evidence=evidence or "hold", source="calls",
                             source_id=int(r["id"]), service=r["service"] or None,
                             named_by="call" if r["service"] else None))
        qualifies = app in self._seen_apps or app in self._calls_apps_now
        for r in self.store._query(
                "SELECT id, started_at, ended_at, state, process, host, meeting, mic_s, by_yuki FROM timeline"
                " WHERE meeting IS NOT NULL AND process = ? AND started_at < ? AND ended_at > ?"
                " ORDER BY started_at, id", (app, hi, lo)):
            if r["state"] != "present":
                continue
            if r["host"]:
                calling, service, named_by = self._sites.judge(app, r["host"], r["meeting"]) \
                    if self._sites is not None else (False, None, None)
                if not calling:
                    continue
                evidence = "site"
            else:
                if not (qualifies and (r["mic_s"] or 0) > 0):
                    continue
                service, named_by, evidence = None, None, "hold"
            # a stretch begun while Yuki acted (the timeline's acting marker) stays a candidate, marked Yuki's (D18)
            out.append(_Cand(at=float(r["started_at"]), end=float(r["ended_at"]), chain_end=float(r["ended_at"]),
                             evidence=evidence, source="timeline", source_id=int(r["id"]), service=service,
                             by_yuki=bool(r["by_yuki"]), named_by=named_by))
        return out

    def _rebuild_joins(self, app: str, lo: float, hi: float, now: float) -> tuple[int, int]:
        """Recompute ``app``'s joins in a window wide enough to hold every chain it touches; returns (written,
        deleted)."""
        groups: list[list[_Cand]] = []
        for _ in range(6):
            groups = cluster_joins(self._join_candidates(app, lo, hi, now))
            if not groups:
                break
            g_lo = min(c.at for g in groups for c in g)
            g_hi = max(c.chain_end for g in groups for c in g)
            if g_lo - JOIN_GAP_S >= lo and g_hi + JOIN_GAP_S <= hi:
                break
            lo, hi = min(lo, g_lo - JOIN_GAP_S), max(hi, g_hi + JOIN_GAP_S)
        moments = [join_moment(app, g) for g in groups]
        if moments:
            runs = self._runs(min(m.at for m in moments), max(m.at for m in moments) + ATTRIBUTION_LAG_S["call_join"])
            moments = [attribute(m, runs, now=now) for m in moments]
        existing = self.store.moments_between(lo, hi, kinds=["call_join"], app=app, sources=["calls", "timeline"])
        old = {(e["source"], e["source_id"]): e for e in existing}
        keep = {(m.source, m.source_id) for m in moments}
        stale = [e["id"] for k, e in old.items() if k not in keep]
        changed = [m for m in moments if _differs(old.get((m.source, m.source_id)), m)]
        if changed:
            self.store.save_moments([m.row(self.store.cipher) for m in changed])
            for m in changed:
                if (m.source, m.source_id) not in old:
                    self._log_moment(m, now)
        if stale:
            self.store.delete_moments(stale)
        return len(changed), len(stale)

    def _game_voice(self, r: Any, now: float) -> int:
        is_game = self._is_game or (lambda p: False)
        if not is_game_voice(r, is_game=is_game):
            gone = [e["id"] for e in self.store.moments_between(kinds=["game_voice"], sources=["calls"])
                    if e["source_id"] == int(r["id"])]
            if gone:
                self.store.delete_moments(gone)
            return 0
        m = Moment(kind="game_voice", at=call_at(float(r["detected_at"]), r["hold_since"]), ended_at=r["ended_at"],
                   source="calls", source_id=int(r["id"]), app=app_key(r["process"]) or None, evidence="game_fact")
        self.store.save_moments([m.row(self.store.cipher)])
        return 1

    def _runs(self, lo: float, hi: float) -> list[RunWindow]:
        try:
            return list(self._runs_fn(lo, hi))
        except Exception:  # noqa: BLE001
            return []

    # -- pages ---------------------------------------------------------------------

    #: A visit's rows: the columns the page rule reads (the path is read per visit, only when a template is asked).
    _ROW = "SELECT " + _VISIT_COLS + " FROM timeline"

    @staticmethod
    def _page_of(r: Any) -> Any:
        """A page visit's identity: its page key (one page, one app)."""
        return r["page_key"]

    @staticmethod
    def _host_of(r: Any) -> Any:
        """A host-scope visit's identity (RULE_VERSION 3): one browser on one registrable domain; a row of a meeting,
        a withheld page or one begun while Yuki acted, or with no host, belongs to none (it breaks the stretch)."""
        if not r["host"] or r["meeting"] or r["withheld"] or r["by_yuki"]:
            return None
        host = str(r["host"]).casefold()
        if (host[4:] if host.startswith("www.") else host) in templated_hosts():
            return None    # a site file's host: its pages are template pages (unchanged)
        return (r["process"], registrable_domain(host))

    def _step(self, r: Any, *, back: bool, same: Callable[[Any], Any] | None = None) -> Any | None:
        """The present/away row (any app) right before (``back``) or after ``r`` in time, when it goes on with
        ``r``'s visit (the same ``same(row)`` identity, default the page key; touching); else None."""
        same = same or self._page_of
        start = float(r["started_at"])
        want = same(r)
        if want is None:
            return None
        if back:
            got = self.store._query(self._ROW + " WHERE started_at < ? AND state IN ('present', 'away')"
                                    " ORDER BY started_at DESC, id DESC LIMIT 1", (start,))
            if got and same(got[0]) == want and start - float(got[0]["ended_at"]) <= VISIT_GAP_S:
                return got[0]
            return None
        got = self.store._query(self._ROW + " WHERE started_at >= ? AND (started_at > ? OR id > ?)"   # an index range
                                " AND state IN ('present', 'away') ORDER BY started_at, id LIMIT 1",
                                (start, start, int(r["id"])))
        if got and same(got[0]) == want and float(got[0]["started_at"]) - float(r["ended_at"]) <= VISIT_GAP_S:
            return got[0]
        return None

    def _extend(self, rows: Sequence[Any], same: Callable[[Any], Any] | None = None) -> list[Any]:
        """A run of rows of one visit walked back past its first row and on past its last (at most 500 each way)."""
        before: list[Any] = []
        cur = rows[0]
        for _ in range(500):
            cur = self._step(cur, back=True, same=same)
            if cur is None:
                break
            before.append(cur)
        out = before[::-1] + list(rows)
        for _ in range(500):
            nxt = self._step(out[-1], back=False, same=same)
            if nxt is None:
                break
            out.append(nxt)
        return out

    def _visit(self, r: Any, same: Callable[[Any], Any] | None = None) -> list[Any]:
        """The rows of the visit ``r`` is in: neighbours (in time, any app) of the same identity, touching."""
        return self._extend([r], same)

    #: Above this many changed page rows (a rebuild's chunk), visits are grouped in one ordered pass, not row by row.
    SEQUENTIAL_PAGES = 64

    def _visits_span(self, rows: Sequence[Any], same: Callable[[Any], Any] | None = None) -> list[list[Any]]:
        """The visits touching ``rows``, grouped in one ordered pass over their time span; the first visit is walked
        back past the span's start and the last one on past its end, so a visit cut by a chunk's edge is whole."""
        same = same or self._page_of
        lo = min(float(r["started_at"]) for r in rows) - VISIT_GAP_S
        hi = max(float(r["ended_at"]) for r in rows) + VISIT_GAP_S
        # the rows that start inside the span (an index range); a visit begun before it is the first group, walked back
        span = self.store._query(self._ROW + " WHERE state IN ('present', 'away') AND started_at >= ?"
                                 " AND started_at <= ? ORDER BY started_at, id", (lo, hi))
        groups: list[list[Any]] = []
        for r in span:
            last = groups[-1][-1] if groups else None
            ident = same(r)
            if last is not None and ident is not None and same(last) == ident and \
                    float(r["started_at"]) - float(last["ended_at"]) <= VISIT_GAP_S:
                groups[-1].append(r)
            else:
                groups.append([r])
        if groups:
            groups[0] = self._extend(groups[0], same)
            if len(groups) > 1:
                groups[-1] = self._extend(groups[-1], same)
        wanted = {int(r["id"]) for r in rows}
        return [g for g in groups if any(int(x["id"]) in wanted for x in g)]

    def _path(self, row_id: int) -> str | None:
        """One timeline row's page path, decrypted in-process only (for a URL template; never kept or logged)."""
        got = self.store._query("SELECT path_ciphertext FROM timeline WHERE id = ?", (int(row_id),))
        return self.store._dec(got[0][0]) if got and got[0][0] else None

    def _pages(self, rows: Sequence[Any], now: float, *, skip: dict[int, tuple] | None = None
               ) -> tuple[int, dict[int, tuple]]:
        """Page moments of the visits touching ``rows``; returns (written, {visit's first row id: its shape}).
        ``skip``: the previous chunk's shapes in the same pass (a visit across a chunk's edge, unchanged since, is
        written once)."""
        cfg = self._config()
        rows = [r for r in rows if r["host"] and not r["meeting"] and not r["withheld"]]
        if not rows:
            return 0, {}
        if len(rows) > self.SEQUENTIAL_PAGES:
            visits = self._visits_span(rows)
        else:
            visits = [self._visit(r) for r in rows]
        skip = skip or {}
        done: dict[int, tuple] = {}
        out: list[Moment] = []
        for visit in visits:
            first = visit[0]
            if int(first["id"]) in done:
                continue
            dwell = sum(float(v["active_s"] or 0.0) + float(v["passive_s"] or 0.0) for v in visit)
            shape = (len(visit), int(visit[-1]["id"]), float(visit[-1]["ended_at"]), dwell)
            done[int(first["id"])] = shape
            if skip.get(int(first["id"])) == shape:
                continue
            if first["meeting"] or first["withheld"] or not first["host"]:
                continue
            template = None
            if self.page_entity is not None:
                try:
                    path = self._path(int(first["id"])) or "/"
                    template = self.page_entity(str(first["host"]), path)   # path read in-process only
                except Exception:  # noqa: BLE001
                    template = None
                    self.counters["template_errors"] += 1
            if template is None and dwell < cfg.page_dwell_s:
                continue
            host = str(first["host"]).casefold()
            facts: dict[str, Any] = {"host": host[4:] if host.startswith("www.") else host, "rows": len(visit)}
            m = Moment(kind="page", at=float(first["started_at"]), ended_at=float(visit[-1]["ended_at"]),
                       source="timeline", source_id=int(first["id"]), app=app_key(first["process"]) or None,
                       value=round(dwell, 1), evidence="timeline", by="yuki" if first["by_yuki"] else "user")
            if template is not None:
                tid, value = template
                facts["template"] = str(tid)
                m.service = str(tid).split(".", 1)[0] or None
                m.entity, m.keep_entity = str(value), True
            else:
                m.entity_digest = first["page_key"]
            m.facts = facts
            out.append(m)
        if out:
            runs = self._runs(min(m.at for m in out), max(m.at for m in out) + ATTRIBUTION_LAG_S["page"])
            out = [attribute(m, runs, now=now) for m in out]
            self.store.save_moments([m.row(self.store.cipher) for m in out])
        return len(out), done

    def _host_pages(self, rows: Sequence[Any], now: float, *, skip: dict[int, tuple] | None = None
                    ) -> tuple[int, dict[int, tuple]]:
        """Host-scope page moments (RULE_VERSION 3; phase L design §3.3, H3) of the visits touching ``rows``: one
        browser's touching rows on one registrable domain no site file covers, whose dwell reaches ``[moments]
        host_dwell_s``. Entity: the domain (encrypted, ``entity_key`` across browsers); ``facts.host`` the domain and
        ``facts.top_host`` the host the visit dwelt on most (hosts are kept in clear on page moments, as today); no
        path, no title. Source ``timeline_host``. Returns (written, {visit's first row id: its shape})."""
        cfg = self._config()
        rows = [r for r in rows if self._host_of(r) is not None]
        if not rows:
            return 0, {}
        if len(rows) > self.SEQUENTIAL_PAGES:
            visits = self._visits_span(rows, self._host_of)
        else:
            visits = [self._visit(r, self._host_of) for r in rows]
        skip = skip or {}
        done: dict[int, tuple] = {}
        out: list[Moment] = []
        for visit in visits:
            first = visit[0]
            if int(first["id"]) in done or self._host_of(first) is None:
                continue
            dwell = sum(float(v["active_s"] or 0.0) + float(v["passive_s"] or 0.0) for v in visit)
            shape = (len(visit), int(visit[-1]["id"]), float(visit[-1]["ended_at"]), dwell)
            done[int(first["id"])] = shape
            if skip.get(int(first["id"])) == shape or dwell < cfg.host_dwell_s:
                continue
            domain = registrable_domain(first["host"])
            if not domain:
                continue
            by_host: Counter = Counter()
            for v in visit:
                h = str(v["host"] or "").casefold()
                by_host[h[4:] if h.startswith("www.") else h] += float(v["active_s"] or 0.0) + float(v["passive_s"] or 0.0)
            top = by_host.most_common(1)[0][0] if by_host else domain
            out.append(Moment(kind="page", at=float(first["started_at"]), ended_at=float(visit[-1]["ended_at"]),
                              source="timeline_host", source_id=int(first["id"]), app=app_key(first["process"]) or None,
                              value=round(dwell, 1), evidence="timeline", entity=domain, keep_entity=True,
                              facts={"host": domain, "top_host": top, "scope": "host", "rows": len(visit)}))
        if out:
            runs = self._runs(min(m.at for m in out), max(m.at for m in out) + ATTRIBUTION_LAG_S["page"])
            out = [attribute(m, runs, now=now) for m in out]
            self.store.save_moments([m.row(self.store.cipher) for m in out])
        return len(out), done

    # -- messages sent ---------------------------------------------------------------

    def _messages(self, now: float, cutoff: float, *, full: bool = False) -> int:
        """``message_sent`` from the user's new messages past the cursor (``full``: from the first), chunk by chunk."""
        cursor = 0 if full else int(self._cursor("messages"))
        top = self.store._query("SELECT max(id) FROM messages")[0][0]
        if top is None or int(top) <= cursor:
            return 0
        written, last, contacts = 0, cursor, None
        while True:
            rows = self.store._query(
                "SELECT m.id, m.thread_id, m.first_seen, m.at, t.process, t.app FROM messages m"
                " JOIN threads t ON t.id = m.thread_id WHERE m.id > ? AND m.id <= ? AND m.is_me = 1"
                " AND m.status = 'new' AND m.first_seen >= ? ORDER BY m.id LIMIT ?",
                (last, int(top), cutoff, self.READ_CHUNK))
            if not rows:
                break
            last = int(rows[-1]["id"])
            if contacts is None:
                contacts = self._contacts()
            written += self._message_moments(rows, contacts, now)
            if len(rows) < self.READ_CHUNK:
                break
        self.store.set_moment_cursor("messages", str(int(top)))
        return written

    def _message_moments(self, rows: Sequence[Any], contacts: dict[int, int], now: float) -> int:
        out: list[Moment] = []
        for r in rows:
            app = app_key(r["process"]) or app_key(r["app"]) or None
            facts: dict[str, Any] = {"thread_id": int(r["thread_id"])}
            if r["thread_id"] in contacts:
                facts["contact_id"] = contacts[r["thread_id"]]
            if r["at"] is not None:
                facts["label_lag_s"] = round(float(r["first_seen"]) - float(r["at"]), 1)
            out.append(Moment(kind="message_sent", at=float(r["first_seen"]), source="messages",
                              source_id=int(r["id"]), app=app, entity=f"thread:{int(r['thread_id'])}",
                              evidence="capture", facts=facts))
        if out:
            runs = self._runs(min(m.at for m in out), max(m.at for m in out) + ATTRIBUTION_LAG_S["message_sent"])
            out = [attribute(m, runs, now=now) for m in out]
            self.store.save_moments([m.row(self.store.cipher) for m in out])
            for m in out:
                self._log_moment(m, now)
        return len(out)

    def _contacts(self) -> dict[int, int]:
        out: dict[int, int] = {}
        try:
            rows = self.store._query("SELECT contact_id, thread_ids FROM contact_routes WHERE ended_at IS NULL"
                                     " AND contact_id IS NOT NULL")
        except Exception:  # noqa: BLE001
            return out
        for r in rows:
            try:
                ids = json.loads(r["thread_ids"] or "[]")
            except ValueError:
                continue
            for t in ids if isinstance(ids, list) else []:
                try:
                    out.setdefault(int(t), int(r["contact_id"]))
                except (TypeError, ValueError):
                    continue
        return out

    def _log_moment(self, m: Moment, now: float) -> None:
        if not self._quiet:
            _log_moment(self._log, m, now)


def _merge_spans(spans: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    out: list[list[float]] = []
    for lo, hi in sorted(spans):
        if out and lo <= out[-1][1]:
            out[-1][1] = max(out[-1][1], hi)
        else:
            out.append([lo, hi])
    return [(a, b) for a, b in out]


def _differs(old: dict[str, Any] | None, m: Moment) -> bool:
    if old is None:
        return True
    facts = m.facts or {}
    return (abs(float(old["at"]) - m.at) > 1e-6 or old.get("ended_at") != m.ended_at or old.get("evidence") != m.evidence
            or old.get("service") != m.service or old.get("by") != m.by or old.get("request_id") != m.request_id
            or (old.get("facts") or {}) != facts)


_CODE_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-")


def _code_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """The extras a live source passed, content-free: numbers, booleans and code words (lower-case ASCII letters,
    digits, ``_`` and ``-``, at most 40, starting with a letter: ``covered``, ``english``, ``warm``); anything else
    (a name, a host, a link) is dropped. Callers pass Yuki's own code words only."""
    out: dict[str, Any] = {}
    for k, v in facts.items():
        if not isinstance(k, str) or not k or set(k) - _CODE_CHARS:
            continue
        if isinstance(v, bool) or (isinstance(v, (int, float)) and v == v):
            out[k] = v
        elif isinstance(v, str) and 0 < len(v) <= 40 and v[0].isalpha() and not set(v) - _CODE_CHARS:
            out[k] = v
    return out


def _media_key(session: dict[str, Any]) -> int:
    """A media session's key among the open spans: a RAM-only hash (salted per process) of the app id and title the
    timeline's read reported, so a stop finds its start whatever app the id resolved to at either read."""
    return hash((str(session.get("app_id") or ""), str(session.get("title") or "")))


def _log_moment(log: Callable[..., Any], m: Moment, now: float) -> None:
    """One content-free ``moment`` line (kinds, app keys, code words, a 12-hex digest; never the entity; ``service``
    only when a call or a verdict named it, never a host or the user's own label)."""
    fields: dict[str, Any] = dict(kind=m.kind, app=m.app, service=m.service if m.log_service else None, by=m.by,
                                  request_id=m.request_id,
                                  evidence=m.evidence, means=m.means, lag_s=round(max(0.0, now - m.at), 1),
                                  entity_digest=log_digest(m.entity_digest))
    if m.kind == "volume_change":
        fields.update(value_from=m.value_from, value=m.value)
    try:
        log("moment", **{k: v for k, v in fields.items() if v is not None})
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Live kinds: the writer and the hub
# ---------------------------------------------------------------------------


class MomentWriter:
    """Live moments from any thread: :meth:`put` queues (never blocks on the database), :meth:`flush` writes the queue
    in one transaction (the service's 1 s beat). A live moment's ``source_id`` is its event time in ms, unique per
    writer, so its end can be written to the same row later."""

    MAX_QUEUE = 5000

    def __init__(self, store: Any) -> None:
        self.store = store
        self._q: deque[Moment] = deque()
        self._lock = threading.Lock()
        self._last_key = 0
        self.dropped = 0
        self.written = 0

    def key(self, at: float) -> int:
        with self._lock:
            k = int(at * 1000)
            if k <= self._last_key:
                k = self._last_key + 1
            self._last_key = k
            return k

    def put(self, m: Moment) -> None:
        with self._lock:
            if len(self._q) >= self.MAX_QUEUE:
                self._q.popleft()
                self.dropped += 1
            self._q.append(m)

    def pending(self) -> int:
        return len(self._q)

    def flush(self) -> list[Moment]:
        with self._lock:
            if not self._q:
                return []
            batch, self._q = list(self._q), deque()
        now = time.time()
        for m in batch:
            if m.seen_at is None:
                m.seen_at = now
        try:
            self.store.save_moments([m.row(self.store.cipher) for m in batch])
        except Exception:
            with self._lock:   # tried again on the next beat (a busy database), never more than the queue holds
                room = max(0, self.MAX_QUEUE - len(self._q))
                self.dropped += max(0, len(batch) - room)
                self._q.extendleft(reversed(batch[:room]))
            raise
        self.written += len(batch)
        return batch


class MomentHub:
    """Owns the writer and the extractor in the memory service (D.1: build, start, beat, stats, stop).

    Inputs (each cheap and never raising into its caller): :meth:`on_media_change` and :meth:`on_front_change` (the
    timeline's thread), :meth:`site` (the watcher's reads), :meth:`volume_change` and :meth:`media_context` (the volume
    watcher and the Spotify window reader), :meth:`nudge` (the call worker, after a call starts or ends). Other parts
    follow the timeline's media and front changes through :meth:`subscribe_media` / :meth:`subscribe_front` (the
    Spotify reader: "its music started", "it left the front") without a hook of their own."""

    #: Open live spans kept to write their end (media playing, a calling site in front); the oldest go first.
    MAX_OPEN = 256

    def __init__(
        self, store: Any, *, privacy: Any = None, log: Callable[..., Any] | None = None,
        db_path: str | Path | None = None, calling_service: Callable[[str], str | None] | None = None,
        is_game: Callable[[str], bool] | None = None,
        page_entity: Callable[[str, str], tuple[str, str] | None] | None = None,
        runs_fn: Callable[[float, float], list[RunWindow]] | None = None,
        acting_fn: Callable[[], Any] | None = None, config: Callable[[], MomentsConfig] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        from yuki.memory.timeline import MeetingRules, PrivacySection

        self.store = store
        self._log_fn = log
        self._clock = clock
        if config is None:
            section = PrivacySection(privacy, "moments", MomentsConfig.from_dict)
            config = section.get
        self.config = config
        meetings = PrivacySection(privacy, "meetings", MeetingRules.from_dict)
        calls_section = None
        try:
            from yuki.memory.calls import CallConfig

            calls_section = PrivacySection(privacy, "calls", CallConfig.from_dict)
        except Exception:  # noqa: BLE001
            calls_section = None
        self._runs_fn = runs_fn or (lambda lo, hi: skill_runs_between(store, lo, hi))
        self.extractor = Extractor(
            store, config=self.config, meeting_rules=meetings.get,
            calls_apps=(lambda: calls_section.get().apps) if calls_section is not None else None,
            is_game=is_game, page_entity=page_entity if page_entity is not None else default_page_entity(),
            runs_fn=self._runs_fn, log=log, clock=clock)
        self.writer = MomentWriter(store)
        self.calling_service = calling_service
        #: ``tab_known(hwnd) -> bool``: whether the calling site's tab in that window was identified by its last read
        #: (``yuki.memory.callevidence.SiteTabs.known``, set by the service; it runs before :meth:`site` on the same
        #: sighting). None: not known (``facts.tab_known`` left out).
        self.tab_known_fn: Callable[[int], bool] | None = None
        self._db_path = db_path
        if acting_fn is None and db_path is not None:
            from yuki.memory import acting as _acting

            acting_fn = lambda: _acting.current(db_path)   # noqa: E731
        self._acting_fn = acting_fn
        self._lock = threading.Lock()
        #: Media playing: a RAM-only hash of the session's (app id, title) -> (source_id, app, the start's facts). Keyed
        #: by what the timeline's media read reports, never by the app it resolves (that can change between reads).
        self._media_open: dict[int, tuple[int, str | None, dict[str, Any]]] = {}
        self._site_open: dict[int, tuple[str, int, dict[str, Any]]] = {}     # hwnd -> (host, source_id, facts)
        self._retry_at = 0.0
        self._front_subs: list[Callable[..., Any]] = []
        self._media_subs: list[Callable[..., Any]] = []
        self._nudged = threading.Event()
        self._next_refresh = 0.0
        self._next_prune = 0.0
        self._backfill: threading.Thread | None = None
        self._backfill_state: str | None = None
        self.cpu_s = 0.0
        self.beats = 0
        self.refreshes = 0
        self.refresh_ms: deque[float] = deque(maxlen=200)
        self.errors = 0
        self.by_kind: Counter = Counter()
        self.inputs: Counter = Counter()
        #: Phase L: the skill learner's incremental pass (``yuki.skills.learn.incremental``) and when it may run
        #: (``yuki.skills.learn.quiet``: idle, no game in front, no call); replaceable (``None``: the defaults).
        self.learn_fn: Callable[[float], Any] | None = None
        self.learn_quiet_fn: Callable[[float], str | None] | None = None
        self._learn_due = False
        self._learn_next = 0.0
        self._learn_thread: threading.Thread | None = None
        self.learn_runs = 0

    # -- plumbing -----------------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _on(self) -> MomentsConfig | None:
        try:
            cfg = self.config()
        except Exception:  # noqa: BLE001
            return None
        return cfg if cfg.enabled else None

    def _acting(self) -> tuple[bool, str | None]:
        if self._acting_fn is None:
            return False, None
        try:
            a = self._acting_fn()
        except Exception:  # noqa: BLE001
            return False, None
        return (a is not None), (getattr(a, "request_id", None) if a is not None else None)

    def subscribe_front(self, fn: Callable[..., Any]) -> None:
        """``fn(prev_key, new_key, **facts)`` on every front change the timeline reports (its thread; must not block)."""
        self._front_subs.append(fn)

    def subscribe_media(self, fn: Callable[..., Any]) -> None:
        """``fn(started, stopped, **facts)`` on every media change (the timeline's thread; titles in RAM only: a
        subscriber must never log or keep them)."""
        self._media_subs.append(fn)

    # -- the service's beat ----------------------------------------------------------

    def start(self, now: float | None = None) -> None:
        """End the live spans a previous run left open (it stopped without :meth:`stop`); rebuild the derived moments
        once, off the loop, when the rules changed (or on the first start)."""
        if self._on() is None:
            return
        self._end_left_open()
        try:
            rule = self.store.moment_cursor("rule")
        except Exception:  # noqa: BLE001
            rule = None
        if rule == str(RULE_VERSION):
            return
        self._start_rebuild()

    def _end_left_open(self) -> None:
        """Live spans still open from a run that never stopped cleanly end when that run was last seen alive (its last
        present or away stretch's end), marked ``ended=restart``."""
        try:
            last = self.store._query("SELECT ended_at FROM timeline WHERE state IN ('present', 'away')"
                                     " ORDER BY ended_at DESC LIMIT 1")
            n = self.store.end_open_moments(float(last[0][0]) if last else 0.0, sources=("media", "site"),
                                            kinds=("media_play", "site_front"), reason="restart")
            if n:
                self._log("moment_source", source="restart", ended=n)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._log("error", where="moments.end_open", error=type(exc).__name__)

    def _start_rebuild(self) -> None:
        """The one-off rebuild thread. On a failure (a busy database) the rule checkpoint stays old and the stored
        cursors stay where they were; the beat skips its refresh meanwhile and tries again after
        :data:`REBUILD_RETRY_S`, again off the loop."""
        self._backfill_state = "running"

        def run() -> None:
            t0 = time.perf_counter()
            try:
                counts = self.extractor.rebuild()
                self._backfill_state = "done"
                self._log("moment_source", source="rebuild", rule=RULE_VERSION,
                          ms=round((time.perf_counter() - t0) * 1000), **{k: v for k, v in counts.items() if v})
            except Exception as exc:  # noqa: BLE001
                self._retry_at = self._clock() + REBUILD_RETRY_S
                self._backfill_state = "error"
                self.errors += 1
                self._log("error", where="moments.rebuild", error=type(exc).__name__)

        self._backfill = threading.Thread(target=run, name="yuki-memory-moments-rebuild", daemon=True)
        self._backfill.start()

    def nudge(self, source: str = "calls") -> None:
        """A source changed (the call worker after a call starts or ends): refresh on the next beat."""
        self.inputs[f"nudge_{source}"] += 1
        self._nudged.set()

    def beat(self, now: float | None = None) -> None:
        """Called from the service's 1 s loop: write queued live moments; refresh the derived ones when due."""
        t0 = time.thread_time()
        try:
            cfg = self._on()
            if cfg is None:
                return
            self.beats += 1
            now = self._clock() if now is None else float(now)
            try:
                batch = self.writer.flush()
                for m in batch:
                    if not m.partial:
                        self.by_kind[m.kind] += 1
                        if m.kind in LEARN_END_KINDS and m.by != "yuki":
                            self._learn_due = True
                        if m.kind != "page":
                            _log_moment(self._log, m, now)
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self._log("error", where="moments.flush", error=type(exc).__name__)
            running = self._backfill is not None and self._backfill.is_alive()
            if not running and self._backfill_state == "error" and now >= self._retry_at:
                self._start_rebuild()
                running = True
            running = running or self._backfill_state == "error"   # no incremental pass until the rebuild succeeded
            if not running and (self._nudged.is_set() or now >= self._next_refresh):
                self._nudged.clear()
                self._next_refresh = now + cfg.refresh_s
                p0 = time.perf_counter()
                try:
                    counts = self.extractor.refresh(now)
                    self.refreshes += 1
                    for k in DERIVED_KINDS:
                        if counts.get(k):
                            self.by_kind[k] += max(0, counts[k])
                            if k in LEARN_END_KINDS:
                                self._learn_due = True
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self._log("error", where="moments.refresh", error=type(exc).__name__)
                self.refresh_ms.append((time.perf_counter() - p0) * 1000.0)
            if not running:
                self._maybe_learn(now)
            if not running and now >= self._next_prune:
                self._next_prune = now + PRUNE_EVERY_S
                try:
                    pruned = self.store.prune_moments(now - cfg.keep_days * 86400.0)
                    if pruned:
                        self._log("moment_source", source="prune", deleted=pruned, keep_days=cfg.keep_days)
                except Exception as exc:  # noqa: BLE001
                    self.errors += 1
                    self._log("error", where="moments.prune", error=type(exc).__name__)
        finally:
            self.cpu_s += time.thread_time() - t0

    def _maybe_learn(self, now: float) -> None:
        """Phase L's incremental pass (design §9.1): due after a refresh or flush that wrote an occasion-end moment;
        runs on a short-lived thread (never the 1 s loop), at most every :data:`LEARN_EVERY_S`, only when
        ``yuki.skills.learn.quiet`` says the user is idle with no game in front and no call. Never raises."""
        if not self._learn_due or now < self._learn_next:
            return
        if self._learn_thread is not None and self._learn_thread.is_alive():
            return
        try:
            if self.learn_quiet_fn is not None:
                why = self.learn_quiet_fn(now)
            else:
                from yuki.skills.learn import quiet

                why = quiet(self.store, now, idle_s=LEARN_IDLE_S)
        except Exception:  # noqa: BLE001 - unknown: not quiet (fails closed)
            why = "unknown"
        if why:
            self._learn_next = now + 60.0    # asked again in a minute, never every beat
            return
        self._learn_due = False
        self._learn_next = now + LEARN_EVERY_S
        fn = self.learn_fn

        def run() -> None:
            try:
                if fn is not None:
                    fn(now)
                else:
                    from yuki.skills.learn import incremental

                    incremental(self.store, now=now, log=self._log_fn)
                self.learn_runs += 1
            except Exception as exc:  # noqa: BLE001
                self.errors += 1
                self._log("error", where="moments.learn", error=type(exc).__name__)

        self._learn_thread = threading.Thread(target=run, name="yuki-memory-learn", daemon=True)
        self._learn_thread.start()

    def stop(self, now: float | None = None) -> None:
        """The service is stopping: the open live spans (media playing, a calling site in front) end now, marked
        ``ended=stop`` (nothing was seen after it), and the queue is written."""
        at = self._clock() if now is None else float(now)
        with self._lock:
            media, self._media_open = list(self._media_open.values()), {}
            sites, self._site_open = list(self._site_open.values()), {}
        for key, _app, f in media:
            self.writer.put(Moment(kind="media_play", at=at, source="media", source_id=key, ended_at=at, partial=True,
                                   facts={**f, "ended": "stop"}))
        for _host, key, f in sites:
            self.writer.put(Moment(kind="site_front", at=at, source="site", source_id=key, ended_at=at, partial=True,
                                   facts={**f, "ended": "stop"}))
        try:
            self.writer.flush()
        except Exception:  # noqa: BLE001
            pass

    def stats(self) -> dict[str, Any]:
        """Content-free counters for the service's stats line."""
        ms = sorted(self.refresh_ms)

        def pct(p: float) -> float | None:
            return round(ms[min(len(ms) - 1, int(p * len(ms)))], 2) if ms else None

        return {
            "beats": self.beats, "refreshes": self.refreshes, "cpu_ms": round(self.cpu_s * 1000.0, 1),
            "refresh_ms": {"p50": pct(0.5), "p90": pct(0.9), "max": pct(1.0)}, "written_live": self.writer.written,
            "queued": self.writer.pending(), "dropped": self.writer.dropped, "errors": self.errors,
            "kinds": dict(self.by_kind), "inputs": dict(self.inputs), "rebuild": self._backfill_state,
            "media_open": len(self._media_open), "site_open": len(self._site_open), "learn_runs": self.learn_runs,
        }

    def log_sources(self) -> None:
        """One ``moment_source`` line per source (counts and cursor lags, content-free)."""
        now = self._clock()
        for name in ("calls", "timeline", "messages"):
            try:
                value = self.store.moment_cursor(name)
            except Exception:  # noqa: BLE001
                value = None
            lag = None
            if name != "messages" and value:
                try:
                    lag = round(now - float(value), 1)
                except ValueError:
                    lag = None
            self._log("moment_source", source=name, mode="cursor", cursor_lag_s=lag,
                      rows=self.extractor.counters.get(f"{name}_rows", 0))
        self._log("moment_source", source="live", mode="event", writes=self.writer.written,
                  inputs=dict(self.inputs), errors=self.errors, cpu_ms=round(self.cpu_s * 1000.0, 1))

    # -- live inputs -------------------------------------------------------------------

    def on_media_change(self, started: Sequence[dict[str, Any]], stopped: Sequence[dict[str, Any]], *,
                        at: float | None = None, by_yuki: bool = False, request_id: str | None = None,
                        **facts: Any) -> None:
        """The timeline's media read saw sessions start or stop playing (``{app_id, app, title}`` each; the title is
        digested here and never kept unless ``[moments] media_titles``)."""
        at = self._clock() if at is None else float(at)
        for fn in list(self._media_subs):
            try:
                fn(started, stopped, at=at, **facts)
            except Exception:  # noqa: BLE001
                self.errors += 1
        cfg = self._on()
        if cfg is None:
            return
        self.inputs["media"] += 1
        try:
            runs = self._runs(at - ATTRIBUTION_LAG_S["media_play"] - 3600.0, at) if started else []
            cipher = self.store.cipher
            for s in stopped:   # first: a session that stops and starts again in one read is ended, then begun
                with self._lock:
                    found = self._media_open.pop(_media_key(s), None)
                if found is not None:
                    self.writer.put(Moment(kind="media_play", at=at, source="media", source_id=found[0],
                                           ended_at=at, partial=True))
            for s in started:
                app = s.get("app") or None
                title = str(s.get("title") or "")
                digest = cipher.digest("moment", "media_play", app or "", title) if title else None
                keep = bool(cfg.media_titles and title and not self._withheld_lately(app, at))
                key = self.writer.key(at)
                f: dict[str, Any] = {"initial": True} if facts.get("initial") else {}
                m = Moment(kind="media_play", at=at, source="media", source_id=key, app=app, entity=title if keep else None,
                           keep_entity=keep, entity_digest=digest, evidence="poll", facts=dict(f),
                           entity_key=entity_key(cipher, app, title) if title else None)
                if by_yuki:
                    m.by, m.request_id = "yuki", request_id
                m = attribute(m, runs, now=at)
                with self._lock:
                    old = self._media_open.pop(_media_key(s), None)   # a start never stopped: that one ends here
                    self._media_open[_media_key(s)] = (key, app, f)
                    while len(self._media_open) > self.MAX_OPEN:   # a stop never seen: forget the oldest
                        self._media_open.pop(next(iter(self._media_open)))
                if old is not None:
                    self.writer.put(Moment(kind="media_play", at=at, source="media", source_id=old[0], ended_at=at,
                                           partial=True))
                self.writer.put(m)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._log("error", where="moments.media", error=type(exc).__name__)

    def _withheld_lately(self, app: str | None, at: float) -> bool:
        """The app had a private, blocked or adult stretch in the last 10 minutes (then its title is a digest only)."""
        if not app:
            return True
        try:
            rows = self.store._query("SELECT count(*) FROM timeline WHERE process = ? AND ended_at > ?"
                                     " AND withheld IS NOT NULL", (app, at - 600.0))
            return bool(rows and rows[0][0])
        except Exception:  # noqa: BLE001
            return True

    def on_front_change(self, prev_key: str, new_key: str, *, at: float | None = None, hwnd: int = 0,
                        prev_hwnd: int = 0, **facts: Any) -> None:
        """Another window came to the front (the timeline's foreground event): an open ``site_front`` of another window
        ends; subscribers learn of it (the Spotify reader reads its slider when Spotify leaves the front)."""
        at = self._clock() if at is None else float(at)
        self.inputs["front"] += 1
        for fn in list(self._front_subs):
            try:
                fn(prev_key, new_key, at=at, hwnd=hwnd, prev_hwnd=prev_hwnd, **facts)
            except Exception:  # noqa: BLE001
                self.errors += 1
        self._close_sites(at, keep_hwnd=hwnd)

    def _close_sites(self, at: float, *, keep_hwnd: int | None = None, only_hwnd: int | None = None) -> None:
        with self._lock:
            gone = [h for h in self._site_open if (only_hwnd is None or h == only_hwnd) and h != keep_hwnd]
            ended = [self._site_open.pop(h) for h in gone]
        for _host, key, _facts in ended:
            self.writer.put(Moment(kind="site_front", at=at, source="site", source_id=key, ended_at=at, partial=True))

    def site(self, screen: dict[str, Any]) -> None:
        """A window the watcher read (``watcher.on_screen``): a blocked page of a calling site in front is a
        ``site_front`` (app and service only: never its title or path; the host stays in this process). Non-calling
        blocked sites (a bank) give nothing. At most one per (window, host) while it stays in front."""
        if self._on() is None:
            return
        try:
            hwnd = int(screen.get("hwnd") or 0)
            host = str(screen.get("host") or "").strip().casefold()
            blocked = bool(screen.get("blocked"))
            if not blocked or not host:
                if hwnd and hwnd in self._site_open:
                    self._close_sites(float(screen.get("at") or self._clock()), only_hwnd=hwnd)
                return
            with self._lock:
                open_ = self._site_open.get(hwnd)
            if open_ is not None and open_[0] == host:
                return
            service = self.calling_service(host) if self.calling_service is not None else None
            at = float(screen.get("at") or self._clock())
            if open_ is not None:
                self._close_sites(at, only_hwnd=hwnd)
            if not service:
                return
            self.inputs["site"] += 1
            app = app_key(str(screen.get("process") or "")) or None
            key = self.writer.key(at)
            service = " ".join(str(service).split())[:40]
            if service.casefold() in (host, host[4:] if host.startswith("www.") else host):
                service = None   # a calling site with no product name known: never its host (logs carry no host)
            facts: dict[str, Any] = {"sighting": "first" if open_ is None else "again"}
            tab_known = None
            if self.tab_known_fn is not None and hwnd:
                try:
                    tab_known = bool(self.tab_known_fn(hwnd))
                except Exception:  # noqa: BLE001 - unknown
                    tab_known = None
            if tab_known is not None:
                facts["tab_known"] = tab_known
            m = Moment(kind="site_front", at=at, source="site", source_id=key, app=app,
                       service=service, evidence="site", facts=facts)
            yuki, request_id = self._acting()
            if yuki:
                m.by, m.request_id = "yuki", request_id
            m = attribute(m, self._runs(at - 3600.0, at), now=at)
            with self._lock:
                self._site_open[hwnd] = (host, key, dict(m.facts))
                while len(self._site_open) > self.MAX_OPEN:
                    self._site_open.pop(next(iter(self._site_open)))
            self.writer.put(m)
            self._log("site_front", app=app, service=m.service, sighting=m.facts["sighting"], by=m.by,
                      request_id=m.request_id, tab_known=tab_known)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._log("error", where="moments.site", error=type(exc).__name__)

    def volume_change(self, app: str | None, value_from: float | None, value: float, *, means: str,
                      at: float | None = None, front: str | None = None, yuki_context: bool = False,
                      since: float | None = None, **facts: Any) -> None:
        """A level moved and stayed (``means``: ``master`` | ``session`` | ``in_app``; 0-100). ``yuki_context``: the
        Core Audio event carried Yuki's own context GUID; ``since``: an in-app read's previous read (D18)."""
        cfg = self._on()
        if cfg is None or not cfg.volume_events:
            return
        at = self._clock() if at is None else float(at)
        self.inputs["volume"] += 1
        try:
            f: dict[str, Any] = _code_facts(facts)
            if front:
                f["front"] = app_key(front) or None
            if since is not None:
                f["since"] = round(float(since), 3)
            m = Moment(kind="volume_change", at=at, source="spotify" if means == "in_app" else "volume",
                       source_id=self.writer.key(at), app=app_key(app) or app or None,
                       value=round(float(value), 1), value_from=None if value_from is None else round(float(value_from), 1),
                       means=means, evidence="window_read" if means == "in_app" else "endpoint_event",
                       facts={k: v for k, v in f.items() if v is not None})
            if yuki_context:
                m.by = "yuki"
            m = attribute(m, self._runs(at - 3600.0, at), now=at)
            self.writer.put(m)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._log("error", where="moments.volume", error=type(exc).__name__)

    def media_context(self, app: str | None, name: str, *, at: float | None = None, link: str | None = None,
                      keep_name: bool = True, since: float | None = None, **facts: Any) -> None:
        """Which playlist plays (the Spotify window reader's "Pause {name}" row): the name encrypted with a digest, and
        a learned ``spotify:`` link encrypted; ``keep_name`` False (a Spotify-owned playlist, an unparsed row) keeps
        the digest only."""
        if self._on() is None or not name:
            return
        at = self._clock() if at is None else float(at)
        self.inputs["media_context"] += 1
        try:
            f = _code_facts(facts)
            if since is not None:
                f["since"] = round(float(since), 3)
            m = Moment(kind="media_context", at=at, source="spotify", source_id=self.writer.key(at),
                       app=app_key(app) or app or None, entity=name, keep_entity=bool(keep_name),
                       evidence="window_read", facts=f,
                       secret_facts={"uri": link} if (link and keep_name) else {})
            m.digest_with(self.store.cipher)
            m = attribute(m, self._runs(at - 3600.0, at), now=at)
            self.writer.put(m)
        except Exception as exc:  # noqa: BLE001
            self.errors += 1
            self._log("error", where="moments.media_context", error=type(exc).__name__)

    def _runs(self, lo: float, hi: float) -> list[RunWindow]:
        try:
            return list(self._runs_fn(lo, hi))
        except Exception:  # noqa: BLE001
            return []


def default_page_entity() -> Callable[[str, str], tuple[str, str] | None] | None:
    """The sites' URL templates (one place: ``yuki/connect/sites/``, phase F ``fLive``), when that package is there:
    ``page_entity(host, path) -> (template id, value) | None``. None until then: page moments key on the page key."""
    try:
        from yuki.connect import templates  # type: ignore[import-not-found]
    except Exception:  # noqa: BLE001
        return None
    fn = getattr(templates, "page_entity", None)
    return fn if callable(fn) else None


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _open(db: Path | None) -> Any:
    from yuki.memory.store import Store, default_db_path

    return Store.open(Path(db) if db else default_db_path())


def _copy(db: Path | None, dest: Path) -> Path:
    """A read-only copy of ``db`` (default: the live database) into ``dest``, an empty folder
    (:func:`yuki.eval.dbcopy.copy_db`)."""
    from yuki.eval.dbcopy import copy_db
    from yuki.memory.store import default_db_path

    return copy_db(Path(db) if db else default_db_path(), dest)


def _summary(store: Any, since: float | None = None) -> dict[str, Any]:
    from datetime import datetime

    days: dict[str, Counter] = defaultdict(Counter)
    evidence: Counter = Counter()
    for m in store.moments_between(since=since):
        day = datetime.fromtimestamp(m["at"] - 4 * 3600).strftime("%Y-%m-%d")
        days[day][m["kind"]] += 1
        if m["kind"] == "call_join":
            evidence[(m["app"] or "", m["evidence"] or "")] += 1
    return {"table": store.moments_counts(),
            "per_day": {d: dict(c) for d, c in sorted(days.items())},
            "call_join_by_app_evidence": {f"{a}/{e}": n for (a, e), n in sorted(evidence.items())}}


def _bench(db: Path | None, hours: float) -> dict[str, Any]:
    """Rebuild on a temporary copy, then ``hours`` of synthetic events through the hub (fake clock, real store):
    CPU (this thread), RSS growth, refresh ms. The source database is only read."""
    import random
    import tempfile

    import psutil

    proc = psutil.Process()
    out: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="yuki-moments-bench-") as tmp:
        path = _copy(db, Path(tmp))
        store = _open(path)
        try:
            ex = Extractor(store)
            rss0 = proc.memory_info().rss
            t0, c0 = time.perf_counter(), time.thread_time()
            counts = ex.rebuild()
            out["rebuild"] = {"s": round(time.perf_counter() - t0, 3), "cpu_s": round(time.thread_time() - c0, 3),
                              "counts": counts}
            clock = [time.time()]
            hub = MomentHub(store, config=lambda: MomentsConfig(), clock=lambda: clock[0],
                            calling_service=lambda host: "Bench Meet" if host == "meet.example" else None,
                            acting_fn=lambda: None, runs_fn=lambda lo, hi: [])
            hub._next_refresh = clock[0] + 60
            rng = random.Random(7)
            rss1 = proc.memory_info().rss
            c1 = time.thread_time()
            beats = int(hours * 3600)
            for i in range(beats):
                clock[0] += 1.0
                if i % 15 == 0 and rng.random() < 0.2:   # a media change on the 15 s read
                    title = f"bench title {rng.randrange(40)}"
                    hub.on_media_change([{"app_id": "bench.app", "app": "benchapp", "title": title}], [], at=clock[0])
                if rng.random() < 0.01:
                    hub.volume_change("benchapp", 40.0, 40.0 + rng.randrange(-10, 10), means="session", at=clock[0])
                if rng.random() < 0.02:
                    hub.on_front_change("a", "b", at=clock[0], hwnd=rng.randrange(1, 5))
                if rng.random() < 0.005:
                    hub.site({"process": "zen", "hwnd": 7, "host": "meet.example", "blocked": "blocked_host",
                              "at": clock[0]})
                if rng.random() < 0.002:
                    hub.nudge("calls")
                hub.beat(clock[0])
            hub.stop()
            idle_cpu = time.thread_time() - c1
            out["synthetic"] = {"hours": hours, "beats": beats, "cpu_s": round(idle_cpu, 3),
                                "cpu_percent_one_core": round(100.0 * idle_cpu / (hours * 3600.0), 4),
                                "rss_growth_mb": round((proc.memory_info().rss - rss1) / 2**20, 2),
                                "stats": hub.stats()}
            out["rss_growth_total_mb"] = round((proc.memory_info().rss - rss0) / 2**20, 2)
        finally:
            store.close()
    return out


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.moments", description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("rebuild", "backfill", "stats", "bench"):
        p = sub.add_parser(name)
        p.add_argument("--db", type=Path, default=None, help="database (default: Yuki's own)")
        if name in ("rebuild", "backfill"):
            p.add_argument("--dry-run", action="store_true", help="run on a temporary copy and print what it gives")
        if name == "backfill":
            p.add_argument("--since", required=True, help="ISO date or time (local)")
        if name == "bench":
            p.add_argument("--hours", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.cmd == "bench":
        print(json.dumps(_bench(args.db, args.hours), indent=1, default=str))
        return 0
    if args.cmd == "stats":
        import sqlite3

        from yuki.memory.store import default_db_path

        db = Path(args.db) if args.db else default_db_path()
        conn = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
        try:
            if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='moments'").fetchone():
                print("no moments table (database before migration 40)")
                return 0
        finally:
            conn.close()
        store = _open(db)
        try:
            print(json.dumps(_summary(store), indent=1, default=str))
        finally:
            store.close()
        return 0
    import tempfile
    from datetime import datetime

    tmp = tempfile.TemporaryDirectory(prefix="yuki-moments-") if args.dry_run else None
    try:
        db = _copy(args.db, Path(tmp.name)) if tmp is not None else args.db
        store = _open(db)
        try:
            ex = Extractor(store, page_entity=default_page_entity())
            t0 = time.perf_counter()
            if args.cmd == "rebuild":
                counts = ex.rebuild()
            else:
                since = datetime.fromisoformat(args.since).timestamp()
                counts = ex.rebuild(since=since)
            print(json.dumps({"cmd": args.cmd, "dry_run": bool(args.dry_run), "s": round(time.perf_counter() - t0, 3),
                              "counts": counts, **_summary(store)}, indent=1, default=str))
        finally:
            store.close()
    finally:
        if tmp is not None:
            tmp.cleanup()
    return 0


__all__ = [
    "MOMENT_KINDS", "DERIVED_KINDS", "LIVE_KINDS", "SEEN_VIAS", "SEEN_EVIDENCE", "RULE_VERSION", "JOIN_GAP_S",
    "LEARN_EVERY_S", "LEARN_IDLE_S", "LEARN_END_KINDS", "entity_key", "registrable_domain", "templated_hosts",
    "ATTRIBUTION_LAG_S", "MomentsConfig", "Moment", "RunWindow", "attribute", "skill_runs_between", "call_at",
    "is_call_join", "is_game_voice", "game_judge", "AppIdentity", "CallingSites", "persisted_site_verdicts",
    "cluster_joins", "join_moment", "Extractor", "MomentWriter", "MomentHub", "default_page_entity", "log_digest",
]


if __name__ == "__main__":
    raise SystemExit(main())
