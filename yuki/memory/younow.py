"""One live "you right now" record (YouNow), built once, in the memory service (yState; state-design.md §3).

Every part of Yuki used to work out "what is the user doing right now?" on its own: the card worker every 15 s
(``nudges.observe``), each card maker again for its own model call, the thinker per request. This module keeps one
typed, capped record in the memory service's RAM, next to the Now board, and rebuilds a part of it only when
something in that part changed. Readers: the in-process card judge (``jCore``), and since ``yConsumers`` the brain's
request turn and the voice talker's one line, over the Now pipe (below).

**Sections** (schema v1, §3.2; every list capped, every text clipped, ≤ 12 KB of JSON in all):

* ``presence`` - present / away / locked / off, and ``stretch`` (whether the timeline has an open stretch: False
  with no user window in front); ``at_pc_since``, ``settled_at``, ``idle_since``, ``away_at``, ``day_began``
  (instants, never growing durations: readers compute "how long" when they read); the last break; time at the PC
  today (``base_s`` + the live part since ``since``); the last 7 days; the current activity session.
* ``focus`` - hold facts only: the game (:func:`focus_game`), the return hold's end, full screen, the meeting, the
  call (a game's own voice chat is not a call, D8), Yuki acting, the hold names at build time. No Now-board field is
  copied: the record carries ``board_version`` (top level, read every beat) and nothing of the screen.
* ``changed`` - what just changed (≤ 12, last 30 min): moments of the known kinds (``page`` left out, unknown kinds
  ignored) plus four transitions this module synthesises: ``call_end``, ``game_end``, ``sit_down``, ``switch``.
* ``work`` - the thread now (``stands``: where it stands), ≤ 5 active threads, ≤ 5 goals, ≤ 6 items due in 24 h or
  overdue, the list's size, the work board's counts (``board(replies=False)``), ≤ 3 plans in the next 24 h (the
  events scan's dated events). ``overdue`` is right as of the build (a rebuild is due when it turns); readers that
  care compare ``due_at`` with their own now, as :func:`render` does.
* ``usual`` (yUsual) - the user's routines (``yuki.memory.routines``, mined nightly from the skill learner's
  occasions) as they fall today: ``now`` (an active routine whose time is within its hit width,
  ``routines.HIT_WINDOW_MIN``, of now), ``today`` (later today, ≤ 5), each with its learned skill's key and name (the
  user's told label first, else the learned name; a nameless skill's code label is never taken for a name), what it is
  in plain words (``routines.KIND_PHRASES``), its clock minute and window, offer mode and whether its end state already
  happened today (``routines.done_today``); and the catalog's counts (capabilities, learned, trusted).
* ``prefs`` (yUsual) - ``usuals``: per preference slot (``<capability>:<slot>@<scope>``) the option the choice rule
  picks when it is a confirmed usual or a clear observed choice (``yuki.skills.prefs.choose`` rules 1 and 2; rule 3's
  guesses are left out), ≤ 12, confirmed first. Taste-filled slots are left out (their options are entity keys; taste
  is phase T's).
* ``about`` - the nightly brief by reference: how to be with them, routine, careful, how many people.
* ``cards`` - the card on screen (the UI marked it shown, no reaction, within the card gap), the last card, today's
  shown cards per ``kind/trigger`` and their reactions, and the judge's own facts (modes, Today queue, next wake,
  budget left) published in RAM through :meth:`YouBuilder.judge_facts` (never written, so they never mark a part
  dirty).

**Refresh** (§3.3): :meth:`YouBuilder.beat` runs on the service's existing 1 s loop, after ``moments.beat()``. It
compares a few values held in RAM and rebuilds a section only when its inputs moved:

* ``focus`` - the game gate's ``on_change``, the call worker's change hook (fanned out with ``moments.nudge``), the
  acting follower's event, a change of :meth:`TimelineRecorder.flags`, the gate dying, the set of open calls, and the
  meeting's audio (read at most every 15 s, only while a meeting row is within ``meeting_hold_min``, as
  ``NudgeWorker._meeting_held``); front changes only while the gate is dead (the fallback reads the open stretch);
* ``presence`` - a state change in the timeline's flags (present / away / locked / paused, and the open stretch
  closing or opening: no user window in front, counted once it holds for a second beat, since the timeline swaps
  stretches through "none open"), Yuki starting or stopping acting, the stretch reaching its projected
  ``settled_at``, a front app held past the hop rule with another key than the session's, a day boundary, and the
  timeline's next flush after a bridged hole (below);
* ``changed`` - the moments hub's counters moved (compared in RAM), a media change, a synthesised transition, its
  oldest item leaving the 30 min window;
* ``work`` / ``cards`` / ``about`` / ``usual`` / ``prefs`` - the database changed: ``total_changes()`` (the service's
  own connection) or ``PRAGMA data_version`` (the tray's writes), at most once per 2 s, then one probe query names the
  dirty sections (``usual``: the routines, the skills rows, a new skill run; ``prefs``: the preference rows).
  ``nudge_state`` keys of the judge (``judge_*``) are never probed, so the judge's bookkeeping never marks a section.
  ``work`` also at the next instant one of its time windows changes (an item becomes due or overdue, enters or leaves
  the 24 h window, a plan or a meeting passes); ``usual`` at the next instant a routine's window opens or closes, at
  the routines' day boundary (04:00), and on a new moment while a routine of ``now`` is not yet done today (the user
  may have just done it);
* every section - a gap in the beats longer than :data:`STALE_S` (the process was suspended, as in a sleep with no
  lock on wake, or the loop stalled).

A rebuilt section whose content did not change keeps its old object and version. Nothing is rebuilt because time
passed: holds that end with time (the return hold, the card gap, away after ``presence_min``, settling, the meeting's
audio window) are timestamps that :func:`holds_now` compares when read, or one instant the beat compares.

**The judge's wake** (:attr:`YouBuilder.wake`, an in-process ``threading.Event``) is set only on: a change of the
hold set (as :func:`holds_now` derives it, so a hold's time running out counts), a breakpoint (``sit_down``,
``switch``, ``call_end``, ``game_end``, a new ``message_sent``), a reaction on a card, and the judge's own deadline
(``cards.next_wake``). Changes to ``work``, ``about`` or ``usual`` never wake it.

**Freshness** is judged by the reader: every beat publishes a new top-level mapping with ``beat_at`` (the sections
are shared by reference; this bumps no version). :func:`holds_now` answers ``["unknown"]`` when ``now - beat_at >
STALE_S``, so a stalled loop never leaves a stale "no game" in force.

**Other processes** (yConsumers, §3.4, §11): the Now pipe's op ``you`` serves the record (``yuki.memory.now``:
``NowServer`` finds this process's builder through :func:`live_builder`; ``NowClient.you()`` keeps a version-cached
copy). When the set of holds changes (and only then) the builder sets one named auto-reset event per reader
(``now.you_event_name``, readers ``talk`` and ``cards``), so the voice talker's line cache and the tray's card queue
re-ask instead of polling. The voice wake path never reads it.

**Renderers** (pure; content in RAM only, never logged): ``render(you, "cards")`` (the card writer, ≤ 2,500),
``render(you, "thinker")`` (the brain's request turn, ≤ 1,200: focus and the day, what just changed for "that" / "it",
the thread now and the top threads with next steps, due and overdue items, the usual now and the confirmed usuals),
``render(you, "talker")`` (one line, ≤ 300: focus words and up to three just-changed items; never to-dos, threads,
news or the usual now: the 25 Sept rule).

**Publishing** swaps an immutable snapshot by reference: sections are frozen (``MappingProxyType`` and tuples) and
never changed after publishing; :func:`thaw` gives plain dicts and lists (JSON).

**Privacy**: RAM only, never written to disk and never in the MCP. Logs are content-free: ``you_built`` (section,
version, ms, bytes), ``you_clipped`` (section, n), ``focus_unknown`` (source, state, duration), ``you_source``,
``you_off``; the service's stats line carries :meth:`YouBuilder.stats` (beat cost p50/p90, builds, wakes).
``python -m yuki.memory.younow --show`` prints the record content-free (sizes, counts, flags, times); ``--content``
prints it in full, to the user's own terminal only. It always runs on a read-only copy of the database.

**Equivalence with what it replaces** (state-design.md §7, yState item 3), and the differences:

* ``presence`` = ``nudges.observe()``'s stretch rules on the same rows: ``presence_sessions`` over ``wide_rows`` (the
  last 36 h, Yuki's own stretches left out), ``break_min``, ``presence_min``, ``_break_between`` for the last break;
  ``day_began`` = ``checkins.day_began`` (the latest settled stretch after a break of
  ``max(DAY_BREAK_S, resume_after_h)``). Difference: the open stretch is not in the database until the timeline's next
  30 s flush, so while the timeline says present, the time since the last row (or since the return, or since Yuki
  stopped acting) is counted as presence (observe saw it up to 30 s late). That live part never bridges more than a
  flush: when the newest of those marks is older than ``ONGOING_S`` (the process was suspended, or the timeline
  stalled), the live part starts now, as observe saw the hole in the rows, and presence is read again once the
  timeline's next flush has landed. A live part that begins now counts as a sliver, so a return is seen unsettled
  at once (``settling``, then ``sit_down`` at the projected ``settled_at``). With no open stretch (the desktop, the
  taskbar or a shell list in front: the timeline records nothing and does no idle accounting), ``state`` is
  ``away`` and ``stretch`` False: presence is the stored rows alone, ``idle_since`` their last presence and ``away``
  held from ``presence_min`` after it, as observe; the stretch that just closed counts up to the moment it closed.
  While Yuki acts, no live part is added (its rows are left out, as observe).
* ``focus.call`` = ``NudgeWorker._call_open``: a call of kind ``call`` among the open statuses holds; a game's voice
  chat (kind ``game``) is reported (``kind = "game"``) but holds nothing; an unreadable store gives ``open = None``.
* ``focus.meeting.audio_held`` = ``NudgeWorker._meeting_held``: the latest meeting row within ``meeting_hold_min``,
  its app with an active audio render session, read at most every 15 s; unreadable gives ``None`` (each reader
  decides, as ``fail_closed`` did). The window's end re-reads focus once, so the hold ends with it. The meeting hold
  (``meeting``) = observe's: ``focus.meeting.front`` (the live flag), or a meeting row within ``MEETING_GAP_S`` (the
  timestamp ``front_until``, compared when read).
* ``focus_game()`` = ``live_state()``'s rule (the gate's ``front_game()``, or with ``[trace] game_gate = "running"``
  any game running) and the board's ``game_front_fn`` (the name; True when only the rollback's running game holds;
  None with no live gate); with no live gate the card side falls back to the program in front
  (``nudges._game_front``), ``source = "fallback"``. Difference: that program is the hub's latest front event when
  there is one (observe read the newest row, up to a flush behind). The Spotify reader's check is the same rule (no
  live gate: held). Under the rollback the board's True also reaches the tray's input backstop (``board_front_source``),
  so it refuses while a game runs behind: stricter, the rollback only.
* ``focus.game.return_hold_until`` = observe's return hold (a full-screen or game stretch that ended less than
  ``return_hold_s`` ago), plus the instant the gate or the timeline saw the game or full screen end (the row may be
  up to 30 s behind).
* ``focus.fullscreen`` is the open stretch's flag (the timeline's ``flags()``, as ``live_state()``); observe also held
  on the newest row's flag for up to ``ONGOING_S`` after it ended, which the return hold covers anyway (a snapshot
  would otherwise keep that until its next rebuild).
* ``focus.yuki_acting`` comes from the acting follower's event alone; observe also held while the newest row was
  Yuki's for up to ``ONGOING_S`` after it ended.
* ``board_version`` sits at the top level, read every beat like ``beat_at`` (in ``focus`` it would be stale between
  rebuilds, or bump the version on every rebuild).
* Focus reads the last minutes of the timeline raw (:func:`recent_rows`, by the ``ended_at`` index) without
  ``Store._apply_proof``, which only re-reads rows written before the live call-evidence gate.

Public API::

    YouBuilder(store, privacy=, log=, db_path=, timeline_fn=, gate_fn=, board=, hub=, acting_now=, live=)
        .beat(now=None)                      # the service loop, after moments.beat()
        .on_gate_change(decision) / .on_call_change(source) / .on_acting(bool)   # change hooks, any thread
        .on_front(prev_key, new_key, **facts) / .on_media(started, stopped, **facts)   # moments.subscribe_*
        .judge_facts(modes=, digest_queued=, next_wake=, budget_left=)   # the judge's own facts, RAM only
        .snapshot() -> mapping | None        # the current immutable record (None: [you] enabled = false)
        .wake                                # threading.Event for the judge
        .stats() -> dict                     # content-free, for the service's stats line
    focus_game(gate, fallback=None) -> {"front", "name", "running", "source"}
    holds_now(you, now) -> [hold names]      # ["unknown"] when stale or absent
    presence_now(you, now) -> {"present", "away", "settled", "at_pc_s", "idle_s", "today_s"}
    day_began(clips, config, since=, sessions=None) -> (start, gap) | None
    day_began_from(store, now, config) -> (start, gap) | None
    render(you, "cards" | "thinker" | "talker", now=None) -> str   # <= 2,500 / 1,200 / 300 characters (RAM only)
    live_builder() -> YouBuilder | None      # this process's live builder (the Now pipe's op "you")
    thaw(you) -> plain dicts and lists
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import weakref
from collections import Counter, OrderedDict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

from yuki.memory.checkins import DAY_BREAK_S, day_boundary, day_totals, user_day
from yuki.memory.nudges import (
    ONGOING_S,
    RESUME_SEARCH_S,
    NudgeConfig,
    _break_between,
    _game_process,
    activity_sessions,
    game_row,
    presence_sessions,
    todo_items,
    wide_rows,
)
from yuki.memory.store import CALL_OPEN_STATUSES, app_key
from yuki.memory.timeline import FLUSH_EVERY_S, MEETING_GAP_S, PrivacySection, _clip

#: The record's schema version (a new field is additive; a removed field bumps it).
SCHEMA_V = 1
#: A record whose last beat is older than this is "not known" (every reader checks it).
STALE_S = 5.0
#: The database is asked whether it changed at most this often (then one probe query names the dirty sections).
DB_EVERY_S = 2.0
#: A new front app held this long is a ``switch`` (a breakpoint for the judge).
SWITCH_HELD_S = 60.0
#: ``changed`` keeps this much history, at most this many items.
CHANGED_S = 30 * 60.0
CHANGED_MAX = 12
#: The moment kinds ``changed`` keeps (``page`` is left out: derived late, after a dwell; unknown kinds are ignored).
CHANGED_KINDS: tuple[str, ...] = ("call_join", "game_voice", "site_front", "media_play", "media_context",
                                  "volume_change", "message_sent")
#: The transitions this module synthesises from its own state.
SYNTH_KINDS: tuple[str, ...] = ("call_end", "game_end", "sit_down", "switch")
#: The meeting's audio is read at most this often (as NudgeWorker._meeting_held), only while a meeting row is recent.
MEETING_AUDIO_EVERY_S = 15.0
#: The current activity session is cut from this much of the timeline (as observe's 4 h window).
SESSION_LOOKBACK_S = 4 * 3600.0
#: A moment's entity digest is kept this short: enough to tell two entities apart (equality only), never content.
ENTITY_CHARS = 16
#: Past days kept for the care and evening inputs.
DAYS_KEPT = 7
#: List and text caps.
THREADS_MAX, GOALS_MAX, DUE_MAX, PLANS_MAX, CAREFUL_MAX = 5, 5, 6, 3, 6
TITLE_CHARS, STATE_CHARS, GOAL_CHARS, LABEL_CHARS = 100, 160, 160, 60
WITH_HIM_CHARS, ROUTINE_CHARS, CAREFUL_CHARS = 600, 300, 80
#: ``due``: items due within this long, or overdue by at most this long.
DUE_AHEAD_S = 86400.0
OVERDUE_BACK_S = 7 * 86400.0
#: ``plans``: dated events within this long.
PLANS_AHEAD_S = 86400.0
#: Section caps (bytes of compact JSON, §3.6) and the whole record's.
SECTION_CAPS: dict[str, int] = {"presence": 1536, "focus": 614, "changed": 1229, "work": 4096, "usual": 1536,
                                "prefs": 1229, "about": 1536, "cards": 614}
RECORD_CAP = 12 * 1024
#: The renderers' caps (characters): the card writer, the brain's request turn (inside its overview slot), the talker's
#: volatile line.
RENDER_CAPS: dict[str, int] = {"cards": 2500, "thinker": 1200, "talker": 300}
#: The sections each out-of-process reader asks for (a consumer reads only the sections it renders, §11).
THINKER_SECTIONS: tuple[str, ...] = ("focus", "presence", "changed", "work", "usual", "prefs")
TALKER_SECTIONS: tuple[str, ...] = ("focus", "changed")
#: The talker's line is left out when the record's last beat is older than this when it is read (s): its cache is
#: refreshed in the background, and a stalled service must not put an old "in VALORANT" in the talker's mouth.
TALKER_STALE_S = 15.0
#: The thinker's "just changed" lines and the talker's (newest first).
THINKER_CHANGED, TALKER_CHANGED = 5, 3
#: The thinker's threads besides the thread now.
THINKER_THREADS = 3
#: The sections, in the order a beat rebuilds them (focus and presence synthesise transitions ``changed`` shows).
SECTIONS: tuple[str, ...] = ("focus", "presence", "changed", "work", "usual", "prefs", "about", "cards")
#: The sections the database probe can mark.
DB_SECTIONS: tuple[str, ...] = ("focus", "work", "cards", "about", "usual", "prefs")
#: ``usual``: routines now (at most) and later today (at most); texts.
USUAL_NOW_MAX, USUAL_TODAY_MAX = 3, 5
USUAL_NAME_CHARS, USUAL_WHAT_CHARS = 60, 90
#: A new moment while a ``now`` routine is not done yet re-reads ``usual`` at most this often (s).
USUAL_MOMENT_S = 20.0
#: ``prefs.usuals``: at most this many.
PREFS_MAX = 12
#: Display names of app keys kept per builder (``store.app_display_name``, else the key tidied).
LABELS_KEPT = 128
#: A moment's calling service (a product name the moments recorded) is kept this short.
SERVICE_CHARS = 40
#: The judge's own facts it may publish into ``cards``.
JUDGE_FACTS: tuple[str, ...] = ("modes", "digest_queued", "next_wake", "budget_left")
#: Beat timings kept for the stats line's percentiles (about ten minutes of beats).
BEAT_SAMPLES = 600

#: One probe query: per section, values that move when its source tables change (content-free columns only).
_PROBE_SQL = (
    "SELECT"
    " (SELECT group_concat(id || ':' || ifnull(kind, '')) FROM calls WHERE status IN (%s)),"
    " (SELECT max(updated_at) FROM goals),"
    " (SELECT max(updated_at) FROM work_threads),"
    " (SELECT max(updated_at) FROM todos),"
    " (SELECT ifnull(max(updated_at), 0) || ':' || ifnull(max(id), 0) FROM open_loops),"
    " (SELECT count(*) || ':' || ifnull(max(id), 0) || ':' || ifnull(max(valid_to), 0) || ':' || count(title_ciphertext)"
    "   FROM conversation_facts WHERE kind = 'commitment'),"
    " (SELECT max(updated_at) FROM work_items),"
    " (SELECT updated_at FROM nudge_state WHERE name = 'presence_events'),"
    " (SELECT ifnull(max(id), 0) || ':' || ifnull(max(shown_at), 0) || ':' || ifnull(max(outcome_at), 0) FROM nudges),"
    " (SELECT max(reacted_at) FROM nudges),"
    " (SELECT ifnull(max(id), 0) || ':' || ifnull(max(at), 0) FROM portraits),"
    " (SELECT count(*) || ':' || ifnull(max(updated_at), 0) FROM routines),"
    " (SELECT count(*) || ':' || ifnull(max(updated_at), 0) FROM skills),"
    " (SELECT ifnull(max(id), 0) FROM skill_runs),"
    " (SELECT count(*) || ':' || ifnull(max(updated_at), 0) FROM prefs)"
) % ",".join("'%s'" % s for s in CALL_OPEN_STATUSES)


@dataclass(frozen=True)
class YouConfig:
    """The privacy file's ``[you]`` table (defaults when the file has none)."""

    enabled: bool = True

    @classmethod
    def from_dict(cls, table: dict | None) -> YouConfig:
        return cls(enabled=bool((table or {}).get("enabled", True)))


#: This process's live builder (the memory service makes one), held weakly: the Now pipe's op ``you`` reads it.
_live_ref: Callable[[], Any] = lambda: None   # noqa: E731
_live_lock = threading.Lock()


def live_builder() -> YouBuilder | None:
    """The live :class:`YouBuilder` of this process (the memory service's), or None (none has beaten yet, or it went
    away). A builder registers itself on its first beat, so one the service dropped (its wiring failed, so the loop
    never beats it) is never served. The Now pipe's op ``you`` serves its snapshot, so the service needs no extra
    wiring for it."""
    with _live_lock:
        return _live_ref()


def _register(builder: YouBuilder) -> None:
    global _live_ref
    with _live_lock:
        _live_ref = weakref.ref(builder)


# ---------------------------------------------------------------------------
# Pure helpers: the game in front, the day, holds, freezing
# ---------------------------------------------------------------------------


def focus_game(gate: Any, fallback: Callable[[], bool] | None = None) -> dict[str, Any]:
    """The one rule for "a game is in front", shared by ``live_state()`` (the cards' gate), the Now board's
    ``game_front_fn`` and YouNow.

    With a live gate: ``name`` = ``GameGate.front_game()`` (the game's stem, ``"d3d"`` in exclusive full screen, or
    ""), ``front`` = a name, or with ``[trace] game_gate = "running"`` (the rollback) any game running anywhere;
    ``running`` = ``game_running()``; ``source = "gate"``. With no gate or a dead one: ``name`` None (the board's "not
    known"), ``front`` = ``fallback()`` (the open stretch's program, as ``nudges._game_front``) or None with no
    fallback, ``running`` True (fails closed, as ``running_holds``), ``source`` "fallback" or "none"."""
    if gate is not None and getattr(gate, "alive", False):
        name = str(gate.front_game() or "")
        running = bool(gate.game_running())
        return {"front": bool(name) or (gate.scope == "running" and running), "name": name, "running": running,
                "source": "gate"}
    front = None
    if fallback is not None:
        try:
            front = bool(fallback())
        except Exception:  # noqa: BLE001 - unknown, never raised into a reader
            front = None
    return {"front": front, "name": None, "running": True, "source": "fallback" if fallback is not None else "none"}


def day_began(clips: Sequence[Any], config: Any, *, since: float,
              sessions: Sequence[Any] | None = None) -> tuple[float, float] | None:
    """``(start, gap)``: when the user's day at the PC began - the latest settled stretch after a break of at least
    ``max(DAY_BREAK_S, resume_after_h)`` (their night) - and how long that break was; None when there is none
    (checkins.day_began's rule, pure: clipped timeline rows in, the window's start ``since``). ``sessions``: the
    rows' ``presence_sessions`` already computed."""
    if sessions is None:
        sessions = presence_sessions(clips, config)
    settled = [s for s in sessions if s.settled]
    need = max(DAY_BREAK_S, float(getattr(config, "resume_after_h", 3.0)) * 3600.0)
    found = None
    for i, s in enumerate(settled):
        gap = s.start - (settled[i - 1].end if i else since)
        if gap >= need:
            found = (s.start, gap)
    return found


def day_began_from(store: Any, now: float, config: Any) -> tuple[float, float] | None:
    """:func:`day_began` over the store's content-free rows of the last 36 h (Yuki's own left out); None on a read
    failure (for the history readers: ``checkins.day_began``, ``api.day_context``, ``journal.user_day_line``)."""
    since = now - 36 * 3600.0
    try:
        clips = _clip([r for r in store.timeline_states(since, now) if not r.by_yuki], since, now)
    except Exception:  # noqa: BLE001
        return None
    return day_began(clips, config, since=since)


def focus_holds(focus: Any, now: float) -> list[str]:
    """The hold names the focus facts give at ``now`` (the policy maps them to its classes): ``game`` /
    ``game_unknown``, ``return_hold``, ``fullscreen`` / ``fullscreen_unknown``, ``call`` / ``call_unknown``,
    ``meeting_audio`` / ``meeting_audio_unknown``, ``meeting``, ``yuki_acting``."""
    if not focus:
        return ["focus_unknown"]
    out: list[str] = []
    game = focus.get("game") or {}
    if game.get("front") is None:
        out.append("game_unknown")
    elif game.get("front"):
        out.append("game")
    until = game.get("return_hold_until")
    if until is not None and now < until:
        out.append("return_hold")
    full = focus.get("fullscreen")
    if full is None:
        out.append("fullscreen_unknown")
    elif full:
        out.append("fullscreen")
    call = focus.get("call") or {}
    if call.get("open") is None:
        out.append("call_unknown")
    elif call.get("open") and call.get("kind") == "call":
        out.append("call")
    meeting = focus.get("meeting") or {}
    if meeting.get("audio_held") is None:
        out.append("meeting_audio_unknown")
    elif meeting.get("audio_held"):
        out.append("meeting_audio")
    front_until = meeting.get("front_until")
    if meeting.get("front") or (front_until is not None and now < front_until):
        out.append("meeting")
    if focus.get("yuki_acting"):
        out.append("yuki_acting")
    return out


def holds_now(you: Any, now: float) -> list[str]:
    """Every hold true at ``now``, re-derived from the record's facts and timestamps (staleness checked here):
    ``["unknown"]`` when there is no record or its last beat is older than :data:`STALE_S`. Besides
    :func:`focus_holds`: ``locked``, ``presence_unknown`` (memory paused or the timeline not running), ``away`` (no
    presence for ``presence_min``, as observe), ``settling`` (back, not yet ``break_min`` of presence), and
    ``card_showing`` (a card the UI shows, within the card gap)."""
    if not you:
        return ["unknown"]
    try:
        if now - float(you["beat_at"]) > STALE_S:
            return ["unknown"]
    except (KeyError, TypeError, ValueError):
        return ["unknown"]
    out = focus_holds(you.get("focus"), now)
    pres = you.get("presence") or {}
    state = pres.get("state")
    if state == "locked":
        out.append("locked")
    elif state not in ("present", "away"):
        out.append("presence_unknown")
    else:
        away_at = pres.get("away_at")
        if state != "present" and (away_at is None or now >= away_at):
            out.append("away")
        elif pres.get("settled_at") is None or now < pres["settled_at"]:
            out.append("settling")
    showing = (you.get("cards") or {}).get("showing")
    if showing and now < float(showing.get("until") or 0.0):
        out.append("card_showing")
    return out


def presence_now(you: Any, now: float) -> dict[str, Any]:
    """Durations computed at read time from the record's instants: ``present``, ``away`` (as :func:`holds_now`),
    ``settled``, ``at_pc_s`` (None while away), ``idle_s`` (None while present), ``today_s``."""
    pres = (you or {}).get("presence") or {}
    state = pres.get("state")
    present = state == "present"
    away_at = pres.get("away_at")
    away = not present and (away_at is None or now >= away_at)
    since = pres.get("at_pc_since")
    settled_at = pres.get("settled_at")
    today = pres.get("today_at_pc") or {}
    live = today.get("since")
    return {"present": present, "away": away,
            "settled": bool(not away and settled_at is not None and now >= settled_at),
            "at_pc_s": round(now - since, 1) if since is not None and not away else None,
            "idle_s": round(now - pres["idle_since"], 1) if not present and pres.get("idle_since") else None,
            "today_s": round(float(today.get("base_s") or 0.0) + (now - live if live is not None else 0.0), 1)}


def _freeze(value: Any) -> Any:
    if isinstance(value, (dict, MappingProxyType)):
        return MappingProxyType({k: _freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(v) for v in value)
    return value


def thaw(value: Any) -> Any:
    """A frozen record (or part) as plain dicts and lists (JSON)."""
    if isinstance(value, (dict, MappingProxyType)):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value


def _size(value: Any) -> int:
    return len(json.dumps(thaw(value), ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8"))


def _text(value: Any, cap: int) -> str | None:
    """Whitespace squashed and clipped (None for nothing)."""
    s = " ".join(str(value or "").split())
    if not s:
        return None
    return s if len(s) <= cap else s[:cap - 1].rstrip() + "…"


def _r(value: float | None, nd: int = 1) -> float | None:
    return None if value is None else round(float(value), nd)


def _num(value: Any) -> float | int | None:
    """A number the judge published (anything else: None)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if isinstance(value, int) else round(float(value), 1)


#: Which lists give way first when a section is over its cap (index 0: the oldest; -1: the last).
_CLIP_ORDER: dict[str, tuple[tuple[tuple[str, ...], int], ...]] = {
    "presence": ((("days",), 0),),
    "changed": (((), 0),),
    "work": ((("plans",), -1), (("threads",), -1), (("goals",), -1), (("due",), -1)),
    "about": ((("careful",), -1),),
    "cards": ((("modes",), -1), (("today",), -1), (("reactions",), -1)),
    "usual": ((("today",), -1), (("now",), -1)),
    "prefs": ((("usuals",), -1),),
}


def _clip_section(name: str, value: Any, cap: int) -> tuple[Any, int]:
    """Drop list items (or dict entries) in :data:`_CLIP_ORDER` until the section fits ``cap`` bytes; then shorten its
    longest texts. Returns the section and how many things gave way (the builder logs ``you_clipped``)."""
    n = 0
    if _size(value) <= cap:
        return value, 0
    for path, index in _CLIP_ORDER.get(name, ()):
        while _size(value) > cap:
            target = value
            for key in path:
                target = target.get(key) if isinstance(target, dict) else None
            if isinstance(target, list) and target:
                target.pop(index)
            elif isinstance(target, dict) and target:
                target.pop(list(target)[index])
            else:
                break
            n += 1
    while _size(value) > cap:   # still over: the longest text in the section loses half
        best: tuple[int, Any, Any] | None = None
        stack = [value]
        while stack:
            node = stack.pop()
            items = node.items() if isinstance(node, dict) else enumerate(node) if isinstance(node, list) else ()
            for k, v in items:
                if isinstance(v, str) and (best is None or len(v) > best[0]):
                    best = (len(v), node, k)
                elif isinstance(v, (dict, list)):
                    stack.append(v)
        if best is None or best[0] < 8:
            break
        best[1][best[2]] = best[1][best[2]][: best[0] // 2] + "…"
        n += 1
    return value, n


class _LiveRow:
    """The open present stretch the timeline has not flushed yet (it writes every 30 s)."""

    state = "present"
    by_yuki = False
    fullscreen = False
    meeting = None
    process = None


@dataclass
class _LiveClip:
    start: float
    end: float
    row: Any = _LiveRow()

    @property
    def present_s(self) -> float:
        return max(0.0, self.end - self.start)


@dataclass(frozen=True)
class _Row:
    """The content-free columns of one recent timeline row (what focus reads)."""

    started_at: float
    ended_at: float
    state: str
    process: str | None
    host: str | None
    fullscreen: bool
    meeting: str | None
    by_yuki: bool


#: The last minutes of the timeline by the ``ended_at`` index (``ORDER BY started_at + 0`` keeps the planner off the
#: ``started_at`` index, which would walk the whole table: ~9 ms against ~0.1 ms on 12k rows).
_RECENT_SQL = ("SELECT started_at, ended_at, state, process, host, fullscreen, meeting, by_yuki FROM timeline"
               " WHERE ended_at > ? AND started_at < ? ORDER BY started_at + 0, id")


def recent_rows(store: Any, since: float, until: float) -> list[_Row]:
    """The timeline's rows that end after ``since`` (content-free; nothing decrypted). Read raw, without
    ``Store._apply_proof``: the proof only re-reads rows written before the live call-evidence gate (migration 27),
    and rows of the last minutes were all judged live, so for them it changes nothing."""
    return [_Row(float(r[0]), float(r[1]), str(r[2] or ""), r[3], r[4], bool(r[5]), r[6], bool(r[7]))
            for r in store._query(_RECENT_SQL, (since, until))]


def meeting_audio(process: str, render_fn: Callable[[], Any] | None = None) -> bool | None:
    """Whether the meeting's app (an app key) has an active audio render session now: ``NudgeWorker._meeting_held``'s
    read (session state only, ~7 ms). None when it could not be read."""
    if not process:
        return False
    try:
        sessions = render_fn() if render_fn is not None else None
        if sessions is None:
            from yuki.perception.audio import render_sessions

            sessions = render_sessions()
        import psutil

        for sess in sessions or []:
            if sess.get("state") != "active" or not sess.get("pid"):
                continue
            try:
                name = psutil.Process(int(sess["pid"])).name()
            except Exception:  # noqa: BLE001 - the process is gone
                continue
            if app_key(name) == process:
                return True
        return False
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


class YouBuilder:
    """Builds and publishes the record on the memory service's beat (no thread of its own).

    Args:
        store: the service's store (read only: existing readers and ``store._query``; nothing is written).
        privacy: the service's ``PrivacyConfig`` (``[you]`` and ``[nudges]`` are read from it).
        log: ``log(type, **fields)``, content-free.
        timeline_fn / gate_fn: the timeline recorder and the game gate, read late (either may be None or die).
        board: the Now board (only its ``version`` is read). hub: the moments hub (its counters are read).
        acting_now: whether Yuki was acting when this was built (the follower tells changes after that).
        live: False for the offline CLI: no audio read, no live timeline (its flags come from the newest row).
    """

    def __init__(
        self, store: Any, *, privacy: Any = None, log: Callable[..., Any] | None = None, db_path: Any = None,
        timeline_fn: Callable[[], Any] | None = None, gate_fn: Callable[[], Any] | None = None, board: Any = None,
        hub: Any = None, acting_now: bool = False, live: bool = True, render_fn: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.db_path = db_path
        #: This builder's start (wall clock, ms): with ``version`` the Now pipe's "unchanged" token, so a reader's copy
        #: from an earlier service process (whose version count started at 0 too) is never taken for the current one.
        self.epoch = round(time.time(), 3)
        self._log_fn = log
        self._clock = clock
        self._live = bool(live)
        self._timeline_fn = timeline_fn or (lambda: None)
        self._gate_fn = gate_fn or (lambda: None)
        self._board = board
        self._hub = hub
        self._render_fn = render_fn
        self._you_cfg = PrivacySection(privacy, "you", YouConfig.from_dict)
        self._nudge_cfg = PrivacySection(privacy, "nudges", NudgeConfig.from_dict)
        #: Set for the card judge on hold changes, breakpoints, reactions and its deadline (never on work/about).
        self.wake = threading.Event()
        self.last_wake: tuple[float, tuple[str, ...]] | None = None
        self._lock = threading.Lock()                 # guards what the change hooks set from other threads
        self._dirty: set[str] = set(SECTIONS)
        self._acting = bool(acting_now)
        self._front: tuple[str, float] | None = None  # (app key, since) of the user's own front app
        self._judge: dict[str, Any] = {}
        # beat state (the service loop's thread only)
        self._round: set[str] = set()
        self._breaks: list[str] = []
        self._sections: dict[str, Any] = {s: MappingProxyType({}) for s in SECTIONS}
        self._plain: dict[str, Any] = {}
        self._built: dict[str, Any] = {}
        self._built_frozen: Any = MappingProxyType({})
        self._version = 0
        self._you: Any = None
        self._on = True
        self._flags: dict[str, Any] | None = None
        self._flip_at: float | None = None            # when the timeline's flags turned present
        self._gap_at: float | None = None             # when the open stretch closed (no user window in front)
        self._gap_from_present = False                # ... and whether the user was present right before
        self._gap_first: float | None = None          # a gap seen on one beat (it counts once seen on the next)
        self._recheck_at: float | None = None         # presence re-read once the timeline's flush lands (a bridge)
        self._record_clips: dict[str, int] = {}       # section -> what gave way to the whole-record cap
        self._acting_end: float | None = None         # when Yuki last stopped acting
        self._acting_checked = False                  # acting re-read once on the first beat (a change before wiring)
        self._due: dict[str, float] = {}              # section -> the next instant it must be re-read
        self._beat_last: float | None = None
        self._fullscreen_end: float | None = None
        self._meeting_live_end: float | None = None
        self._game_left_at: float | None = None
        self._game_was: tuple[bool | None, str | None] = (None, None)
        self._gate_alive: bool | None = None
        self._fallback = False
        self._source: str | None = None
        self._audio_until: float | None = None
        self._audio_process = ""
        self._audio_at: float | None = None
        self._audio_held: bool | None = False
        self._call_was: tuple[bool, str | None] = (False, None)
        self._call_unknown_since: float | None = None
        self._sit_watch: float | None = None          # the stretch seen unsettled (its start)
        self._sit_pending: float | None = None        # its projected settled_at
        self._switch_told: float | None = None
        self._switch_key: str | None = None
        self._session_checked: float | None = None
        self._next_day_at: float | None = None
        self._days_for: str | None = None
        self._days: list[dict[str, Any]] = []
        self._synth: deque[dict[str, Any]] = deque(maxlen=64)
        self._message_seen: float | None = None
        self._hub_seen: tuple | None = None
        self._db_at = float("-inf")
        self._db_sig: tuple | None = None
        self._probe_seen: dict[str, Any] = {}
        self._reacted_seen: Any = ...           # max(reacted_at) last seen (... before the first probe)
        self._deadline_told: float | None = None
        self._holds: frozenset[str] | None = None
        # yUsual / yConsumers
        self._registered = False                      # live_builder() serves this one from its first beat
        self._labels: OrderedDict[str, str | None] = OrderedDict()   # app key -> display name (LRU, LABELS_KEPT)
        self._usual_open = False                      # a routine of usual.now not yet done today (moments re-read it)
        self._usual_done: set[tuple[int, Any]] = set()   # (routine id, routines day) seen done (stays done that day)
        self._usual_words: OrderedDict[tuple[Any, ...], tuple[str | None, str | None]] = OrderedDict()   # (LRU)
        self._push: Any = None                        # the readers' named hold events (None: not made yet; False: none)
        self.pushes = 0
        # counters (content-free)
        self.beats = 0
        self.errors = 0
        self.db_checks = 0
        self.probes = 0
        self.unchanged = 0
        self.beat_gaps = 0
        self.bridges = 0
        self.builds: Counter = Counter()
        self.wakes: Counter = Counter()
        self.clipped: Counter = Counter()
        self._clipped_last: dict[str, int] = {}
        self._beat_ms: deque[float] = deque(maxlen=BEAT_SAMPLES)

    # -- plumbing -----------------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _mark(self, *sections: str) -> None:
        with self._lock:
            self._dirty.update(sections)

    def snapshot(self) -> Any:
        """The current record (immutable; ``None`` while ``[you] enabled = false``). Readers check ``beat_at``."""
        return self._you

    # -- change hooks (any thread; cheap; never raise) ------------------------------

    def on_gate_change(self, decision: Any = None) -> None:
        """``GameGate.on_change``: the game in front, D3D full screen or the running set changed."""
        self._mark("focus")

    def on_call_change(self, source: str = "calls") -> None:
        """The call worker's change hook (a call or game voice chat detected or ended)."""
        self._mark("focus")

    def on_acting(self, acting: bool) -> None:
        """The acting follower's event: Yuki started or stopped acting for the user (presence adds no live part while
        Yuki acts, so it is re-read on both edges)."""
        with self._lock:
            if self._acting and not acting:
                self._acting_end = float(self._clock())
            self._acting = bool(acting)
            self._dirty.update(("focus", "presence"))

    def on_front(self, prev_key: str, new_key: str, *, at: float | None = None, by_yuki: bool = False,
                 **_facts: Any) -> None:
        """``MomentHub.subscribe_front``: another app came to the front (app keys only; Yuki's own fronts ignored)."""
        if by_yuki:
            return
        key = str(new_key or "")
        try:
            at = float(at) if at else float(self._clock())
        except (TypeError, ValueError):
            at = float(self._clock())
        with self._lock:
            if self._front is None or self._front[0] != key:
                self._front = (key, at)
            if self._fallback:   # a dead gate: the open stretch's program decides the game
                self._dirty.add("focus")

    def on_media(self, started: Sequence[Any], stopped: Sequence[Any], **_facts: Any) -> None:
        """``MomentHub.subscribe_media``: a media session started or stopped (titles are never read here)."""
        self._mark("changed")

    def judge_facts(self, **facts: Any) -> None:
        """The judge's own facts for ``cards`` (``modes``, ``digest_queued``, ``next_wake``, ``budget_left``), kept in
        RAM only (never a database write, so they never mark a section through the probe)."""
        with self._lock:
            for k in JUDGE_FACTS:
                if k in facts:
                    self._judge[k] = facts[k]
            self._dirty.add("cards")

    # -- the beat -------------------------------------------------------------------

    def beat(self, now: float | None = None) -> None:
        """Called from the service's 1 s loop (after ``moments.beat()``). Compares RAM values, rebuilds the sections
        whose inputs moved, publishes the record and wakes the judge on its events. Never raises."""
        t0 = time.perf_counter()
        try:
            self._beat(float(self._clock()) if now is None else float(now))
        except Exception as exc:  # noqa: BLE001 - the service loop must never stop here
            self.errors += 1
            self._log("error", where="younow.beat", error=type(exc).__name__)
        finally:
            self.beats += 1
            self._beat_ms.append((time.perf_counter() - t0) * 1000.0)

    def _beat(self, now: float) -> None:
        if not self._you_cfg.get().enabled:
            if self._on:
                self._on = False
                self._you = None
                self._beat_last = None   # turned off is no beat gap
                self._log("you_off", enabled=False)
            return
        if not self._on:
            self._on = True
            self._mark(*SECTIONS)
            self._log("you_off", enabled=True)
        if self._live and not self._registered:
            self._registered = True
            _register(self)   # the Now pipe's op "you" serves this builder from now on
        cfg = self._nudge_cfg.get()
        with self._lock:
            self._round, self._dirty = self._dirty, set()
            front = self._front
        self._breaks = []
        if self._beat_last is not None and now - self._beat_last > STALE_S:
            # the process was suspended (a sleep with no lock on wake) or the loop stalled: nothing seen meanwhile
            # (the timeline reopens its stretch with the same flags), so every section is read again
            self.beat_gaps += 1
            self._round.update(SECTIONS)
        self._beat_last = now
        if not self._acting_checked:
            self._acting_checked = True
            self._check_acting()
        self._watch_flags(now)
        self._watch_gate()
        self._watch_audio(now)
        self._watch_time(now, cfg, front)
        self._watch_hub(now)
        if now - self._db_at >= DB_EVERY_S:
            self._db_at = now
            self._watch_db(now)
        nw = self._judge.get("next_wake")
        if isinstance(nw, (int, float)) and now >= nw and self._deadline_told != nw:
            self._deadline_told = float(nw)
            self._breaks.append("deadline")
        version = self._version
        for name in SECTIONS:
            if name in self._round:
                self._rebuild(name, now, cfg)
        self._publish(now)
        if self._version != version:
            self._fit_record(now)
        holds = frozenset(holds_now(self._you, now))
        if holds != self._holds:
            self._holds = holds
            self._breaks.append("holds")
            self._push_holds()
        if self._breaks:
            for b in self._breaks:
                self.wakes[b] += 1
            self.last_wake = (now, tuple(self._breaks))
            self.wake.set()

    def _push_holds(self) -> None:
        """Tell the other processes' readers (§3.4 tray push) that the set of holds changed: one named auto-reset event
        per reader (``now.you_event_name``), set only here, never on a plain front change. Live builders only."""
        if not self._live or self._push is False:
            return
        if self._push is None:
            try:
                from yuki.memory.now import YOU_READERS, NamedEvent, you_event_name

                events = [NamedEvent(you_event_name(self.db_path, r)) for r in YOU_READERS]
                self._push = [e for e in events if e.handle] or False
            except Exception as exc:  # noqa: BLE001 - readers then poll their own pace
                self._log("error", where="younow.push", error=type(exc).__name__)
                self._push = False
            if self._push is False:
                return
        for event in self._push:
            event.set()
        self.pushes += 1

    def _check_acting(self) -> None:
        """Whether Yuki is acting, read once (the follower tells the changes; one before the wiring would be lost)."""
        if not self._live or self.db_path is None:
            return
        try:
            from yuki.memory import acting as _acting

            acting = _acting.current(self.db_path) is not None
        except Exception:  # noqa: BLE001 - the follower's next event decides
            return
        with self._lock:
            if acting == self._acting:
                return
            if self._acting:
                self._acting_end = float(self._clock())
            self._acting = acting
        self._round.update(("focus", "presence"))

    def _watch_flags(self, now: float) -> None:
        """The timeline's live flags (one attribute read): a change marks focus; a state change (the open stretch
        closing or opening included) marks presence."""
        flags = self._read_flags(now)
        old = self._flags
        gap = False
        if flags is not None:
            flags = {**flags, "stretch": flags.get("state") is not None}
            gap = not flags["stretch"] and not flags.get("blocked")
            if gap:
                # no open stretch (the desktop, the taskbar or a shell list in front): the timeline records nothing
                # and does no idle accounting, so presence is read from the stored rows alone (observe's view);
                # nothing full screen and no meeting in front (as live_state() read it)
                flags.update(fullscreen=False, meeting=False)
        if not gap:
            self._gap_first = None
        elif old is not None and old.get("stretch", True) and not old.get("blocked") and self._gap_first is None:
            # the timeline swaps stretches through "none open" (a window switch, a state flip, Yuki acting): a gap
            # counts once it is still there on the next beat, so a swap or a pass over the taskbar rebuilds nothing
            self._gap_first = now
            return
        if flags == old:
            return
        self._flags = flags
        self._round.add("focus")
        old = old or {}
        new = flags or {}
        if old.get("state") != new.get("state") or old.get("blocked") != new.get("blocked"):
            self._round.add("presence")
            if new.get("state") == "present" and not new.get("blocked"):
                self._flip_at = now
            if gap:
                self._gap_at = self._gap_first if self._gap_first is not None else now   # when it was first seen
                self._gap_from_present = old.get("state") == "present" and not old.get("blocked")
        if old.get("meeting") and not new.get("meeting"):
            self._meeting_live_end = now
        if old.get("fullscreen") and not new.get("fullscreen"):
            self._fullscreen_end = now

    def _read_flags(self, now: float) -> dict[str, Any] | None:
        if not self._live:
            return self._flags if self._flags is not None else self._flags_from_rows(now)
        tl = self._timeline_fn()
        if tl is None or not getattr(tl, "alive", False):
            return None
        try:
            return dict(tl.flags())
        except Exception:  # noqa: BLE001
            return None

    def _flags_from_rows(self, now: float) -> dict[str, Any]:
        """Offline (the CLI): the newest stored row stands for the open stretch when it is still going; nothing going:
        away (nothing in front of the user is being recorded), so the stored presence decides "away" after
        ``presence_min``, as observe did."""
        try:
            rows = recent_rows(self.store, now - ONGOING_S, now)
        except Exception:  # noqa: BLE001
            rows = []
        last = rows[-1] if rows else None
        if last is None or last.ended_at < now - ONGOING_S or last.state not in ("present", "away"):
            return {"state": "away", "blocked": None, "fullscreen": False, "meeting": False}
        return {"state": last.state, "blocked": None, "fullscreen": bool(last.fullscreen),
                "meeting": bool(last.meeting)}

    def _watch_gate(self) -> None:
        gate = self._gate_fn()
        alive = bool(gate is not None and getattr(gate, "alive", False))
        if alive != self._gate_alive:
            self._gate_alive = alive
            self._round.add("focus")

    def _watch_audio(self, now: float) -> None:
        """The meeting's audio, only while a meeting row is within ``meeting_hold_min`` (at most every 15 s). The
        window's end re-reads focus once (``_meeting`` then clears it), so ``audio_held`` ends with the window."""
        if not self._live or self._audio_until is None:
            return
        if now >= self._audio_until:
            self._audio_until = None   # once: _meeting re-reads the rows (a newer meeting row opens a new window)
            self._round.add("focus")
            return
        if self._audio_at is not None and now - self._audio_at < MEETING_AUDIO_EVERY_S:
            return
        self._audio_at = now
        held = meeting_audio(self._audio_process, self._render_fn)
        if held != self._audio_held:
            self._audio_held = held
            self._round.add("focus")

    def _watch_time(self, now: float, cfg: NudgeConfig, front: tuple[str, float] | None) -> None:
        """Instants reached (timestamp compares): a day boundary, an instant a section asked for (``_due``: a time
        window of ``work`` or ``changed`` moving, the second after a return), the stretch's projected settle, a front
        app held long enough for a ``switch`` or for a new session."""
        if self._next_day_at is not None and now >= self._next_day_at:
            self._round.update(("presence", "cards"))
        for name, at in list(self._due.items()):
            if now >= at:
                del self._due[name]
                self._round.add(name)
        if self._sit_pending is not None and now >= self._sit_pending:
            self._sit_pending = None
            self._round.add("presence")
        if front is None:
            return
        key, since = front
        if self._switch_told != since and now - since >= SWITCH_HELD_S:
            self._switch_told = since
            if key != self._switch_key:
                self._switch_key = key
                self._add_synth("switch", since, app=key or None)
        if self._session_checked != since and now - since >= cfg.session_excursion_min * 60.0:
            self._session_checked = since
            session = (self._plain.get("presence") or {}).get("session") or {}
            if key and session.get("key") != f"app:{key}":
                self._round.add("presence")

    def _watch_hub(self, now: float) -> None:
        hub = self._hub
        if hub is None:
            return
        try:
            sig = (int(hub.writer.written), sum(v for k, v in dict(hub.by_kind).items() if k != "page"))
        except Exception:  # noqa: BLE001
            return
        if sig != self._hub_seen:
            self._hub_seen = sig
            self._round.add("changed")
            if self._usual_open:
                # a routine due now, not done yet: this moment may be the user doing it; read again shortly (at most
                # every USUAL_MOMENT_S: a page routine's done check walks the day's page moments)
                self._due_at("usual", max(now, (self._built.get("usual") or {}).get("at") or 0.0) + USUAL_MOMENT_S)

    def _watch_db(self, now: float) -> None:
        """``total_changes()`` + ``data_version`` (one query); when either moved, one probe query names the sections
        whose source tables changed. The judge's ``nudge_state`` keys are never probed."""
        try:
            row = self.store._query("SELECT total_changes(), (SELECT data_version FROM pragma_data_version())")
            sig = tuple(row[0]) if row else None
        except Exception:  # noqa: BLE001
            self.errors += 1
            return
        self.db_checks += 1
        if sig == self._db_sig:
            return
        self._db_sig = sig
        self.probes += 1
        try:
            p = tuple(self.store._query(_PROBE_SQL)[0])
        except Exception as exc:  # noqa: BLE001 - an older database: rebuild them all, at most once a minute, said once
            failed = self._probe_seen.get("error")
            if failed is None:
                self._log("error", where="younow.probe", error=type(exc).__name__)
            if failed is None or now - failed >= 60.0:
                self._probe_seen["error"] = now
                self._round.update(DB_SECTIONS)
            self._db_sig = None   # asked again at the next check
            return
        probes = {"focus": p[0], "work": p[1:8], "cards": p[8:10], "about": p[10], "usual": p[11:14], "prefs": p[14]}
        for section, value in probes.items():
            if self._probe_seen.get(section, ...) != value:
                self._probe_seen[section] = value
                self._round.add(section)
        if p[9] != self._reacted_seen:
            if self._reacted_seen is not ...:
                self._breaks.append("reaction")
            self._reacted_seen = p[9]

    def _add_synth(self, kind: str, at: float, *, app: str | None = None) -> None:
        """A transition this module saw itself: kept for ``changed`` and a breakpoint for the judge."""
        self._synth.append({"kind": kind, "synth": True, "at": _r(at, 0), "app": app, "entity": None, "by": "user"})
        self._round.add("changed")
        self._breaks.append(kind)

    # -- rebuilding -------------------------------------------------------------------

    def _label(self, key: Any) -> str | None:
        """An app key's display name for the renderers ("Zen" for ``zen``, "VALORANT" for its stem): the name the
        user's own timeline rows show it under most (``store.app_display_name``), else the key tidied
        (``appnames.prettify``, structural). Read once per key per run (the cache is bounded, least recently used out
        first, so the keys of every beat stay)."""
        key = str(key or "").strip()
        if not key:
            return None
        if key in self._labels:
            self._labels.move_to_end(key)
            return self._labels[key]
        try:
            name = self.store.app_display_name(key)
        except Exception:  # noqa: BLE001
            name = None
        if not name:
            try:
                from yuki.core.appnames import prettify

                name = prettify(key)
            except Exception:  # noqa: BLE001
                name = key
        while len(self._labels) >= LABELS_KEPT:
            self._labels.popitem(last=False)
        self._labels[key] = _text(name, LABEL_CHARS)
        return self._labels[key]

    def _due_at(self, name: str, at: float | None) -> None:
        """Ask for ``name`` to be re-read at the instant ``at`` (the earliest asked wins until it is rebuilt)."""
        if at is None:
            return
        cur = self._due.get(name)
        if cur is None or at < cur:
            self._due[name] = float(at)

    def _rebuild(self, name: str, now: float, cfg: NudgeConfig) -> None:
        t0 = time.perf_counter()
        known = True
        self._due.pop(name, None)   # the builder asks again for its next instant
        try:
            value = getattr(self, f"_build_{name}")(now, cfg)
            if isinstance(value, tuple):
                value, known = value
        except Exception as exc:  # noqa: BLE001 - the section keeps its last value, marked not known
            self.errors += 1
            self._log("error", where=f"younow.{name}", error=type(exc).__name__)
            value, known = None, False
        ms = round((time.perf_counter() - t0) * 1000.0, 2)
        self.builds[name] += 1
        if value is not None:
            value, n = _clip_section(name, value, SECTION_CAPS.get(name, RECORD_CAP))
            if n:
                self.clipped[name] += n
            if n != self._clipped_last.get(name, 0):   # said when it starts, changes or stops, not on every rebuild
                self._clipped_last[name] = n
                self._log("you_clipped", section=name, n=n)
            if value != self._plain.get(name):
                self._version += 1
                self._record_clips.pop(name, None)   # a fresh value: the whole-record cap is checked again
                self._plain[name] = value
                self._sections[name] = _freeze(value)
                v = self._version
                self._log("you_built", section=name, version=v, ms=ms, bytes=_size(value))
            else:
                self.unchanged += 1
                v = (self._built.get(name) or {}).get("v", self._version)
        else:
            v = (self._built.get(name) or {}).get("v", 0)
        known = bool(known and value is not None)
        prev = self._built.get(name)
        if prev is not None and bool(prev.get("known")) != known:
            # readers trust built[*].known (api.day_context): a flip is a change, so the Now pipe's "unchanged"
            # answer never hands back a copy with the old flag (at / ms move on every rebuild and are not readers')
            self._version += 1
            v = self._version
        self._built[name] = {"at": _r(now), "v": v, "ms": ms, "known": known}
        self._built_frozen = _freeze(self._built)

    def _publish(self, now: float) -> None:
        """A new top-level mapping each beat (``beat_at``, the board's version), the sections by reference."""
        board = self._board
        you = {"v": SCHEMA_V, "version": self._version, "beat_at": _r(now, 3),
               "board_version": getattr(board, "version", None) if board is not None else None,
               "built": self._built_frozen, **self._sections}
        self._you = MappingProxyType(you)

    def _fit_record(self, now: float) -> None:
        """The whole published record within :data:`RECORD_CAP` (the section caps add up to about that before
        ``built`` and the top level): when over, sections give way by their own clip rules, ``work`` first. Run only
        on a beat that changed a section; ``you_clipped(section="record")`` (what gave way, in the sections as
        published) is said when it starts, changes or stops."""
        for name in ("work", "about", "changed", "presence", "usual", "prefs", "cards", "focus"):
            over = _size(self._you) - RECORD_CAP
            if over <= 0:
                break
            plain = self._plain.get(name)
            if not plain:
                continue
            value, k = _clip_section(name, thaw(plain), max(2, _size(plain) - over))
            if k:
                self._record_clips[name] = self._record_clips.get(name, 0) + k
                self.clipped["record"] += k
                self._version += 1
                self._plain[name] = value
                self._sections[name] = _freeze(value)
                self._built[name] = {**(self._built.get(name) or {}), "v": self._version}
                self._built_frozen = _freeze(self._built)
                self._publish(now)
        n = sum(self._record_clips.values())
        if n != self._clipped_last.get("record", 0):
            self._clipped_last["record"] = n
            self._log("you_clipped", section="record", n=n)

    # -- focus --------------------------------------------------------------------------

    def _build_focus(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        hold_s = cfg.meeting_hold_min * 60.0
        window = max(hold_s, MEETING_GAP_S, cfg.return_hold_s) + 60.0
        rows = recent_rows(self.store, now - window, now)   # every row: Yuki's own count for the return hold
        last = rows[-1] if rows else None
        ongoing = last is not None and last.ended_at >= now - ONGOING_S
        with self._lock:
            front_key = self._front[0] if self._front is not None else None

        def fallback() -> bool:   # a dead gate: the program in front (the hub's front event, else the newest row)
            return bool(_game_process(front_key)) if front_key else bool(ongoing and game_row(last))

        game = focus_game(self._gate_fn(), fallback=fallback)
        # the game's display name for the renderers ("in VALORANT"); the exclusive full-screen "d3d" has none
        game["label"] = self._label(game["name"]) if game.get("name") and game["name"] != "d3d" else None
        self._fallback = game["source"] != "gate"
        if game["source"] != self._source:
            if self._source is not None:
                self._log("you_source", part="game", source=game["source"], was=self._source)
            self._source = game["source"]
        was_front, was_name = self._game_was
        if was_front and game["front"] is False:
            self._game_left_at = now
            self._add_synth("game_end", now, app=was_name or None)
        self._game_was = (game["front"], game["name"] or was_name if game["front"] else None)
        # the return hold: a full-screen or game stretch that ended less than return_hold_s ago (observe), or the
        # moment the gate / the timeline saw it end (the row may be up to a flush behind)
        ends = [r.ended_at for r in rows if (r.fullscreen or game_row(r)) and r.ended_at >= now - cfg.return_hold_s]
        ends += [x for x in (self._game_left_at, self._fullscreen_end) if x is not None]
        until = max(ends) + cfg.return_hold_s if ends else None
        game["return_hold_until"] = _r(until) if until is not None and until > now else None
        flags = self._flags
        if flags is not None and flags.get("fullscreen") is not None:
            fullscreen: bool | None = bool(flags["fullscreen"])   # the open stretch; one that just ended: return hold
        elif self._live:
            fullscreen = None   # no live timeline: not known (a hold)
        else:
            fullscreen = bool(ongoing and last.fullscreen) if rows else None
        meeting = self._meeting(now, rows, flags, hold_s)
        call, call_known = self._call(now)
        focus = {"game": game, "fullscreen": fullscreen, "meeting": meeting, "call": call,
                 "call_unknown_since": _r(self._call_unknown_since), "yuki_acting": self._acting}
        focus["holds"] = focus_holds(focus, now)
        return focus, bool(call_known and (game["front"] is not None) and fullscreen is not None)

    def _meeting(self, now: float, rows: Sequence[Any], flags: dict[str, Any] | None, hold_s: float) -> dict[str, Any]:
        live = bool(flags and flags.get("meeting"))
        meets = [r for r in rows if r.meeting and not r.by_yuki]
        ends = [max(r.ended_at for r in meets)] if meets else []
        if live:
            ends.append(now)
        if self._meeting_live_end is not None:
            ends.append(self._meeting_live_end)
        end = max(ends) if ends else None
        front_until = end + MEETING_GAP_S if end is not None and not live else None
        if end is not None and now - end < hold_s:
            recent = [r for r in meets if r.ended_at >= now - hold_s]
            latest = max(recent, key=lambda r: r.started_at) if recent else None
            process = app_key(latest.process or "") if latest is not None else ""
            if process != self._audio_process or self._audio_until is None:
                self._audio_at = None
            self._audio_process, self._audio_until = process, end + hold_s
            if self._live and process and (self._audio_at is None or now - self._audio_at >= MEETING_AUDIO_EVERY_S):
                self._audio_at = now
                self._audio_held = meeting_audio(process, self._render_fn)
            held = (self._audio_held if self._live else None) if process else False
        else:
            self._audio_until, self._audio_process, self._audio_held, self._audio_at = None, "", False, None
            held = False
        # front: the live flag only; a meeting row's tail is the timestamp front_until (compared when read, so the
        # hold ends with time without a rebuild)
        return {"front": live, "audio_held": held,
                "front_until": _r(front_until) if front_until is not None and front_until > now else None}

    def _call(self, now: float) -> tuple[dict[str, Any], bool]:
        try:
            calls = list(self.store.calls(statuses=CALL_OPEN_STATUSES))
        except Exception:  # noqa: BLE001 - unreadable: unknown (the policy fails closed on it)
            if self._call_unknown_since is None:
                self._call_unknown_since = now
                self._log("focus_unknown", source="call", state="start")
            return {"open": None, "kind": None, "since": None}, False
        if self._call_unknown_since is not None:
            self._log("focus_unknown", source="call", state="end", duration_s=round(now - self._call_unknown_since, 1))
            self._call_unknown_since = None
        call = next((c for c in calls if getattr(c, "kind", "call") != "game"), None)
        voice = next((c for c in calls if getattr(c, "kind", "call") == "game"), None)
        chosen = call or voice
        was_open, was_app = self._call_was
        if was_open and call is None:
            self._add_synth("call_end", now, app=was_app)
        self._call_was = (call is not None, (getattr(call, "process", None) or None) if call is not None else None)
        app = app_key(getattr(chosen, "process", None) or "") if chosen is not None else None
        # the call's app as the call worker named it ("Discord"), else the key's display name; the meeting service
        # when a meeting row named one (a product name, for "on a Meet call")
        label = _text(getattr(chosen, "app", None), LABEL_CHARS) if chosen is not None else None
        return {"open": chosen is not None, "kind": ("call" if call is not None else "game") if chosen else None,
                "since": _r(getattr(chosen, "detected_at", None)) if chosen is not None else None,
                "app": app, "label": label or (self._label(app) if app else None),
                "service": _text(getattr(chosen, "service", None), SERVICE_CHARS) if chosen is not None else None}, True

    # -- presence -----------------------------------------------------------------------

    def _build_presence(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        flags = self._flags
        stretch_open = bool(flags is not None and flags.get("stretch", True))
        if flags is None or flags.get("blocked") == "paused":
            state = "off"
        elif flags.get("blocked") == "locked":
            state = "locked"
        elif not stretch_open:
            state = "away"   # no user window in front: the stored rows alone (observe's view), away after presence_min
        elif flags.get("state") in ("present", "away"):
            state = flags["state"]
        else:
            state = "off"
        since = now - RESUME_SEARCH_S
        rows = wide_rows(self.store, now)
        clips: list[Any] = _clip(rows, since, now)
        tail_start = None                          # the live part (present, an open stretch): since then, going on
        closed: _LiveClip | None = None            # the stretch that just closed, up to when it closed
        with self._lock:
            acting, acting_end = self._acting, self._acting_end
        if not acting:   # while Yuki acts its rows are left out and nothing live is added (as observe)
            last_end = max((c.end for c in clips), default=None)
            marks = [x for x in (last_end, self._flip_at, acting_end) if x is not None]
            if state == "present":
                tail_start = min(max(marks), now) if marks else now
                if now - tail_start > ONGOING_S:
                    # never bridge more than a flush: the process was suspended (a sleep with no lock on wake) or the
                    # timeline stalled, so the live part starts now (observe saw the hole in the rows); read again
                    # once the timeline's next flush has landed (a stall's rows then fill the hole)
                    tail_start = self._flip_at = now
                    self._recheck_at = now + FLUSH_EVERY_S + 5.0
                    self.bridges += 1
                # the live part; one that begins now holds a sliver, so a new stretch is seen (unsettled) at once
                clips.append(_LiveClip(tail_start, max(now, tail_start + 0.01)))
            elif state == "away" and not stretch_open and self._gap_from_present and self._gap_at is not None:
                # the stretch closed while they were present; its last row may not have landed yet
                start = max(marks) if marks else self._gap_at
                if start < self._gap_at and self._gap_at - start <= ONGOING_S:
                    closed = _LiveClip(start, self._gap_at)
                    clips.append(closed)
        sessions = presence_sessions(clips, cfg)
        stretch = sessions[-1] if sessions else None
        brk = cfg.break_min * 60.0
        at_pc_since = settled_at = idle_since = None
        last_break = None
        if stretch is not None:
            at_pc_since = stretch.start
            settled_at = self._settled_at(clips, stretch, brk, now, live=tail_start is not None)
            prev = sessions[-2] if len(sessions) >= 2 else None
            b = _break_between(clips, prev.end if prev is not None else since, stretch.start)
            last_break = {"kind": b["kind"], "s": b["seconds"], "reaches_back": prev is None}
            if tail_start is None:
                idle_since = stretch.end
        began = day_began(clips, cfg, since=since, sessions=sessions)
        # time at the PC today: the stored part (and a just-closed stretch's unstored end), plus the live part since
        # tail_start
        day_start = cfg.resume_day_start
        today = user_day(now, day_start)
        start_of_day = day_boundary(today, day_start)
        base_s = sum(c.present_s for c in _clip(rows, start_of_day, now) if c.row.state == "present")
        if closed is not None:
            base_s += max(0.0, closed.end - max(closed.start, start_of_day))
        self._next_day_at = start_of_day + 86400.0
        if self._days_for != today:
            self._days_for = today
            self._days = [{"day": d.day, "at_pc_s": round(d.present_s, 1), "end": _r(d.last)}
                          for d in day_totals(self.store, now, DAYS_KEPT + 1, cfg) if d.day != today][-DAYS_KEPT:]
        session = self._session(rows, now, cfg)
        # the judge's sit_down: a stretch seen unsettled that has now settled (never one already settled at start)
        if stretch is not None and (settled_at is None or settled_at > now):
            self._sit_watch = stretch.start
            self._sit_pending = settled_at if tail_start is not None else None
        elif stretch is not None and self._sit_watch is not None and abs(self._sit_watch - stretch.start) < 60.0:
            self._sit_watch = self._sit_pending = None
            self._add_synth("sit_down", settled_at, app=None)
        else:
            self._sit_watch = self._sit_pending = None
        if self._switch_key is None and session is not None:
            self._switch_key = str(session["key"]).partition(":")[2] or None
        if self._recheck_at is not None:   # kept until reached (any rebuild before it asks again)
            if now >= self._recheck_at:
                self._recheck_at = None
            else:
                self._due_at("presence", self._recheck_at)
        presence = {
            "state": state, "stretch": stretch_open, "idle_since": _r(idle_since),
            "away_at": _r(idle_since + cfg.presence_min * 60.0) if idle_since is not None else None,
            "at_pc_since": _r(at_pc_since), "settled_at": _r(settled_at), "last_break": last_break,
            "day_began": _r(began[0]) if began else None, "day_gap_s": _r(began[1]) if began else None,
            "today_at_pc": {"base_s": round(base_s, 1), "since": _r(tail_start)},
            "days": list(self._days), "session": session}
        return presence, state != "off"

    @staticmethod
    def _settled_at(clips: Sequence[Any], stretch: Any, need: float, now: float, *, live: bool) -> float | None:
        """When the stretch held ``break_min`` of presence (interpolated in the clip that crossed it); while present
        and not there yet, when it will (projected); None when it never settled and they are away."""
        got = 0.0
        for c in clips:
            if c.end <= stretch.start or c.start > stretch.end or c.row.state != "present" or c.row.by_yuki:
                continue
            p = c.present_s
            if p <= 0:
                continue
            if got + p >= need:
                return c.start + (c.end - c.start) * max(0.0, need - got) / p
            got += p
        return now + (need - got) if live else None

    def _session(self, rows: Sequence[Any], now: float, cfg: NudgeConfig) -> dict[str, Any] | None:
        """The current activity session and the one before (``activity_sessions``: app runs, short hops folded in)."""
        sessions = activity_sessions(rows, now - SESSION_LOOKBACK_S, now, cfg)
        if not sessions:
            return None
        cur = sessions[-1]
        prev = sessions[-2] if len(sessions) >= 2 else None
        return {"key": cur.key, "label": _text(cur.label, LABEL_CHARS), "start": _r(cur.start),
                "prev": None if prev is None else {"key": prev.key, "label": _text(prev.label, LABEL_CHARS),
                                                   "start": _r(prev.start), "end": _r(prev.end),
                                                   "present_s": round(prev.present_s, 1)}}

    # -- changed ------------------------------------------------------------------------

    def _build_changed(self, now: float, cfg: NudgeConfig) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        newest_message = None
        for m in self.store.moments_between(now - CHANGED_S, now + 1.0, kinds=CHANGED_KINDS):
            if m.get("kind") == "message_sent":
                newest_message = max(newest_message or 0.0, float(m["at"]))
            item = {"kind": m.get("kind"), "synth": False, "at": _r(m.get("at"), 0), "app": m.get("app") or None,
                    "entity": (m.get("entity_digest") or "")[:ENTITY_CHARS] or None, "by": m.get("by") or "user"}
            if m.get("service"):   # a calling site's product name (call_join, site_front): never a title
                item["service"] = _text(m["service"], SERVICE_CHARS)
            items.append(item)
        items += [dict(s) for s in self._synth if s["at"] is not None and s["at"] >= now - CHANGED_S]
        items.sort(key=lambda x: (x["at"] or 0.0))
        # the app's display name, for the thinker's and the talker's words: kept only when it says more than the key
        # tidied (the renderers fall back to that, _app_words), so the record's byte cap keeps more items
        from yuki.core.appnames import prettify

        for item in items[-CHANGED_MAX:]:
            if item.get("app"):
                label = self._label(item["app"])
                if label and label != _text(prettify(str(item["app"])) or str(item["app"]), LABEL_CHARS):
                    item["label"] = label
        if newest_message is not None:
            if self._message_seen is not None and newest_message > self._message_seen:
                self._breaks.append("message_sent")
            self._message_seen = max(newest_message, self._message_seen or 0.0)
        elif self._message_seen is None:
            self._message_seen = 0.0
        items = items[-CHANGED_MAX:]
        if items:   # the oldest kept item leaves the window then
            self._due_at("changed", min(x["at"] or now for x in items) + CHANGED_S)
        return items

    # -- work ---------------------------------------------------------------------------

    def _build_work(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        known = True
        threads: list[dict[str, Any]] = []
        thread_now = None
        try:
            active = list(self.store.work_threads(("active",), limit=40))

            def rank(t: Any) -> float:   # recency x importance
                age_h = max(0.0, now - float(t.last_seen or 0.0)) / 3600.0
                return float(t.importance or 5) / (1.0 + age_h / 24.0)

            def shape(t: Any) -> dict[str, Any]:
                # "stands", not "state": where the thread stands is the user's own text (never a machine word)
                return {"id": t.id, "title": _text(t.title, TITLE_CHARS), "stands": _text(t.shown or t.state, STATE_CHARS),
                        "next": _text(t.next, STATE_CHARS), "open_n": len(t.open_items or []), "last_at": _r(t.last_seen),
                        "importance": t.importance}

            if active:
                thread_now = shape(max(active, key=lambda t: float(t.last_seen or 0.0)))
            threads = [shape(t) for t in sorted(active, key=rank, reverse=True)[:THREADS_MAX]]
        except Exception:  # noqa: BLE001
            known = False
        goals: list[dict[str, Any]] = []
        try:
            for g in self.store.goals(("open", "unknown"), limit=GOALS_MAX):
                goals.append({"id": g.id, "text": _text(g.text, GOAL_CHARS), "scope": g.scope, "status": g.status,
                              "thread_id": g.work_thread_id, "due_at": _r(g.due_at),
                              "next_checkin_at": _r(g.next_checkin_at)})
        except Exception:  # noqa: BLE001
            known = False
        due: list[dict[str, Any]] = []
        list_n = 0
        turns: list[float] = []   # the instants a time window here changes (re-read then: _due)
        try:
            items = [i for i in todo_items(self.store, now=now) if i.status == "open"]
            list_n = len(items)
            for i in items:
                if i.due_at is not None:   # enters the 24 h window, turns overdue, leaves the overdue window
                    turns += [t for t in (i.due_at - DUE_AHEAD_S, i.due_at, i.due_at + OVERDUE_BACK_S) if t > now]
                if i.due_at is None or not now - OVERDUE_BACK_S <= i.due_at <= now + DUE_AHEAD_S:
                    continue
                due.append({"ref": i.ref, "title": _text(i.title, TITLE_CHARS), "due_at": _r(i.due_at),
                            "all_day": bool(i.all_day), "owner": i.owner, "overdue": i.due_at < now})
            due = sorted(due, key=lambda d: d["due_at"])[:DUE_MAX]
        except Exception:  # noqa: BLE001
            known = False
        board = {"replies_owed": 0, "waiting_on": 0, "coming_up_n": 0}
        try:
            from yuki.memory import workboard

            b = workboard.board(self.store, now, replies=False)
            board["waiting_on"] = sum(1 for d in (b.get("loops") or {}).values()
                                      if isinstance(d, dict) and d.get("kind") == "waiting")
            board["coming_up_n"] = len(b.get("meetings") or [])
            for m in b.get("meetings") or []:   # a meeting leaves "coming up" when it ends (the board's own rule)
                if isinstance(m, dict) and isinstance(m.get("at"), (int, float)):
                    turns.append(float(m["at"]) + (86400.0 if m.get("all_day") else 3600.0))
            board["replies_owed"] = sum(1 for it in workboard._items(self.store, "reply").values()
                                        if isinstance(it.get("data"), dict) and it["data"].get("needs_reply")
                                        and it.get("mark") != "no_reply")
        except Exception:  # noqa: BLE001
            known = False
        plans: list[dict[str, Any]] = []
        try:
            raw = self.store.nudge_state("presence_events")
            events = json.loads(self.store.cipher.decrypt(raw)) if raw else []
            for ev in events if isinstance(events, list) else []:
                at = ev.get("at") if isinstance(ev, dict) else None
                if not isinstance(at, (int, float)) or ev.get("asked"):
                    continue
                turns += [t for t in (at - PLANS_AHEAD_S, at) if t > now]   # enters the window, passes
                if not now <= at <= now + PLANS_AHEAD_S:
                    continue
                plans.append({"id": ev.get("id"), "at": _r(at), "title": _text(ev.get("what"), TITLE_CHARS)})
            plans = sorted(plans, key=lambda p: p["at"])[:PLANS_MAX]
        except Exception:  # noqa: BLE001
            known = False
        if turns:
            self._due_at("work", min(turns))
        return {"thread_now": thread_now, "threads": threads, "goals": goals, "due": due, "list_n": list_n,
                "board": board, "plans": plans}, known

    # -- usual / prefs (yUsual, after phase L) -------------------------------------------------

    def _build_usual(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        """The routines as they fall today and the catalog's counts (read-only imports of ``routines`` and the
        catalog; existing readers and ``store._query`` only). A routine is ``now`` while its time is within its hit
        width (``routines.HIT_WINDOW_MIN``) and ``today`` before that; one past its window has left both. Rebuilt on
        the probe (routines, skills, skill runs), at the next window edge or the routines' day boundary, and on a new
        moment while a ``now`` routine is not yet done."""
        from yuki.memory import routines as R

        known = True
        skills: tuple[Any, ...] = ()
        counts: dict[str, int] = {}
        try:
            skills = R.load_skills(self.store)   # the catalog on this store: one count query when unchanged
            counts = {"capabilities": sum(1 for s in skills if getattr(s, "origin", "") == "capability"),
                      "learned": sum(1 for s in skills if getattr(s, "origin", "") == "learned")}
            got = self.store._query("SELECT count(*) FROM skills WHERE origin = 'learned' AND state = 'trusted'"
                                    " AND deleted_at IS NULL AND retired_at IS NULL")
            counts["trusted"] = int(got[0][0] or 0) if got else 0
        except Exception:  # noqa: BLE001 - a database before migration 43, or no skill files
            known = False
        today = R.day_of(now)
        start = R.day_start(today)
        turns = [R.day_start(today + timedelta(days=1))]   # the routines' next 04:00 day
        width = R.HIT_WINDOW_MIN * 60.0
        now_items: list[dict[str, Any]] = []
        later: list[dict[str, Any]] = []
        try:
            rows = [r for r in self.store.routines(states=["active"])
                    if r.get("at_min") is not None and R.dayset_has(str(r.get("dayset")), today)]
        except Exception:  # noqa: BLE001 - no routines table yet
            rows, known = [], False
        rows.sort(key=lambda r: R.from_clock(float(r["at_min"])))
        names = self._skill_names([str(r.get("skill_id") or "") for r in rows]) if rows else {}
        for row in rows:
            centre = start + R.from_clock(float(row["at_min"])) * 60.0
            lo, hi = centre - width, centre + width
            if now > hi:
                continue   # past today
            turns += [t for t in (lo, hi) if t > now]
            bucket = now_items if now >= lo else later
            if len(bucket) >= (USUAL_NOW_MAX if bucket is now_items else USUAL_TODAY_MAX):
                continue
            name, what = self._usual_words_of(row, names, skills)
            done_key = (int(row["id"]), today)
            done = done_key in self._usual_done   # done stays done for the day: never read again
            if not done:
                try:
                    done = bool(R.done_today(self.store, row, now, skills=skills))
                except Exception:  # noqa: BLE001
                    done = False
                if done:
                    if len(self._usual_done) >= 64:
                        self._usual_done.clear()
                    self._usual_done.add(done_key)
            bucket.append({"routine_id": int(row["id"]), "skill_id": row.get("skill_id"), "name": name,
                           "what": what, "at_min": round(float(row["at_min"]), 1), "window": [_r(lo, 0), _r(hi, 0)],
                           "state": row.get("state"), "offer_mode": row.get("offer_mode") or "early",
                           "done_today": done})
        self._usual_open = any(not i["done_today"] for i in now_items)
        self._due_at("usual", min(turns))
        return {"now": now_items, "today": later, "skills": counts}, known

    def _skill_names(self, keys: Sequence[str]) -> dict[str, str]:
        """The learned names of these skill keys (decrypted in RAM). A nameless skill's code-built label (lName stores
        it as the name with ``provenance.named`` false) is never taken for a name, unless the user renamed it."""
        keys = [k for k in dict.fromkeys(keys) if k]
        if not keys:
            return {}
        out: dict[str, str] = {}
        try:
            rows = self.store._query(
                "SELECT key, name_ciphertext, provenance FROM skills WHERE origin = 'learned' AND deleted_at IS NULL"
                " AND key IN (%s)" % ",".join("?" * len(keys)), tuple(keys))
        except Exception:  # noqa: BLE001
            return {}
        for key, name_ct, prov_raw in rows:
            try:
                prov = json.loads(prov_raw) if prov_raw else {}
                name = " ".join(str(self.store._dec(name_ct) or "").split()) if name_ct else ""
            except Exception:  # noqa: BLE001
                continue
            if not isinstance(prov, dict):
                prov = {}
            if name and (prov.get("named") is not False or prov.get("named_by") == "user"):
                out[str(key)] = name
        return out

    def _usual_words_of(self, row: dict[str, Any], names: dict[str, str],
                        skills: Sequence[Any]) -> tuple[str | None, str | None]:
        """``(name, what)`` of a routine: the user's told label, else its learned skill's name; and what it is in
        plain words (``routines.KIND_PHRASES``: "join a Meet call in Zen"). Cached per routine row version and its
        skill's current name (a Playbook rename shows at the next rebuild, not at the next mining)."""
        learned = names.get(str(row.get("skill_id") or ""))
        key = (row.get("id"), row.get("updated_at"), learned)
        hit = self._usual_words.get(key)
        if hit is not None:
            self._usual_words.move_to_end(key)
            return hit
        from yuki.memory import routines as R

        try:
            label = self.store.routine_label(row)
        except Exception:  # noqa: BLE001
            label = None
        name = _text(label or learned, USUAL_NAME_CHARS)
        what = None
        try:
            kind, thing, app = R._things(self.store, row, None, skills)
            verb2 = R.KIND_PHRASES.get(kind, ("", "do this in {app}", ""))[1]
            what = _text(verb2.format(thing=thing or "it", app=app or "an app"), USUAL_WHAT_CHARS)
        except Exception:  # noqa: BLE001
            what = None
        while len(self._usual_words) >= 64:
            self._usual_words.popitem(last=False)
        self._usual_words[key] = (name, what)
        return name, what

    def _build_prefs(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        """``usuals``: per preference slot the option the choice rule picks when it is a confirmed usual (rule 1) or a
        clear observed choice (rule 2), confirmed first, ≤ :data:`PREFS_MAX`. Rule 3's guesses ("the most recent", said
        aloud when used) are left out, and so are taste-filled slots (their options are entity keys; taste is phase
        T's). Read-only (``prefs.choose``); rebuilt only when the preference rows change."""
        from yuki.skills.prefs import choose

        try:
            slots = self.store._query("SELECT DISTINCT slot, context FROM prefs WHERE ended_at IS NULL")
        except Exception:  # noqa: BLE001 - a database before migration 41
            return {"usuals": []}, False
        try:
            from yuki.skills.catalog import load

            caps = {s.key: s for s in load().skills if getattr(s, "origin", "") == "capability"}
        except Exception:  # noqa: BLE001
            caps = {}
        found: list[tuple[int, float, dict[str, Any]]] = []
        for slot, context in slots:
            slot, context = str(slot or ""), str(context or "")
            cap_key, _, rest = slot.partition(":")
            cap = caps.get(cap_key)
            spec = cap.slot(rest.partition("@")[0]) if cap is not None else None
            if spec is not None and getattr(spec, "taste", ""):
                continue
            choice = choose(self.store, slot, context=context, now=now)
            if choice is None or choice.source not in ("usual", "observed"):
                continue
            item = {"key": slot, "value": _text(choice.option, LABEL_CHARS),
                    "state": "confirmed" if choice.source == "usual" else "observed"}
            if context:
                item["context"] = _text(context, LABEL_CHARS)
            found.append((0 if choice.source == "usual" else 1, -float(choice.n or 0.0), item))
        found.sort(key=lambda f: (f[0], f[1], f[2]["key"]))
        return {"usuals": [f[2] for f in found[:PREFS_MAX]]}, True

    # -- about ----------------------------------------------------------------------------

    def _build_about(self, now: float, cfg: NudgeConfig) -> tuple[dict[str, Any], bool]:
        portrait = self.store.latest_portrait()
        brief = portrait.brief if portrait is not None and isinstance(portrait.brief, dict) else None
        if not brief:
            return {"brief_at": None, "with_him": None, "routine": None, "careful": [], "people_n": 0}, False
        careful = [c for c in (_text(x, CAREFUL_CHARS) for x in (brief.get("careful") or [])) if c][:CAREFUL_MAX]
        return {"brief_at": _r(portrait.at), "with_him": _text(brief.get("with_him"), WITH_HIM_CHARS),
                "routine": _text(brief.get("routine"), ROUTINE_CHARS), "careful": careful,
                "people_n": len([p for p in brief.get("people") or [] if isinstance(p, dict)])}, True

    # -- cards ----------------------------------------------------------------------------

    def _build_cards(self, now: float, cfg: NudgeConfig) -> dict[str, Any]:
        gap = cfg.card_gap_min * 60.0
        start_of_day = day_boundary(user_day(now, cfg.resume_day_start), cfg.resume_day_start)
        since = min(start_of_day, now - gap)
        rows = self.store._query("SELECT id, at, kind, trigger, shown_at, reaction FROM nudges WHERE at >= ? AND at < ?"
                                 " ORDER BY at, id", (since, now + 1.0))
        up = [r for r in rows if r["at"] >= now - gap and r["shown_at"] is not None and not r["reaction"]]
        showing = None
        if up:   # NudgeWorker._attention's card on screen: shown by the UI, no reaction, written within the card gap
            n = up[-1]
            showing = {"id": n["id"], "type": f"{n['kind']}/{n['trigger'] or ''}", "since": _r(n["shown_at"]),
                       "until": _r(float(n["at"]) + gap)}
        shown = [r for r in rows if r["shown_at"] is not None and r["shown_at"] >= start_of_day]
        newest = self.store._query("SELECT max(coalesce(shown_at, at)) FROM nudges")
        with self._lock:
            judge = dict(self._judge)
        modes = judge.get("modes")
        return {"showing": showing, "last_at": _r(newest[0][0]) if newest and newest[0][0] is not None else None,
                "today": dict(Counter(f"{r['kind']}/{r['trigger'] or ''}" for r in shown)),
                "reactions": dict(Counter(r["reaction"] for r in shown if r["reaction"])),
                "budget_left": _num(judge.get("budget_left")),
                "modes": {str(k)[:40]: str(v)[:8] for k, v in modes.items()} if isinstance(modes, dict) else {},
                "digest_queued": _num(judge.get("digest_queued")), "next_wake": _num(judge.get("next_wake"))}

    # -- stats ------------------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        """Content-free counters for the service's stats line: the beat's cost (p50 / p90 / max over the last ~10
        minutes), builds per section, wakes per reason, database checks and probes, the record's version and size."""
        ms = sorted(self._beat_ms)

        def pct(p: float) -> float | None:
            return round(ms[min(len(ms) - 1, int(p * len(ms)))], 3) if ms else None

        you = self._you
        try:
            size = _size({k: v for k, v in you.items()}) if you is not None else 0
        except Exception:  # noqa: BLE001
            size = 0
        return {"on": you is not None, "beats": self.beats,
                "beat_ms": {"p50": pct(0.5), "p90": pct(0.9), "max": pct(1.0)},
                "builds": dict(self.builds), "unchanged": self.unchanged, "wakes": dict(self.wakes),
                "db_checks": self.db_checks, "probes": self.probes, "version": self._version, "bytes": size,
                "clipped": dict(self.clipped), "errors": self.errors, "beat_gaps": self.beat_gaps,
                "bridges": self.bridges, "pushes": self.pushes,
                "game_source": self._source, "call_unknown": self._call_unknown_since is not None}


# ---------------------------------------------------------------------------
# Renderers (pure)
# ---------------------------------------------------------------------------


def _hm(at: float | None) -> str:
    return datetime.fromtimestamp(at).strftime("%H:%M") if at else "?"


def _dur(seconds: float | None) -> str:
    if seconds is None:
        return "?"
    m = int(max(0.0, seconds) // 60)
    return f"{m // 60}h{m % 60:02d}m" if m >= 60 else f"{m}m"


def _focus_words(you: Any, now: float) -> str:
    focus = you.get("focus") or {}
    held = set(holds_now(you, now))
    words = []
    game = focus.get("game") or {}
    if "game" in held:
        from yuki.memory.gamegate import game_name

        words.append(f"in {game_name(game['name'])}" if game.get("name") else "in a game")
    elif "return_hold" in held:
        words.append("just left a game or full screen")
    if "fullscreen" in held and "game" not in held:
        words.append("something full screen in front")
    call = focus.get("call") or {}
    if "call" in held:
        words.append(f"on a call since {_hm(call.get('since'))}")
    elif call.get("open") and call.get("kind") == "game":
        words.append("a game's voice chat open")
    if held & {"meeting", "meeting_audio"}:
        words.append("in a meeting")
    if "yuki_acting" in held:
        words.append("Yuki is acting for them")
    if "unknown" in held:
        return "not known (the record is stale)"
    if "focus_unknown" in held:
        return "not known (the focus facts are not built yet)"
    words += _unknown_words(held)
    return "; ".join(words) or "nothing holding"


def _render_cards(you: Any, now: float) -> list[tuple[int, str]]:
    """(priority, line): lower priority numbers survive the cap first."""
    lines: list[tuple[int, str]] = [(0, f"[You now, {_hm(now)}]"), (0, f"Focus: {_focus_words(you, now)}.")]
    pres = you.get("presence") or {}
    p = presence_now(you, now)
    parts = []
    if p["away"]:
        parts.append(f"away for {_dur(p['idle_s'])}" if p["idle_s"] is not None else "away")
    elif pres.get("at_pc_since"):
        parts.append(f"at the PC since {_hm(pres['at_pc_since'])} ({_dur(p['at_pc_s'])})"
                     + ("" if p["settled"] else ", just back"))
    lb = pres.get("last_break")
    if lb and lb.get("s"):
        parts.append(f"last break {_dur(lb['s'])} ({lb.get('kind')})")
    if pres.get("day_began"):
        parts.append(f"day began {_hm(pres['day_began'])} after {_dur(pres.get('day_gap_s'))} away")
    parts.append(f"today at the PC {_dur(p['today_s'])}")
    lines.append((1, "Presence: " + "; ".join(parts) + "."))
    s = pres.get("session")
    if s:
        text = f"Session: {s.get('label') or s.get('key')} since {_hm(s.get('start'))}"
        if s.get("prev"):
            pv = s["prev"]
            text += f"; before: {pv.get('label') or pv.get('key')} {_hm(pv.get('start'))}-{_hm(pv.get('end'))}"
        lines.append((2, text + "."))
    work = you.get("work") or {}
    tn = work.get("thread_now")
    if tn:
        lines.append((2, f"Thread now [W{tn['id']}]: {tn.get('title') or ''} - {tn.get('stands') or ''}"
                         + (f"; next: {tn['next']}" if tn.get("next") else "")))
    for t in work.get("threads") or []:
        if tn and t["id"] == tn["id"]:
            continue
        lines.append((5, f"Thread [W{t['id']}]: {t.get('title') or ''}" + (f"; next: {t['next']}" if t.get("next") else "")))
    for g in work.get("goals") or []:
        due = f" (due {datetime.fromtimestamp(g['due_at']):%a %H:%M})" if g.get("due_at") else ""
        lines.append((3, f"Goal [G{g['id']}] ({g.get('status')}): {g.get('text') or ''}{due}"))
    for d in work.get("due") or []:
        when = datetime.fromtimestamp(d["due_at"]).strftime("%a" if d.get("all_day") else "%a %H:%M")
        lines.append((3, f"Due [{d['ref']}]: {d.get('title') or '(untitled item)'}, {when}"
                         + (" (overdue)" if d["due_at"] < now else "")))   # from the instant, not the built flag
    b = work.get("board") or {}
    lines.append((4, f"Their list: {work.get('list_n', 0)} open; replies owed {b.get('replies_owed', 0)}, "
                     f"waiting on others {b.get('waiting_on', 0)}, coming up {b.get('coming_up_n', 0)}."))
    for pl in work.get("plans") or []:
        if pl["at"] < now:   # passed since the build
            continue
        lines.append((4, f"Plan [event:{pl.get('id')}]: {pl.get('title') or ''}, "
                         f"{datetime.fromtimestamp(pl['at']):%a %H:%M}"))
    about = you.get("about") or {}
    if about.get("with_him"):
        lines.append((3, f"How to be with them: {about['with_him']}"))
    if about.get("routine"):
        lines.append((4, f"Routine: {about['routine']}"))
    if about.get("careful"):
        lines.append((4, "Careful (private to them): " + "; ".join(about["careful"])))
    cards = you.get("cards") or {}
    today = cards.get("today") or {}
    if today:
        reacts = ", ".join(f"{k} {v}" for k, v in (cards.get("reactions") or {}).items())
        lines.append((4, "Cards today: " + ", ".join(f"{k} {v}" for k, v in today.items())
                         + (f"; reactions: {reacts}" if reacts else "") + "."))
    return lines


def _ago(seconds: float) -> str:
    s = max(0.0, float(seconds))
    if s < 90:
        return f"{int(s)} s ago"
    if s < 90 * 60:
        return f"{int(round(s / 60))} min ago"
    return f"{s / 3600:.1f} h ago"


def _app_words(item: Any) -> str:
    """An item's app as said: the display name the builder kept, else the key tidied (structural), else "an app"."""
    label = item.get("label")
    if label:
        return str(label)
    app = item.get("app")
    if app:
        from yuki.core.appnames import prettify

        return prettify(str(app)) or str(app)
    return "an app"


#: What each kind of change was, in plain words (the closed vocabulary of moments, D1, and this module's synthesised
#: transitions: machine codes to wording, like the routines' KIND_PHRASES; never matched against anyone's words).
CHANGE_WORDS: dict[str, str] = {
    "media_play": "{app} started playing", "media_context": "a playlist started in {app}",
    "volume_change": "{app}'s volume changed", "site_front": "{service} came to the front in {app}",
    "call_join": "joined {service} in {app}", "game_voice": "a game's voice chat started in {app}",
    "message_sent": "a message was sent in {app}", "call_end": "a call ended in {app}", "game_end": "left {app}",
    "sit_down": "sat down at the PC", "switch": "switched to {app}",
}
#: The ``{service}`` words when a moment named none.
CHANGE_SERVICE: dict[str, str] = {"site_front": "a call page", "call_join": "a call"}


def change_words(item: Any) -> str:
    """One ``changed`` item in plain words ("Spotify started playing"), ``(by Yuki)`` when Yuki's own run made it."""
    kind = str(item.get("kind") or "")
    pattern = CHANGE_WORDS.get(kind)
    if pattern is None:
        return ""
    text = pattern.format(app=_app_words(item), service=item.get("service") or CHANGE_SERVICE.get(kind, ""))
    return text + (" (by Yuki)" if item.get("by") == "yuki" else "")


def _newest_changes(you: Any, n: int, *, skip: tuple[str, ...] = ()) -> list[Any]:
    """The newest ``n`` items of ``changed`` that have words, newest first."""
    out = []
    for item in sorted(you.get("changed") or (), key=lambda x: -float(x.get("at") or 0.0)):
        if item.get("kind") in skip or not change_words(item):
            continue
        out.append(item)
        if len(out) >= n:
            break
    return out


def _call_words(call: Any) -> str:
    where = call.get("label") or (_app_words(call) if call.get("app") else "")
    service = call.get("service")
    if service and where and service.casefold() != str(where).casefold():
        return f"on a {service} call in {where}"
    return f"on a {service or where} call" if (service or where) else "on a call"


#: A focus fact that could not be read, in words for the thinker and the card writer: unknown counts as held (state
#: design §3.1, §11), so each says what to assume. Machine hold names to wording; the talker leaves them out.
UNKNOWN_WORDS: dict[str, str] = {
    "game_unknown": "whether a game is in front is not known: treat a game as in front",
    "fullscreen_unknown": "whether something is full screen is not known: treat it as full screen",
    "call_unknown": "call status not known: treat as on a call",
    "meeting_audio_unknown": "meeting audio not known: treat as in a meeting",
}


def _unknown_words(held: set[str]) -> list[str]:
    """The ``*_unknown`` focus holds in words, in :data:`UNKNOWN_WORDS` order."""
    return [words for name, words in UNKNOWN_WORDS.items() if name in held]


def _focus_phrase(you: Any, held: set[str], *, talker: bool = False) -> str:
    """The focus in words from the hold names (labels the builder kept: "in VALORANT", "on a Discord call"). The
    talker gets only a game, a call, a game's voice chat and a meeting."""
    focus = you.get("focus") or {}
    words = []
    game = focus.get("game") or {}
    if "game" in held:
        if game.get("label"):
            words.append(f"in {game['label']}")
        else:
            words.append("in a full-screen game" if game.get("name") == "d3d" else "in a game")
    elif "return_hold" in held and not talker:
        words.append("just left a game or full screen")
    if "fullscreen" in held and "game" not in held and not talker:
        words.append("something full screen in front")
    call = focus.get("call") or {}
    if "call" in held:
        words.append(_call_words(call) + ("" if talker else f" since {_hm(call.get('since'))}"))
    elif call.get("open") and call.get("kind") == "game":
        words.append(f"a game's voice chat open in {_app_words(call)}" if call.get("app") else
                     "a game's voice chat open")
    if held & {"meeting", "meeting_audio"} and "call" not in held:
        words.append("in a meeting")
    if "yuki_acting" in held and not talker:
        words.append("Yuki is acting for them")
    return "; ".join(words)


def _render_thinker(you: Any, now: float) -> list[tuple[int, str]]:
    """(priority, line) for the brain's request turn (§3.5): focus and the day, what just changed (for "that", "it",
    "the song"), the thread now and the top threads with next steps, due and overdue items, the usual now and the
    confirmed usuals. Ids in brackets are memory's own (``W`` threads, due refs)."""
    lines: list[tuple[int, str]] = []
    held = set(holds_now(you, now))
    if held & {"unknown", "focus_unknown"}:   # a stale record, or a focus section never built: fail closed
        try:
            age = (f" (its last beat was {_ago(now - float(you['beat_at']))})" if "unknown" in held
                   else " (the focus facts are not built yet)")
        except (KeyError, TypeError, ValueError):
            age = ""
        lines.append((0, f"Focus: not known just now{age}: treat a game or a call as in front until a look shows "
                         f"otherwise."))
    else:
        # "no game, call or meeting" only when no focus hold at all is present, the unknown ones included
        words = [w for w in (_focus_phrase(you, held),) if w] + _unknown_words(held)
        lines.append((0, f"Focus: {'; '.join(words) or 'no game, call or meeting'}."))
    pres = you.get("presence") or {}
    p = presence_now(you, now)
    parts = []
    if pres.get("day_began"):
        parts.append(f"their day at the PC began {_hm(pres['day_began'])} after {_dur(pres.get('day_gap_s'))} away "
                     f"(\"this morning\" and \"today\" mean since then)")
    if p["away"]:
        parts.append(f"away for {_dur(p['idle_s'])}" if p["idle_s"] is not None else "away")
    elif pres.get("at_pc_since"):
        parts.append(f"at the PC since {_hm(pres['at_pc_since'])}" + ("" if p["settled"] else ", just back"))
    if parts:
        lines.append((1, "Day: " + "; ".join(parts) + "."))
    changes = _newest_changes(you, THINKER_CHANGED)
    if changes:
        lines.append((2, "Just changed (newest first): " + "; ".join(
            f"{_hm(c.get('at'))} {change_words(c)} ({_ago(now - float(c.get('at') or now))})" for c in changes) + "."))
    work = you.get("work") or {}
    tn = work.get("thread_now")
    if tn:
        lines.append((2, f"Thread now [W{tn['id']}]: {tn.get('title') or ''}"
                         + (f" - {tn['stands']}" if tn.get("stands") else "")
                         + (f"; next: {tn['next']}" if tn.get("next") else "")))
    others = [t for t in work.get("threads") or () if not tn or t.get("id") != tn.get("id")][:THINKER_THREADS]
    for t in others:
        lines.append((4, f"Thread [W{t['id']}]: {t.get('title') or ''}" + (f"; next: {t['next']}" if t.get("next")
                                                                           else "")))
    for d in work.get("due") or ():
        when = datetime.fromtimestamp(d["due_at"]).strftime("%a" if d.get("all_day") else "%a %H:%M")
        lines.append((3, f"Due [{d['ref']}]: {d.get('title') or '(untitled item)'}, {when}"
                         + (" (overdue)" if d["due_at"] < now else "")))
    usual = you.get("usual") or {}
    for u in usual.get("now") or ():
        lines.append((3, "Usual now: " + _usual_words(u)
                         + ("; already done today" if u.get("done_today") else "; not done yet today") + "."))
    confirmed = [x for x in (you.get("prefs") or {}).get("usuals") or () if x.get("state") == "confirmed"]
    if confirmed:
        lines.append((5, "Their confirmed usuals: " + "; ".join(f"{x['key']} = {x.get('value')}" for x in confirmed)
                         + "."))
    later = list(usual.get("today") or ())
    if later:
        lines.append((6, "Usual later today: " + "; ".join(_usual_words(u) for u in later) + "."))
    return lines


def now_lines(you: Any, now: float | None = None, *, wide: bool = False) -> list[str]:
    """The you-card's "right now" (contract C1, ``yuki.memory.youcard``): the focus, the day and what just changed;
    ``wide`` (the planner's and the judge's card) adds the thread now. The thinker render's own lines (priority <= 2),
    content in RAM only. ``[]`` with no record."""
    if not you:
        return []
    now = time.time() if now is None else float(now)
    out = []
    for prio, text in _render_thinker(you, now):
        if prio > 2 or (not wide and text.startswith("Thread now")):
            continue
        out.append(text)
    return out


def _usual_words(u: Any) -> str:
    what = u.get("what") or "their usual"
    name = u.get("name")
    at = u.get("at_min")
    m = int(float(at) % 1440.0) if isinstance(at, (int, float)) else None   # a clock minute (routines.hm's form)
    when = f" around {m // 60:02d}:{m % 60:02d}" if m is not None else ""
    return (f"\"{name}\" ({what})" if name else what) + when


def _render_talker(you: Any, now: float) -> str:
    """The talker's one volatile line (§3.5): focus words and up to :data:`TALKER_CHANGED` just-changed items, as of
    the record's last beat (its cache is refreshed in the background); ``""`` when nothing holds and nothing changed,
    or when the record is older than :data:`TALKER_STALE_S`. Never to-dos, threads, news or the usual now."""
    try:
        beat_at = float(you["beat_at"])
    except (KeyError, TypeError, ValueError):
        return ""
    if now - beat_at > TALKER_STALE_S:
        return ""
    held = set(holds_now(you, min(now, beat_at)))   # the facts as of their beat; the cache said how fresh that is
    cap = RENDER_CAPS["talker"]
    focus = _focus_phrase(you, held, talker=True)
    head = f"Now: {focus}." if focus else ""
    items: list[str] = []
    for c in _newest_changes(you, TALKER_CHANGED, skip=("sit_down",)):
        item = f"{change_words(c)} {_ago(now - float(c.get('at') or now))}"
        tail = "Just now: " + "; ".join([*items, item]) + "."
        if len(f"{head} {tail}".strip()) > cap:
            break   # the older items give way
        items.append(item)
    tail = ("Just now: " + "; ".join(items) + ".") if items else ""
    return f"{head} {tail}".strip()


def render(you: Any, kind: str = "cards", *, now: float | None = None) -> str:
    """The record in words for one reader (content: RAM only, never logged), at most :data:`RENDER_CAPS` characters;
    lines give way by priority. ``cards``: the card writer's context (focus, presence and session, work, plans, about,
    today's cards). ``thinker``: the brain's request turn (focus and the day, just changed, thread now and top threads,
    due, the usual now and confirmed usuals; a stale record says "not known", fail closed). ``talker``: one line, focus
    words and up to three just-changed items (``""`` when there is nothing to say or the record is stale). ``""`` with
    no record."""
    if kind not in RENDER_CAPS:
        raise ValueError(f"kind must be one of {', '.join(RENDER_CAPS)}, not {kind!r}")
    if not you:
        return ""
    now = time.time() if now is None else float(now)
    cap = RENDER_CAPS[kind]
    if kind == "talker":
        text = _render_talker(you, now)
        return text if len(text) <= cap else text[: cap - 1].rstrip() + "…"
    lines = _render_thinker(you, now) if kind == "thinker" else _render_cards(you, now)
    order = sorted(range(len(lines)), key=lambda i: (lines[i][0], i))
    keep: set[int] = set()
    total = 0
    for i in order:
        text = lines[i][1]
        if total + len(text) + 1 > cap:
            continue
        keep.add(i)
        total += len(text) + 1
    return "\n".join(lines[i][1] for i in range(len(lines)) if i in keep)[:cap]


# ---------------------------------------------------------------------------
# CLI: python -m yuki.memory.younow --show [--content]
# ---------------------------------------------------------------------------

#: Strings at these paths are machine words (enums, ids, app keys, a game's stem): shown as they are by ``--show``.
#: Whitelisted by path, never by a bare key, so the user's own text under a like-named key is never printed.
_WORD_PATHS = frozenset({
    "presence.state", "presence.last_break.kind", "presence.session.key", "presence.session.prev.key",
    "focus.game.name", "focus.game.source", "focus.call.kind",
    "changed[].kind", "changed[].app", "changed[].by",
    "work.goals[].scope", "work.goals[].status", "work.due[].ref", "work.due[].owner",
    "cards.showing.type", "focus.call.app",
    "usual.now[].state", "usual.now[].offer_mode", "usual.today[].state", "usual.today[].offer_mode",
    "prefs.usuals[].key", "prefs.usuals[].state",
})


def content_free(value: Any, now: float, path: str = "") -> Any:
    """The record with every text replaced by its length (machine words at :data:`_WORD_PATHS` kept) and instants as
    seconds from ``now``."""
    if isinstance(value, (dict, MappingProxyType)):
        return {k: content_free(v, now, f"{path}.{k}" if path else str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [content_free(v, now, f"{path}[]") for v in value]
    if isinstance(value, str):
        return value if path in _WORD_PATHS and len(value) <= 40 else f"<{len(value)} chars>"
    if isinstance(value, float) and value > 1e9:
        return f"{value - now:+.0f} s"
    return value


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.younow", description=__doc__.split("\n")[0])
    parser.add_argument("--show", action="store_true", help="build the record once and print it (content-free)")
    parser.add_argument("--content", action="store_true", help="print it in full (to your own terminal only)")
    parser.add_argument("--db", default=None, help="database to copy and read (default: Yuki's own, read-only copy)")
    parser.add_argument("--privacy", default=None, help="privacy file (default: the packaged defaults)")
    parser.add_argument("--at", default=None, help="'now' for the build: an ISO local time, or 'last' (the newest row)")
    args = parser.parse_args(argv)
    if not args.show:
        parser.print_help()
        return 0
    from pathlib import Path

    from yuki.eval.dbcopy import copy_db, open_copy, remove_copy

    copy = copy_db(Path(args.db) if args.db else None)   # the source is opened ?mode=ro; the copy is deleted below
    store = None
    try:
        store = open_copy(copy)
        privacy = None
        if args.privacy:
            from yuki.memory.privacy import PrivacyConfig

            privacy = PrivacyConfig(Path(args.privacy))
        if args.at == "last":
            got = store._query("SELECT max(ended_at) FROM timeline")
            now = float(got[0][0]) if got and got[0][0] else time.time()
        elif args.at:
            now = datetime.fromisoformat(args.at).timestamp()
        else:
            now = time.time()
        builder = YouBuilder(store, privacy=privacy, live=False, clock=lambda: now)
        builder.beat(now)
        you = builder.snapshot()
        out: dict[str, Any] = {"at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                               "holds": holds_now(you, now), "presence_now": presence_now(you, now),
                               "build_ms": {k: v.get("ms") for k, v in (you or {}).get("built", {}).items()},
                               "stats": builder.stats()}
        if args.content:
            out["you"] = thaw(you)
            for kind in RENDER_CAPS:
                out[f"render_{kind}"] = render(you, kind, now=now)
        else:
            out["you"] = content_free(thaw(you), now)
            for kind in RENDER_CAPS:
                out[f"render_{kind}_chars"] = len(render(you, kind, now=now))
        sys.stdout.write(json.dumps(out, indent=1, ensure_ascii=False, default=str) + "\n")
    finally:
        if store is not None:
            store.close()
        remove_copy(copy)
    return 0


__all__ = [
    "SCHEMA_V", "STALE_S", "SECTIONS", "SECTION_CAPS", "RECORD_CAP", "RENDER_CAPS", "CHANGED_KINDS", "SYNTH_KINDS",
    "THINKER_SECTIONS", "TALKER_SECTIONS", "CHANGE_WORDS", "YouConfig", "YouBuilder", "focus_game", "focus_holds",
    "holds_now", "presence_now", "day_began", "day_began_from", "meeting_audio", "render", "change_words", "thaw",
    "content_free", "live_builder", "now_lines",
]


if __name__ == "__main__":
    raise SystemExit(main())
