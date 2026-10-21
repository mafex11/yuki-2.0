"""Yuki's read/write door into memory (docs/MEMORY.md, "Yuki integration").

Yuki (the agent and the UI) runs in its own process and opens its own
:class:`~yuki.memory.store.Store` on the same database file as the running
``yuki-memory`` service; WAL plus the store's busy timeout make that safe. It
never talks to the service directly: the service is steered with flag files next
to the database (``paused``, ``refresh_portrait``) and observed through its named
mutex and the database itself.

Nothing here calls a model. Embeddings (for ``recall``, know-how similarity and
rule matching) come from the memory service's embedder over its local pipe
(:mod:`yuki.memory.embed_ipc`); only while the service is down does a local
embedder load, lazily and in the background, in this process.

Usage::

    from yuki.memory.api import MemoryClient
    memory = MemoryClient.open()              # default %LOCALAPPDATA%\\Yuki\\memory\\memory.db
    memory.portrait_text()
    memory.recall("what did Kenji ask me", since="2026-09-20")    # facts and episodes
    memory.activity(since="2026-09-24", group_by="site")          # time per site, visits, stretches
    memory.episodes(since="2026-09-24")                           # what the user was doing, told

Conversation memory (Yuki's own exchanges; extracted by the service, yuki.memory.conversations)::

    memory.log_turn(session_id, request_id, at, user_text, reply_text, actions, outcome) -> int
    memory.standing_context() -> str                  # rules/preferences/feedback (<= 2,400) + commitments (<= 1,200)
    memory.resume_context(within_hours=6.0) -> str | None   # last session's summary + last 6 exchanges, <= 4,000
    memory.remember_rule(text, source_turn=None, supersedes=None) -> int   # immediate, the user's words;
                                                             # supersedes a near-duplicate (or the given id)
    memory.revoke_rule(ref) -> int                           # "rule:12" as standing_context shows it; 0 if not one
    (both ask the service for a debounced portrait re-render, for its Relationship section)
    memory.recall(...)                                       # also kind "chat" (turns) and "session" (summaries)

To-dos and the coach (the service's nudge worker, yuki.memory.nudges)::

    memory.todos(include_done=False) -> list[dict]      # open loops + commitments + the user's own to-dos
    memory.add_todo(text, due=None) -> "todo:N"
    memory.complete_todo(ref) -> id | None              # an id ("todo:3", "commit:5", "loop:12"), never text
    memory.reopen_todo(ref) -> id | None                # Undo of end_todo on "todo:N" within 10 min (the window)
    memory.pending_nudges() -> list[dict]               # unshown, oldest first; wait on Local\\YukiNudgeReady
    memory.recent_nudges(hours=12) -> list[dict]        # shown ones of the last N hours, with shown_at + reaction
    memory.ack_nudge(nudge_id, reaction)                # shown | dismissed | replied | snoozed
    memory.snooze_nudges(minutes);  memory.nudge_status() -> dict

The user's goals and Yuki's check-ins (migration 30, yuki.memory.checkins)::

    memory.goals(scope=None, statuses=("open", "unknown"), today=False) -> list[dict]
        # what the user is going for (today's, their work's), as their check-in answers said or inferred
    standing_parts()["groups"]["goal"]                  # today's goals in the commitments block of every request

Getting to know the user (yuki.memory.questions; asked on cards of kind "question")::

    memory.answer_question(ref, answer="", outcome="answered" | "skipped" | "never") -> dict
        # the user's own words -> a user-origin portrait correction naming the question; render flag
    memory.skip_question(ref, never=False) -> dict      # the card's Skip / "Don't ask"
    memory.questions(statuses=None, limit=50) -> list[dict]

The weekly review (written by the service, yuki.memory.review)::

    memory.weekly_review(which="latest") -> dict | None   # "latest", an ISO week "2026-W39" or a date in it
    memory.run_weekly_review()                             # flag file; the service writes one now (non-blocking)
    memory.recall(...)                                     # also kind "review"

Fast memory, provenance, Yuki's persona, standing requests (docs/MEMORY.md, "Fast memory")::

    memory.relevant(query, limit=4, embed_wait_s=None) -> list[dict]   # the few memories bearing on a message,
                                                   # each with its provenance line; the same vector as knowhow()
    memory.yuki_persona() -> str | None            # "how Yuki talks", for the agent's profile layer and the mouth
    memory.add_standing_request(condition, quote, request=None, repeat=False, days=None) -> "request:N"
    memory.standing_requests() -> list[dict];  memory.cancel_standing_request(ref) -> bool
    memory.memory_diary(limit=50) -> list[dict]    # what dreaming and the persona pass changed, newest first

Calls, both sides (the service's call worker, yuki.memory.calls; since 3 Oct 2026 the user's microphone plus
the call app's own sound - the other people - and a game's own voice chat as a session of its own; only the
microphone was kept from 24 Sept to 3 Oct)::

    memory.call_status() -> {"call": {...} | None, "recording": bool}   # wait on Local\\YukiCallReady
    memory.answer_call(call_id, "yes" | "no" | "always" | "never" | "none") -> bool
    memory.stop_call() -> int | None                     # "Stop remembering this call"
    memory.call_audio_waiting() -> dict | None           # "N calls waiting to be remembered"
    memory.erase_call_audio() -> int                     # "Erase waiting call audio" (every track)
    memory.call_prefs() -> list[dict];  memory.set_call_pref(app, "always" | "never" | None)

People and routes (migration 28, yuki.memory.contacts; SQL only, no model call)::

    memory.contact(ref, near=None) -> dict | None        # one person: routes ranked with counts and margin, their
                                                          # aliases and misheard names, the user's last messages each
    memory.people_directory() -> str                     # the compact people block (<= ~900 chars) for cached layers
    memory.prefer_route(ref, "route:N", words) -> dict   # the user's lasting route choice, in their own words
    memory.frame_facts(words, said=, screen=, apps=)     # what the task frame needs (yuki.agent.frame)
    memory.people_list() -> list[dict]                   # the window's Me page: routes, counts, "rhythm" (14 days)

The personal brief and personal-ness (yuki.memory.brief; written by the nightly portrait render, migration 31)::

    memory.brief() -> dict | None                        # with_him, people, own_bits, now, routine, careful
    memory.brief_for("thinker" | "talker" | "cards") -> str   # the slice for one consumer ("" before the first)
    memory.brief_slice(consumer) -> {"text", "ids", "at"}     # the same with its line ids (the personal-ness log)
    memory.record_surfaced(kind, ref, brief_ids, fact_ids, session_id=, at=, brief_at=) -> int
        # which brief lines and memory items a reply or card was written from (ids only; the nightly judge)
    memory.activity_header(since=None, until=None) -> dict    # threads touched, things finished, dates ahead

Practice memory (migration 29, yuki.memory.practice; its learners were retired by migration 45)::

    memory.record_run(run) -> dict                       # one agent request's step trace (the agent loop writes it)
    memory.record_hand_trace(session_id, name, args, ok, verified, report, ms, app=, target=, words=,
                             injected=, cited=, control=) -> dict
    memory.workflows() -> dict                           # practice ways for the run card's chips
    memory.workflow_edit(ref, "save" | "edit" | "disable" | "enable" | "forget", words="") -> dict
    memory.playbook() -> dict                            # the window's Playbook page (audit 17 Oct)
    memory.skill_edit("skill:N", action, words="") -> dict   # the Playbook's buttons (a way's Rename / Delete)
    memory.you_card(purpose="talker" | "planner" | "judge") -> str   # what Yuki knows about the user (contract C1)
    memory.run_ledger(since=None, until=None) -> list[dict]   # what Yuki did: requests and their acting steps

The 3 Oct feedback (migration 33; docs/research/revamp-2026-10-02/38 and 43-build-appA-memory.md). Direct ops, no model
on the click path; every text a list returns has a ``shown`` form said to the user (None until written)::

    memory.pin_fact("fact:N", on=True) -> {"ok", "ref", "pinned"}          # pin AND unpin (its own flag)
    memory.misheard(days=7, limit=8) -> {"items": [journal dict + "ref", "heard_in", "channel_known", "people"],
                                         "total"}
    memory.resolve_heard("journal:N", "right" | "wrong" | "undo", correction_id=None)
        -> {"ok", "ref", "verdict", "nature", "correction_id"}            # leaves misheard() at once
    memory.review_queue() -> {"items": [...], "cap": 5, "reviewed_today", "left_today", "waiting", "merged"}
    memory.review_mark("fact:N", "right" | "wrong" | "notmine" | "undo", correction_id=None) -> dict
    memory.day_connector(day=None) -> {"day", "label", "text", "memory", "open", "threads", "exchanges"} | None
    memory.chat_days(days=14) -> [{"day", "count", "first", "last"}];  memory.turns_on(day) -> [turn dict]
    memory.people_list() / memory.contact(ref)   # "usual" a plain line, "usual_route" its label object, one "summary"

What happened on calls and meetings (timeline digests written by the service, yuki.memory.calldigest)::

    memory.calls_in(since=None, until=None) -> list[dict]      # merged sessions: app, kind, times, each side's
                                                               # speech and tracks, gists
    memory.call_timeline(call, since=None, until=None) -> dict | None   # the session's blocks and dated lines,
                                                               # each line with who spoke (who, app, track_kind)
    memory.call_transcript(call, since=None, until=None, page=1, per_page=60, who="all") -> dict | None
                                                               # the exact words of every side, labelled
    memory.recall(...)            # a fact heard on a call with a digest carries "call": "call:<id>"
"""

from __future__ import annotations

import ctypes
import json
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory import secret_filter
from yuki.memory.provenance import describe as describe_source
from yuki.memory.store import (
    PAUSE_FLAG,
    REFRESH_FLAG,
    REVIEW_FLAG,
    STANDING_KINDS,
    ConversationFact,
    ConversationTurn,
    JournalEntry,
    Store,
    app_key,
    flag_path,
    request_render,
    service_mutex_name,
    signal_turns,
)

#: A standing request fires once, or (asked for every time) at most this many cards; cards of one
#: request at least this far apart; open for this many days unless the user says otherwise.
REQUEST_REPEAT_FIRES = 5
REQUEST_COOLDOWN_S = 30 * 60.0
REQUEST_DAYS = 14.0

#: Know-how for the same app whose embedding is at least this close to a new
#: entry is treated as the same procedure restated, and superseded by it.
KNOWHOW_DUPLICATE_COSINE = 0.85
#: A rule given with :meth:`MemoryClient.remember_rule` whose embedding is at
#: least this close to an active rule or preference supersedes it.
RULE_DUPLICATE_COSINE = 0.85
#: Hard budgets (characters, ~4 per token) of the blocks Yuki attaches to requests.
STANDING_CONTEXT_CHARS = 1_200
#: The user's rules, preferences and coach feedback have their own budget, apart from the commitments:
#: every one of them is the user's own instruction and must be on every request with the id forget_rule
#: takes. With one shared 1,200 cap, five long rules pushed the "don't nudge me" preference out, so when
#: the user said "you can nudge me again" (2026-09-26 22:15) Yuki had no id to withdraw it by.
STANDING_RULES_CHARS = 2_400
#: The user's own sites, accounts and links (conversation facts of kind resource, migration 22) have their own
#: budget: a handful of short lines, on every request, so "open my portfolio" needs no lookup.
STANDING_RESOURCES_CHARS = 700
#: The coach's state in the standing context (its own budget, after the capped part).
COACH_CONTEXT_CHARS = 600
#: The coach's state is shown when it looked this many times in the last COACH_QUIET_S and said nothing.
COACH_QUIET_S = 3 * 3600.0
COACH_QUIET_LOOKS = 4
RESUME_CONTEXT_CHARS = 4_000
RESUME_EXCHANGES = 6
#: When the last exchange is older than the resume window: the last day digest (and open promises) instead, at most
#: this long, from a day at most this many days back (migration 33, report 38 §9.3).
RESUME_FALLBACK_CHARS = 1_000
RESUME_FALLBACK_DAYS = 7
#: The "To review" queue (report 38 §12.3; the mock: at most 5 a day): facts written in the last this-many days,
#: "unsure" at or below this confidence; open loops are to-dos and are never reviewed here.
REVIEW_PER_DAY = 5
REVIEW_WITHIN_DAYS = 14
REVIEW_UNSURE = 0.6
REVIEW_SKIP_KINDS = ("open_loop",)
#: An Undo that came after the Wrong's correction pass started (review A3): the answer stands, the item stays gone.
TOO_LATE_TO_UNDO = "Too late to undo: Yuki is already updating memory from your answer."
#: "Maybe misheard" (report 38 §5): uncertain entries of the last this-many days, this many listed (newest first).
MISHEARD_DAYS = 7
MISHEARD_SHOWN = 8
#: The user's goals (migration 30, yuki.memory.checkins): today's and their work's, in the commitments block of every
#: request (their own budget, so they never push a to-do or a rule out, nor the other way round).
STANDING_GOALS_CHARS = 700
#: The parts of the standing context, in the order they are shown (:meth:`MemoryClient.standing_parts`).
STANDING_GROUPS: tuple[str, ...] = ("rule", "preference", "coach", "resource", "commitment", "todo", "goal")
#: The parts the one-string standing context (:meth:`MemoryClient.standing_context`, read by the voice talker
#: through ``yuki.agent.memory.split_standing``) carries: the goals reach the agent through the parts only.
STANDING_TEXT_GROUPS: tuple[str, ...] = ("rule", "preference", "coach", "resource", "commitment", "todo")
#: Which of them are the user's own instructions for how Yuki talks or behaves: the agent's cached rules layer.
STANDING_RULE_GROUPS: tuple[str, ...] = ("rule", "preference")
#: The heading above each part (a fixed format of memory's own; ``yuki.agent.memory.split_standing`` reads it).
STANDING_HEADINGS: dict[str, str] = {
    "rule": "Rules the user set for Yuki:",
    "preference": "The user's preferences about how Yuki talks or works:",
    # One Yuki (the user's requirement, 3 Oct 2026): the cards are Yuki's own, never a separate "coach".
    "coach": ("What the user said back to your own cards (feedback on that kind of card, not a rule for "
              "everything; forget_rule takes these ids too):"),
    "resource": ("The user's own sites, accounts and links (use them directly when the "
                 "user names one, e.g. \"open my portfolio\"):"),
    "commitment": "Yuki's own commitments (what Yuki promised or was asked to do later):",
    "todo": ("The user's own to-dos (their plans and work, not Yuki's: bring one up only when they ask about "
             "their plans, never offer to do one):"),
}
#: The goal part's heading (migration 30): kept apart from :data:`STANDING_HEADINGS`, whose set the one-string
#: context's reader mirrors (``yuki.agent.memory.STANDING_SECTION_HEADINGS``); the goals reach the agent through the
#: parts only, whose ``headings`` carry it.
STANDING_GOAL_HEADING = ("The user's goals - today's and their work's - as they told you on your check-ins, or as you "
                         "inferred (unknown: not confirmed by them); context for what they are going for, never a "
                         "list to read out:")
#: Short texts whose vectors :meth:`MemoryClient.similarity` keeps (a to-do list is a few dozen lines).
TEXT_VEC_CACHE = 256


def render_standing(parts: dict[str, Any], groups: tuple[str, ...] = STANDING_GROUPS) -> str:
    """The standing context text from :meth:`MemoryClient.standing_parts` (only ``groups``), with the coach's
    state last; ``""`` when there is nothing."""
    header = parts.get("header") or "[Standing rules and open commitments, from Yuki's conversations with the user]"
    headings = parts.get("headings") or {**STANDING_HEADINGS, "goal": STANDING_GOAL_HEADING}
    lines: list[str] = []
    for g in groups:
        rows = [str(item.get("line") or "") for item in (parts.get("groups") or {}).get(g) or [] if item.get("line")]
        if rows:
            lines += [headings.get(g, g), *rows]
    text = "\n".join([header, *lines]) if lines else ""
    coach = parts.get("coach_state")
    if coach:   # its own budget: why the coach is quiet is not a rule and never pushes one out
        text = (text or header) + "\n" + coach
    return text
#: Reciprocal-rank-fusion constant for merging vector and keyword hits.
_RRF_K = 60.0
_SYNCHRONIZE = 0x00100000
_SENTENCE_END = ".!?…。！？"
_CLOSERS = "\"')]”’」』"


def _clip_sentences(text: str | None, limit: int) -> str | None:
    """``text`` (whitespace collapsed) cut after its last whole sentence within ``limit``.

    Returns the whole text when it fits, ``None`` when even its first sentence
    does not: callers drop the item instead of cutting a sentence.
    """
    t = " ".join((text or "").split())
    if len(t) <= limit:
        return t
    best = 0
    for i in range(min(len(t), limit)):
        if t[i] not in _SENTENCE_END:
            continue
        end = i + 1
        while end < len(t) and t[end] in _CLOSERS:
            end += 1
        if end <= limit and (end == len(t) or t[end] == " " or t[i] in "。！？"):
            best = end
    return t[:best].rstrip() if best else None


def _day(at: float) -> str:
    return datetime.fromtimestamp(at).strftime("%Y-%m-%d")


def _ago(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h {minutes} min" if minutes else f"{hours} h"


def _iso(at: float | None) -> str | None:
    """Local time with offset, seconds precision."""
    if at is None:
        return None
    return datetime.fromtimestamp(float(at)).astimezone().isoformat(timespec="seconds")


def _time_arg(value: Any) -> float | None:
    """Epoch seconds from None / epoch number / datetime / date / ISO string (naive = local)."""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise TypeError("time argument must not be a bool")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, datetime):
        return value.timestamp()
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).timestamp()
    if isinstance(value, str):
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        return datetime.fromisoformat(text).timestamp()
    raise TypeError(f"unsupported time argument {value!r}")


def service_running(db_path: str | Path | None = None) -> bool:
    """Whether a ``yuki-memory`` service holds its mutex for this database (this session)."""
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    kernel32.OpenMutexW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
    handle = kernel32.OpenMutexW(_SYNCHRONIZE, False, service_mutex_name(db_path))
    if not handle:
        return False
    kernel32.CloseHandle(ctypes.c_void_p(handle))
    return True


#: ``call_transcript``'s ``who``: every line, only the user's (their microphone), or only the other people's (each
#: app's own sound: whoever spoke on the call, a game's teammates and its own voice lines).
TRANSCRIPT_WHO: tuple[str, ...] = ("all", "user", "others")


def _track_of(line: dict) -> tuple[str, str, str]:
    """Which recording a call line came from: the user's microphone, or one app's own sound."""
    if line.get("who") == "others":
        return ("others", str(line.get("app") or ""), str(line.get("track_kind") or ""))
    return ("user", "", "")


def transcript_lines(captures: list[dict], yuki_spans: Any = ()) -> list[dict]:
    """A call's captures (``Store.call_side_captures``) -> every line of every side in time order, as
    ``Store.call_lines`` labels them (``who`` "user" | "others", ``app``, ``track_kind``; ``heard_by`` on the user's
    measured lines; a line from before 3 Oct 2026 is the user's), each with ``repeat`` and ``to_yuki``.

    ``calldigest.prepare``'s rules, read over every side (report 61): a line stored twice (the same time, speaker and
    words) is kept once; one whose words equal one of the few kept before it on its own track within minutes (the
    recogniser looping on that recording's noise; another track saying the same words is no loop) is ``repeat``;
    ``to_yuki`` ("yes" | "maybe" | "") is only ever set on the user's lines - Yuki hears only the user's microphone.
    Identity and timings only."""
    from yuki.memory.calldigest import REPEAT_LOOKBACK, REPEAT_WITHIN_S, _same_words

    seen: set[tuple[float, str, str]] = set()
    items: list[dict] = []
    for cap in captures:
        for u in Store.call_lines(cap.get("data") if isinstance(cap, dict) else None):
            text = " ".join(str(u.get("text") or "").split())
            try:
                at = float(u.get("at"))
            except (TypeError, ValueError):
                continue
            key = (round(at, 2), u["who"], text)
            if not text or key in seen:
                continue
            seen.add(key)
            try:
                end = max(at, float(u.get("end") or at))
            except (TypeError, ValueError):
                end = at
            heard = u.get("heard_by")
            items.append({"at": at, "end": end, "text": text, "lang": str(u.get("lang") or ""),
                          "unclear": bool(u.get("unclear")), "who": u["who"], "app": u["app"],
                          "track_kind": u["track_kind"],
                          "heard_by": ([str(h) for h in heard] if u["who"] == "user" and isinstance(heard, list)
                                       else None),
                          "repeat": False, "to_yuki": ""})
    items.sort(key=lambda x: x["at"])
    kept: dict[tuple[str, str, str], list[dict]] = {}
    for x in items:
        if x["who"] == "user":
            hits = [sure for lo, hi, sure in yuki_spans if x["end"] >= lo and x["at"] <= hi]
            x["to_yuki"] = "yes" if any(hits) else "maybe" if hits else ""
        mine = kept.setdefault(_track_of(x), [])
        words = _same_words(x["text"])
        if any(_same_words(k["text"]) == words and x["at"] - k["end"] <= REPEAT_WITHIN_S
               for k in mine[-REPEAT_LOOKBACK:]):
            x["repeat"] = True
        else:
            mine.append(x)
    return items


def _timeline_line(line: dict, app: str) -> dict:
    """A digest line for the tools: every field the digest kept (read generically), its time as ISO, and who spoke
    (``who`` "user" | "others", ``app``, ``track_kind`` "call" | "game"; report 61's contract) - a line without them
    (every digest written before report 61's build) is the user's, on the row's app."""
    out = dict(line)
    out["at"] = _iso(line["at"])
    out["nature"] = line.get("nature", "fact")
    out["who"] = "others" if line.get("who") == "others" else "user"
    out["app"] = str(line.get("app") or app or "")
    out["track_kind"] = str(line.get("track_kind") or "call")
    return out


class MemoryClient:
    """Memory as Yuki sees it. Thread-safe (the store serialises access)."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self._embedder: Any = None
        self._embedder_lock = threading.Lock()
        self._app_names: dict[str, set[str]] = {}
        #: Why the last :meth:`log_turn` returned 0 (content-free), or ``None``.
        self.last_turn_error: str | None = None
        self._nudge_section: Any = None
        #: ``(query, vector)`` of the last message embedded: :meth:`knowhow` and :meth:`relevant` share it.
        self._query_vecs: dict[str, np.ndarray] = {}
        #: Vectors of short texts measured against a request's words (:meth:`similarity`: the to-dos).
        self._text_vecs: dict[str, np.ndarray] = {}
        self._relevant_index: Any = None
        self._relevant_lock = threading.Lock()

    @classmethod
    def open(cls, path: str | Path | None = None) -> "MemoryClient":
        """Open memory at ``path`` (default ``%LOCALAPPDATA%\\Yuki\\memory\\memory.db``).

        Opens this process's own Store on the file (creating it, its key and its
        schema if missing, and applying any pending additive migration); safe
        while the ``yuki-memory`` service has the same file open (WAL).
        """
        return cls(Store.open(path))

    def close(self) -> None:
        """Close the store."""
        self.store.close()

    def __enter__(self) -> "MemoryClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # -- helpers -----------------------------------------------------------

    def _flag(self, name: str) -> Path:
        return flag_path(self.store.path, name)

    @property
    def embedder(self) -> Any:
        """The :class:`~yuki.memory.embed_ipc.EmbedClient`: the memory service's embedder over its
        local pipe, else a local one loaded lazily (``yuki.memory.embed_ipc``)."""
        with self._embedder_lock:
            if self._embedder is None:
                from yuki.memory.embed_ipc import EmbedClient

                self._embedder = EmbedClient(self.store.cipher, self.store.path)
            return self._embedder

    # -- Now: the memory service's live picture of the screen (yuki.memory.now, 2026-09-29) -------------

    def now(self, wait_s: float = 0.05) -> dict[str, Any] | None:
        """What the memory service last read of every visible window, popup and toast, in RAM
        (:meth:`yuki.memory.now.NowBoard.snapshot`), or ``None`` when the service does not answer within
        ``wait_s``.  No model call, no database read."""
        from yuki.memory.now import shared_client

        client = shared_client(self.store.path)
        return client.now(wait_s=wait_s) if client is not None else None

    def now_text(self, *, foreground: int | None = None, wait_s: float = 0.05, **caps: Any) -> str:
        """:meth:`now` formatted by :func:`yuki.memory.now.format_now` (``caps``: its size limits); ``""``
        without an answer."""
        from yuki.memory.now import format_now

        return format_now(self.now(wait_s), foreground=foreground, **caps)

    def _embed(self, text: str, wait_s: float | None = None) -> np.ndarray | None:
        """Embedding of ``text``, or ``None`` if none could be had within ``wait_s`` (default: a long bound).

        A caller on a request's path passes a short ``wait_s`` and goes on without the vector
        rather than waiting for a model to load (the service's embedder is used when it runs).
        The last text's vector is kept, so the know-how and fast-memory lookups of one request
        embed its words once.
        """
        cached = self._query_vecs.get(text)
        if cached is not None:
            return cached
        try:
            vecs = self.embedder.embed([text], wait_s)
        except Exception:
            return None
        vec = None if vecs is None or len(vecs) == 0 else vecs[0]
        if vec is not None:
            if len(self._query_vecs) >= 8:   # the last few messages: the current one and its predecessor
                self._query_vecs.pop(next(iter(self._query_vecs)))
            self._query_vecs[text] = vec
        return vec

    def _journal_app_names(self, app: str) -> list[str]:
        """Display names the journal may carry for ``app`` (a display name or a process name).

        The name as given, its :func:`app_key` form ("spotify" matches the app
        "Spotify"), every name memory stored for that app before
        (:func:`yuki.core.appnames.aliases`: "WhatsApp.Root" for "WhatsApp"), and
        the name of any running process with that image name ("chrome.exe" ->
        "Google Chrome"; found names are cached). Journal facts are also matched
        through ``threads.process``.
        """
        from yuki.core import appnames

        key = app_key(app) or ""
        names = {app.strip(), key} | self._app_names.get(key, set())
        try:
            import psutil

            for proc in psutil.process_iter(["pid", "name", "exe"]):
                if app_key(proc.info.get("name")) == key and proc.info.get("exe"):
                    name = appnames.name_for_exe(proc.info["exe"], pid=proc.info.get("pid"),
                                                 image=proc.info.get("name") or "")
                    if name:
                        self._app_names.setdefault(key, set()).add(name)
                        names.add(name)
                        break
        except Exception:
            pass
        for name in list(names):
            names |= appnames.aliases(name)
        return sorted(n for n in names if n)

    # -- portrait ----------------------------------------------------------

    def portrait_text(self) -> str | None:
        """The latest rendered portrait (decrypted), or ``None`` if there is none yet.

        One page (at most ~1,500 tokens) written for Yuki about the user: work,
        interests, people and what is pending with them, routines, preferences,
        open loops. Rebuilt nightly/weekly by the service, and soon after
        :meth:`correct_portrait` or :meth:`refresh_portrait`.
        """
        portrait = self.store.latest_portrait()
        return portrait.text if portrait else None

    def portrait_sections(self) -> dict[str, str] | None:
        """The latest portrait's sections as fields (``work``, ``interests``, ``routines``, ``behaviour``, ``people``,
        ``preferences``, ``relationship``, ``personality``, ``open_loops``; migration 27), each written within its own
        budget (``yuki.memory.portrait.PORTRAIT_FIELDS``), or ``None`` (no portrait, or one rendered before the fields).
        For consumers that need one part, not the page."""
        portrait = self.store.latest_portrait()
        return dict(portrait.sections) if portrait and portrait.sections else None

    # -- the personal brief (revamp 09 P1, yuki.memory.brief) ---------------------------------------------------------

    def brief(self) -> dict | None:
        """The personal brief as stored (``with_him``, ``people``, ``own_bits``, ``now``, ``routine``, ``careful``),
        written by the nightly render with the portrait, or ``None`` before the first render that wrote one."""
        portrait = self.store.latest_portrait()
        return dict(portrait.brief) if portrait and portrait.brief else None

    def brief_slice(self, consumer: str = "thinker") -> dict:
        """The brief cut for one consumer (``thinker`` | ``talker`` | ``cards``): ``{"text", "ids", "at"}``, ``text``
        "" when there is no brief yet (``yuki.memory.brief.slice_for``). ``ids`` are the brief lines in it (what the
        personal-ness log records). Never raises for a known consumer."""
        from yuki.memory.brief import slice_for

        try:
            portrait = self.store.latest_portrait()
        except Exception:
            portrait = None
        brief = portrait.brief if portrait else None
        return slice_for(brief, consumer, at=portrait.at if portrait else None)

    def brief_for(self, consumer: str = "thinker") -> str:
        """The brief cut for one consumer, as a plain string ("" when there is none yet): ``thinker`` the whole
        brief (the agent's profile layer), ``talker`` the speaking style (tone and names), at most ~2,100
        characters (choosing for the user reads :meth:`you_card`), ``cards`` the whole brief as ABOUT THE USER
        lines."""
        return str(self.brief_slice(consumer).get("text") or "")

    def you_card(self, purpose: str = "talker") -> str:
        """The you-card (audit 17 Oct, contract C1; :func:`yuki.memory.youcard.build_you_card`): one compact,
        code-built page of what Yuki knows about the user for a decider - who they are, what they are doing right now,
        what they usually do at this hour, their things (sites and usual browser, channels, playlists and what they
        mean, people with app and heard names, voice channels, their own ways), open loops, a two-line recent past;
        each line says how sure. ``purpose``: ``talker`` (<= ~1,800 chars), ``planner`` or ``judge`` (<= ~3,000). No
        model call; cached in RAM. ``""`` on any failure; never raises."""
        try:
            from yuki.memory.youcard import build_you_card

            return build_you_card(self.store, purpose=str(purpose or "talker"))
        except Exception:  # noqa: BLE001
            return ""

    def record_surfaced(self, kind: str, ref: str, brief_ids: Any = (), fact_ids: Any = (), *,
                        session_id: str | None = None, at: float | None = None,
                        brief_at: float | None = None) -> int:
        """Record which brief lines and memory items one reply or card was written from (ids only; the nightly
        personal-ness judge reads them, ``yuki.memory.brief.PersonalJudge``). Returns the row id, 0 when it could
        not be kept. Never raises."""
        try:
            return self.store.add_surfaced(kind, ref, at=at, brief_ids=list(brief_ids or ()),
                                           fact_ids=list(fact_ids or ()), session_id=session_id, brief_at=brief_at)
        except Exception:
            return 0

    def correct_portrait(self, text: str) -> int:
        """Record a user-confirmed correction to the portrait; returns its fact id.

        ``text`` is the correction in plain words, as the user confirmed it
        ("The user left Corvana in August and now works at Acme"). It is
        stored as a portrait fact of kind ``correction`` with origin ``user`` and
        confidence 1.0. The next render follows it over anything inferred; the
        next portrait run folds it into the facts it concerns (superseding the
        contradicted ones) and retires it. Also asks the service for a render
        (:meth:`rerender_portrait`: the render run folds pending corrections into the facts
        first, one small call), not a full refresh: a refresh re-read the whole journal since the
        last run and audited again (2026-09-26: $0.34 + $0.71 for one correction). Yuki sees the
        correction at once anyway: fast memory embeds it on the next lookup.
        """
        text = secret_filter.redact_text((text or "").strip())
        if not text:
            raise ValueError("correction text is empty")
        fact_id = self.store.add_correction(text)
        self.rerender_portrait()
        return fact_id

    def refresh_portrait(self) -> None:
        """Ask the service to rebuild the portrait now (writes the ``refresh_portrait`` flag file).

        Non-blocking: returns at once. The service picks the flag up within a
        second or so while it runs (or at its next start), deletes it, runs the
        portrait worker over the journal since the last run and re-renders.
        """
        path = self._flag(REFRESH_FLAG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")

    def rerender_portrait(self) -> None:
        """Ask the service to re-render the portrait from the current facts (the ``render_portrait`` flag).

        Cheaper than :meth:`refresh_portrait`: no operations call over the
        journal, one render. Debounced by the service: it renders once the flag
        has been left alone for a short while, so a burst of rule changes costs
        one render; every call restarts the wait. Non-blocking; never raises.
        """
        request_render(self.store.path)

    # -- the user's own verdicts on what memory holds (migration 33; report 38 §4, §5, §12.3) -------------
    # Direct, deterministic ops: one transaction each, no model call on the click path. A "wrong" also queues the
    # correction pass (the ripple through derived items) in the background, exactly as a Forget did; its Undo within
    # the window takes the verdict back and cancels the queued pass.

    @staticmethod
    def _fact_id(ref: Any) -> int | None:
        text = str(ref or "").strip().strip("[]").split(":")[-1].lstrip("FfPp")
        return int(text) if text.isdigit() else None

    @staticmethod
    def _journal_id(ref: Any) -> int | None:
        text = str(ref or "").strip().strip("[]").split(":")[-1].lstrip("Jj")
        return int(text) if text.isdigit() else None

    def pin_fact(self, ref: Any, on: bool = True) -> dict:
        """Pin (``on``) or unpin a current portrait fact - a routine, a fact, a person's fact (``fact:N``). The pin is
        its own flag (never ``origin``, which says "you told me"); a pinned fact changes only on the user's
        correction or later evidence. One UPDATE and a diary line (pass ``pin``), synchronous, no model. Returns
        ``{"ok", "ref", "pinned"}`` (``ok`` False with ``error`` when it is not a current fact)."""
        fid = self._fact_id(ref)
        if fid is None:
            return {"ok": False, "ref": str(ref or ""), "error": "not a fact"}
        ok = self.store.set_fact_pinned(fid, bool(on))
        out = {"ok": ok, "ref": f"fact:{fid}", "pinned": bool(on) if ok else None}
        if not ok:
            out["error"] = "That fact has changed since; open it again."
        # no re-render: the pin changes no word of the page Yuki reads (a render is a model call)
        return out

    def _ripple(self, subject: str, memory_had: str, words: str) -> int | None:
        """Queue the correction pass for a "wrong" verdict (the fold and the reword reach derived items), in the
        Me page's fixed correction format; the pass runs in the service, never on the click path. Never raises."""
        try:
            text = (f'About "{" ".join(str(subject or "this").split())}" (memory had: "{" ".join(memory_had.split())}"), '
                    f'the user said in their own words: "{words}"')
            return self.store.add_correction(secret_filter.redact_text(text))
        except Exception:
            return None

    #: What :meth:`correct_item` takes: the ref prefixes the lists give -> the correction target's letter.
    _CORRECT_KINDS = {"fact": "P", "f": "P", "p": "P", "thread": "W", "w": "W", "episode": "E", "e": "E",
                      "session": "E", "journal": "J", "j": "J", "heard": "J", "todo": "T", "t": "T", "commit": "C",
                      "c": "C"}

    def correct_item(self, ref: Any, words: str) -> dict:
        """Correct one item from the user's own short words about what is wrong (the feedback round 3, W3): a portrait
        fact or a person's summary (``fact:N``), a thread of work (``thread:N``), a work session (``episode:N``) or a
        journal entry such as a "maybe misheard" one (``journal:N``), or one item of the to-do list as :meth:`todos`
        names it (``todo:N``, ``commit:N``; ``loop:N`` corrects the loop's portrait fact); ``words`` is what they said
        ("he's my cousin, not a classmate", "that was last week"). Returns at once: the correction is recorded in the Me page's fixed
        format with the item as its target, and the service is woken to run the correction pass, which rewrites the
        item (and anything else carrying the same wrong thing) a moment later - bi-temporally, the old version kept,
        a diary line each; the user never edits text by hand. Poll :meth:`correction_status` with ``correction_id``
        (``updating`` -> ``done``); Undo before the pass starts: ``cancel_correction(correction_id)``. Returns ``{"ok",
        "ref", "correction_id", "state": "updating"}``, ``ok`` False with ``error`` (plain words). Never raises."""
        text = " ".join(str(words or "").split())
        raw = str(ref or "").strip().strip("[]")
        head, _, tail = raw.partition(":")
        letter = self._CORRECT_KINDS.get(head.lower()) if tail else self._CORRECT_KINDS.get(raw[:1].lower())
        num = tail if tail else raw[1:]
        if head.lower() == "loop" and tail.strip().isdigit():   # an open loop is its portrait fact's shown form
            try:
                loop = next((x for x in self.store.open_loops("open") if x.id == int(tail)), None)
            except Exception:
                loop = None
            if loop is not None and loop.portrait_fact_id is not None:
                letter, num = "P", str(loop.portrait_fact_id)
        out: dict[str, Any] = {"ok": False, "ref": raw, "correction_id": None, "state": None}
        if letter is None or not num.isdigit():
            out["error"] = "That can't be corrected here."
            return out
        if not text:
            out["error"] = "Say what's wrong in a few words."
            return out
        from yuki.memory.corrections import target_candidate

        try:
            cand = target_candidate(self.store, f"{letter}{int(num)}")
        except Exception:
            cand = None
        if cand is None:
            out["error"] = "That has changed since; open it again."
            return out
        obj = cand.obj
        if letter == "W":
            subject, had = obj.title, " ".join(str(obj.shown or obj.state or "").split())
        elif letter == "E":
            subject = f"a work session on {datetime.fromtimestamp(obj.started_at):%A %d %B}"
            had = " ".join(str(obj.summary or obj.text or "").split())
        elif letter == "P":
            subject, had = obj.subject or obj.kind, " ".join(str(obj.shown or obj.text or "").split())
        elif letter in ("T", "C"):   # a to-do on the list (the user's own, or Yuki's commitment)
            subject, had = "an item on the user's to-do list", " ".join(str(obj.text or "").split())
        else:
            src = obj.source if isinstance(getattr(obj, "source", None), dict) else {}
            subject = "what Yuki heard" if src.get("heard") else (obj.app or "a journal entry")
            had = obj.fact + (f' (heard "{src["heard"]}", Yuki guessed "{src.get("meant")}")'
                              if src.get("heard") and src.get("meant") else "")
        correction = (f'About "{" ".join(str(subject or "this").split())}" (memory had: "{had}"), '
                      f'the user said in their own words: "{text}"')
        try:
            cid = self.store.add_correction(secret_filter.redact_text(correction), target=cand.ref)
        except Exception as exc:
            out["error"] = f"memory: {type(exc).__name__}"
            return out
        self.rerender_portrait()   # wakes the service: its scheduler runs queued corrections before anything else
        prefix = {"P": "fact", "W": "thread", "E": "episode", "J": "journal", "T": "todo", "C": "commit"}[letter]
        out.update(ok=True, ref=f"{prefix}:{cand.item_id}", correction_id=cid, state="updating")
        return out

    def cancel_correction(self, ref: Any) -> dict:
        """The Undo of :meth:`correct_item` (or any queued correction) before its pass starts: the correction ends and
        its pass is skipped. ``{"ok", "id", "error"?}``; once the pass started, ``ok`` False with
        :data:`TOO_LATE_TO_UNDO`."""
        try:
            cid = int(str(ref).strip().strip("[]").split(":")[-1].lstrip("F"))
        except ValueError:
            return {"ok": False, "id": ref, "error": "not a correction"}
        ok = bool(self.store.cancel_correction(cid))
        return {"ok": ok, "id": cid} if ok else {"ok": False, "id": cid, "error": TOO_LATE_TO_UNDO}

    def review_queue(self, now: float | None = None) -> dict:
        """The day's "To review" queue (report 38 §12.3, the mock's design): current facts Yuki wrote that the user
        has not answered or pinned, written in the last :data:`REVIEW_WITHIN_DAYS` days, ranked by importance x
        (1 - confidence) then newest, at most :data:`REVIEW_PER_DAY` a day (answers today count against it). Returns
        ``{"items": [{"ref": "fact:N", "id", "kind", "aspect", "subject", "headline" (the short label), "text" (the
        shown form; None until written), "flag": "unsure" | "new", "confidence", "importance", "since",
        "created_at"}], "cap", "reviewed_today", "left_today", "waiting" (how many qualify in all), "merged":
        {"count", "since"} (facts the nightly tidy merged in the last 7 days: "I merged N similar facts")}``.
        Read only, no model. Never raises (an empty queue)."""
        from yuki.memory.checkins import day_boundary, user_day

        now = time.time() if now is None else float(now)
        out = {"items": [], "cap": REVIEW_PER_DAY, "reviewed_today": 0, "left_today": REVIEW_PER_DAY, "waiting": 0,
               "merged": {"count": 0, "since": now - 7 * 86400.0}}
        try:
            start = day_boundary(user_day(now))
            done = self.store.review_counts(start)
            since = now - REVIEW_WITHIN_DAYS * 86400.0
            pool = [f for f in self.store.portrait_facts(include_corrections=False)
                    if f.kind not in REVIEW_SKIP_KINDS and f.origin != "user" and not f.pinned
                    and f.reviewed_at is None and (f.created_at or 0.0) >= since]

            def rank(f: Any) -> tuple[float, float]:
                conf = 0.5 if f.confidence is None else float(f.confidence)
                return (-(float(f.importance or 3) * (1.0 - conf)), -(f.created_at or 0.0))

            pool.sort(key=rank)
            left = max(0, REVIEW_PER_DAY - done)
            out.update(reviewed_today=done, left_today=left, waiting=len(pool))
            out["items"] = [{"ref": f"fact:{f.id}", "id": f.id, "kind": f.kind, "aspect": f.aspect,
                             "subject": f.subject, "headline": f.summary, "text": f.shown,
                             "flag": "unsure" if (f.confidence is not None and f.confidence <= REVIEW_UNSURE) else "new",
                             "confidence": f.confidence, "importance": f.importance, "since": f.valid_from,
                             "created_at": f.created_at} for f in pool[:left]]
            merges = self.store.changelog(since=out["merged"]["since"], pass_="dream", limit=200)
            out["merged"]["count"] = sum(max(0, len(m.get("fact_ids") or []) - 1) for m in merges
                                         if m.get("action") == "merge")
        except Exception:
            pass
        return out

    def review_mark(self, ref: Any, verdict: str, *, correction_id: int | None = None) -> dict:
        """The user's answer to one "To review" fact, at once and without a model: ``right`` - reviewed and pinned
        (it stays as it is); ``wrong`` / ``notmine`` - retired now, and the correction pass queued for the ripple
        (returns its ``correction_id``); ``undo`` (within the window's Undo) - takes it back: a retired fact is
        current again and the queued pass (``correction_id``) is cancelled - only while it has not started: once it has,
        nothing is restored and ``ok`` is False with :data:`TOO_LATE_TO_UNDO` (the window keeps the item removed).
        Returns ``{"ok", "ref", "verdict", "correction_id", "cancelled"?}``, ``ok`` False with ``error`` when nothing
        could be done."""
        fid = self._fact_id(ref)
        verdict = str(verdict or "").strip().lower()
        out: dict[str, Any] = {"ok": False, "ref": f"fact:{fid}" if fid else str(ref or ""), "verdict": verdict,
                               "correction_id": None}
        if fid is None or verdict not in ("right", "wrong", "notmine", "undo"):
            out["error"] = "say right, wrong, notmine or undo for a fact"
            return out
        now = time.time()
        fact = self.store.portrait_fact(fid)
        if fact is None:
            out["error"] = "no such fact"
            return out
        if verdict == "right":
            out["ok"] = self.store.mark_fact_reviewed(fid, now, pin=True, note=f"The user confirmed F{fid} (review).")
        elif verdict in ("wrong", "notmine"):
            if fact.valid_to is not None:
                out["error"] = "That fact has changed since; open it again."
                return out
            from yuki.memory.store import FactChange

            self.store.mark_fact_reviewed(fid, now, note=f"The user said F{fid} is {'not theirs' if verdict == 'notmine' else 'wrong'} (review).")
            [(ok, _new)] = self.store.commit_portrait_changes(
                None, [FactChange("INVALIDATE", fid, fact.kind, fact.subject, "", None, [], now, "user")])
            out["ok"] = bool(ok)
            if ok:
                words = "That isn't about me. It's not mine." if verdict == "notmine" else "It's just wrong, forget it"
                out["correction_id"] = self._ripple(fact.subject or fact.kind, fact.text, words)
                self.rerender_portrait()
        else:
            # the queued ripple is taken back first: once its pass started, the answer stands (review A3: a restore
            # with the "forget it" pass still running would be undone again by that pass)
            if correction_id:
                out["cancelled"] = self.store.cancel_correction(int(correction_id))
                if not out["cancelled"]:
                    out["error"] = TOO_LATE_TO_UNDO
                    return out
            if fact.valid_to is None:
                out["ok"] = self.store.mark_fact_reviewed(fid, None, pin=False if fact.origin != "user" else None,
                                                          note=f"The user took back their answer on F{fid}.")
            else:
                out["ok"] = self.store.restore_fact(fid) and self.store.mark_fact_reviewed(
                    fid, None, note=f"The user took back their answer on F{fid}.")
                if out["ok"]:
                    self.rerender_portrait()
        if not out["ok"] and "error" not in out:
            out["error"] = "That fact has changed since; open it again."
        return out

    # -- know-how ----------------------------------------------------------

    @staticmethod
    def _format_knowhow(app: str | None, text: str) -> str:
        return f"[{app}] {text}" if app else text

    def knowhow(self, app: str | None = None, query: str | None = None, limit: int = 8,
                embed_wait_s: float | None = None, *, min_cosine: float | None = None,
                apps: Any = (), app_min_cosine: float | None = None) -> list[str]:
        """Current (valid) procedures that worked on this PC, as short strings.

        ``app`` is the foreground window's process name ("arc.exe",
        "Spotify.exe"); it is matched in its normalised form (casefolded, no
        ``.exe``: "arc"), so "Arc.exe", "arc.exe" and "arc" are the same app.
        Order: entries for ``app`` first (newest first), then entries most
        similar to ``query`` (local embeddings, any app). With no query the
        rest is general know-how (entries without an app), or with no app
        either, every current entry. Each string is the procedure, prefixed with
        ``[app]`` when it belongs to one: "[arc] pass the URL as a launch
        argument". At most ``limit`` entries. ``embed_wait_s`` bounds the wait for the query's
        embedding; past it the similar entries are left out (the app's own still come back).

        ``min_cosine`` (2026-10-03, revamp 02 D3): a request's know-how by measured relevance, not by the window in
        front: every entry must be at least this similar to ``query``; an entry of one of the apps in play
        (``app`` and ``apps``: the apps a request names, from its caller) needs only ``app_min_cosine``. Without
        the query's vector nothing comes back (no unranked filler). ``None``: the old order above.
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []
        if min_cosine is not None:
            return self._knowhow_near(query, limit, embed_wait_s, min_cosine=float(min_cosine),
                                      apps=[a for a in (app, *(apps or ())) if a],
                                      app_min_cosine=float(min_cosine if app_min_cosine is None else app_min_cosine))
        picked: list[tuple[str | None, str]] = []
        seen: set[int] = set()
        if app:
            for k in self.store.knowhow_current(app):
                if len(picked) >= limit:
                    break
                picked.append((k.app, k.text))
                seen.add(k.id)
        if len(picked) < limit and query:
            vec = self._embed(query, embed_wait_s)
            if vec is not None:
                for k in self.store.search_knowhow(vec, limit - len(picked), exclude_ids=sorted(seen)):
                    picked.append((k.app, k.text))
                    seen.add(k.id)
        if len(picked) < limit and not query:
            # with an app: its entries plus general ones; with neither: everything current
            for k in self.store.knowhow_current(None, any_app=not app):
                if len(picked) >= limit:
                    break
                if k.id not in seen:
                    picked.append((k.app, k.text))
                    seen.add(k.id)
        return [self._format_knowhow(a, t) for a, t in picked[:limit]]

    def _knowhow_near(self, query: str | None, limit: int, embed_wait_s: float | None, *, min_cosine: float,
                      apps: list[str], app_min_cosine: float) -> list[str]:
        """:meth:`knowhow` by measured relevance: entries whose cosine to ``query`` clears ``min_cosine`` (an entry
        of an app in ``apps``: ``app_min_cosine``), best first."""
        query = (query or "").strip()
        keys = {k for k in (app_key(a) for a in apps) if k}
        vec = self._embed(query, embed_wait_s) if query else None
        if vec is None:
            # relevance cannot be measured now (no words, or the embedder is not up): the apps in play are the only
            # measured signal left - their own entries, newest first (a lesson just learned in the app in front
            # still reaches the next request in it)
            picked = [k for key in sorted(keys) for k in self.store.knowhow_current(key)]
            picked.sort(key=lambda k: -float(k.valid_from or 0.0))
            return [self._format_knowhow(k.app, k.text) for k in picked[:limit]]
        hits = self.store.search_knowhow(vec, 64)
        scored = []
        for k in hits:
            score = float(getattr(k, "score", 0.0) or 0.0)
            floor = app_min_cosine if (k.app and k.app in keys) else min_cosine
            if score >= floor:
                scored.append((score, k))
        scored.sort(key=lambda s: -s[0])
        return [self._format_knowhow(k.app, k.text) for _s, k in scored[:limit]]

    def remember_how(self, app: str | None, text: str, source_request: str | None = None, *,
                     origin: str = "agent") -> int:
        """Store a procedure that worked (after a successful task); returns its id.

        ``app`` is the process name of the app it applies to ("arc.exe"; stored
        normalised as "arc", see :meth:`knowhow`), ``None`` for general
        know-how; ``text`` the procedure in one or two sentences;
        ``source_request`` the user request it came from. If a current entry for
        the same app says the same thing (embedding cosine >= 0.85, or identical
        text), that entry is superseded (its ``valid_to`` set, history kept)
        instead of piling up near-duplicates. ``origin``: who it came from (``agent`` by default; integ29, mc merge
        note M1: the brain's ``remember`` kind ``how`` passes ``taught``, a way the user told Yuki, so the next
        request's how-block shows it as taught at once).
        """
        text = secret_filter.redact_text((text or "").strip())
        source_request = secret_filter.redact_text(source_request) if source_request else source_request
        if not text:
            raise ValueError("know-how text is empty")
        app = app_key(app)
        vec = self._embed(text)
        supersedes = self.store.knowhow_duplicate(app, text, vec, KNOWHOW_DUPLICATE_COSINE)
        if supersedes is not None and self._fresh_way_held(supersedes, text):
            supersedes = None
        model = getattr(self._embedder, "model_name", None) if vec is not None else None
        return self.store.add_knowhow(
            app, text, source_request=source_request, vector=vec, embed_model=model, supersedes=supersedes,
            origin=str(origin or "agent"),
        )

    def _fresh_way_held(self, kid: int, text: str) -> bool:
        """integ29 (S2, the D8 hold of :meth:`yuki.memory.conversations.ConversationWorker.fresh_way` for every caller):
        whether the current entry ``kid`` a new know-how entry would supersede is a way to follow (``WAY_ORIGINS``)
        written at most ``FRESH_ITEM_S`` ago, its text not the same as ``text``. A misheard "don't use X" reads as close
        to "use X" as a restatement does, so it is then written beside that way, which stays in force; a confirmed
        reversal supersedes it in the worker's pass or when said again later. Logged ``knowhow_held`` with the id only.
        ``False`` when it can't be read."""
        try:
            from yuki.memory.conversations import FRESH_ITEM_S, WAY_ORIGINS

            rows = self.store._query("SELECT * FROM knowhow WHERE id = ? AND valid_to IS NULL", (int(kid),))
            if not rows or str(rows[0]["origin"] or "") not in WAY_ORIGINS:
                return False
            if time.time() - float(rows[0]["valid_from"]) > FRESH_ITEM_S:
                return False
            if self.store._knowhow(rows[0]).text.casefold() == (text or "").casefold():
                return False   # a pure restatement: it supersedes, never a duplicate beside it
        except Exception:  # noqa: BLE001
            return False
        self._skills_log("knowhow_held", knowhow=int(kid))
        return True

    # -- recall ------------------------------------------------------------

    def recall(
        self, query: str, since=None, until=None, app: str | None = None, limit: int = 15,
        embed_wait_s: float | None = None,
    ) -> list[dict]:
        """Journal facts, episodes and past conversations with Yuki relevant to ``query``, best first.

        Merges vector search (local embedder, loaded on first use) and keyword
        search (every whitespace-separated term present, case-insensitive) over
        the journal, the episodes, the conversation turns (what the user said to
        Yuki and what Yuki replied and did) and the session summaries, by
        reciprocal rank fusion. ``since``/``until``
        take epoch seconds, a datetime/date or an ISO string (naive = local time;
        ``since`` inclusive, ``until`` exclusive; an episode or session counts
        when its span overlaps). ``app`` takes either the display name the journal records
        ("Google Chrome", "Spotify") or a process name ("chrome.exe",
        "Spotify.exe"), case-insensitively: a fact matches when its app is one
        of the names that app goes by, or when it came from a window of that
        process; with ``app`` given, episodes, chats and sessions (not tied to
        one app) are left out.
        Each hit: ``{"kind": "fact" | "episode" | "chat" | "session" | "review",
        "at": ISO local time, "until": ISO | None (an episode's, session's or
        review period's end), "app": str | None, "host": str | None, "fact": str,
        "importance": int | None, "score": float}``, plus ``"nature"`` on journal facts (fact | trait |
        story | humor | uncertain, yuki.memory.personality: a joke or an uncertain item is not a fact;
        readers label them). ``fact`` is the journal fact,
        the episode text, the session summary, a weekly review (``Weekly review
        2026-W39 (Mon 14 Sep - Sun 20 Sep): ...``, its whole text), or for a chat
        one exchange: ``The user said: "..." Yuki replied: "..." Yuki did: ...``
        (long messages cut after whole sentences). ``score`` is the fused rank
        score; higher is better. Reviews, like episodes, are left out when
        ``app`` is given. ``embed_wait_s`` bounds the wait for the query's embedding; past
        it the search is keyword-only (the voice grounding lookup cannot wait for a model load).
        """
        query = (query or "").strip()
        limit = max(0, int(limit))
        if not query or limit == 0:
            return []
        s, u = _time_arg(since), _time_arg(until)
        names = self._journal_app_names(app) if app and app.strip() else None
        process = app if names else None
        pool = max(limit * 2, 20)
        scores: dict[tuple[str, int], float] = {}
        entries: dict[tuple[str, int], Any] = {}

        def add(kind: str, rank: int, item: Any) -> None:
            key = (kind, item.id)
            entries.setdefault(key, item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)

        vec = self._embed(query, embed_wait_s)
        if vec is not None:
            for rank, e in enumerate(self.store.search_journal(vec, s, u, names, pool, process=process)):
                add("fact", rank, e)
            if names is None:
                for rank, ep in enumerate(self.store.search_episodes(vec, s, u, pool)):
                    add("episode", rank, ep)
                for rank, t in enumerate(self.store.search_turns(vec, s, u, pool)):
                    add("chat", rank, t)
                for rank, sm in enumerate(self.store.search_summaries(vec, s, u, pool)):
                    add("session", rank, sm)
                for rank, rv in enumerate(self._reviews(lambda: self.store.search_reviews(vec, s, u, pool))):
                    add("review", rank, rv)
        for rank, e in enumerate(self.store.keyword_journal(query, s, u, names, pool, process=process)):
            add("fact", rank, e)
        if names is None:
            for rank, ep in enumerate(self.store.keyword_episodes(query, s, u, pool)):
                add("episode", rank, ep)
            for rank, t in enumerate(self.store.keyword_turns(query, s, u, pool)):
                add("chat", rank, t)
            for rank, sm in enumerate(self.store.keyword_summaries(query, s, u, pool)):
                add("session", rank, sm)
            for rank, rv in enumerate(self._reviews(lambda: self.store.keyword_reviews(query, s, u, pool))):
                add("review", rank, rv)

        def at(key: tuple[str, int]) -> float:
            item = entries[key]
            if key[0] == "review":
                return item.period_start
            return item.at if key[0] in ("fact", "chat") else item.started_at

        order = sorted(scores, key=lambda k: (-scores[k], -at(k)))[:limit]
        out = []
        try:   # a fact heard on a call whose timeline digest exists points at it (yuki.memory.calldigest)
            digested = self.store.digested_call_threads() if any(k[0] == "fact" for k in order) else {}
        except Exception:
            digested = {}
        for key in order:
            item = entries[key]
            score = round(scores[key], 5)
            if key[0] == "fact":
                call_id = digested.get(item.thread_id) if item.thread_id is not None else None
                out.append({
                    "kind": "fact", "at": _iso(item.at), "until": None, "app": item.app, "host": item.host,
                    "fact": item.fact, "importance": item.importance, "score": score,
                    "nature": getattr(item, "nature", "fact"),
                    "source": describe_source(getattr(item, "source", None), app=item.app, at=item.at),
                    "other_ai": bool(getattr(item, "tainted", False)),
                    **({"call": f"call:{call_id}"} if call_id is not None else {}),
                })
            elif key[0] == "chat":
                out.append({
                    "kind": "chat", "at": _iso(item.at), "until": None, "app": None, "host": None,
                    "fact": self._chat_text(item), "importance": None, "score": score,
                    "source": describe_source(None, where="a conversation with Yuki", at=item.at),
                })
            elif key[0] == "review":
                out.append({
                    "kind": "review", "at": _iso(item.period_start), "until": _iso(item.period_end), "app": None,
                    "host": None, "fact": self._review_line(item), "importance": None, "score": score,
                })
            else:
                out.append({
                    "kind": key[0], "at": _iso(item.started_at), "until": _iso(item.ended_at), "app": None,
                    "host": None, "fact": item.text, "importance": None, "score": score,
                })
        return out

    # -- fast memory (yuki.memory.relevant) ------------------------------------

    @property
    def relevant_index(self) -> Any:
        with self._relevant_lock:
            if self._relevant_index is None:
                from yuki.memory.relevant import RelevantIndex

                self._relevant_index = RelevantIndex(self.store)
            return self._relevant_index

    def relevant(self, query: str, limit: int | None = None, embed_wait_s: float | None = None,
                 now: float | None = None, previous: str | None = None, **tuning: Any) -> list[dict]:
        """The few memories that bear on ``query`` (the user's message), best first, each with where it came from.

        Portrait facts, journal facts, commitments and episodes of any age, ranked by the shared
        embedder's similarity plus the importance written with each and recency
        (:mod:`yuki.memory.relevant`). Nothing clears the bar: ``[]``. ``embed_wait_s`` bounds the
        wait for the message's embedding (a request's path goes on without it). Each hit:
        ``{kind: fact | portrait | commitment | resource | episode, id, at, until, text, source, score, cosine,
        importance, nature}`` (a resource also has ``value``: the user's own site, account or link); ``source`` is the provenance line
        (:func:`yuki.memory.provenance.describe`).

        ``previous``: the conversation's previous message. A follow-up ("elaborate", "what was that?")
        names nothing itself, so the previous message's memories are searched too, ranked
        :data:`yuki.memory.relevant.FOLLOW_UP_DISCOUNT` below the current message's (its vector was made
        on the previous request and is reused).
        """
        from yuki.memory import relevant as rel

        query = (query or "").strip()
        if not query:
            return []
        vec = self._embed(query, embed_wait_s)
        if vec is None:
            return []
        index = self.relevant_index
        self._embed_new_facts(embed_wait_s)
        index.refresh()
        n = int(limit or rel.LIMIT)
        hits = index.search(vec, now=now, limit=n, **tuning)
        prev = (previous or "").strip()
        if prev and prev != query:
            pvec = self._query_vecs.get(prev)
            if pvec is None:
                pvec = self._embed(prev, embed_wait_s)
            if pvec is not None:
                seen = {(h["kind"], h["id"]) for h in hits}
                more = [dict(h, score=round(h["score"] - rel.FOLLOW_UP_DISCOUNT, 4), follow_up=True)
                        for h in index.search(pvec, now=now, limit=n, **tuning) if (h["kind"], h["id"]) not in seen]
                hits = sorted(hits + more, key=lambda h: -h["score"])[:n]
        return [h for h in (self._relevant_item(hit) for hit in hits) if h is not None]

    def _embed_new_facts(self, wait_s: float | None) -> None:
        """Portrait facts written since the service last embedded (the user's correction a minute ago, an
        answer, a new version) get their vector now, in one batch; usually there are none (one indexed query)."""
        try:
            missing = self.store.portrait_facts_without_vectors(16)
            if not missing:
                return
            from yuki.memory.relevant import portrait_fact_text

            vecs = self.embedder.embed([portrait_fact_text(f) for f in missing], wait_s)
            if vecs is not None and len(vecs) == len(missing):
                self.store.add_portrait_fact_vectors(list(zip([f.id for f in missing], vecs)),
                                                     getattr(self.embedder, "model_name", None) or "")
        except Exception:
            pass

    def _relevant_item(self, hit: dict) -> dict | None:
        """Decrypt one picked item and write its provenance line."""
        kind, item_id = hit["kind"], int(hit["id"])
        base = {"id": item_id, "score": hit["score"], "cosine": hit["cosine"], "importance": hit["importance"]}
        if kind == "journal":
            e = self.store.journal_entries([item_id]).get(item_id)
            if e is None:
                return None
            return {**base, "kind": "fact", "at": _iso(e.at), "until": None, "text": e.fact, "nature": e.nature,
                    "source": describe_source(e.source, app=e.app, at=e.at)}
        if kind == "portrait":
            f = self.store.portrait_fact(item_id)
            if f is None:
                return None
            return {**base, "kind": "portrait", "at": _iso(f.valid_from), "until": None, "text": f.text,
                    "nature": "fact", "fact_kind": f.kind, "source": self.fact_provenance(f)}
        if kind == "commitment":
            c = self.store.conversation_fact(item_id)
            if c is None:
                return None
            said = "said on a call" if c.origin == "call" else "said to Yuki" if c.quote else "from a conversation with Yuki"
            quote = f': "{" ".join(c.quote.split())[:240]}"' if c.quote else ""
            state = "open" if c.status == "active" else c.status
            return {**base, "kind": "commitment", "at": _iso(c.valid_from), "until": None,
                    "text": f"{c.text} ({state})", "nature": "fact",
                    "source": f"{said}, {datetime.fromtimestamp(c.valid_from):%a %d %b %H:%M}{quote}"}
        if kind == "resource":
            c = self.store.conversation_fact(item_id)
            if c is None or c.status != "active":
                return None
            value = f" ({c.value})" if c.value and c.value.casefold() not in c.text.casefold() else ""
            quote = f': "{" ".join(c.quote.split())[:240]}"' if c.quote else ""
            return {**base, "kind": "resource", "at": _iso(c.valid_from), "until": None, "text": c.text + value,
                    "value": c.value, "nature": "fact",
                    "source": f"the user's own, from a conversation with Yuki, "
                              f"{datetime.fromtimestamp(c.valid_from):%a %d %b %H:%M}{quote}"}
        if kind == "episode":
            eps = self.store.episodes_by_ids([item_id])
            if not eps:
                return None
            ep = eps[0]
            span = f"{datetime.fromtimestamp(ep.started_at):%a %d %b %H:%M}-{datetime.fromtimestamp(ep.ended_at):%H:%M}"
            return {**base, "kind": "episode", "at": _iso(ep.started_at), "until": _iso(ep.ended_at),
                    "text": ep.text, "nature": "fact",
                    "source": f"episode {span}, written from the measured timeline and the journal"}
        return None

    def fact_provenance(self, fact: Any) -> str:
        """Where a portrait fact came from: the user's own correction, or its earliest cited journal entry
        that has words, with how many more entries back it."""
        if getattr(fact, "origin", "") == "user" and not fact.source_ids:
            return f"the user's own correction, {datetime.fromtimestamp(fact.valid_from):%a %d %b %H:%M}"
        entries = self.store.journal_entries(fact.source_ids or [])
        if not entries:
            return f"memory's portrait, since {datetime.fromtimestamp(fact.valid_from):%a %d %b}"
        ordered = sorted(entries.values(), key=lambda e: e.at)
        first = next((e for e in ordered if (e.source or {}).get("quote")), ordered[0])
        more = len(ordered) - 1
        line = describe_source(first.source, app=first.app, at=first.at)
        return line + (f" (and {more} more entr{'y' if more == 1 else 'ies'})" if more else "")

    # -- Yuki's persona (yuki.memory.persona) ------------------------------------

    def yuki_persona(self) -> str | None:
        """The "how Yuki talks" block (written nightly from how the user reacts, evidence-cited), or ``None``.

        For the agent's profile layer and the voice mouth's prompt; plain lines, no ids.
        """
        try:
            items = self.store.persona_items()
        except Exception:
            return None
        if not items:
            return None
        from yuki.memory.persona import render_persona

        return render_persona(items)

    # -- standing requests --------------------------------------------------------

    def add_standing_request(self, condition: str, quote: str, request: str | None = None, repeat: bool = False,
                             days: float | None = None) -> str:
        """Save "tell me when X happens": the condition (plain words) and the user's own words asking for it.

        The journal's model checks every new batch against the open ones and a match writes one
        card (cooldown, fire budget and expiry in code: :mod:`yuki.memory.journal`). ``repeat``:
        every time it happens (up to :data:`REQUEST_REPEAT_FIRES` cards), else once. ``days``: expiry
        (default :data:`REQUEST_DAYS`). Returns ``"request:N"``.
        """
        condition = secret_filter.redact_text(" ".join((condition or "").split()))
        quote = secret_filter.redact_text(" ".join((quote or "").split()))
        if not condition or not quote:
            raise ValueError("a standing request needs what to watch for and the user's words")
        now = time.time()
        rid = self.store.add_standing_request(
            condition, quote, request=secret_filter.redact_text(request) if request else None,
            max_fires=REQUEST_REPEAT_FIRES if repeat else 1, cooldown_s=REQUEST_COOLDOWN_S,
            expires_at=now + 86400.0 * float(days if days else REQUEST_DAYS), at=now)
        return f"request:{rid}"

    def standing_requests(self, include_ended: bool = False) -> list[dict]:
        """Open standing requests (with ``include_ended``: all), each with its ``ref``."""
        rows = self.store.standing_requests(None if include_ended else ("open",))
        return [{**r, "ref": f"request:{r['id']}"} for r in rows]

    def cancel_standing_request(self, ref: Any) -> bool:
        """End an open standing request by its id (``request:3``, ``3``); ``False`` when it is not an open one."""
        text = str(ref or "").strip().strip("[]")
        text = text.split(":", 1)[1] if text.casefold().startswith("request:") else text
        try:
            return self.store.end_standing_request(int(text), "cancelled")
        except (TypeError, ValueError):
            return False

    # -- the memory diary ----------------------------------------------------------

    def memory_diary(self, limit: int = 50, since: Any = None) -> list[dict]:
        """What dreaming and the persona pass changed, newest first: ``{at, pass, action, text, fact_ids, ...}``."""
        rows = self.store.changelog(since=_time_arg(since), limit=limit)
        return [{**r, "at": _iso(r["at"])} for r in rows]

    @staticmethod
    def _reviews(fetch: Any) -> list[Any]:
        """Weekly reviews from ``fetch()``; ``[]`` on a store without them (an older schema)."""
        try:
            return list(fetch())
        except Exception:
            return []

    @staticmethod
    def _review_line(review: Any) -> str:
        """A weekly review as one recall text: its week and span, then the review."""
        first = datetime.fromtimestamp(review.period_start)
        last = datetime.fromtimestamp(review.period_end)
        text = " ".join((review.text or "").split())
        return f"Weekly review {review.week} ({first:%a %d %b} - {last:%a %d %b}): {text}"

    @staticmethod
    def _message(text: str | None, limit: int) -> str:
        clipped = _clip_sentences(text, limit)
        if clipped is None:
            return f"(a long message of {len(text or '')} characters, not shown)"
        return clipped

    @classmethod
    def _chat_text(cls, t: ConversationTurn) -> str:
        """One exchange as a recall line: the user's own words (never Yuki's notes around them, migration 27), what
        Yuki replied and what it did."""
        from yuki.memory.conversations import has_card_context, own_words, yuki_words_request

        parts: list[str] = []
        if yuki_words_request(t.user_text):
            parts.append(f'The user sent Yuki\'s own suggestion: "{cls._message(t.user_text, 400)}"')
        else:
            words = own_words(t.user_text) or (t.user_text or "").strip()
            if words:
                parts.append(f'The user said: "{cls._message(words, 600)}"'
                             + (" (replying to Yuki's card)" if has_card_context(t.user_text) else ""))
        if t.reply_text:
            parts.append(f'Yuki replied: "{cls._message(t.reply_text, 600)}"')
        if t.actions:
            parts.append(f"Yuki did: {cls._actions_line(t.actions, 300)}.")
        return " ".join(parts) or "(an exchange with nothing said)"

    @staticmethod
    def _actions_line(actions: list[str], limit: int) -> str:
        """Actions joined by "; ", whole items only, "(+N more)" for the ones left out."""
        kept: list[str] = []
        for a in actions:
            if len("; ".join([*kept, a])) > limit - 14:   # room for " (+N more)"
                break
            kept.append(a)
        rest = len(actions) - len(kept)
        return "; ".join(kept) + (f" (+{rest} more)" if rest else "") if kept else f"({len(actions)} actions)"

    # -- conversation memory (yuki.memory.conversations) ----------------------

    def log_turn(
        self, session_id: str, request_id: int | str, at: float, user_text: str, reply_text: str | None,
        actions: list[str], outcome: str, addressed: str | None = None, envelope: str | None = None,
    ) -> int:
        """Store one exchange between the user and Yuki; returns its turn id (0 if it could not be stored).

        ``session_id``: Yuki's session (one app run); ``request_id``: the
        request's number in it; ``at``: when the user spoke (epoch seconds);
        ``user_text``: what the user said; ``reply_text``: Yuki's final reply
        (``None`` if there was none); ``actions``: one short line per tool call
        ("open_url https://open.spotify.com ok"); ``outcome``: how the request
        ended ("done", "cancelled", "error", ...). Encrypted at rest.

        Queues the exchange for the ``yuki-memory`` service, which extracts
        rules, preferences, commitments and journal facts from it within about a
        minute (at once from the third pending exchange) and summarises the
        session once it ends (30 minutes without an exchange, or a new
        session id). Fast (well under 20 ms; the write waits at most 8 ms for
        the database, else a background writer finishes it) and never raises.
        ``addressed``: who a voice turn was said to, from voice's ``turn_closed`` ("yuki" | "others" |
        "unanswered"); a turn said to others is kept for the record and never extracted (no rule, to-do or fact);
        an unanswered call turn (the next words came first; nobody judged it) is extracted, the extractor judging.
        ``envelope``: the notes Yuki put after the user's words, when the caller has them apart; ``None``: read
        off ``user_text`` (:func:`yuki.memory.store.split_envelope`). Stored apart from the words (migration 27).
        """
        try:
            # A key or password the user pastes is used by the agent in the moment (it has the words in its
            # own context); what memory keeps, and later sends to a model, has it as "[API key hidden]".
            user_text = secret_filter.redact_text(user_text or "")
            reply_text = secret_filter.redact_text(reply_text) if reply_text else reply_text
            actions = [secret_filter.redact_text(str(a)) for a in (actions or ())]
            envelope = secret_filter.redact_text(envelope) if envelope else None
            turn_id = self.store.add_turn(session_id, request_id, at, user_text, reply_text, actions, outcome,
                                          addressed=addressed, envelope=envelope)
        except Exception as exc:
            self.last_turn_error = f"{type(exc).__name__}: {exc}"
            return 0
        try:
            signal_turns(self.store.path)
        except Exception:
            pass
        return turn_id

    def _standing_line(self, f: ConversationFact) -> str | None:
        from yuki.memory.conversations import normalize_quote

        text = _clip_sentences(f.text, 300)
        quote = _clip_sentences(f.quote, 240) if f.quote else None
        if f.kind == "resource":
            if text is None:
                return None
            value = f.value if f.value and f.value.casefold() not in text.casefold() else ""
            return f"- [resource:{f.id}] {text}" + (f" ({value})" if value else "")
        if f.kind == "commitment":
            if text is None:
                return None
            due = f"due {datetime.fromtimestamp(f.due_at):%Y-%m-%d %H:%M}".replace(" 00:00", "") if f.due_at else ""
            said = "said in a call" if f.origin == "call" else "said" if f.owner == "user" else "asked"
            dates = ", ".join(x for x in (due, f"{said} {_day(f.valid_from)}") if x)
            return f"- [commit:{f.id}] {text} ({dates})"   # the to-do id todo done takes
        if text is None and quote is None:
            return None
        # the id forget_rule takes: the model picks the rule, no words are matched (no-pattern audit F)
        ident = f"feedback:{f.id}" if f.kind == "feedback" else f"rule:{f.id}"
        if text is None or (quote and normalize_quote(quote) == normalize_quote(text)):
            return f'- [{ident}] "{quote}" (the user\'s words, {_day(f.valid_from)})'
        words = f' Their words: "{quote}"' if quote else ""
        return f"- [{ident}] {text}{words} ({_day(f.valid_from)})"

    def standing_parts(self) -> dict[str, Any]:
        """The standing context as its parts (2026-10-03, revamp 09 P0.1), each item whole, budgets applied.

        Returns ``{"header": str, "headings": {group: heading}, "groups": {group: [{"ref", "line", "text"}]},
        "coach_state": str | None}`` with the groups of :data:`STANDING_GROUPS`:

        * ``rule`` / ``preference``: the user's own instructions for how Yuki talks or behaves (the agent's
          cached rules layer holds these and nothing else);
        * ``coach``: what the user said back to coach cards (feedback on that kind of card, not a rule);
        * ``resource``: the user's own sites, accounts and links;
        * ``commitment``: Yuki's own commitments (what Yuki promised or was asked to do later);
        * ``todo``: the user's own to-dos (their plans and work, from a chat or a call: never Yuki's promise).

        The parts used to reach the agent as one string parsed by its headings, and with no active rule the whole
        string (to-dos included) became the cached rules layer: six stale to-dos sat in the block headed "every one
        applies to every reply" (09 §3.4). Never raises (empty groups when memory cannot be read).
        """
        header = "[Standing rules and open commitments, from Yuki's conversations with the user]"
        out: dict[str, Any] = {"header": header, "headings": {**STANDING_HEADINGS, "goal": STANDING_GOAL_HEADING},
                               "groups": {g: [] for g in STANDING_GROUPS}, "coach_state": None}
        try:
            facts = self.store.conversation_facts(STANDING_KINDS)
            cards = {f.id: self.store.fact_card(f) for f in facts if f.kind in ("preference", "feedback")}
            items = [(f, line) for f in facts if (line := self._standing_line(f))]
        except Exception:
            return out

        def group(f: ConversationFact) -> str:
            if f.kind == "feedback" or (f.kind == "preference" and cards.get(f.id) is not None):
                return "coach"
            if f.kind == "commitment" and (f.origin == "call" or f.owner == "user"):
                return "todo"   # the user's own to-do, from a call or a chat: never Yuki's promise
            return f.kind

        def size(chosen: list[tuple[ConversationFact, str]], part: tuple[str, ...]) -> int:
            lines = [header]
            for g in STANDING_GROUPS:
                rows = [line for f, line in chosen if group(f) == g and g in part]
                if rows:
                    lines += [STANDING_HEADINGS[g], *rows]
            return len("\n".join(lines))

        # Two budgets: the user's standing instructions (rules, preferences, coach feedback) and the
        # commitments, so neither can push the other out. Over a budget, what goes first is what
        # matters least on every request: the user's own to-dos, then Yuki's commitments; within the
        # instructions, coach feedback, then preferences, rules last. Oldest first within each. (Call
        # commitments once pushed "call me captain" out of every request; rules once pushed out the
        # preference the user then withdrew.)
        drop_order = {"todo": 0, "commitment": 1, "coach": 2, "preference": 3, "rule": 4, "resource": 5}
        instructions = ("rule", "preference", "coach")
        kept = sorted(items, key=lambda item: (item[0].valid_from, item[0].id))
        for part, cap in ((instructions, STANDING_RULES_CHARS), (("commitment", "todo"), STANDING_CONTEXT_CHARS),
                          (("resource",), STANDING_RESOURCES_CHARS)):
            while any(group(c[0]) in part for c in kept) and size(kept, part) > cap:
                victim = min((c for c in kept if group(c[0]) in part),
                             key=lambda item: (drop_order.get(group(item[0]), 0), item[0].valid_from, item[0].id))
                kept.remove(victim)
        for f, line in kept:
            ref = (f"resource:{f.id}" if f.kind == "resource" else f"commit:{f.id}" if f.kind == "commitment"
                   else f"feedback:{f.id}" if f.kind == "feedback" else f"rule:{f.id}")
            out["groups"][group(f)].append({"ref": ref, "line": line, "text": " ".join((f.text or "").split()),
                                            "due_at": f.due_at})
        try:
            out["groups"]["goal"] = self._goal_items()
        except Exception:
            out["groups"]["goal"] = []
        try:
            out["coach_state"] = self.coach_context() or None
        except Exception:
            out["coach_state"] = None
        return out

    def _goal_items(self, now: float | None = None) -> list[dict[str, Any]]:
        """The standing parts' goal group: today's goals, then their work's open or unconfirmed ones, within
        :data:`STANDING_GOALS_CHARS` (whole lines)."""
        from yuki.memory.checkins import current_goals, goal_line

        now = time.time() if now is None else float(now)
        day_start = self._nudges_config().resume_day_start
        goals = current_goals(self.store, now, day_start)
        titles: dict[int, str] = {}
        wanted = [g.work_thread_id for g in goals if g.work_thread_id]
        if wanted:
            titles = {t.id: t.title for t in self.store.work_threads(None, ids=wanted)}
        items, used = [], 0
        for g in goals:
            line = "- " + goal_line(g, now, lambda v, n: " ".join(str(v or "").split())[:n], day_start, titles)
            if used + len(line) > STANDING_GOALS_CHARS:
                break
            used += len(line)
            items.append({"ref": g.ref, "line": line, "text": g.text, "due_at": g.due_at})
        return items

    def goals(self, scope: str | None = None, statuses: Any = ("open", "unknown"), today: bool = False,
              limit: int = 30) -> list[dict]:
        """The user's goals (migration 30, yuki.memory.checkins), most recently updated first: ``scope`` day | thread
        | project; ``statuses`` (None: all); ``today``: only today's day goals. Each: ``{"id": "G12", "scope", "day",
        "today", "thread": "W5" | None, "text", "words" (their own words), "source", "source_ref", "status",
        "evidence", "due", "next_checkin", "last_checkin", "checkins", "since", "updated"}``. Read-only, never
        raises (``[]``)."""
        from yuki.memory.checkins import goal_dict, user_day

        try:
            day_start = self._nudges_config().resume_day_start
            now = time.time()
            day = user_day(now, day_start) if today else None
            rows = self.store.goals(tuple(statuses) if statuses else None, scope="day" if today else scope, day=day,
                                    limit=limit)
            return [goal_dict(g, now, day_start) for g in rows]
        except Exception:
            return []

    def standing_context(self) -> str:
        """The user's active rules and preferences for Yuki, and Yuki's open commitments, as one block.

        Rules and preferences are how the user told Yuki to talk or behave,
        each in the user's own words with the date it was said; commitments are
        what the user asked Yuki to do later or Yuki promised, with the due date
        when one was given and the date asked. Hard cap 1,200 characters (~300
        tokens): when it is over, items are dropped whole (never cut
        mid-sentence), the user's own to-dos first, then commitments, then preferences, rules
        last, oldest first within each. ``""`` when there is nothing (or memory cannot be read).
        Meant to be attached to every request after the cached portrait.

        The same text as ever, built from :meth:`standing_parts` (the voice talker reads it through
        ``yuki.agent.memory.split_standing``, which keeps only the rule and preference sections as rules).
        """
        return render_standing(self.standing_parts(), STANDING_TEXT_GROUPS)

    def similarity(self, query: str, texts: Any, embed_wait_s: float | None = None) -> list[float] | None:
        """Cosine of each text to ``query`` on the shared embedder (measured relevance, no words matched), or
        ``None`` when the query's vector cannot be had within ``embed_wait_s``. The texts' vectors are kept
        (:data:`TEXT_VEC_CACHE` of them), so a to-do list attached to every request is embedded once."""
        query = (query or "").strip()
        texts = [" ".join(str(t or "").split()) for t in (texts or [])]
        if not query:
            return None
        if not texts:
            return []
        q = self._embed(query, embed_wait_s)
        if q is None:
            return None
        missing = [t for t in dict.fromkeys(texts) if t and t not in self._text_vecs]
        if missing:
            try:
                vecs = self.embedder.embed(missing, embed_wait_s)
            except Exception:
                vecs = None
            if vecs is not None and len(vecs) == len(missing):
                for t, v in zip(missing, vecs):
                    if len(self._text_vecs) >= TEXT_VEC_CACHE:
                        self._text_vecs.pop(next(iter(self._text_vecs)))
                    self._text_vecs[t] = np.asarray(v, dtype=np.float32)
        qv = np.asarray(q, dtype=np.float32).ravel()
        qn = float(np.linalg.norm(qv)) or 1.0
        out: list[float] = []
        for t in texts:
            v = self._text_vecs.get(t)
            if v is None or v.shape != qv.shape:
                return None   # a text not embedded in time: relevance is unmeasured, the caller decides
            out.append(round(float(v @ qv) / ((float(np.linalg.norm(v)) or 1.0) * qn), 4))
        return out

    def resume_context(self, within_hours: float = 6.0) -> str | None:
        """Where the latest conversation left off, when it ended within ``within_hours``; else ``None``.

        The latest session is the one of the newest exchange. The block holds
        its summary (when the service has written one: after 30 idle minutes or
        once a new session starts) and its last (up to 6) exchanges, oldest
        first: what the user said, Yuki's reply and one line of Yuki's actions.
        Hard cap 4,000 characters (~1,000 tokens): long messages are cut after
        whole sentences (or shown as "a long message") and, when still over,
        the oldest exchanges are dropped whole. Never raises (``None`` on errors).

        When the last exchange is older than ``within_hours`` (a new day), the block is the last day digest and the
        open promises instead (:meth:`_resume_from_day`, at most :data:`RESUME_FALLBACK_CHARS`; migration 33), so
        the first request of the day knows yesterday; ``None`` when there is no recent digest either.
        """
        try:
            latest = self.store.latest_turn()
            now = time.time()
            if latest is None or now - latest.at > float(within_hours) * 3600.0:
                return self._resume_from_day(now)
            sid = latest.session_id
            turns = self.store.turns_for_session(sid, limit=RESUME_EXCHANGES)
            start, end, count = self.store.session_span(sid)
            summaries = self.store.session_summaries(sid, limit=1)
        except Exception:
            return None
        if not turns:
            return None
        start = start if start is not None else turns[0].at
        end = end if end is not None else turns[-1].at
        header = (f"[Where the last conversation with the user left off: {datetime.fromtimestamp(start):%Y-%m-%d %H:%M}"
                  f"-{datetime.fromtimestamp(end):%H:%M}, {count} exchange{'' if count == 1 else 's'}, "
                  f"ended {_ago(now - end)} ago]")
        summary = ""
        if summaries:
            text = _clip_sentences(summaries[-1].text, 1_500)
            if text:
                summary = f"Summary: {text}"

        def block(t: ConversationTurn) -> str:
            lines = [f"{datetime.fromtimestamp(t.at):%H:%M} The user: {self._message(t.user_text, 700)}"]
            lines.append(f"  Yuki: {self._message(t.reply_text, 900)}" if t.reply_text else "  Yuki: (no reply)")
            extra = []
            if t.actions:
                extra.append("actions: " + self._actions_line(t.actions, 240))
            if t.outcome:
                extra.append(f"outcome: {t.outcome}")
            if extra:
                lines.append("  " + " | ".join(extra))
            return "\n".join(lines)

        blocks = [block(t) for t in turns]

        def render(chosen: list[str], summary_text: str) -> str:
            parts = [header]
            if summary_text:
                parts.append(summary_text)
            if chosen:
                shown = len(chosen)
                parts.append(f"Last {shown} exchange{'' if shown == 1 else 's'}, oldest first:")
                parts.extend(chosen)
            return "\n".join(parts)

        while blocks and len(render(blocks, summary)) > RESUME_CONTEXT_CHARS:
            blocks.pop(0)   # the oldest exchange goes first
        if len(render(blocks, summary)) > RESUME_CONTEXT_CHARS:
            summary = ""
        return render(blocks, summary)

    def _resume_from_day(self, now: float) -> str | None:
        """The resume block when the last exchange is older than the window (report 38 §9.3): the last day digest -
        the same line the user sees at the top of the new chat - and the open promises, at most
        :data:`RESUME_FALLBACK_CHARS`; None when there is none from the last :data:`RESUME_FALLBACK_DAYS` days.

        It says which day the digest is from and never calls an older day "the last day": when yesterday had
        activity and its digest is not written yet, it asks for it (the flag, as :meth:`day_connector` does) and says
        that yesterday's is missing (review A1: Yuki and the page must not disagree about yesterday)."""
        from yuki.memory.daydigest import day_window, previous_days, today

        try:
            asked = self._ask_day_digest(now)
            if asked:   # say so only when they talked with Yuki yesterday (journal entries alone may make no digest)
                y_start, y_end = day_window(previous_days(1, now)[0])
                asked = bool(self.store.turn_times(y_start, y_end))
            found = self.store.day_digests(before=today(now), limit=1)
            if not found or found[0]["day"] < previous_days(RESUME_FALLBACK_DAYS, now)[-1]:
                return None
            d = found[0]
            start, _end = day_window(d["day"])
            when = f"{datetime.fromtimestamp(start + 6 * 3600):%A %d %B}"
            if d["day"] == previous_days(1, now)[0]:
                head = f"[Yesterday ({when}) with the user, from memory's day digest; this is a new chat]"
            else:
                head = (f"[From memory's day digest of {when}, the latest one written"
                        + ("; the user also talked with you yesterday, whose digest is not written yet" if asked
                           else "")
                        + "; this is a new chat]")
            parts = [head, _clip_sentences(d["text"], 600) or ""]
            opened = [o.get("text") for o in (d.get("refs") or {}).get("open") or [] if isinstance(o, dict)]
            if opened:
                parts.append("Still open then: " + "; ".join(str(o) for o in opened if o))
            promises = [f.title or f.text for f in self.store.conversation_facts(("commitment",))][-5:]
            if promises:
                parts.append("Open promises and to-dos: " + "; ".join(self._message(p, 120) for p in promises))
            text = "\n".join(p for p in parts if p)
            while len(text) > RESUME_FALLBACK_CHARS and len(parts) > 2:
                parts.pop()
                text = "\n".join(p for p in parts if p)
            return text[:RESUME_FALLBACK_CHARS] if text else None
        except Exception:
            return None

    def remember_rule(self, text: str, source_turn: int | None = None, supersedes: int | None = None) -> int:
        """Store a standing rule the user just gave, in the user's words; returns its id.

        ``text``: the rule as the user said it ("call me captain always");
        ``source_turn``: the :meth:`log_turn` id it came from, if known. Stored
        at once (kind ``rule``, origin ``user``), so it shows in
        :meth:`standing_context` from the next request. An active rule or
        preference that says the same thing (identical words, or embedding
        cosine >= 0.85) is superseded by it (bi-temporal: the old version ends,
        history kept). ``supersedes``: the id of an active rule or preference
        this one restates or replaces, when the caller knows it (the words may
        differ too much for the embedding to tell); an id that is not an active
        rule or preference is ignored. Asks the service for a portrait
        re-render (:meth:`rerender_portrait`). Raises ``ValueError`` for an
        empty text.
        """
        from yuki.memory.conversations import normalize_quote

        text = secret_filter.redact_text(" ".join((text or "").split()))
        if not text:
            raise ValueError("rule text is empty")
        vec = self._embed(text)
        candidates = self.store.conversation_facts(("rule", "preference"))
        given = int(supersedes) if supersedes else None
        supersedes = given if any(f.id == given for f in candidates) else None
        best = 1.0 if supersedes is not None else -1.0
        wanted = normalize_quote(text)
        for f in candidates if supersedes is None else ():
            if wanted in (normalize_quote(f.text), normalize_quote(f.quote)):
                supersedes, best = f.id, 1.0
                break
        if supersedes is None and vec is not None and candidates:
            stored = self.store.conversation_fact_vectors([f.id for f in candidates])
            for f in candidates:
                other = stored.get(f.id)
                if other is None or other.shape != vec.shape:
                    continue
                score = float(np.dot(vec, other) / ((np.linalg.norm(other) * np.linalg.norm(vec)) or 1.0))
                if score >= RULE_DUPLICATE_COSINE and score > best:
                    supersedes, best = f.id, score
        model = getattr(self._embedder, "model_name", None) if vec is not None else None
        fact_id = self.store.add_conversation_fact(
            "rule", text, quote=text, subject="", origin="user",
            source_turn_ids=[int(source_turn)] if source_turn else (), vector=vec, embed_model=model,
            supersedes=supersedes,
        )
        self.rerender_portrait()
        return fact_id

    def revoke_rule(self, ref: str, *, note: str | None = None) -> int:
        """Mark one active rule (or preference) revoked by its id; returns the id, or 0.

        ``ref`` is the id :meth:`standing_context` shows (``"rule:12"``, also
        ``"[rule:12]"`` or ``"12"``): the model picks the rule the user
        withdrew; no text is matched here (docs/research/no-pattern-audit.md F).
        The rule is kept as history (status ``revoked``, ``valid_to`` now). 0
        when ``ref`` is not the id of an active rule or preference. A
        revocation asks the service for a portrait re-render
        (:meth:`rerender_portrait`).

        ``note``: why it ended, stored as its end note. The default says the user withdrew it, which is true for
        the callers that exist (the agent's forget_rule on the user's words, the Yuki window's forget button); code
        that ends a rule for another reason (a rule moved into Yuki's defaults) must say so here - on 27 Sept five
        rules revoked by a script read "the user withdrew it" (revamp 09 §3.1).
        """
        prefix, sep, number = str(ref or "").strip().strip("[]").strip().rpartition(":")
        if (sep and prefix.strip() not in ("rule", "preference", "feedback")) or not number.strip().isdigit():
            return 0
        wanted = int(number)
        try:
            candidates = self.store.conversation_facts(("rule", "preference", "feedback"))
        except Exception:
            return 0
        target: ConversationFact | None = next((f for f in candidates if f.id == wanted), None)
        if target is None:
            return 0
        ok = self.store.end_conversation_fact(target.id, "revoked", note=(note or "").strip() or "the user withdrew it")
        if ok:
            self.rerender_portrait()
        return target.id if ok else 0

    # -- activity (the timeline) and episodes ---------------------------------

    @staticmethod
    def _window(since: Any, until: Any) -> tuple[float, float]:
        """``[since, until)`` as epoch seconds; default since local midnight, until now."""
        s, u = _time_arg(since), _time_arg(until)
        now = datetime.now().timestamp()
        if u is None:
            u = now
        if s is None:
            s = datetime.fromtimestamp(u if u <= now else now).replace(
                hour=0, minute=0, second=0, microsecond=0).timestamp()
            if s >= u:
                s = u - 86400.0
        if s >= u:
            raise ValueError("since must be before until")
        return s, u

    def activity(self, since=None, until=None, group_by: str = "site", limit: int = 15) -> dict:
        """How the user spent their time in ``[since, until)``, measured by the timeline.

        ``since``/``until`` as for :meth:`recall` (default: since local midnight,
        until now). ``group_by``: ``"app"`` (Google Chrome, Claude), ``"site"``
        (instagram.com; a window without a web page counts as its app) or
        ``"page"`` (one page or window title). Returns::

            {"since": ISO, "until": ISO, "group_by": str,
             "totals": {"present_s", "active_s", "passive_s", "away_s", "media_s",
                        "background_media_s", "switches", "activities", "first_at", "last_at"},
             "items": [{"label", "app", "host", "present_s", "active_s", "passive_s", "media_s",
                        "visits", "longest_s", "longest_start", "longest_end", "first_at", "last_at",
                        "titles": [{"title", "present_s"}]}],      # most time first, at most ``limit``
             "interleaving": [{"a", "b", "switches"}],             # back and forth between two activities
             "background_media": [{"app", "seconds"}],
             "meetings": [{"label", "app", "host", "start", "end", "seconds", "in_front_s", "mic_s", ...}],
             "calls": [{"app", "process", "start", "end", "ongoing", "seconds", "window_s", "in_front_s",
                        "background_s", "meanwhile", "drops", "power", "text", ...}],
             "background": [{"app", "kind": "call", "seconds"}],   # in a call while not in front
             "power": [{"kind": "off" | "asleep" | "unknown", "start", "end", "seconds", "source"}]}

        ``calls`` (yuki.memory.callspans): every call overlapping the window - an
        app using the microphone, in front or not - merged across memory-service
        restarts and short drops, with its real start and end even outside the
        window (``window_s`` is the part inside it), the time its app was in front
        and what else was in front meanwhile; ``text`` is one plain line for it.
        The item of a call's app gets ``call_s`` (call time inside the window);
        ``background`` is call time its app spent behind other windows.

        ``power`` (and ``totals.off_s`` / ``asleep_s`` / ``unknown_s``) is the
        PC's own history from Windows (yuki.memory.power): shut down, asleep, or
        stopped without a clean shutdown. Time in none of the rows and none of
        these spans is time the PC was on and memory was not recording.

        *present* = the user at it: *active* (input within the last minute)
        plus *passive* (no input while that app played media: watching or
        listening). *away* = no input for over a minute and no media from the
        app in front. *media_s* = that app's media playing. A *visit* is one
        uninterrupted run (another activity, an away stretch or a gap over a
        minute ends it); *longest* is the longest run. Times are ISO local.
        """
        from yuki.memory.timeline import aggregate

        if group_by not in ("app", "site", "page"):
            raise ValueError(f"group_by must be 'app', 'site' or 'page', not {group_by!r}")
        s, u = self._window(since, until)
        rows = self.store.timeline_between(s, u)
        agg = aggregate(rows, s, u, group_by, limit=max(1, int(limit)))
        agg["since"], agg["until"] = _iso(s), _iso(u)
        totals = agg["totals"]
        totals["first_at"], totals["last_at"] = _iso(totals["first_at"]), _iso(totals["last_at"])
        self._add_calls(agg, rows, s, u, group_by)
        for item in agg["items"]:
            item.pop("key", None)
            for k in ("longest_start", "longest_end", "first_at", "last_at"):
                item[k] = _iso(item[k])
        for span in agg.get("power", []):
            span["start"], span["end"] = _iso(span["start"]), _iso(span["end"])
        for span in agg.get("meetings", []):
            span["start"], span["end"] = _iso(span["start"]), _iso(span["end"])
        return agg

    #: The activity header's sizes: threads touched, things finished, dates ahead (and how far ahead), open items each.
    HEADER_THREADS = 6
    HEADER_DONE = 6
    HEADER_AHEAD = 6
    HEADER_AHEAD_DAYS = 7
    HEADER_OPEN_ITEMS = 2

    def activity_header(self, since=None, until=None, *, now: float | None = None) -> dict:
        """What changed and what is open in ``[since, until)`` (revamp 09 P2.1), for the head of the ``activity``
        result, so "what did I do / what's left / where was I" is answered from content first, not app minutes:

            {"since", "until",
             "threads": [{"ref": "W12", "title", "status", "state", "open": [...], "touched", "last"}],  # most events
             "done": [{"ref": "J345", "at", "app", "text"}],          # what the user finished, shipped, merged, sent
             "ahead": [{"ref", "at", "text", "kind"}]}                # dated items from now to a week ahead

        Code only assembles stored rows (thread events, the journal's milestone mark, dated commitments, goals,
        to-dos); the model words the answer. Never raises (empty lists)."""
        now = time.time() if now is None else float(now)
        s, u = self._window(since, until)
        out: dict[str, Any] = {"since": _iso(s), "until": _iso(u), "threads": [], "done": [], "ahead": []}
        try:
            for tid, n, last in self.store.work_threads_touched(s, u, limit=self.HEADER_THREADS):
                t = self.store.work_thread(tid)
                if t is None or t.status in ("merged", "dismissed"):
                    continue
                out["threads"].append({"ref": f"W{t.id}", "title": t.title, "status": t.status, "state": t.state,
                                       "open": list(t.open_items[: self.HEADER_OPEN_ITEMS]), "touched": n,
                                       "last": _iso(last)})
        except Exception:
            pass
        try:
            ids = [i for i, _at in self.store.milestones_since(0, s, u)]
            for j in self.store.journal_by_ids(ids[-self.HEADER_DONE:]):
                out["done"].append({"ref": f"J{j.id}", "at": _iso(j.at), "app": j.app, "text": j.fact})
        except Exception:
            pass
        horizon = now + self.HEADER_AHEAD_DAYS * 86400.0
        ahead: list[dict[str, Any]] = []
        try:
            from yuki.memory.nudges import todo_items

            for item in todo_items(self.store, now=now):
                if item.status == "open" and item.due_at is not None and now - 3600.0 <= item.due_at <= horizon:
                    ahead.append({"ref": item.ref, "at": item.due_at, "text": item.text, "kind": item.source})
        except Exception:
            pass
        try:
            for g in self.store.goals(("open", "unknown"), limit=20):
                when = g.due_at
                if when is None and g.day:
                    when = datetime.strptime(g.day, "%Y-%m-%d").timestamp()
                if when is not None and now - 3600.0 <= when <= horizon:
                    ahead.append({"ref": g.ref, "at": when, "text": g.text, "kind": "goal"})
        except Exception:
            pass
        ahead.sort(key=lambda a: a["at"])
        out["ahead"] = [{**a, "at": _iso(a["at"])} for a in ahead[: self.HEADER_AHEAD]]
        return out

    def _add_calls(self, agg: dict, rows: list, since: float, until: float, group_by: str) -> None:
        """``calls``, ``background`` and the items' ``call_s`` (yuki.memory.callspans); never raises."""
        from collections import Counter

        from yuki.memory.callspans import call_spans, describe_call
        from yuki.memory.timeline import group_of

        agg["calls"], agg["background"] = [], []
        try:
            now = time.time()
            spans = call_spans(self.store, since, until, now=now)
        except Exception:
            return
        by_process: dict[str, list[dict]] = {}
        background: dict[str, float] = {}
        for sp in spans:
            by_process.setdefault(sp["process"], []).append(sp)
            background[sp["app"]] = background.get(sp["app"], 0.0) + sp["window_background_s"]
            call = dict(sp, text=describe_call(sp, now=now, window=True))
            call["start"], call["end"] = _iso(sp["start"]), _iso(sp["end"])
            for part in (*call["drops"], *call["power"]):
                part["start"], part["end"] = _iso(part["start"]), _iso(part["end"])
            agg["calls"].append(call)
        agg["background"] = [{"app": a, "kind": "call", "seconds": round(v, 1)}
                             for a, v in sorted(background.items(), key=lambda kv: -kv[1]) if v >= 60]
        # the item a call's app is counted under at this grouping: where its own rows spent most time
        # during its calls (Discord's own window; for a call in a browser tab, its meeting rows)
        for process, calls in by_process.items():
            call_s = sum(sp["window_s"] for sp in calls)
            keys: Counter = Counter()
            for r in rows:
                if r.state == "present" and (r.process or app_key(r.app)) == process and any(
                        r.ended_at > sp["start"] and r.started_at < sp["end"] for sp in calls):
                    keys[group_of(r, group_by)[0]] += (r.active_s or 0.0) + (r.passive_s or 0.0) + 1.0
            for key, _ in keys.most_common():
                item = next((i for i in agg["items"] if i.get("key") == key), None)
                if item is not None:
                    item["call_s"] = round(item.get("call_s", 0.0) + call_s, 1)
                    break

    def current_calls(self) -> list[dict]:
        """The calls going on now (an app using the microphone, merged across memory-service restarts):
        ``[{"app", "process", "service", "since": ISO, "seconds"}]``, oldest first. Never raises."""
        from yuki.memory.callspans import open_calls

        try:
            return [dict(c, since=_iso(c["since"])) for c in open_calls(self.store)]
        except Exception:
            return []

    def episodes(self, since=None, until=None, limit: int = 50) -> list[dict]:
        """Episodes overlapping ``[since, until)`` (default today), oldest first.

        Each: ``{"start": ISO, "end": ISO, "text": str, "final": bool (False while
        its window is still open and may be rewritten), "totals": dict}``, and since migration 35 (the feedback
        round 3, W2) ``"id"``, ``"ref"`` (``episode:N``, what :meth:`correct_item` takes), ``"title"`` (a few words,
        distinct; None until written) and ``"summary"`` (one or two short sentences said to the user; None until
        written). ``text`` is memory's own narrative (third person, with times): the app shows title and summary,
        and the time per app as data from the timeline.
        """
        s, u = self._window(since, until)
        rows = self.store.episodes_between(s, u)[-max(1, int(limit)):]
        return [
            {"start": _iso(e.started_at), "end": _iso(e.ended_at), "text": e.text, "final": e.final,
             "totals": (e.aggregates or {}).get("totals") or {}, "id": e.id, "ref": f"episode:{e.id}",
             "title": getattr(e, "title", None), "summary": getattr(e, "summary", None)}
            for e in rows
        ]

    # -- to-dos and the coach's nudges (yuki.memory.nudges) ------------------------

    def todos(self, include_done: bool = False) -> list[dict]:
        """The user's to-do list: one merged view of three sources.

        * open loops from the portrait (what is owed by or to the user, on
          direct evidence), ``source: "loop"``;
        * Yuki's open commitments from conversation memory (what the user
          handed Yuki for later, or Yuki promised), ``source: "commitment"``;
        * to-dos the user added (:meth:`add_todo`), ``source: "user"``.

        Each item::

            {"id": "loop:12" | "commit:5" | "todo:3", "text": str,
             "title": str | None,        # the app's three-to-six-word title (migration 21, model-written;
                                         # None until written)
             "source": "loop" | "commitment" | "user",
             "due": ISO local time with offset ("2026-09-25T15:00:00+05:30"), or a
                    date "2026-09-25" when no time was given, or None,
             "status": "open" | "done",
             "evidence": str | None,     # a loop: who asked, where, when ("asked by Varun in
                                         # Slack on 2026-09-23"); a commitment: the user's words;
                                         # a user to-do: None
             "since": ISO}               # when it was opened / asked / added

        Open items first, soonest due first, undated ones after them (oldest
        first); with ``include_done``, then the items done in the last 14 days
        (newest first). Never raises (``[]`` if memory cannot be read).
        """
        from yuki.memory.nudges import item_dict, todo_items

        try:
            return [item_dict(i) for i in todo_items(self.store, include_done=include_done)]
        except Exception:
            return []

    def add_todo(self, text: str, due: str | None = None) -> str:
        """Add a to-do in the user's words; returns its id ``"todo:N"``.

        ``due``: ``None``, a date ``"YYYY-MM-DD"`` (due that day, no time: the
        reminder comes at the configured morning time, 09:00 by default), or a
        local date and time (``"YYYY-MM-DD HH:MM"``, ``"YYYY-MM-DDTHH:MM"``,
        full ISO with an offset, or epoch seconds as a string). With a due
        time the service writes a ``reminder`` nudge at that time (or at the
        first allowed moment after: not in quiet hours, a meeting, full
        screen, while snoozed, paused or away), once. Raises ``ValueError``
        for an empty text or a due it cannot read.
        """
        text = " ".join((text or "").split())
        if not text:
            raise ValueError("to-do text is empty")
        due_at, all_day = None, False
        if due is not None and str(due).strip():
            raw = str(due).strip()
            if len(raw) == 10 and raw[4] == "-" and raw[7] == "-":
                due_at, all_day = datetime.strptime(raw, "%Y-%m-%d").timestamp(), True
            else:
                try:
                    due_at = float(raw)
                except ValueError:
                    due_at = _time_arg(raw.replace(" ", "T", 1) if "T" not in raw else raw)
        return f"todo:{self.store.add_todo(text, due_at=due_at, all_day=all_day)}"

    def complete_todo(self, ref: str) -> str | None:
        """Mark one to-do done; returns its id, or ``None`` when nothing matched (or it was not open).

        ``ref`` is an id from :meth:`todos` or the standing context
        (``"loop:12"``, ``"commit:5"``, ``"todo:3"``): the model picks the
        item; no text is matched (docs/research/no-pattern-audit.md F).

        * a user to-do becomes ``done``;
        * a commitment ends as ``done`` (kept as history, "the user marked it done");
        * an open loop becomes ``done`` and its portrait fact is invalidated; a
          user correction is recorded ("the user said this is done") so the
          portrait follows it and later runs do not re-open it from old
          evidence, and the portrait is re-rendered soon (the
          ``refresh_portrait`` flag).
        """
        from yuki.memory.nudges import complete_item

        try:
            return complete_item(self.store, ref)
        except Exception:
            return None

    @staticmethod
    def _nudge_dict(n: Any) -> dict:
        from yuki.memory.offers import card_offer_view

        # a typed offer only (yuki.memory.offers): what the card shows and its button runs, by code; a free-text
        # offer from before 2026-10-03 gets no button (a sentence is not something code can run)
        inputs = getattr(n, "inputs", None)
        trigger = str(n.trigger or "")
        # a check-in card's moment (day_start, followup, ...), as Yuki wrote it: the card's title comes from it. The
        # card judge's check-ins carry it in their trigger (``checkin:<moment>``, jSwitch: the ledger counts them
        # without decrypting); its Today card asks the day's question (``today``, a question card: ``day_start``)
        moment = None
        if isinstance(inputs, dict) and trigger == "checkin":
            moment = inputs.get("moment")
        elif trigger.startswith("checkin:"):
            moment = trigger.split(":", 1)[1]
        elif trigger == "today" and n.kind == "question":
            moment = "day_start"
        # a call's keep list (report 76 item 5, yuki.memory.admission): its lines, each answered with recap_keep
        keep = inputs.get("keep") if isinstance(inputs, dict) and trigger == "recap" else None
        keep = [{"ref": str(k.get("ref")), "text": str(k.get("text") or ""),
                 "who": "others" if k.get("who") == "others" else "user"} for k in keep or []
                if isinstance(k, dict) and k.get("ref")] or None
        # the Today card / "While you were away" (the card judge, jSwitch): its numbered lines with their ids, so a
        # reply keeps which line it answers (nudge_reply_request) and the window's per-line Open / Not useful work
        lines = None
        if isinstance(inputs, dict) and trigger in ("today", "away") and isinstance(inputs.get("lines"), list):
            lines = [{"n": int(ln.get("n") or 0), "type": str(ln.get("type") or ""), "ref": str(ln.get("ref") or ""),
                      "text": str(ln.get("text") or "")} for ln in inputs["lines"] if isinstance(ln, dict)]
        return {"id": n.id, "at": _iso(n.at), "kind": n.kind, "text": n.text, "reason": n.reason, "ref": n.ref,
                "trigger": n.trigger, "offer": card_offer_view(n), "outcome": getattr(n, "outcome", None),
                "moment": moment, "outcome_at": _iso(getattr(n, "outcome_at", None)),
                **({"keep": keep, "head": str(inputs.get("head") or "")} if keep else {}),
                **({"lines": lines, "head": str(inputs.get("head") or "")} if lines is not None else {})}

    def _nudges_config(self) -> Any:
        """The user's ``[nudges]`` table (read only; the service applies the same), defaults when unreadable."""
        from yuki.memory.nudges import NudgeConfig

        try:
            from types import SimpleNamespace

            from yuki.memory.privacy import user_config_path
            from yuki.memory.timeline import PrivacySection

            if self._nudge_section is None:
                self._nudge_section = PrivacySection(SimpleNamespace(path=user_config_path()), "nudges",
                                                     NudgeConfig.from_dict)
            return self._nudge_section.get()
        except Exception:
            return NudgeConfig()

    def offer_act(self, nudge_id: int) -> dict:
        """The user pressed a card's offer button: check it again and run it, by code (:func:`yuki.memory.offers.act`):
        ``{"ok": True, "do", "state", ...}`` (``todo_add``: added; ``show_memory``: ``items``; ``bring_back``: the
        ``url`` and ``title`` for the UI to open; ``draft``: ``text``; ``ask``: ``text`` for the ask bar, unsent;
        ``run_skill``: ``running`` with the ``skill`` and ``slots`` the tray runs itself, then reports with
        :meth:`offer_result`) or ``{"ok": False, "why"}`` (``expired`` after 30 minutes, or a routine card's own
        expiry, ``gone``, ``not_ready``, ``off``, ``already``, ``unknown``, ``no_offer``). Never a model call, never an
        agent run; a second press changes nothing."""
        from yuki.memory import offers

        try:
            return offers.act(self.store, int(nudge_id), enabled=self._nudges_config().offers != "off")
        except Exception as exc:
            return {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}

    def offer_result(self, nudge_id: int, ok: bool, detail: dict | None = None) -> bool:
        """What came of a routine card's skill run in the tray (``run_skill``, PLAN.md Phase R): ``ok`` when its
        done-check saw it done. Only a ``running`` card moves (``done`` / ``failed``); the routine's ledger gets
        ``run_ok`` / ``run_failed`` with ``detail``'s content-free fields (:func:`yuki.memory.offers.offer_result`)."""
        from yuki.memory import offers

        try:
            return offers.offer_result(self.store, int(nudge_id), bool(ok),
                                       detail if isinstance(detail, dict) else None)
        except Exception:
            return False

    def routine_feedback(self, nudge_id: int, kind: str) -> dict:
        """The user's "Less of these" (``less``: early -> "started a minute ago" -> quiet; on a quiet routine, off) or
        a "don't offer this" (``never``: off) on a routine card, once per card
        (:func:`yuki.memory.routine_offers.feedback`): ``{"ok", "routine", "mode_from", "mode_to"}`` or ``{"ok":
        False, "why"}``. Code only, at once."""
        from yuki.memory import routine_offers

        try:
            return routine_offers.feedback(self.store, int(nudge_id), str(kind))
        except Exception as exc:
            return {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}

    def routine_card_due(self, nudge_id: int) -> dict:
        """Whether a routine card that waited in the tray's queue may still show now: an alias of :meth:`card_due`
        (for a routine card, :func:`yuki.memory.routine_offers.card_still_due`, unchanged), kept for an older tray."""
        return self.card_due(nudge_id)

    def _judge_config(self) -> Any:
        """The user's ``[judge]`` table (read only; the service applies the same), defaults when unreadable."""
        from yuki.memory.cardjudge import JudgeConfig

        try:
            from types import SimpleNamespace

            from yuki.memory.privacy import user_config_path
            from yuki.memory.timeline import PrivacySection

            if getattr(self, "_judge_section", None) is None:
                self._judge_section = PrivacySection(SimpleNamespace(path=user_config_path()), "judge",
                                                     JudgeConfig.from_dict)
            return self._judge_section.get()
        except Exception:
            return JudgeConfig()

    def card_due(self, nudge_id: int) -> dict:
        """The tray's question right before it shows **any** queued card (jSwitch, state-design §4.4 M1): ``{"due":
        True}`` or ``{"due": False, "why", "final"}``. A routine card: :func:`yuki.memory.routine_offers.card_still_due`
        (unchanged). Any other card: with the card judge live (``[judge] mode = "on"``) the policy runs again with
        fresh reads (:func:`yuki.memory.attend.fresh_check`: the switches, the game in front, an open call, the
        meeting, Yuki acting, quiet hours; a read that fails holds) - held is not final (the tray asks again on a later
        look); an outcome, a reaction or the card's own expiry is final (the tray drops it). With the judge off or in
        shadow every other card is due (the old chain's tray, exactly). Code only, never raises."""
        from yuki.memory import routine_offers

        now = time.time()
        try:
            n = self.store.nudge(int(nudge_id))
        except Exception:
            return {"due": False, "why": "error", "final": False}
        if n is None:
            return {"due": False, "why": "unknown", "final": True}
        if n.kind == "notice" and n.trigger == routine_offers.TRIGGER:
            try:
                return routine_offers.card_still_due(self.store, int(n.id), now=now)
            except Exception as exc:
                return {"due": False, "why": "error", "final": False, "error": type(exc).__name__}
        if self._judge_config().mode != "on":
            return {"due": True}
        if getattr(n, "outcome", None) is not None:
            return {"due": False, "why": f"outcome:{n.outcome}", "final": True}
        if n.reaction is not None:
            return {"due": False, "why": f"reaction:{n.reaction}", "final": True}
        inputs = n.inputs if isinstance(n.inputs, dict) else {}
        try:
            expires = float(inputs.get("expires_at")) if inputs.get("expires_at") is not None else None
        except (TypeError, ValueError):
            expires = None
        if expires is not None and now > expires:
            return {"due": False, "why": "expired", "final": True}
        try:
            from yuki.memory.attend import fresh_check
            from yuki.memory.candidates import TYPES
            from yuki.memory.cardjudge import type_of

            ctrig = None
            if n.checkin_id is not None:
                rows = self.store._query("SELECT trigger FROM nudge_checkins WHERE id = ?", (int(n.checkin_id),))
                ctrig = rows[0][0] if rows else None
            t = "checkin:day_start" if (n.trigger == "today" and n.kind == "question") else type_of(n.kind, n.trigger,
                                                                                                    ctrig)
            info = TYPES.get(t) if t else None
            # a weekly review the user asked for (review.py's on-demand path, unchanged by §4.4): their switches and
            # quiet hours don't hold it, a game, a call or a meeting still do
            asked = False
            if n.kind == "review" and inputs.get("review_id") is not None:
                rows = self.store._query("SELECT trigger FROM weekly_reviews WHERE id = ?", (int(inputs["review_id"]),))
                asked = bool(rows) and rows[0][0] == "demand"
            d = fresh_check(self.store, db_path=self.store.path, config=self._nudges_config(), now=now,
                            type=t or "*", urgency=info.urgency if info is not None else "any", asked=asked)
        except Exception as exc:
            return {"due": False, "why": "error", "final": False, "error": type(exc).__name__}
        if d.allow:
            return {"due": True}
        return {"due": False, "why": d.reason, "final": False}

    def card_queue(self) -> dict:
        """What's waiting (§4.7, the user's "what's waiting?": they pick the moment): the Today queue the card judge
        keeps while live, best first - ``{"mode", "at", "lines": [{"type", "ref", "asks", "text", "queued_at"}]}``
        (``ref``: the line's id, ``Q12`` a question to answer with ``answer_question("question:12", ...)``, ``W9`` a
        thread, ``R2`` the weekly review, ``C41`` a call). Empty with the judge off or in shadow. Never raises."""
        cfg = self._judge_config()
        out: dict[str, Any] = {"mode": cfg.mode, "at": None, "lines": []}
        if cfg.mode != "on":
            return out
        try:
            from yuki.memory.cardjudge import QUEUE_STATE

            raw = self.store.nudge_state(QUEUE_STATE)
            data = json.loads(self.store.cipher.decrypt(raw)) if raw else {}
        except Exception:
            return out
        if not isinstance(data, dict):
            return out
        now = time.time()
        lines = []
        for ln in data.get("lines") or []:
            if not isinstance(ln, dict):
                continue
            exp = ln.get("expires_at")
            if exp is not None and float(exp) <= now:
                continue
            lines.append({"type": str(ln.get("type") or ""), "ref": str(ln.get("ref") or ""),
                          "asks": bool(ln.get("asks")), "text": str(ln.get("text") or ""),
                          "queued_at": _iso(ln.get("queued_at"))})
        out.update(at=_iso(data.get("at")), lines=lines)
        return out

    def digest_line_reaction(self, nudge_id: int, n: int, reaction: str) -> dict:
        """One line of a Today card (or a "While you were away" list) answered from the window (question 7): ``open``
        (the user opened what it points at) or ``not_useful``. Recorded content-free (``judge_digest_reactions``: card
        id, line number, the reaction), once per line (a later press replaces it); the card judge's ledger reads it
        (an opened line is used, a not-useful one is not). Returns ``{"ok", "card", "n", "reaction", "type", "ref"}``
        (the line's type and id, so the window can open it) or ``{"ok": False, "why"}`` (``bad_reaction``,
        ``unknown``, ``not_on_card``). Code only."""
        reaction = str(reaction or "").strip().lower()
        if reaction not in ("open", "not_useful"):
            return {"ok": False, "why": "bad_reaction"}
        try:
            card = self.store.nudge(int(nudge_id))
        except Exception:
            card = None
        if card is None:
            return {"ok": False, "why": "unknown"}
        lines = card.inputs.get("lines") if isinstance(card.inputs, dict) else None
        try:
            num = int(n)
        except (TypeError, ValueError):
            return {"ok": False, "why": "not_on_card"}
        line = next((ln for ln in lines or [] if isinstance(ln, dict) and int(ln.get("n") or -1) == num), None)
        if line is None:
            return {"ok": False, "why": "not_on_card"}
        from yuki.memory.cardjudge import DIGEST_REACTIONS_STATE, STATE_KEEP

        try:
            data = json.loads(self.store.nudge_state(DIGEST_REACTIONS_STATE) or "{}")
            data = data if isinstance(data, dict) else {}
        except Exception:
            data = {}
        per = data.get(str(card.id)) if isinstance(data.get(str(card.id)), dict) else {}
        per[str(num)] = reaction
        data[str(card.id)] = per
        if len(data) > STATE_KEEP:
            keep = sorted(data, key=lambda k: int(k) if str(k).isdigit() else -1)[-STATE_KEEP:]
            data = {k: data[k] for k in keep}
        try:
            self.store.set_nudge_state(DIGEST_REACTIONS_STATE, json.dumps(data, separators=(",", ":")))
        except Exception as exc:
            return {"ok": False, "why": "error", "error": type(exc).__name__}
        return {"ok": True, "card": card.id, "n": num, "reaction": reaction, "type": str(line.get("type") or ""),
                "ref": str(line.get("ref") or "")}

    def digest_line_reactions(self) -> dict:
        """What :meth:`digest_line_reaction` recorded, for the window's Activity rows (jSwitch merge note):
        ``{card id: {line number: "open" | "not_useful"}}`` (strings), content-free. ``{}`` on none or an unreadable
        state. Never raises."""
        try:
            from yuki.memory.cardjudge import DIGEST_REACTIONS_STATE

            data = json.loads(self.store.nudge_state(DIGEST_REACTIONS_STATE) or "{}")
        except Exception:
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(card): {str(n): str(r) for n, r in per.items()} for card, per in data.items()
                if isinstance(per, dict)}

    def card_kinds(self) -> dict:
        """The window's Settings list of card kinds (question 7): ``{"mode": the judge's (off | shadow | on), "kinds":
        [{"kind", "words", "mode", "mode_words", "why", "why_words", "by", "shown", "used", "can_raise",
        "can_lower"}]}`` - each kind, where it is (a card at a good moment, a line on the Today card, only at the best
        moments, off) and why (where it starts, you said less of these, the last ones went unused, ...). Content-free
        counts; never raises."""
        try:
            from yuki.memory.cardjudge import kind_list

            return {"mode": self._judge_config().mode, "kinds": kind_list(self.store, time.time(),
                                                                           self._nudges_config())}
        except Exception as exc:
            return {"mode": None, "kinds": [], "error": type(exc).__name__}

    def card_kind_set(self, kind: str, action: str) -> dict:
        """The user moves one kind from the Settings list (question 7): ``up`` (back one step towards where it starts;
        the ledger counts its use afresh from now) or ``down`` (one step towards off, as "Less of these"). The user's
        word: recorded as theirs, and the ledger never raises a kind they lowered. Reminders, standing requests and the
        routine card keep their own rules. ``{"ok", "kind", "mode"}`` or ``{"ok": False, "why"}``. The memory
        service's judge reads it at its next ledger read (within five minutes)."""
        try:
            from yuki.memory.cardjudge import set_kind_mode

            return set_kind_mode(self.store, str(kind or ""), str(action or ""))
        except Exception as exc:
            return {"ok": False, "why": "error", "error": type(exc).__name__}

    def recap_keep(self, nudge_id: int, ref: Any, answer: str) -> dict:
        """The user's answer to one line of a call's keep list (the recap card, yuki.memory.admission): ``keep`` (into
        memory as said), ``joke`` (kept as a joke of theirs, never a fact), ``not_true`` (stays out) or ``undo`` (the
        answer taken back). Only a line that card carries is answered (``ref`` ``journal:N`` in its ``keep``). No
        model, at once. Returns ``{"ok", "ref", "answer", "nature"}``, ``ok`` False with ``why`` (``unknown``,
        ``not_on_card``, ``already``, ``moved``, ``bad_answer``). A line a re-journal replaced since the card was
        written is answered on the held fact that took its place (``moved_to``, :func:`yuki.memory.admission.moved_held`);
        ``moved`` when the re-journal's fact on that line is not held (in memory now, or left out)."""
        from yuki.memory import admission

        answer = str(answer or "").strip().lower()
        out: dict[str, Any] = {"ok": False, "ref": str(ref or ""), "answer": answer, "nature": None}
        if answer not in admission.ANSWERS:
            return {**out, "why": "bad_answer"}
        try:
            n = self.store.nudge(int(nudge_id))
        except Exception:
            n = None
        inputs = getattr(n, "inputs", None) if n is not None else None
        refs = {str(k.get("ref")) for k in (inputs or {}).get("keep") or [] if isinstance(k, dict)} \
            if isinstance(inputs, dict) else set()
        jid = self._journal_id(ref)
        if n is None:
            return {**out, "why": "unknown"}
        if jid is None or f"journal:{jid}" not in refs:
            return {**out, "why": "not_on_card"}
        return self._answer_held(jid, answer, out)

    def _answer_held(self, jid: int, answer: str, out: dict[str, Any]) -> dict:
        """One held fact answered (:meth:`recap_keep`, :meth:`resolve_held`). Review V3: a re-journal since the card or
        the window row was written replaced the line's held fact; the answer goes to the held fact that took its place,
        and when that line's new fact is not held there is nothing to answer (``moved``, never "already")."""
        from yuki.memory import admission

        try:
            target = admission.moved_held(self.store, jid)
            done = admission.resolve_held(self.store, target, answer)
            if done is None and admission.replaced(self.store, target):
                return {**out, "why": "moved", "error": "I read that again since, and it changed."}
        except Exception as exc:
            return {**out, "why": "error", "error": f"{type(exc).__name__}: {exc}"}
        if done is None:
            return {**out, "why": "already"}
        return {**out, "ok": True, "ref": f"journal:{jid}", "nature": done["nature"],
                **({"moved_to": f"journal:{target}"} if target != jid else {})}

    def held_facts(self, reasons: Any = None, days: float | None = None) -> list[dict]:
        """Facts memory is holding back until the user says yes (yuki.memory.admission), newest first: ``{"ref",
        "text" (said to the user), "fact", "reason" (call | sensitive), "at" ISO, "call", "who" (user | others: whose
        line, for a call), "kind" (call | page | message | ...: where it came from), "where", "shown" (``text`` is
        Yuki's second-person line, not the stored "The user ..." sentence)}``. Dropped ones (judged not meant) are
        never listed. For a private surface (the window's "Waiting for your yes"); a sensitive one never goes on a
        corner card. Answer with :meth:`resolve_held`. Read only, never raises."""
        from yuki.memory import admission

        try:
            wanted = tuple(reasons) if reasons else (admission.HOLD_CALL, admission.HOLD_SENSITIVE)
            since = None if days is None else time.time() - float(days) * 86400.0
            return [{"ref": f"journal:{h.id}", "text": h.shown, "fact": h.fact, "reason": h.reason, "at": _iso(h.at),
                     "call": f"call:{h.call_id}" if h.call_id is not None else None,
                     "who": "others" if h.source.get("who") == "others" else "user",
                     "kind": str(h.source.get("kind") or ""), "where": str(h.source.get("where") or ""),
                     "app": h.app,
                     "shown": bool(h.source.get(admission.SHOWN_KEY))}
                    for h in admission.held_facts(self.store, since=since, reasons=wanted)
                    if h.reason != admission.HOLD_DROP]
        except Exception:
            return []

    def resolve_held(self, ref: Any, answer: str, *, correction_id: int | None = None) -> dict:
        """The user's explicit answer to a held fact from a private surface (the window): as :meth:`recap_keep`,
        without a card (``keep`` here is the explicit yes a sensitive fact needs). ``correction_id`` is accepted and
        ignored (the window's answer path passes one; nothing here queues a correction pass)."""
        from yuki.memory import admission

        answer = str(answer or "").strip().lower()
        jid = self._journal_id(ref)
        out: dict[str, Any] = {"ok": False, "ref": str(ref or ""), "answer": answer, "nature": None}
        if jid is None or answer not in admission.ANSWERS:
            return {**out, "why": "bad_answer"}
        return self._answer_held(jid, answer, out)   # review V3: a row shown before a re-journal

    def offer_undo(self, nudge_id: int) -> dict:
        """Undo a card's offer that ran (``todo_add``: the to-do ends as a wrong capture; ``bring_back``: recorded,
        the UI brings back what was in front): ``{"ok": True, "state": "undone"}`` or ``{"ok": False, "why"}``."""
        from yuki.memory import offers

        try:
            return offers.undo(self.store, int(nudge_id))
        except Exception as exc:
            return {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}

    def offer_outcome(self, nudge_id: int, outcome: str) -> bool:
        """What became of an ``ask`` suggestion put in the ask bar: ``done`` (the request ran), ``stopped`` (stopped
        within a minute), ``abandoned`` (closed unsent). Read by the per-kind learning (yuki.memory.offers)."""
        from yuki.memory import offers

        try:
            return offers.record_outcome(self.store, int(nudge_id), str(outcome))
        except Exception:
            return False

    # -- questions for the user (yuki.memory.questions) -----------------------------

    @staticmethod
    def _question_id(ref: Any) -> int | None:
        """``question:12`` / ``12`` -> 12 (plumbing on our own id format); ``None`` otherwise."""
        text = str(ref or "").strip()
        prefix, sep, number = text.partition(":")
        if sep and prefix.strip() == "question":
            text = number.strip()
        return int(text) if text.isdigit() else None

    def answer_question(self, ref: Any, answer: str = "", outcome: str = "answered") -> dict:
        """Record the user's reply to one of Yuki's questions (``ref``: ``"question:12"``).

        ``outcome``: ``answered`` (``answer`` is the user's own words; it becomes a
        user-origin portrait correction that states the question it answers, and the
        portrait folds it into the facts it concerns within about a minute - the
        ``render_portrait`` flag - superseding a wrong one; the answer to one of Yuki's
        check-ins (origin ``checkin``) goes into the user's goals instead, folded by the
        service's card worker, yuki.memory.checkins), ``skipped`` (not now:
        not asked again) or ``never`` (don't ask about this). Returns ``{"saved":
        bool, "question": ref, "outcome", "correction_id", "why"}``; never raises for
        a question that is unknown or already answered (``saved`` false, ``why`` says).
        """
        qid = self._question_id(ref)
        if outcome not in ("answered", "skipped", "never"):
            raise ValueError("outcome must be answered, skipped or never")
        if qid is None:
            return {"saved": False, "question": ref, "outcome": outcome, "correction_id": None,
                    "why": "not a question id (question:<n>)"}
        q = self.store.question(qid)
        if q is None:
            return {"saved": False, "question": ref, "outcome": outcome, "correction_id": None, "why": "no such question"}
        if outcome == "answered" and not (answer or "").strip():
            return {"saved": False, "question": q.ref, "outcome": outcome, "correction_id": None,
                    "why": "an answer needs the user's words"}
        fact_id = self.store.answer_question(qid, outcome, answer or "")
        if fact_id is None:
            return {"saved": False, "question": q.ref, "outcome": outcome, "correction_id": None,
                    "why": f"already {q.status}"}
        if outcome == "answered" and q.origin != "checkin":   # a check-in's answer goes into the goals instead
            request_render(self.store.path)
        return {"saved": True, "question": q.ref, "outcome": outcome, "correction_id": fact_id or None,
                "subject": q.subject, "asked": q.question, "why": None}

    def skip_question(self, ref: Any, never: bool = False) -> dict:
        """The card's Skip (``never=False``) or "Don't ask" (``never=True``) for question ``ref``."""
        return self.answer_question(ref, "", "never" if never else "skipped")

    def questions(self, statuses: Any = None, limit: int = 50) -> list[dict]:
        """Yuki's questions for the user, newest first (content decrypted), for the UI and the agent."""
        rows = self.store.questions(tuple(statuses) if statuses else None, limit=limit)
        return [{"id": q.ref, "kind": q.kind, "subject": q.subject, "question": q.question, "evidence": q.evidence,
                 "importance": q.importance, "origin": q.origin, "status": q.status, "answer": q.answer,
                 "at": _iso(q.at), "asked_at": _iso(q.asked_at), "answered_at": _iso(q.answered_at)} for q in rows]

    def pending_nudges(self) -> list[dict]:
        """Nudges the service wrote that the UI has not shown yet, oldest first.

        Each: ``{"id": int, "at": ISO, "kind": "praise" | "nudge" | "reminder" |
        "review" | "resume" | "question" | "notice", "text": str, "reason": str | None, "ref", "trigger",
        "offer": dict | None, "outcome": str | None}`` (``notice``: an observation card, yuki.memory.presence,
        ``trigger`` its channel; ``offer``: its typed offer, :func:`yuki.memory.offers.card_offer_view` - ``do``,
        ``label``, ``steps``, ``refs``, ``text``, ``due``, the fixed ``wont`` line, ``undo``, ``expires_at``, and
        ``target`` / ``items`` - run by code through :meth:`offer_act`, never by the agent; ``outcome``: what came
        of it; ``resume``: picking up after the PC was off or asleep; it expires like a praise). ``text`` is what to show
        the user (one or two short sentences, already in their register and
        following their rules); ``reason`` is the coach's one-line why, for a
        tooltip or the log, not for the user. A ``review`` is the teaser of a
        new weekly review (:meth:`weekly_review` has the whole text; its reason
        says where it is). A praise or nudge not shown within 15 minutes
        (``[nudges] deliver_within_min``) expires and is no longer returned;
        reminders and reviews never expire. The service sets the named
        auto-reset event ``Local\\YukiNudgeReady`` after writing one, so the UI
        can wait on it and call this at once. Call :meth:`ack_nudge` with
        ``"shown"`` when one is on screen. Never raises.
        """
        try:
            from yuki.memory.nudges import NudgeConfig
            from yuki.memory.timeline import PrivacySection

            window, notice_window = NudgeConfig().deliver_within_min, NudgeConfig().presence_deliver_min
            try:   # the user's [nudges] table, read only (the service applies the same window)
                from types import SimpleNamespace

                from yuki.memory.privacy import user_config_path

                if self._nudge_section is None:
                    self._nudge_section = PrivacySection(SimpleNamespace(path=user_config_path()), "nudges",
                                                         NudgeConfig.from_dict)
                cfg = self._nudge_section.get()
                window, notice_window = cfg.deliver_within_min, cfg.presence_deliver_min
            except Exception:
                pass
            now = time.time()
            cutoff, notice_cutoff = now - window * 60.0, now - notice_window * 60.0
            return [self._nudge_dict(n) for n in self.store.nudges(pending=True)
                    if n.kind in ("reminder", "review") or n.at >= (notice_cutoff if n.kind == "notice" else cutoff)]
        except Exception:
            return []

    def recent_nudges(self, hours: float = 12) -> list[dict]:
        """Nudges written in the last ``hours`` that were shown to the user, oldest first. Read-only.

        Each: :meth:`pending_nudges`'s fields plus ``"shown_at": ISO`` and
        ``"reaction": "shown" | "dismissed" | "replied" | "snoozed"``
        (``shown``: it was on screen and nothing more, e.g. a card that faded
        unread). Never-shown ones (pending or expired) are left out. For the
        UI's history of what Yuki said. An observation card (kind ``notice``) that expired before the screen was
        free is in it too, with ``"missed": True`` and no ``shown_at``: it stays reachable, never shown as new.
        Never raises.
        """
        try:
            since = time.time() - max(0.0, float(hours)) * 3600.0
            return [{**self._nudge_dict(n), "shown_at": _iso(n.shown_at), "reaction": n.reaction,
                     **({"missed": True} if n.shown_at is None else {})}
                    for n in self.store.nudges(since=since)
                    if n.shown_at is not None or (n.kind == "notice" and n.reaction == "expired")]
        except Exception:
            return []

    def ack_nudge(self, nudge_id: int, reaction: str, reply: str | None = None) -> None:
        """Record what happened to a nudge: ``"shown"`` | ``"dismissed"`` | ``"replied"`` | ``"snoozed"`` |
        ``"accepted"`` (the card's offer button was pressed; what came of it is the card's ``outcome``,
        :meth:`offer_act`) | ``"less"`` ("Less of these": feedback on that kind of card, read by its budget and by
        COACH FEEDBACK; never a mute of others).

        ``shown`` when it appears on screen (it leaves :meth:`pending_nudges`);
        then, if the user acts on it, ``dismissed`` / ``replied`` / ``snoozed``
        (a later reaction replaces ``shown``; ``shown`` never replaces a real
        reaction). ``snoozed`` only records the reaction: call
        :meth:`snooze_nudges` for the quiet period itself. The coach reads the
        reactions (it gets sparser after dismissals) and the nightly portrait
        gets their counts for its Relationship section. Raises ``ValueError``
        for another reaction; an unknown id is ignored.
        """
        words = secret_filter.redact_text(reply) if reply else None
        self.store.react_to_nudge(int(nudge_id), str(reaction), reply=words)

    def snooze_nudges(self, minutes: int) -> None:
        """No check-ins and no reminders for ``minutes`` from now (a due reminder waits, it is not lost);
        ``0`` ends a snooze. The service sees it at its next pass (every 15 s)."""
        minutes = max(0, int(minutes))
        self.store.set_nudge_state("quiet_until", str(time.time() + minutes * 60.0) if minutes else None)

    def nudge_status(self) -> dict:
        """The coach at a glance (content-free).

        ``{"quiet_until": ISO | None (a snooze in force), "last_nudge_at": ISO |
        None (the newest nudge of any kind), "today": {"praise": n, "nudge": n,
        "reminder": n}}`` - nudges written since local midnight.
        """
        out: dict[str, Any] = {"quiet_until": None, "last_nudge_at": None,
                               "today": {"praise": 0, "nudge": 0, "reminder": 0, "resume": 0, "question": 0,
                                         "notice": 0}}
        try:
            now = time.time()
            quiet = self.store.nudge_state("quiet_until")
            if quiet is not None and float(quiet) > now:
                out["quiet_until"] = _iso(float(quiet))
            last = self.store.nudges(limit=1)
            if last:
                out["last_nudge_at"] = _iso(last[-1].at)
            midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            for n in self.store.nudges(since=midnight):
                if n.kind in out["today"]:
                    out["today"][n.kind] += 1
        except Exception:
            pass
        return out

    def coach_context(self, now: float | None = None) -> str:
        """Why Yuki's coach is quiet or what shapes it, as a few lines for the agent; ``""`` when unremarkable.

        Content-free counts plus the coach's own one-line reasons (``nudge_checkins``,
        encrypted): the last card, how many looks said nothing since, and the latest
        reasons - so "no messages from you, what happened?" gets the real answer (on
        2026-09-25 the answer was a one-word reply read as a rule). Only when the coach
        has been silent through several looks in the last hours, or has not run at all
        while the user was around. Hard cap :data:`COACH_CONTEXT_CHARS`.
        """
        now = time.time() if now is None else float(now)
        try:
            since = now - COACH_QUIET_S
            from yuki.memory.nudges import CHECKIN_TRIGGERS, PRESENCE_TRIGGERS

            checks = [c for c in self.store.checkin_reasons(since, limit=60)
                      if c["trigger"] not in ("reminder", "review", "question", "confirm", "resume", *PRESENCE_TRIGGERS,
                                              *CHECKIN_TRIGGERS)]
            last = self.store.nudges(until=now + 1.0, kinds=("praise", "nudge", "resume", "reminder", "question"),
                                     limit=1)
            quiet = self.store.nudge_state("quiet_until")
        except Exception:
            return ""
        lines: list[str] = []
        if quiet is not None:
            try:
                if float(quiet) > now:
                    lines.append(f"- Your cards are snoozed until {datetime.fromtimestamp(float(quiet)):%H:%M}.")
            except ValueError:
                pass
        silent = [c for c in checks if (c["outcome"] == "none" or c["outcome"].startswith(("suppressed", "skipped")))
                  and (not last or c["at"] > last[-1].at)]   # the looks since the last card that said nothing
        if len(silent) >= COACH_QUIET_LOOKS:
            when = (f"the last card was {last[-1].kind} at {datetime.fromtimestamp(last[-1].at):%a %H:%M}" if last
                    else "no card yet")
            said = [c for c in silent if c["reason"]][-3:]
            lines.append(f"- You looked {len(silent)} times in the last {COACH_QUIET_S / 3600:.0f} h and put up no "
                         f"card ({when})" + (". Your latest reasons:" if said else "."))
            for c in said:
                lines.append(f"  - {datetime.fromtimestamp(c['at']):%H:%M} {_clip_sentences(c['reason'], 220) or ''}")
        if not lines:
            return ""
        head = "Your own cards (the ones you put on their screen by yourself) right now:"
        text = "\n".join([head, *lines])
        while len(text) > COACH_CONTEXT_CHARS and len(lines) > 1:
            lines.pop()
            text = "\n".join([head, *lines])
        return text[:COACH_CONTEXT_CHARS]

    def day_context(self, now: float | None = None) -> str | None:
        """When the user's day at the PC began: the end of their last long break (``None`` when unknown).

        "This morning" and "today" mean since they sat down, not since midnight (on
        2026-09-25 "how have I been doing since this morning?" counted the 00:00-05:00
        VALORANT night as morning); the model decides what their words mean.

        yConsumers (state-design.md §2.2, §6.1): a thin wrapper. The memory service's "you right now" record already
        holds it (``presence.day_began``, one small answer on the Now pipe while the record is fresh); else the one
        pure rule, :func:`yuki.memory.younow.day_began_from` (the latest settled stretch after a break of
        ``max(DAY_BREAK_S, resume_after_h)``, the user's ``[nudges]`` table), which the record, the check-ins and the
        card judge share. The brain's request turn carries it in its you-now block; this stays for the old loop's
        memory pack and is deleted in jPrune.
        """
        now = time.time() if now is None else float(now)
        found: tuple[float, float] | None = None
        known = False
        try:
            from yuki.memory.now import shared_client
            from yuki.memory.younow import STALE_S

            client = shared_client(getattr(self.store, "path", None))
            you = client.you(sections=("presence",), wait_s=0.05) if client is not None else None
            pres = (you or {}).get("presence") or {}
            if you and now - float(you.get("beat_at") or 0.0) <= STALE_S and (you.get("built") or {}).get(
                    "presence", {}).get("known"):
                known = True
                if pres.get("day_began") is not None:
                    found = (float(pres["day_began"]), float(pres.get("day_gap_s") or 0.0))
        except Exception:
            known = False
        if not known:
            try:
                from yuki.memory.younow import day_began_from

                found = day_began_from(self.store, now, self._nudges_config())
            except Exception:
                return None
        if found is None:
            return None
        start, gap = found
        from yuki.memory.timeline import format_duration

        day = datetime.fromtimestamp(start)
        return (f"The user's day at the PC began {day:%A %H:%M}, after {format_duration(gap)} away; \"this morning\" "
                f"and \"today\" in their words mean since then, not since midnight.")

    # -- the weekly review (yuki.memory.review) --------------------------------

    @staticmethod
    def _review_week(which: Any) -> str | None:
        """``None`` for the latest; else an ISO week key from ``"2026-W39"`` or a date/datetime in that week."""
        if which is None or (isinstance(which, str) and which.strip().lower() in ("", "latest")):
            return None
        from yuki.memory.review import iso_week

        text = str(which).strip().upper().replace(" ", "")
        if "W" in text:
            year, _, week = text.partition("W")
            year = year.rstrip("-")
            if year.isdigit() and week.isdigit() and 1 <= int(week) <= 53:
                return f"{int(year):04d}-W{int(week):02d}"
            raise ValueError(f"not an ISO week: {which!r} (use 'latest', '2026-W39' or a date)")
        try:
            at = _time_arg(which)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"not an ISO week or a date: {which!r} (use 'latest', '2026-W39' or a date)") from exc
        if at is None:
            return None
        if isinstance(which, str) and len(which.strip()) == 10:
            at += 12 * 3600.0   # a bare date: its midday, whatever the review day boundary
        return iso_week(at)

    def weekly_review(self, which: Any = "latest") -> dict | None:
        """The newest finished weekly review (``"latest"``), or the newest one of an ISO week.

        ``which``: ``"latest"``, an ISO week (``"2026-W39"``: the week of the
        review period's last day) or a date inside that week. ``None`` when
        there is no such review (or memory cannot be read); ``ValueError`` for a
        ``which`` it cannot read. Returns::

            {"id": int, "week": "2026-W39", "trigger": "scheduled" | "demand",
             "start": ISO (the first review day, 04:00), "end": ISO (when its numbers stop),
             "written_at": ISO, "text": str,            # the review as shown, parts under headings
             "sections": {"about", "time", "focus", "done", "still_open": str,
                          "suggestions": [str], "mentions": [to-do ids]},
             "teaser": str, "summary": [str],           # a few lines of the key numbers (computed)
             "numbers": dict,                           # everything computed (yuki.memory.review.week_numbers)
             "candidates": [dict],                      # behaviour patterns proposed to the portrait
             "candidates_given_to_run": int | None, "nudge_id": int | None,
             "model": str, "cost_usd": float | None}
        """
        week = self._review_week(which)
        try:
            rows = self.store.weekly_reviews(week=week, outcomes=("ok",), limit=1)
        except Exception:
            return None
        if not rows:
            return None
        from yuki.memory.review import summary_lines

        r = rows[0]
        try:
            summary = summary_lines(r.numbers)
        except Exception:
            summary = []
        return {
            "id": r.id, "week": r.week, "trigger": r.trigger, "start": _iso(r.period_start), "end": _iso(r.period_end),
            "written_at": _iso(r.finished_at or r.at), "text": r.text, "sections": r.sections, "teaser": r.teaser,
            "summary": summary, "numbers": r.numbers, "candidates": r.candidates,
            "candidates_given_to_run": r.candidates_run_id, "nudge_id": r.nudge_id, "model": r.model,
            "cost_usd": r.cost_usd,
        }

    def run_weekly_review(self) -> None:
        """Ask the service to write a weekly review of the last seven days now (the ``run_weekly_review`` flag file).

        Non-blocking: returns at once. The service sees the flag within about a
        second while it runs (or at its next start), deletes it and writes the
        review (one Sonnet call, 30-90 s), even while memory is paused; its
        teaser then arrives as a ``review`` nudge once the user is at the PC.
        """
        path = self._flag(REVIEW_FLAG)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")

    # -- calls: both sides, each track labelled (yuki.memory.calls) ----------

    def call_status(self) -> dict:
        """The call going on now, for the UI's consent card and recording indicator (what is kept: ``mic``, ``tracks``).

        ``{"call": {...} | None, "recording": bool}``; the call is
        ``{"id", "app", "status", "asking", "consent", "headphones",
        "form_factor", "output_device", "detected_at", "recording_since",
        "session_started_at", "ask_timeout_s", "can_remember_choice"}``
        (``session_started_at``: when the call session began - its first row of
        the same microphone hold, else merged as the Today chart merges rows;
        the pill's timer counts from it). ``asking`` is True while the
        card should be up (no answer yet); ``status`` is ``asking`` |
        ``recording`` | ``waiting_headphones`` (said yes, output is not
        headphones: the microphone stays closed) | ``declined`` |
        ``unanswered`` | ``not_asked`` (nudges were snoozed) | ``stopped`` |
        ``ask_later`` (a game's voice chat whose card waits until it can be seen).
        Content-free. The service sets ``Local\\YukiCallReady`` whenever it
        changes. Never raises.

        Since 3 Oct 2026 a voice session can hold several rows at once (a call
        and a game's voice chat, ``kind`` "call" | "game") and records the
        other side too. ``call`` is a row still going on: the one whose card is
        up (asking, no answer), else one that records (``recording`` |
        ``waiting_headphones``: the indicator and Stop follow it), else the
        call (any status, as before). A game's row is ``call`` only while it
        records or its card is up: a declined, never, not-asked or waiting
        (``ask_later``) game is never ``call`` (voice treats any ``call`` as
        call mode). An ended row is never ``call``. ``recording`` is True
        whenever anything is being kept: a row recording, the microphone or any
        app track recording.
        Added: ``pending_card`` (the card of a game session that ended before
        it could be shown, up now: ``{"id", "app", "kind", "status", "asking",
        "consent", "detected_at", "ended_at", "ask_timeout_s",
        "can_remember_choice"}`` or None - the UI shows it like ``call``'s card;
        its answer goes to ``answer_call`` the same way), ``sessions`` (every
        row still going on, and a pending card's row with ``"ended": True``:
        ``{"id", "app", "kind", "status", "asking", "ended"}``), ``mic``
        (``{"state": "recording" | "paused" | "off" | "asking", "call_id",
        "reason": "not_headphones" | "mic_released" | "call_not_consented" |
        "game_closed" | "game_not_in_use" | "mic_error" | None, "speech_s"}``:
        the user's microphone, recorded once for the session) and ``tracks``
        (``[{"call_id", "app", "process", "track_kind": "call" | "game",
        "state": "recording" | "starting" | "paused" | "off" | "unavailable" |
        "asking", "reason", "speech_s"}]``: each app's own audio, the other
        people). ``stall`` (report 76): a row that records but kept nothing new for ``[calls] stall_min``
        (``{"call_id", "app", "kind", "since", "minutes", "reason": "no_audio" | "no_speech"}``) or None; the
        island says so.
        """
        try:
            from yuki.memory.calls import CallConfig
            from yuki.memory.store import CALL_OPEN_STATUSES

            waiting = self.call_audio_waiting()
            opened = self.store.calls(statuses=CALL_OPEN_STATUSES, limit=8)
            rows = [r for r in opened if r.ended_at is None]
            # a game session that ended before its card could be shown: the card is up now, the session is over
            pending = next((r for r in opened if r.ended_at is not None and r.status == "asking"
                            and r.consent is None), None)
            off = {"state": "off", "call_id": None, "reason": None, "speech_s": 0.0}
            if not rows and pending is None:
                return {"call": None, "recording": False, "waiting": waiting, "sessions": [], "mic": off,
                        "tracks": [], "pending_card": None}

            def asking(r: Any) -> bool:
                return r.status == "asking" and r.consent is None

            # a game's row is "call" only while it records or its card is up; a call's row whatever its status
            live = [r for r in rows if r.kind != "game" or r.status in ("recording", "waiting_headphones") or asking(r)]
            c = (next((r for r in live if asking(r)), None)
                 or next((r for r in live if r.status in ("recording", "waiting_headphones")), None)
                 or next((r for r in live if r.kind == "call"), None))
            timeout = CallConfig().ask_timeout_s
            try:
                from types import SimpleNamespace

                from yuki.memory.privacy import user_config_path
                from yuki.memory.timeline import PrivacySection

                timeout = PrivacySection(SimpleNamespace(path=user_config_path()), "calls",
                                         CallConfig.from_dict).get().ask_timeout_s
            except Exception:
                pass
            mic, tracks = self._call_tracks(rows) if rows else (dict(off), [])
            stall = self._call_stall(rows)
            sessions = [{"id": r.id, "app": r.app, "kind": r.kind, "status": r.status, "asking": asking(r),
                         "ended": False} for r in rows]
            card = None
            if pending is not None:
                sessions.append({"id": pending.id, "app": pending.app, "kind": pending.kind, "status": pending.status,
                                 "asking": True, "ended": True})
                card = {"id": pending.id, "app": pending.app, "kind": pending.kind, "status": pending.status,
                        "asking": True, "consent": pending.consent, "detected_at": _iso(pending.detected_at),
                        "ended_at": _iso(pending.ended_at), "ask_timeout_s": timeout,
                        "can_remember_choice": bool(pending.process)}
            # anything being kept: a row recording, the microphone, or any app's own audio (Stop must be reachable)
            recording = (any(r.status == "recording" for r in rows) or mic.get("state") == "recording"
                         or any(t.get("state") == "recording" for t in tracks))
            if c is None:
                return {"call": None, "recording": recording, "waiting": waiting, "sessions": sessions, "mic": mic,
                        "tracks": tracks, "pending_card": card, "stall": stall}
            session_start = None
            try:   # the call session this row is in: its first row of the same hold (or merged), for the pill
                from yuki.memory.callspans import session_start as _session_start

                session_start = _session_start(self.store, c.id)
            except Exception:
                session_start = None
            call = {
                "id": c.id, "app": c.app, "status": c.status, "consent": c.consent,
                "asking": c.status == "asking" and c.consent is None,
                "headphones": c.headphones, "form_factor": c.output_form_factor, "output_device": c.output_device,
                "detected_at": _iso(c.detected_at), "recording_since": _iso(c.recording_started_at),
                # When the call session began (05 §7.4): the first row of the same microphone hold, else the rows
                # merged as the Today chart merges them. The pill counts from here, never from this row alone.
                "session_started_at": _iso(session_start if session_start is not None else c.detected_at),
                "ask_timeout_s": timeout, "can_remember_choice": bool(c.process),
                # How sure the call is (yuki.memory.callevidence): "fresh" = the screen showed the joined call
                # recently; "stale" = it did, but long ago (voice asks "was that for me?" on ambiguous words).
                "evidence": "stale" if c.via == "screen_stale" else "fresh",
                # Other people in the call as its screen shows (0: the user is alone in it; None: not shown). Voice
                # treats a call with nobody else in it as no call (v3.15).
                "others": self._call_others(c.id),
                # call | game (a game's own voice chat, migration 34)
                "kind": c.kind,
            }
            return {"call": call, "recording": recording, "waiting": waiting, "sessions": sessions, "mic": mic,
                    "tracks": tracks, "pending_card": card, "stall": stall}
        except Exception:
            return {"call": None, "recording": False, "waiting": None}

    def _call_tracks(self, rows: list) -> tuple[dict, list[dict]]:
        """The microphone's and each app track's state for the open rows (the call worker's ``call_tracks`` state,
        when it is about these rows), with the speech kept so far per track. Content-free."""
        import json as _json

        from yuki.memory.calls import TRACKS_STATE

        ids = [r.id for r in rows]
        try:
            data = _json.loads(self.store.nudge_state(TRACKS_STATE) or "{}")
        except Exception:
            data = {}
        if not isinstance(data, dict) or not set(data.get("rows") or []) & set(ids):
            data = {}
        speech = self.store.track_speech(ids)
        mic = dict(data.get("mic") or {"state": "off", "call_id": None, "reason": None})
        mic["speech_s"] = round(sum(v for (side, _p), v in speech.items() if side != "others"), 1)
        known = {int(t.get("call_id") or 0): t for t in (data.get("tracks") or []) if isinstance(t, dict)}
        tracks = []
        for r in rows:
            t = known.get(r.id) or {}
            state = t.get("state") or ("asking" if r.status in ("asking", "ask_later") and r.consent is None else "off")
            tracks.append({"call_id": r.id, "app": r.app, "process": r.process, "track_kind": r.kind, "state": state,
                           "reason": t.get("reason"),
                           "speech_s": round(speech.get(("others", r.process or ""), 0.0), 1)})
        return mic, tracks

    def _call_stall(self, rows: list) -> dict | None:
        """A row going on that records but kept nothing new for a while (the call worker's measured flag in its
        ``call_tracks`` state: ``{"call_id", "app", "kind", "since", "minutes", "reason": "no_audio" |
        "no_speech"}``), or None. Only for one of ``rows``. Content-free."""
        import json as _json

        from yuki.memory.calls import TRACKS_STATE

        try:
            data = _json.loads(self.store.nudge_state(TRACKS_STATE) or "{}")
        except Exception:
            return None
        stall = data.get("stall") if isinstance(data, dict) else None
        if not isinstance(stall, dict) or stall.get("call_id") not in {r.id for r in rows}:
            return None
        return dict(stall)

    def _call_others(self, call_id: int) -> int | None:
        try:
            import json as _json

            raw = self.store.nudge_state("call_others")
            data = _json.loads(raw) if raw else None
            if isinstance(data, dict) and data.get("call_id") == call_id and data.get("others") is not None:
                return int(data["others"])
        except Exception:
            pass
        return None

    def call_audio_waiting(self) -> dict | None:
        """The user's call audio waiting to be transcribed: ``{"calls", "chunks", "minutes", "mb",
        "oldest_at"}`` (content-free), or None when memory cannot be read. Shown as
        "N calls waiting to be remembered"."""
        try:
            w = self.store.spool_summary()
            return {"calls": int(w["calls"] or 0), "chunks": int(w["chunks"] or 0),
                    "minutes": round(float(w["audio_s"] or 0) / 60.0, 1), "mb": round(float(w["bytes"] or 0) / 2**20, 2),
                    "oldest_at": _iso(w["oldest_at"])}
        except Exception:
            return None

    def erase_call_audio(self) -> int:
        """Delete every piece of call audio still waiting to be transcribed (the tray's "Erase waiting call
        audio"), every track: the user's microphone and each app's own sound (the other people on a call, a game's
        voice chat). The encrypted files go now, their words are never made. Returns the chunks erased. What was
        already transcribed stays remembered."""
        rows = self.store.call_chunks()
        erased, _size = self.store.remove_call_chunks([r["id"] for r in rows], "erased")
        for call_id in sorted({r["call_id"] for r in rows}):
            try:   # a call whose other chunks were already transcribed still reaches the journal
                self.store.journal_call_chunks(call_id)
            except Exception:
                pass
        return erased

    def answer_call(self, call_id: int, answer: str) -> bool:
        """The user's answer on the consent card: ``yes`` | ``no`` | ``always`` | ``never`` | ``none``
        (the card went away unanswered). ``always``/``never`` also hold for later calls of that app.
        False when the call no longer waits for an answer. Wakes the service at once."""
        from yuki.memory.store import call_event_name, signal_event

        ok = self.store.answer_call(int(call_id), str(answer), by="card" if answer != "none" else "ui_timeout")
        signal_event(call_event_name(self.store.path))
        return ok

    def stop_call(self) -> int | None:
        """Stop remembering the call going on now (the tray's "Stop remembering this call"); its id or None."""
        from yuki.memory.store import call_event_name, signal_event

        call_id = self.store.request_call_stop()
        signal_event(call_event_name(self.store.path))
        return call_id

    def call_prefs(self) -> list[dict]:
        """The user's standing call answers: ``[{"app", "decision": "always"|"never", "name", "updated_at"}]``."""
        try:
            return self.store.call_prefs()
        except Exception:
            return []

    def set_call_pref(self, app: str, decision: str | None) -> None:
        """Keep ``always``/``never`` for a call app, or forget it (``None``: ask again every call)."""
        self.store.set_call_pref(app, decision)

    # -- what happened on calls and meetings (yuki.memory.calldigest) ---------------

    @staticmethod
    def call_id_of(ref: Any) -> int | None:
        """A call row id from how the tools show it: ``call:16``, ``[call:16]``, ``16``, ``call:16-21``."""
        text = str(ref or "").strip().strip("[]").strip()
        if text.lower().startswith("call:"):
            text = text[5:]
        text = text.split("-")[0].strip()
        try:
            return int(text)
        except ValueError:
            return None

    def _call_session(self, call_id: int) -> dict | None:
        """The merged session (callspans) that call row ``call_id`` belongs to, or None."""
        from yuki.memory.callspans import merged_calls

        row = self.store.call_record(call_id)
        if row is None:
            return None
        for span in merged_calls(self.store, row.detected_at, (row.ended_at or time.time()) + 1.0):
            if call_id in span["call_ids"]:
                return span
        return None

    def _call_track_rows(self, call_ids: Any) -> list[dict]:
        """The speech kept per call row and track, every chunk transcribed or not (content-free):
        ``[{"call_id", "who": "user" | "others", "app", "track_kind": "call" | "game", "speech_s"}]`` -
        :meth:`Store.track_speech`'s sums with the app and kind of each track, which it does not return. ``who``
        "others" is an app's own sound (the other people on a call, or a game's voice chat); a chunk from before
        3 Oct 2026 is the user's."""
        ids = sorted({int(i) for i in call_ids})
        if not ids:
            return []
        rows = self.store._query(
            "SELECT call_id, side, coalesce(track_app, '') AS app, coalesce(track_kind, '') AS kind,"
            " coalesce(sum(speech_s), 0) AS s FROM call_chunks"
            f" WHERE call_id IN ({','.join('?' * len(ids))}) GROUP BY call_id, side, track_app, track_kind", ids)
        return [{"call_id": int(r["call_id"]), "who": "others" if r["side"] == "others" else "user",
                 "app": r["app"], "track_kind": r["kind"] or "call", "speech_s": float(r["s"] or 0.0)} for r in rows]

    def _session_dict(self, span: dict, rows: dict, digests: dict, waiting: dict,
                      tracks: list[dict] | None = None) -> dict:
        """One merged call for the tools: times, each side's speech, what was recorded, digest state and gists.
        ``tracks``: :meth:`_call_track_rows` of (at least) the session's rows."""
        ids = list(span["call_ids"])
        mine = [t for t in tracks or () if t["call_id"] in ids]
        kept = {t["call_id"] for t in mine}       # rows that kept audio of either side
        speech = 0.0
        gists, parts = [], []
        for cid in ids:
            r, d = rows.get(cid), digests.get(cid)
            if r is None:
                continue
            if d is not None and d["status"] in ("ok", "empty"):
                speech += float(d["speech_s"] or 0.0)
                state = "timeline written" if d["status"] == "ok" else "nothing worth a line"
                if d.get("gist"):
                    # ``shown``: the gist said to the user (migration 33), None until written
                    gists.append({"call_id": cid, "gist": d["gist"], "shown": d.get("gist_shown")})
            elif r.recording_started_at is None and cid not in kept:
                state = ("not recorded (the user said no)" if r.consent in ("no", "never") else
                         "not recorded (no headphones, or stopped)" if r.consent in ("yes", "always") else
                         "not recorded (not asked or no answer)")
            elif waiting.get(cid):
                speech += float(r.speech_s or 0.0)
                state = "audio waiting to be transcribed"
            elif r.captures:
                speech += float(r.speech_s or 0.0)
                state = ("timeline failed" if d is not None and d["status"] == "failed"
                         else "timeline being written" if d is not None and d["status"] == "running"
                         else "timeline not written yet")
            else:
                state = "nothing said"
            parts.append({"call_id": cid, "start": _iso(r.detected_at), "end": _iso(r.ended_at), "state": state})
        # the user's microphone recorded on a row; the others' side: an app's own sound kept speech (3 Oct 2026 on)
        user_recorded = any(rows.get(c) is not None and rows[c].recording_started_at is not None for c in ids)
        others: dict[tuple[str, str], float] = {}
        for t in mine:
            if t["who"] == "others":
                key = (t["app"] or span["app"] or "", t["track_kind"])
                others[key] = others.get(key, 0.0) + t["speech_s"]
        app_tracks = [{"app": a, "track_kind": k, "speech_s": round(s, 1)}
                      for (a, k), s in sorted(others.items(), key=lambda kv: -kv[1])]
        # the user's answer that let it record (or not): "always" / "never" = their standing call rule, "yes" / "no"
        # = their answer on that call's card; the first row that has one (the Activity ledger's "because")
        consent = next((rows[c].consent for c in ids if rows.get(c) is not None and getattr(rows[c], "consent", None)),
                       None)
        from yuki.memory.calldigest import merge_people

        # who was on it and where, as the screen around the call showed (the digest's reading; migration 25)
        contexts = [(digests.get(c) or {}).get("context") or {} for c in ids]
        people = merge_people(*(c.get("people") or [] for c in contexts))
        where = list(dict.fromkeys(c["where"] for c in contexts if c.get("where")))
        from yuki.memory.titles import place_label

        # the structured place (migration 33's digests) as a label, "Discord, #general voice (Friends)"
        places = list(dict.fromkeys(place_label(c.get("place")) for c in contexts if c.get("place")))
        return {
            "ref": f"call:{ids[0]}", "call_ids": ids, "app": span["app"], "service": span.get("service"),
            "kind": span.get("kind", "call"),   # call | game (a game's own voice chat)
            "start": _iso(span["start"]), "end": _iso(span["end"]), "ongoing": bool(span["ongoing"]),
            "seconds": round(sum(e - b for b, e in span["pieces"]), 1), "speech_s": round(speech, 1),
            "recorded": user_recorded or bool(app_tracks), "user_recorded": user_recorded,
            "others_recorded": bool(app_tracks), "others_speech_s": round(sum(others.values()), 1),
            "tracks": app_tracks, "consent": consent,
            "digested": [c for c in ids if (digests.get(c) or {}).get("status") == "ok"],
            "gists": gists, "rows": parts, "people": people, "where": where, "places": [p for p in places if p],
        }

    def calls_in(self, since=None, until=None, limit: int = 30) -> list[dict]:
        """The calls and meetings overlapping ``[since, until)`` (default today), oldest first, rows of one
        continuous session merged (``yuki.memory.callspans``). Each::

            {"ref": "call:16", "call_ids": [16, ..., 21], "app", "service", "kind": "call" | "game",
             "start": ISO, "end": ISO, "ongoing", "seconds",
             "speech_s" (the user's own speech), "recorded" (anything was: the user's microphone or an app's sound),
             "user_recorded" (the user's microphone was), "others_recorded" (an app's own sound kept speech),
             "others_speech_s" (the other people's speech kept, every app), "tracks": [{"app", "track_kind":
             "call" | "game", "speech_s"}] (the other side's tracks: each app whose own sound was kept),
             "digested": [ids with a timeline], "gists": [{"call_id", "gist"}],
             "rows": [{"call_id", "start", "end", "state"}]}

        Since 3 Oct 2026 a call keeps both sides - the user's microphone and the call app's own sound (the other
        people, maybe several on one track) - and a game's own voice chat is a session of its own (``kind``
        "game": its others are teammates and the game's own voice lines); a game's sound kept while a call
        recorded is a ``game`` track of the call. Before, only the user's microphone was kept: ``others_recorded``
        False, ``tracks`` empty. Plus meetings the timeline measured (an app in front holding the microphone or
        camera) that no call row covers: ``{"ref": None, "app", "service", "start", "end", "seconds", "speech_s":
        0, "recorded": False, "transcript": False, "rows": [], ...}`` - hours only, no transcript. ``people``
        (``[{"name", "how"}]``) and ``where`` (``[str]``) are who was on the call and where, as the screen around
        it showed (the digest's reading, yuki.memory.callcontext). Content-free except the gists, people and
        where.
        """
        from collections import Counter

        from yuki.memory.callspans import CONTEXT_S, merged_calls

        s, u = self._window(since, until)
        spans = merged_calls(self.store, s, u, now=time.time())
        out = []
        if spans:
            rows = {r.id: r for r in self.store.calls_overlapping(s - CONTEXT_S, u + CONTEXT_S)}
            digests = self.store.call_digests(i for sp in spans for i in sp["call_ids"])
            waiting = Counter(c["call_id"] for c in self.store.call_chunks(statuses=("spooled",)))
            tracks = self._call_track_rows(i for sp in spans for i in sp["call_ids"])
            out = [dict(self._session_dict(sp, rows, digests, waiting, tracks), transcript=True) for sp in spans]
        try:
            out += self._uncovered_meetings(s, u, spans)
        except Exception:
            pass
        out.sort(key=lambda c: c["start"] or "")
        return out[-max(1, int(limit)):]

    def _uncovered_meetings(self, since: float, until: float, spans: list[dict]) -> list[dict]:
        """Timeline meetings (``timeline.meeting`` rows, joined as the activity output joins them) that no call
        row of the same app or service overlaps: a meeting seen while the call worker was off or not asked."""
        from yuki.memory.timeline import _clip, meetings

        rows = [r for r in self.store.timeline_between(since, until) if r.meeting]
        out = []
        for m in meetings(_clip(rows, since, until)):
            names = {m.get("label"), m.get("app")}
            if any(sp["start"] < m["end"] and sp["end"] > m["start"]
                   and names & {sp.get("app"), sp.get("service")} for sp in spans):
                continue
            out.append({"ref": None, "call_ids": [], "app": m.get("app") or m.get("label"), "service": m.get("label"),
                        "kind": "call", "start": _iso(m["start"]), "end": _iso(m["end"]), "ongoing": False,
                        "seconds": m.get("seconds"), "speech_s": 0.0, "recorded": False, "user_recorded": False,
                        "others_recorded": False, "others_speech_s": 0.0, "tracks": [], "transcript": False,
                        "digested": [], "gists": [], "rows": []})
        return out

    def call_timeline(self, call: Any, since=None, until=None) -> dict | None:
        """What happened on one call (the whole merged session the row ``call`` belongs to), timeline-wise:
        :meth:`calls_in`'s fields plus ``"blocks": [{"call_id", "start", "end", "topic", "lines": [{"at", "text",
        "nature", "who", "app", "track_kind", ...}]}]`` in time order (ISO times), ``"stretches"`` (the blocks
        grouped as a written answer reads), cut to ``[since, until)`` when given, and ``"words_kept_until"`` (ISO:
        the exact transcript's 30-day TTL; None once gone). None: no such call.

        Each line says whose transcript line it rests on (report 61): ``who`` "user" (the user's own words) or
        "others" (an app's own sound - someone on the call, or on ``track_kind`` "game" a teammate or the game's
        own voice lines), with that track's ``app``. A digest written before report 61's build has none of them:
        its lines are the user's (``who`` "user", the row's app, ``track_kind`` "call"). Any other field a digest
        line carries is passed through as it is. ``others_recorded`` is also True when a line rests on the others'
        words.
        """
        from collections import Counter

        cid = self.call_id_of(call)
        span = self._call_session(cid) if cid is not None else None
        if span is None:
            return None
        ids = span["call_ids"]
        rows = {i: self.store.call_record(i) for i in ids}
        digests = self.store.call_digests(ids)
        waiting = Counter(c["call_id"] for c in self.store.call_chunks(statuses=("spooled",)))
        out = self._session_dict(span, rows, digests, waiting, self._call_track_rows(ids))
        apps = {i: (digests.get(i) or {}).get("app") or (rows[i].app if rows.get(i) is not None else "") for i in ids}
        s, u = _time_arg(since), _time_arg(until)
        blocks = []
        for i in ids:
            for b in (digests.get(i) or {}).get("blocks") or []:
                if (s is not None and b["end"] < s) or (u is not None and b["start"] >= u):
                    continue
                lines = [ln for ln in b.get("lines") or []
                         if (s is None or ln["at"] >= s - 60) and (u is None or ln["at"] < u)]
                blocks.append({"call_id": i, "start": b["start"], "end": b["end"], "topic": b.get("topic") or "",
                               "lines": lines})
        from yuki.memory.calldigest import stretches

        blocks.sort(key=lambda b: b["start"])
        groups = stretches(blocks)
        for b in blocks:
            b["start"], b["end"] = _iso(b["start"]), _iso(b["end"])
            b["lines"] = [_timeline_line(ln, apps.get(b["call_id"]) or span["app"]) for ln in b["lines"]]
        if not out["others_recorded"] and any(ln["who"] == "others" for b in blocks for ln in b["lines"]):
            out["others_recorded"] = out["recorded"] = True     # the digest rests on their lines: they were kept
        # the shape a written answer takes: stretches of the call, each with its time range, topics and moments
        out["stretches"] = [{"start": g[0]["start"], "end": g[-1]["end"],
                             "topics": list(dict.fromkeys(b["topic"] for b in g if b["topic"])),
                             "lines": [ln for b in g for ln in b["lines"]]} for g in groups]
        first = self.store._query(
            "SELECT min(c.at) FROM captures c JOIN calls k ON k.thread_id = c.thread_id WHERE k.id IN "
            f"({','.join('?' * len(ids))}) AND c.kind = 'my_call_side'", ids)[0][0]
        out.update(blocks=blocks, since=_iso(s), until=_iso(u),
                   words_kept_until=_iso(first + 30 * 86400.0) if first else None)
        return out

    def call_transcript(self, call: Any, since=None, until=None, page: int = 1, per_page: int = 60,
                        who: str = "all") -> dict | None:
        """The exact words on one call (its merged session), every side, as the recogniser heard them, a page at a
        time: ``{"ref", "app", "kind", "start", "end", "since", "until", "who", "page", "pages", "total",
        "loops_left_out", "sides": {"user": n, "others": n}, "others_recorded", "lines": [{"at": ISO, "call_id",
        "who", "app", "track_kind", "heard_by"?, "text", "lang", "unclear", "repeats", "to_yuki"}]}``.

        Each line says who spoke (:func:`transcript_lines`): ``who`` "user" (the user's microphone) or "others"
        (the ``app``'s own sound: whoever spoke on the call, maybe several people; on ``track_kind`` "game" the
        teammates and the game's own voice lines). ``heard_by`` (the user's lines, when it was measured): the apps
        that were taking the microphone then (``[]``: none). A line from before 3 Oct 2026 is the user's.
        ``who`` keeps "all" lines (default), only the "user"'s or only the "others'" (``ValueError`` for anything
        else); ``sides`` counts both in the window whatever is kept; ``others_recorded``: any of the others' sound
        was kept on this session. An exact repeat of a recent line on its own track (the recogniser's loop on
        noise) is left out and counted on the line it repeats (``repeats``); ``to_yuki`` (only ever on the user's
        lines) is "yes" when voice judged the line said to Yuki, "maybe" when Yuki was listening then; ``unclear``
        is the recogniser's own low confidence. Kept for the 30-day capture TTL. None: no such call.
        """
        from yuki.memory.calldigest import REPEAT_LOOKBACK, _same_words, yuki_spans_for

        side = str(who or "all").strip().lower()
        if side not in TRANSCRIPT_WHO:
            raise ValueError(f"who must be one of {', '.join(TRANSCRIPT_WHO)}, not {who!r}")
        cid = self.call_id_of(call)
        span = self._call_session(cid) if cid is not None else None
        if span is None:
            return None
        s, u = _time_arg(since), _time_arg(until)
        lines: list[dict] = []
        loops = 0
        sides = {"user": 0, "others": 0}
        others_seen = False
        for i in span["call_ids"]:
            captures = self.store.call_side_captures(i)
            last: dict[tuple[str, str, str], list[tuple[str, dict]]] = {}    # per track: (words, line) kept
            for ln in transcript_lines(captures, yuki_spans_for(self.store, captures)):
                others_seen = others_seen or ln["who"] == "others"
                if (s is not None and ln["at"] < s) or (u is not None and ln["at"] >= u):
                    continue
                if side not in ("all", ln["who"]):      # not kept, but counted
                    if not ln["repeat"]:
                        sides[ln["who"]] += 1
                    continue
                track = _track_of(ln)
                words = _same_words(ln["text"])
                if ln["repeat"]:
                    loops += 1
                    for said, prev in reversed(last.get(track, [])[-REPEAT_LOOKBACK:]):
                        if said == words:
                            prev["repeats"] += 1
                            break
                    continue
                sides[ln["who"]] += 1
                out = {"at": ln["at"], "call_id": i, "who": ln["who"], "app": ln["app"],
                       "track_kind": ln["track_kind"], "text": ln["text"][:500], "lang": ln["lang"],
                       "unclear": ln["unclear"], "repeats": 0, "to_yuki": ln["to_yuki"]}
                if ln["heard_by"] is not None:
                    out["heard_by"] = ln["heard_by"]
                lines.append(out)
                last.setdefault(track, []).append((words, out))
        per_page = max(10, min(int(per_page or 60), 150))
        pages = max(1, -(-len(lines) // per_page))
        page = max(1, min(int(page or 1), pages))
        chunk = lines[(page - 1) * per_page: page * per_page]
        for ln in chunk:
            ln["at"] = _iso(ln["at"])
        others_recorded = others_seen or any(t["who"] == "others" for t in self._call_track_rows(span["call_ids"]))
        return {"ref": f"call:{span['call_ids'][0]}", "app": span["app"], "kind": span.get("kind", "call"),
                "start": _iso(span["start"]), "end": _iso(span["end"]), "since": _iso(s), "until": _iso(u),
                "who": side, "page": page, "pages": pages, "total": len(lines), "loops_left_out": loops,
                "sides": sides, "others_recorded": others_recorded, "lines": chunk}

    # -- status and control ------------------------------------------------

    def status(self) -> dict:
        """Memory at a glance (content-free).

        ``{"service_running": bool, "paused": bool, "captures_today": int,
        "facts_today": int, "last_capture_at": ISO | None,
        "portrait_updated_at": ISO | None, "memory_cost_today_usd": float}``.
        "Today" is since local midnight; the cost is the estimate for the journal,
        portrait, episode, conversation, coach and weekly-review model calls (list prices, ``Settings.pricing``).
        ``service_running`` comes from the service's named mutex for this
        database in this Windows session.
        """
        midnight = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        today = self.store.activity_today(midnight)
        portrait = self.store.latest_portrait()
        return {
            "service_running": service_running(self.store.path),
            "paused": self._flag(PAUSE_FLAG).exists(),
            "captures_today": today["captures"],
            "facts_today": today["facts"],
            "last_capture_at": _iso(today["last_capture_at"]),
            "portrait_updated_at": _iso(portrait.at) if portrait else None,
            "memory_cost_today_usd": round(
                today["journal_cost_usd"] + today["portrait_cost_usd"] + today.get("episode_cost_usd", 0.0)
                + today.get("conversation_cost_usd", 0.0) + today.get("nudge_cost_usd", 0.0)
                + today.get("review_cost_usd", 0.0) + today.get("call_digest_cost_usd", 0.0), 6
            ),
        }

    def set_paused(self, paused: bool) -> None:
        """Pause or resume memory capture (writes or removes the ``paused`` flag file).

        While the flag exists the service's watcher captures nothing (it logs one
        ``paused`` event) and no scheduled portrait run starts; the journal
        worker still finishes what was captured before. The service notices
        within about a second; a service started while paused starts paused.
        """
        path = self._flag(PAUSE_FLAG)
        if paused:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
        else:
            try:
                path.unlink()
            except FileNotFoundError:
                pass


    # -- the Yuki window's reads (yuki/ui/window, docs/UI-window.md "Phase 2") ------------------
    # Read-only views for the window's pages; plain dicts, decrypted. Added by the window's builder.

    def day_timeline(self, since: Any, until: Any = None) -> list[dict]:
        """Timeline stretches overlapping ``[since, until)``, oldest first: the Today page's segments.

        Each: ``{"start", "end", "state" (present | away | a power state), "process", "app",
        "title", "host", "active_s", "passive_s", "withheld", "fullscreen", "meeting", "by_yuki"}``
        (epoch seconds; ``title`` is ``None`` when privacy withheld it).
        """
        s, u = _time_arg(since), _time_arg(until)
        rows = self.store.timeline_between(s, u)
        return [{"start": r.started_at, "end": r.ended_at, "state": r.state, "process": r.process, "app": r.app,
                 "title": r.title, "host": r.host, "active_s": r.active_s or 0.0, "passive_s": r.passive_s or 0.0,
                 "withheld": r.withheld, "fullscreen": bool(r.fullscreen), "meeting": r.meeting,
                 "by_yuki": bool(getattr(r, "by_yuki", False))} for r in sorted(rows, key=lambda r: r.started_at)]

    @staticmethod
    def _journal_dict(j: JournalEntry) -> dict:
        """One journal entry for the window. ``source`` (migration 17's provenance: ``{"kind", "by", "where", "call",
        "unclear", "quote"}`` or None), ``call`` (``call:N`` when it was said on a call, joins :meth:`calls_in` and
        the digest's place) and ``where`` (the provenance's where, "Discord call") since migration 33."""
        src = j.source if isinstance(j.source, dict) else None
        source = None if src is None else {k: src.get(k) for k in ("kind", "by", "where", "call", "unclear", "quote")
                                           if src.get(k) not in (None, "")}
        return {"id": j.id, "at": j.at, "app": j.app, "text": j.fact, "nature": j.nature, "importance": j.importance,
                "by_yuki": bool(j.by_yuki), "milestone": bool(j.milestone), "source": source,
                "call": (src or {}).get("call") or None, "where": (src or {}).get("where") or None,
                "reviewed_at": getattr(j, "reviewed_at", None)}

    def journal_entries(self, since: Any = None, until: Any = None) -> list[dict]:
        """Journal facts in ``[since, until)`` (decrypted), oldest first."""
        return [self._journal_dict(j) for j in self.store.journal_between(_time_arg(since), _time_arg(until))]

    def journal_by_ids(self, ids: Any) -> list[dict]:
        """Journal facts by id (the evidence behind a portrait fact)."""
        wanted = sorted({int(i) for i in ids or () if str(i).isdigit() or isinstance(i, int)})
        return [self._journal_dict(j) for j in self.store.journal_by_ids(wanted)] if wanted else []

    def misheard(self, days: float = MISHEARD_DAYS, limit: int = MISHEARD_SHOWN, now: float | None = None) -> dict:
        """The "Maybe misheard" list (report 38 §5-§6): journal entries of the last ``days`` marked uncertain (possibly
        misheard or ambiguous) that the user has not answered, newest first, at most ``limit``, and how many in all.
        Each is :meth:`_journal_dict` plus ``ref`` (``journal:N``, what :meth:`resolve_heard` takes), ``heard_in`` (a
        label: the voice channel for an entry said on a call when the digest saw it, "Discord, #general voice
        (Friends)"; else the call app or the window's app), ``channel_known`` and ``people`` (who the call screen showed
        on that call). Returns ``{"items", "total"}``. Read only, no model. Never raises.

        Since the feedback round 3 (W6, :mod:`yuki.memory.misheard`) only real mishearings are listed: entries whose
        provenance holds the words as heard and Yuki's guess. Each item also has ``heard`` ("sea ra", Latin
        letters), ``guess`` ("Ciara"), ``why`` (who or what makes it fit, English), ``guess_is_name`` and ``shown``
        (the entry said to the user in English, or None: show ``text`` then). The raw transcript (``source.quote``)
        is provenance, never for showing."""
        from yuki.memory.misheard import is_listed
        from yuki.memory.titles import place_label

        now = time.time() if now is None else float(now)
        try:
            entries, _all = self.store.uncertain_journal(now - float(days) * 86400.0)
            entries = [j for j in entries if is_listed(j.source)]
            total = len(entries)
            dicts = [{**self._journal_dict(j), "heard": j.source.get("heard"), "guess": j.source.get("meant"),
                      "why": j.source.get("meant_why") or None, "guess_is_name": bool(j.source.get("meant_name")),
                      "shown": j.source.get("shown") or None} for j in entries[: max(0, int(limit))]]
            calls = {}
            ids = {int(d["call"][5:]) for d in dicts if str(d.get("call") or "").startswith("call:")
                   and str(d["call"])[5:].isdigit()}
            if ids:
                calls = self.store.call_contexts(ids)
            items = []
            for d in dicts:
                ctx = calls.get(int(str(d["call"])[5:])) if d.get("call") and str(d["call"])[5:].isdigit() else None
                label = place_label(ctx.get("place"), ctx.get("where") or "") if ctx else ""
                # a channel is known when the digest's place names one, or its free text says more than the app
                channel = bool(ctx and ((ctx.get("place") or {}).get("channel") or (
                    label and label.casefold() != str(ctx.get("app") or "").casefold())))
                items.append({**d, "ref": f"journal:{d['id']}",
                              "heard_in": label or d.get("where") or d.get("app") or "",
                              "channel_known": channel,
                              "people": [p["name"] for p in (ctx or {}).get("people") or []
                                         if p.get("how") == "on_call_screen"]})
            return {"items": items, "total": total}
        except Exception:
            return {"items": [], "total": 0}

    def resolve_heard(self, ref: Any, verdict: str, *, correction_id: int | None = None) -> dict:
        """The user's Right / Wrong on a "maybe misheard" entry, at once, with no model on the click path:
        ``right`` - the entry is taken as said (its nature becomes ``fact``; nothing is reworded); ``wrong`` - it is
        retired now and the correction pass is queued for the ripple (``correction_id``); ``undo`` (the window's Undo)
        - either is taken back, and a queued pass (``correction_id``) is cancelled first; once that pass started, nothing
        is taken back (``ok`` False, :data:`TOO_LATE_TO_UNDO`); an entry never answered has nothing to undo. The item
        leaves :meth:`misheard` at once (answered entries are never listed again). Returns ``{"ok", "ref", "verdict",
        "nature", "correction_id"}``; ``ok`` False with ``error`` when the entry is gone or already answered.

        With Yuki's guess (the feedback round 3, W6): ``guess_right`` means the GUESS is right - the entry leaves the
        list at once but stays uncertain (``nature`` stays ``uncertain``, never used as a fact) while the correction
        pass, queued with the user's words "It was 'Ciara', not 'sea ra'" and the entry as its target, rewrites it
        (the new version is settled as a fact and the name alias learned once the rewrite carries it;
        ``correction_id``; Undo cancels it while it waits). ``right`` keeps its meaning (taken as said, as heard;
        ``as_heard`` is the same); ``wrong`` retires it; a correction in the user's own words goes through
        :meth:`correct_item`. ``guess_right`` on an entry with no guess is ``right``."""
        jid = self._journal_id(ref)
        verdict = str(verdict or "").strip().lower()
        out: dict[str, Any] = {"ok": False, "ref": f"journal:{jid}" if jid else str(ref or ""), "verdict": verdict,
                               "nature": None, "correction_id": None}
        if jid is None or verdict not in ("right", "as_heard", "guess_right", "wrong", "undo"):
            out["error"] = "say right, guess_right, wrong or undo for a journal entry"
            return out
        guess: dict[str, Any] | None = None
        if verdict == "guess_right":
            from yuki.memory.misheard import is_listed

            rows = self.store.journal_by_ids([jid])
            if rows and is_listed(rows[0].source):
                guess = rows[0].source
                verdict = "pending"
            else:
                verdict = "right"
        if verdict == "as_heard":
            verdict = "right"
        if verdict == "undo" and correction_id:   # the ripple first: once its pass started, the answer stands (A3)
            out["cancelled"] = self.store.cancel_correction(int(correction_id))
            if not out["cancelled"]:
                out["error"] = TOO_LATE_TO_UNDO
                return out
            try:   # report 88: the alias the Right taught goes with it, and the one it replaced comes back (F1)
                from yuki.memory.misheard import undo_learned

                undo_learned(self.store, [{"alias_id": a["id"]} for a in self.store.aliases(name_forms=True)
                                          if a.get("correction_id") == int(correction_id)])
            except Exception:
                pass
        try:
            done = self.store.resolve_journal(jid, verdict)
        except Exception as exc:
            out["error"] = f"memory: {type(exc).__name__}"
            return out
        if done is None:
            out["error"] = "That entry has changed since; open it again."
            return out
        out.update(ok=True, nature=done["nature"])
        if verdict == "wrong":
            out["correction_id"] = self._ripple(done.get("app") or "what Yuki heard", done["text"],
                                                "That was misheard. It's wrong, forget it")
        elif guess is not None:
            from yuki.memory.misheard import right_words

            try:
                text = (f'About "what Yuki heard" (memory had: "{" ".join(done["text"].split())}"), '
                        f'the user said in their own words: "{right_words(guess)}"')
                out["correction_id"] = self.store.add_correction(secret_filter.redact_text(text), target=f"J{jid}")
                self.rerender_portrait()   # wakes the service for the pass
            except Exception:
                pass
            if guess.get("meant_name") and out["correction_id"]:
                # report 88: one answer teaches - the heard form is that person's alias at once (the people index,
                # keyterms and the talker read it from the next lookup); the pass still rewords the entry
                # (review F2: the same check as the names pass - a known name, not itself someone's name, a few
                # words; otherwise the correction pass learns it once a rewrite carries it, as before)
                from yuki.memory.misheard import KnownNames, check_name_pair, learn_name_fixes

                right, _why_not = check_name_pair(guess.get("heard"), guess.get("meant"), KnownNames.of(self.store))
                learned = learn_name_fixes(self.store, [{"heard": guess["heard"], "name": right, "how": "confirmed",
                                                         "at": time.time()}],
                                           source="the misheard list", correction_id=out["correction_id"],
                                           by_user=True) if right else []
                out["alias"] = bool(learned)
        return out

    def name_forms(self, name: str = "Yuki") -> list[str]:
        """The heard forms memory learned for ``name`` (default Yuki's own: how speech recognition wrote it), newest
        first (``name_aliases``; :func:`yuki.memory.misheard.name_forms`). For the voice's fast path. Never raises."""
        from yuki.memory.misheard import name_forms

        return name_forms(self.store, name)

    def portrait_facts_with_evidence(self, evidence_per_fact: int = 6) -> list[dict]:
        """The current portrait facts, each with the journal entries it rests on (newest first).

        Each: ``{"id", "ref": "fact:N", "kind", "subject", "text", "summary", "aspect", "origin", "confidence",
        "since", "shown", "pinned", "reviewed_at", "importance", "created_at", "evidence": [journal dict, ...]}``.
        ``summary`` is the app's short line for the fact (at most eight words; an open loop's is its title),
        model-written (migration 21, yuki.memory.titles), ``None`` until written. ``shown`` is the fact said to the
        user (second person, natural dates; migration 33), ``None`` until written - show it instead of ``text``.
        ``pinned`` is the user's own pin (:meth:`pin_fact`), separate from ``origin == "user"`` ("you told me").
        Corrections waiting to be folded in are left out.
        """
        facts = self.store.portrait_facts(include_corrections=False)
        ids = sorted({i for f in facts for i in f.source_ids})
        entries = {j.id: j for j in self.store.journal_by_ids(ids)} if ids else {}
        out = []
        for f in facts:
            ev = sorted((entries[i] for i in f.source_ids if i in entries), key=lambda j: -j.at)
            out.append({"id": f.id, "ref": f"fact:{f.id}", "kind": f.kind, "subject": f.subject, "text": f.text,
                        "summary": f.summary, "aspect": f.aspect, "origin": f.origin, "confidence": f.confidence, "since": f.valid_from,
                        "shown": f.shown, "pinned": bool(f.pinned), "reviewed_at": f.reviewed_at,
                        "importance": f.importance, "created_at": f.created_at,
                        "evidence": [self._journal_dict(j) for j in ev[:evidence_per_fact]]})
        return out

    def rules(self) -> list[dict]:
        """The user's active rules and preferences, oldest first: ``{"ref": "rule:N", "id", "kind", "text",
        "quote", "at", "origin"}``. ``ref`` is what :meth:`revoke_rule` takes."""
        out = [{"ref": f"rule:{f.id}", "id": f.id, "kind": f.kind, "text": f.text, "quote": f.quote,
                "at": f.valid_from, "origin": f.origin}
               for f in self.store.conversation_facts(("rule", "preference"))]
        return sorted(out, key=lambda r: r["at"])

    def recent_turns(self, limit: int = 40, session_id: str | None = None) -> list[dict]:
        """The latest exchanges with Yuki (the given conversation session, else the newest one), oldest first.

        Each: ``{"id", "session_id", "request_id", "at", "user", "reply", "actions", "outcome", "addressed"}``.
        """
        if session_id is None:
            latest = self.store.latest_turn()
            if latest is None:
                return []
            session_id = latest.session_id
        turns = self.store.turns_for_session(session_id, limit=max(1, int(limit)))
        return [{"id": t.id, "session_id": t.session_id, "request_id": t.request_id, "at": t.at,
                 "user": t.user_text, "reply": t.reply_text or "", "actions": list(t.actions or []),
                 "outcome": t.outcome, "addressed": t.addressed} for t in turns]

    # -- a new chat every day, with memory between days (migration 33; report 38 §9) ---------------------------

    def day_connector(self, day: str | None = None, now: float | None = None) -> dict | None:
        """The line at the top of a new day's chat: the digest of the user's last day before today (or of ``day``,
        ``YYYY-MM-DD``; days start at 04:00), said to them. ``{"day", "label" ("Yesterday", "Thursday 1 October"),
        "text" (the shown form; may be None until written), "memory" (Yuki's form), "open": [{"text", "thread":
        "thread:N" | None}], "threads": ["thread:N"...], "exchanges"}`` or None. When the last completed day with
        anything in it has no digest yet, asks the service to write it (a flag; one Haiku call in the background,
        never on this path) and returns the newest older one, if any, within :data:`RESUME_FALLBACK_DAYS`.
        Read only otherwise. Never raises."""
        from yuki.memory.daydigest import day_window, previous_days, today

        now = time.time() if now is None else float(now)
        try:
            if day is None:
                self._ask_day_digest(now)
                found = self.store.day_digests(before=today(now), limit=1)
            else:
                found = self.store.day_digests(days=[str(day)], limit=1)
            if not found:
                return None
            d = found[0]
            if day is None and d["day"] < previous_days(RESUME_FALLBACK_DAYS, now)[-1]:
                return None
            start, _end = day_window(d["day"])
            from yuki.memory.titles import natural_day

            label = "Yesterday" if d["day"] == previous_days(1, now)[0] else natural_day(start + 6 * 3600, now)
            refs = d.get("refs") or {}
            return {"day": d["day"], "label": label, "text": d.get("shown"), "memory": d["text"],
                    "open": [{"text": o.get("text"), "thread": f"thread:{o['thread']}" if o.get("thread") else None}
                             for o in refs.get("open") or [] if isinstance(o, dict) and o.get("text")],
                    "threads": [f"thread:{t}" for t in refs.get("threads") or []],
                    "exchanges": refs.get("exchanges")}
        except Exception:
            return None

    def _ask_day_digest(self, now: float) -> bool:
        """Yesterday had exchanges or journal entries and has no digest yet: write the ``day_digest`` flag (the
        service's scheduler builds it in the background, one Haiku call; never on this path). True when it asked.
        Content-free reads. Never raises."""
        from yuki.memory.daydigest import DAYDIGEST_FLAG, day_window, previous_days

        try:
            yesterday = previous_days(1, now)[0]
            if self.store.day_digests(days=[yesterday], limit=1):
                return False
            start, end = day_window(yesterday)
            if not (self.store.turn_times(start, end) or self.store.journal_count(start, end)):
                return False
            path = self._flag(DAYDIGEST_FLAG)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(datetime.now().astimezone().isoformat(timespec="seconds"), encoding="utf-8")
            return True
        except Exception:
            return False

    def chat_days(self, days: int = 14, now: float | None = None) -> list[dict]:
        """The user days with exchanges in the last ``days`` (days start at 04:00), newest first: ``{"day", "count",
        "first", "last"}`` (epoch seconds) - the drawer's day dividers and folds. Content-free. Never raises."""
        from yuki.memory.checkins import user_day

        now = time.time() if now is None else float(now)
        try:
            out: dict[str, dict[str, Any]] = {}
            for at, _sid in self.store.turn_times(now - int(days) * 86400.0 - 86400.0, now + 60.0):
                d = user_day(at)
                row = out.setdefault(d, {"day": d, "count": 0, "first": at, "last": at})
                row["count"] += 1
                row["first"], row["last"] = min(row["first"], at), max(row["last"], at)
            return sorted(out.values(), key=lambda r: r["day"], reverse=True)[: int(days) + 1]
        except Exception:
            return []

    def turns_on(self, day: str, limit: int = 200) -> list[dict]:
        """One user day's exchanges with Yuki (``YYYY-MM-DD``, from 04:00), oldest first, in :meth:`recent_turns`'
        shape - loaded when the user opens an earlier day's fold. Never raises (``[]``)."""
        from yuki.memory.daydigest import day_window

        try:
            start, end = day_window(str(day))
            turns = self.store.turns_between(start, end)[-max(1, int(limit)):]
        except Exception:
            return []
        return [{"id": t.id, "session_id": t.session_id, "request_id": t.request_id, "at": t.at,
                 "user": t.user_text, "reply": t.reply_text or "", "actions": list(t.actions or []),
                 "outcome": t.outcome, "addressed": t.addressed} for t in turns]

    def app_colors(self, until: Any = None, days: float = 14.0, limit: int = 8) -> dict[str, int]:
        """Each app's rank by time in front over the last ``days`` (0 = most): the window gives an app the
        same lane colour every day. Keys are the timeline's ``process`` (else the app name), lower-cased."""
        u = _time_arg(until) or time.time()
        rows = self.store._query(
            "SELECT COALESCE(process, app) k, SUM(COALESCE(active_s,0)+COALESCE(passive_s,0)) t FROM timeline "
            "WHERE started_at >= ? AND state = 'present' GROUP BY k ORDER BY t DESC LIMIT ?",
            (u - days * 86400.0, int(limit)))
        return {str(r["k"]).casefold(): i for i, r in enumerate(rows) if r["k"]}

    # ======================================================================================================
    # Added 2026-09-27 for the Yuki window (the user's review): corrections applied everywhere, to-dos apart
    # from Yuki's promises, name aliases. For the UI builder; nothing here calls a model.
    # ======================================================================================================

    #: What the UI shows for a correction's pass (``correction_passes.status`` -> state).
    _CORRECTION_STATES = {"queued": "updating", "applying": "updating", "done": "done", "failed": "failed",
                          "skipped": "done"}

    def correction_status(self, ref: Any) -> dict:
        """Where one correction stands: ``{"id", "state", "changed", "changes", "queued_at", "finished_at",
        "error"}``.

        ``ref``: the id :meth:`correct_portrait` (or :meth:`answer_question`'s ``correction_id``) returned.
        ``state``: ``"updating"`` while the service applies it everywhere (portrait facts, open loops,
        Yuki's commitments and the user's to-dos, journal facts, episodes, questions, persona; usually
        within a minute or two), then ``"done"`` (refresh the pages), ``"failed"`` (the portrait still folds
        it at its next render), or ``"none"`` (no such correction, or one written before corrections were
        applied everywhere). ``changes``: ``[{"surface", "op": "rewrite" | "retire", "old", "new"}]`` of the
        places it changed; ``changed``: how many. Never raises.
        """
        try:
            cid = int(str(ref).strip().strip("[]").split(":")[-1].lstrip("F"))
            row = self.store.correction_pass(cid)
        except Exception:
            return {"id": ref, "state": "none", "changed": 0, "changes": [], "queued_at": None, "finished_at": None,
                    "error": None}
        if row is None:
            return {"id": cid, "state": "none", "changed": 0, "changes": [], "queued_at": None, "finished_at": None,
                    "error": None}
        changes = [{"surface": c.get("surface"), "op": c.get("op"), "old": c.get("old"), "new": c.get("new")}
                   for c in row["changes"] if c.get("ok", True)]
        return {"id": cid, "state": self._CORRECTION_STATES.get(row["status"], row["status"]),
                "changed": len(changes), "changes": changes, "queued_at": _iso(row["queued_at"]),
                "finished_at": _iso(row["finished_at"]), "error": row["error"]}

    def end_todo(self, ref: str, how: str = "done") -> str | None:
        """End one to-do by its id (``"todo:3"``, ``"commit:5"``, ``"loop:12"``, as :meth:`todos` gives it).

        ``how``: ``"done"``, ``"not_needed"`` (the user does not need it any more) or ``"wrong"`` (captured
        by mistake: never a real to-do; the conversation extractor is shown it so the like is not captured
        again). Ended items leave :meth:`todos`, the coach and the reminders. Returns the id, or ``None``
        when nothing open has that id or ``how`` is unknown. Never raises. :meth:`complete_todo` is
        ``end_todo(ref, "done")``.
        """
        from yuki.memory.nudges import end_item

        status = {"done": "done", "not_needed": "dropped", "wrong": "wrong"}.get(str(how or "").strip())
        if status is None:
            return None
        try:
            return end_item(self.store, ref, status)
        except Exception:
            return None

    #: How long after :meth:`end_todo` a to-do the user added can be put back (the window's Undo).
    REOPEN_WITHIN_S = 10 * 60.0

    def reopen_todo(self, ref: str) -> str | None:
        """The Undo of :meth:`end_todo` on a to-do the user added (``"todo:N"``) ended as done or not needed within
        :data:`REOPEN_WITHIN_S`: it is open again, as it was. Returns the id, or ``None`` (another kind of item: a
        commitment or an open loop ends through history this cannot take back; too late; not ended). Never raises."""
        prefix, _, number = str(ref or "").strip().strip("[]").partition(":")
        if prefix != "todo" or not number.strip().isdigit():
            return None
        since = time.time() - self.REOPEN_WITHIN_S
        try:
            ok = self.store._write(lambda conn: conn.execute(
                "UPDATE todos SET status='open', done_at=NULL, updated_at=? WHERE id=? AND status IN ('done','dropped')"
                " AND done_at >= ?", (time.time(), int(number), since)).rowcount == 1)
        except Exception:
            return None
        return f"todo:{int(number)}" if ok else None

    def reopen_item(self, ref: str, *, any_age: bool = False) -> str | None:
        """Put one ended item of the list back, open as it was (the Work page's Undo, Put it back and Not done yet).

        * ``todo:N`` a to-do the user added, ended as done or not needed: within :data:`REOPEN_WITHIN_S`, or at any
          age with ``any_age`` (Put it back from Recently done);
        * ``commit:N`` a commitment ended as done, not needed or not theirs: by the user within
          :data:`REOPEN_WITHIN_S`; with ``any_age``, one memory closed itself (the to-do check saw it done: Not done
          yet) or one done at any age (Put it back). It is active again, its end note cleared;
        * ``loop:N`` an open loop ended within :data:`REOPEN_WITHIN_S`, or at any age with ``any_age`` (Put it back
          from Recently done; review F7): open again, its portrait fact current
          again and the correction that closed it ended (history kept: nothing is deleted).

        Returns the id, or ``None`` (not ended, too late, or a kind that can't be put back). Never raises."""
        prefix, _, number = str(ref or "").strip().strip("[]").partition(":")
        if not number.strip().isdigit():
            return None
        n, now = int(number), time.time()
        since = now - self.REOPEN_WITHIN_S
        try:
            if prefix == "todo":
                ok = self.store._write(lambda conn: conn.execute(
                    "UPDATE todos SET status='open', done_at=NULL, updated_at=? WHERE id=? AND status IN ('done','dropped')"
                    " AND (? OR done_at >= ?)", (now, n, int(bool(any_age)), since)).rowcount == 1)
            elif prefix == "commit":
                rows = self.store._query("SELECT status, expired_at, end_note_ciphertext FROM conversation_facts"
                                         " WHERE id=? AND kind='commitment'", (n,))
                if not rows or rows[0]["status"] not in ("done", "dropped", "wrong"):
                    return None
                from yuki.memory.nudges import END_NOTES   # the notes the user's own end writes (the rest: memory's)

                note = self.store._dec(rows[0]["end_note_ciphertext"]) or ""
                by_user = note in {n for n, _c in END_NOTES.values()}
                recent = float(rows[0]["expired_at"] or 0.0) >= since
                if not ((by_user and recent) or (any_age and (not by_user or rows[0]["status"] == "done"))):
                    return None
                # reopened_at (migration 37): memory closes it again only on evidence after this moment, never on
                # what closed it before (yuki.memory.commitments)
                ok = self.store._write(lambda conn: conn.execute(
                    "UPDATE conversation_facts SET status='active', valid_to=NULL, expired_at=NULL, superseded_by=NULL,"
                    " end_note_ciphertext=NULL, reopened_at=? WHERE id=? AND kind='commitment'"
                    " AND status IN ('done','dropped','wrong')", (now, n)).rowcount == 1)
            elif prefix == "loop":
                def op(conn: Any) -> bool:
                    row = conn.execute("SELECT portrait_fact_id, resolved_at FROM open_loops WHERE id=? AND status='done'"
                                       " AND (? OR resolved_at >= ?)", (n, int(bool(any_age)), since)).fetchone()
                    if row is None:
                        return False
                    at = float(row["resolved_at"])
                    conn.execute("UPDATE open_loops SET status='open', resolved_at=NULL, updated_at=? WHERE id=?", (now, n))
                    if row["portrait_fact_id"] is not None:   # ended with it by complete_open_loop: current again
                        conn.execute("UPDATE portrait_facts SET valid_to=NULL, expired_at=NULL WHERE id=? AND"
                                     " superseded_by IS NULL AND abs(valid_to - ?) < 1.0", (int(row["portrait_fact_id"]), at))
                    # the user's "this is done" correction end_item wrote at the same moment: ended, not deleted
                    conn.execute("UPDATE portrait_facts SET valid_to=?, expired_at=? WHERE kind='correction' AND"
                                 " origin='user' AND valid_to IS NULL AND abs(valid_from - ?) < 1.0", (now, now, at))
                    return True
                ok = self.store._write(op)
            else:
                return None
        except Exception:
            return None
        return f"{prefix}:{n}" if ok else None

    def set_due(self, ref: str, due: str | None) -> str | None:
        """Move an open to-do's or commitment's due date (the user's correction "it's due Monday", read by
        :func:`yuki.memory.workboard.read_correction`): ``due`` ``"YYYY-MM-DD"`` (that day, no time),
        ``"YYYY-MM-DD HH:MM"``, or ``None`` (no date). Returns the id, or ``None`` (not open, not one of those, a date
        it can't read). Never raises."""
        prefix, _, number = str(ref or "").strip().strip("[]").partition(":")
        if prefix not in ("todo", "commit") or not number.strip().isdigit():
            return None
        due_at, all_day = None, False
        if due:
            try:
                raw = str(due).strip()
                if len(raw) == 10:
                    due_at, all_day = datetime.strptime(raw, "%Y-%m-%d").timestamp(), True
                else:
                    due_at = datetime.strptime(raw, "%Y-%m-%d %H:%M").timestamp()
            except ValueError:
                return None
        n, now = int(number), time.time()
        try:
            if prefix == "todo":
                ok = self.store._write(lambda conn: conn.execute(
                    "UPDATE todos SET due_at=?, due_all_day=?, updated_at=? WHERE id=? AND status='open'",
                    (due_at, int(all_day), now, n)).rowcount == 1)
            else:   # a commitment's day is its midnight (nudges reads a midnight due as a day)
                ok = self.store._write(lambda conn: conn.execute(
                    "UPDATE conversation_facts SET due_at=? WHERE id=? AND kind='commitment' AND status='active'",
                    (due_at, n)).rowcount == 1)
        except Exception:
            return None
        return f"{prefix}:{n}" if ok else None

    # -- the Work page (yuki.memory.workboard) ---------------------------------------------------------------------

    def work_board(self, now: float | None = None, *, replies: bool = True) -> dict:
        """The Work page's judged parts (:func:`yuki.memory.workboard.board`): the chats waiting on the user's reply,
        who owes what in the open loops, the meetings ahead, what the user moved to their list. Reads only; never
        raises (``{}`` when memory can't say). ``replies=False``: without reading the chats (Home's Your list)."""
        from yuki.memory.workboard import board

        try:
            return board(self.store, now, replies=replies)
        except Exception:
            return {}

    def work_kept(self, now: float | None = None) -> list[dict]:
        """The user's promises memory saw kept lately (:func:`yuki.memory.workboard.kept_promises`), newest first."""
        from yuki.memory.workboard import kept_promises

        try:
            return kept_promises(self.store, now)
        except Exception:
            return []

    def work_threads_for(self, spans: Any) -> list[dict | None]:
        """The thread of work each ``(start, end)`` stretch belongs to (:func:`yuki.memory.workboard.session_threads`):
        ``{"ref", "title"}`` or ``None`` per stretch. The Work page's "Worked on today"."""
        from yuki.memory.workboard import session_threads

        try:
            return session_threads(self.store, [(float(a), float(b)) for a, b in spans])
        except Exception:
            return [None for _ in spans]

    def work_mark(self, key: str, mark: str | None) -> bool:
        """The user's mark on a Work row (:func:`yuki.memory.workboard.set_mark`): ``no_reply`` / ``opened`` on a chat,
        ``listed`` on a commitment or loop moved to their list; ``None`` takes it back."""
        from yuki.memory.workboard import set_mark

        return set_mark(self.store, key, mark)

    def work_chat(self, key: str) -> dict | None:
        """Where a Needs-your-reply chat is (``chat:<id>``), to bring it forward: its app, process, name, address."""
        from yuki.memory.workboard import chat_place

        prefix, _, number = str(key or "").partition(":")
        return chat_place(self.store, int(number)) if prefix == "chat" and number.isdigit() else None

    def add_todo_words(self, words: str, client: Any = None, *, wait_s: float | None = None) -> tuple[str, str | None]:
        """Add a to-do from the words the user typed, a due date read from them (``"by Monday"``: one small model
        call, :func:`yuki.memory.workboard.read_due`, given at most ``wait_s`` seconds (default
        :data:`~yuki.memory.workboard.READ_WAIT_S`); without a client, on a failure or past that, the words whole and
        no due). Returns ``(id, due)``."""
        from yuki.memory.workboard import READ_WAIT_S, read_due, within

        words = " ".join((words or "").split())
        text, due = within(READ_WAIT_S if wait_s is None else wait_s, lambda: read_due(client, words), (words, None))
        return self.add_todo(text, due), due

    def due_of(self, ref: str) -> str | None:
        """An open to-do's or commitment's due date as :meth:`set_due` takes it (``"YYYY-MM-DD"``, ``"YYYY-MM-DD
        HH:MM"``), ``""`` when it has none, ``None`` when it isn't one of those or can't be read. Correct's Undo puts a
        moved due date back with it."""
        prefix, _, number = str(ref or "").strip().strip("[]").partition(":")
        if prefix not in ("todo", "commit") or not number.strip().isdigit():
            return None
        try:
            if prefix == "todo":
                rows = self.store._query("SELECT due_at, due_all_day AS day FROM todos WHERE id=?", (int(number),))
            else:
                rows = self.store._query("SELECT due_at, NULL AS day FROM conversation_facts WHERE id=? AND"
                                         " kind='commitment'", (int(number),))
        except Exception:
            return None
        if not rows:
            return None
        at = rows[0]["due_at"]
        if at is None:
            return ""
        when = datetime.fromtimestamp(float(at))
        # a commitment's day is its midnight (set_due writes it so)
        day = bool(rows[0]["day"]) if prefix == "todo" else (when.hour, when.minute, when.second) == (0, 0, 0)
        return when.strftime("%Y-%m-%d") if day else when.strftime("%Y-%m-%d %H:%M")

    def aliases(self) -> list[dict]:
        """Names the user corrected: ``[{"heard": "Barun", "name": "Varun", "why", "since"}]``, oldest first."""
        try:
            return [{"heard": a["heard"], "name": a["name"], "why": a["why"], "since": _iso(a["at"])}
                    for a in self.store.aliases()]
        except Exception:
            return []

    def recent_titles(self, process: str, *, hours: float = 12.0, limit: int = 15) -> list[str]:
        """The distinct window titles an app had in front recently, most used first (its tabs, sessions, documents),
        from the timeline (privacy-filtered where it was recorded). For voice (v3.13): an app that draws its own
        interface (a GPU terminal like Warp) exposes no items to UI Automation, but its tabs are its titles, so the
        names the user says can still be heard right and found. Never raises."""
        import unicodedata

        from yuki.memory.store import app_key

        key = app_key(process) or ""
        if not key:
            return []
        try:
            rows = self.store.timeline_of(key, time.time() - float(hours) * 3600.0)
        except Exception:
            return []
        weight: dict[str, float] = {}
        for r in rows:
            if (r.process or "") != key or not r.title:
                continue
            title = " ".join(str(r.title).split())
            # a status glyph an app puts before its title (a spinner, a mark: Unicode symbols) is not part of the name
            while title and unicodedata.category(title[0])[0] in "SPZ":
                title = title[1:].lstrip()
            if title:
                weight[title] = weight.get(title, 0.0) + float((r.active_s or 0) + (r.passive_s or 0) + 1)
        return [t for t, _w in sorted(weight.items(), key=lambda kv: -kv[1])][: max(0, int(limit))]

    def speech_names(self, limit: int = 50) -> list[str]:
        """Names speech recognition should expect (Deepgram keyterms): the right names of the user's
        corrections first, then the words of the user's own sites and accounts (:meth:`user_resources`, spelled
        as confirmed on screen), then the portrait's people and the short names of their work and interests.
        A misheard form is never included (it would teach recognition the mistake). Never raises."""
        try:
            from yuki.voice.keyterms import NAME_KINDS, terms_from_facts

            base = terms_from_facts(self.store.portrait_facts(NAME_KINDS, include_corrections=False), (), limit)
        except Exception:
            base = []
        try:   # every heard form, Yuki's own name's too ("UK"), is kept out
            heard = {a["heard"].casefold() for a in self.store.aliases(name_forms=True)}
        except Exception:
            heard = set()
        resource_terms = [t for r in self.user_resources() if r["confirmed"] for t in r["keyterms"]]
        out: list[str] = []
        for name in [a["name"] for a in self.aliases()] + resource_terms + base:
            if name and name.casefold() not in heard and name.casefold() not in {n.casefold() for n in out}:
                out.append(name)
        return out[: max(0, int(limit))]

    def user_resources(self) -> list[dict]:
        """The user's own sites, accounts, emails, repos and project links that memory holds (conversation facts
        of kind resource, migration 22), oldest first: ``{"ref": "resource:N", "id", "what", "value", "text",
        "keyterms", "confirmed", "since"}``. ``confirmed``: its spelling was seen in the exchange it came from
        (the page that opened, the text typed), not only heard. Never raises."""
        from yuki.memory.conversations import AS_HEARD_NOTE

        try:
            rows = self.store.conversation_facts(("resource",))
        except Exception:
            return []
        return [{"ref": f"resource:{f.id}", "id": f.id, "what": f.subject, "value": f.value, "text": f.text,
                 "keyterms": list(f.keyterms), "confirmed": AS_HEARD_NOTE not in (f.text or ""),
                 "since": _iso(f.valid_from)} for f in rows]

    # ======================================================================================================
    # Connecting the dots (2026-09-29, migration 23; docs/MEMORY.md "Threads of work" and "Connecting the dots"):
    # the whole-memory ranking from the user's words and their screen, the freshest screens, threads of work,
    # what came before a moment, links seen. Nothing here calls a model.
    # ======================================================================================================

    def screen_related(self, wait_s: float = 0.03) -> dict | None:
        """The memory service's screen-triggered recall (:mod:`yuki.memory.screenrecall`): the screen it last
        ranked memory against, its thread of work and the best memories (ids and scores), over the embedder's
        pipe; ``None`` when no service answers within ``wait_s``. Never raises."""
        from yuki.memory.screenrecall import OP

        try:
            call = getattr(self.embedder, "call", None)
            reply = call(OP, wait_s) if callable(call) else None
        except Exception:
            return None
        cache = (reply or {}).get("cache")
        return cache if isinstance(cache, dict) else None

    def related(self, query: str = "", *, previous: str | None = None, screen: Any = None, limit: int | None = None,
                voice: bool = False, embed_wait_s: float | None = None, now: float | None = None,
                with_recent: bool = True, session_id: str | None = None, exclude_recent_s: float | None = None,
                diet: bool = False, action: bool = False) -> dict:
        """What the whole memory holds that bears on this moment: ranked from the user's words (and the previous
        message), what is on their screen now and their current thread of work, across every kind of memory
        (:func:`yuki.memory.related.related`). ``screen``: a live picture from the caller (a dict with ``app``,
        ``title``, ``host``, ``url``, ``text``; or text), else the service's screen recall, else the newest
        capture. Returns ``{"items", "screen", "threads", "recent", "timing", "self_echo"}``. Never raises.

        ``session_id``: the caller's conversation session; its own exchanges of the conversation going on, and
        any exchange of the last ``exclude_recent_s`` seconds (default :data:`yuki.memory.related.SELF_ECHO_S`),
        are the conversation itself, never memory (revamp 02 D2). ``diet``: the brain's relevance-gated block
        (threads on screen or near the words, recent screens of the last minutes or near the words; the agent asks
        for it, the voice talker and the MCP tools get the whole picture). ``action``: the request asks Yuki to act
        (the caller's measure): Yuki's own past exchanges from other days weigh less
        (:data:`yuki.memory.related.ACTION_OWN_DISCOUNT`)."""
        from yuki.memory import related as rel

        try:
            service = None if screen is not None else self.screen_related()
            return rel.related(self, query, previous=previous, screen=screen, limit=limit, voice=voice,
                               embed_wait_s=embed_wait_s, now=now, service=service, with_recent=with_recent,
                               session_id=session_id,
                               exclude_recent_s=rel.SELF_ECHO_S if exclude_recent_s is None else exclude_recent_s,
                               diet=bool(diet) and bool((query or "").strip()) and not voice, action=bool(action))
        except Exception as exc:
            return {"items": [], "screen": None, "threads": [], "recent": [], "timing": {"error": type(exc).__name__}}

    def warm_related(self) -> dict:
        """Load the whole-memory index now (the first :meth:`related` of a process otherwise builds it: ~1 s on
        the real memory of 2026-09-29). Returns its rows per kind; never raises."""
        try:
            return self.relevant_index.refresh()
        except Exception:
            return {}

    def related_text(self, query: str = "", *, voice: bool = False, **kwargs: Any) -> str | None:
        """:meth:`related` as the block a request gets (``voice``: the talker's compact one), ``None`` for none."""
        from yuki.memory import related as rel

        return rel.block_text(self.related(query, voice=voice, **kwargs), voice=voice)

    def recent(self, minutes: float = 45.0, chars: int = 1_800, now: float | None = None) -> list[dict]:
        """The freshest layer: the last ``minutes`` of screens, condensed and capped (:func:`yuki.memory.related.recent`):
        ``[{"start", "end", "app", "title", "host", "kind", "text", "thread_id", "by_yuki"}]``, oldest first."""
        from yuki.memory import related as rel

        return rel.recent(self.store, minutes=minutes, chars=chars, now=now)

    @staticmethod
    def _thread_dict(t: Any) -> dict:
        from yuki.memory.workthreads import thread_line

        return {"ref": f"thread:{t.id}", "id": t.id, "title": t.title, "status": t.status, "state": t.state,
                "open": list(t.open_items), "involves": list(t.entities), "first_seen": _iso(t.first_seen),
                "last_seen": _iso(t.last_seen), "events": t.events, "importance": t.importance,
                "line": thread_line(t), "shown": getattr(t, "shown", None),
                # migration 35 (the feedback round 3, W1): the next step said to the user, and who closed it
                "next": getattr(t, "next", None), "closed_by": getattr(t, "closed_by", None),
                "closed_at": _iso(getattr(t, "closed_at", None))}

    @staticmethod
    def thread_id_of(ref: Any) -> int | None:
        """``thread:12`` / ``W12`` / ``12`` -> 12 (the id formats the lists show)."""
        text = str(ref or "").strip().strip("[]")
        text = text.split(":")[-1].lstrip("Ww")
        return int(text) if text.isdigit() else None

    def _thread_extras(self, rows: list[Any]) -> dict[int, dict[str, Any]]:
        """The Yuki window's Work page per thread (revamp E12): ``checks`` (milestones landed / open items:
        ``yuki.memory.workthreads.checklists``) and ``involves_typed`` (each involved name typed person / app / thing by
        identity: ``typed_entities``). Read only; ``{}`` when they cannot be read."""
        from yuki.memory.workthreads import checklists, typed_entities

        out: dict[int, dict[str, Any]] = {int(t.id): {} for t in rows}
        for key, fn in (("checks", checklists), ("involves_typed", typed_entities)):
            try:
                for tid, value in fn(self.store, rows).items():
                    out.setdefault(int(tid), {})[key] = value
            except Exception:
                pass
        return out

    def threads(self, statuses: Any = ("active", "paused"), query: str | None = None, limit: int = 12,
                embed_wait_s: float | None = None) -> list[dict]:
        """The user's threads of work, most recently touched first (``query``: closest in meaning first, any
        status but merged). Each: ``{"ref": "thread:N", "id", "title", "status", "state", "open", "involves",
        "first_seen", "last_seen", "events", "importance", "line", "checks", "involves_typed"}`` - ``checks``:
        ``{"done", "open", "total", "next"}`` (milestones that landed on it / its open items); ``involves_typed``:
        ``[{"name", "type": "person" | "app" | "thing"}]``. Never raises."""
        try:
            if query and query.strip():
                pool = self.store.work_threads(("active", "paused", "done"))
                vec = self._embed(query.strip(), embed_wait_s)
                if vec is not None and pool:
                    vecs = self.store.work_thread_vectors([t.id for t in pool])
                    q = np.asarray(vec, dtype=np.float32)
                    q = q / (float(np.linalg.norm(q)) or 1.0)

                    def score(t: Any) -> float:
                        v = vecs.get(t.id)
                        if v is None or v.shape != q.shape:
                            return -1.0
                        return float(v @ q / (float(np.linalg.norm(v)) or 1.0))

                    pool.sort(key=lambda t: -score(t))
                rows = pool[: max(0, int(limit))]
            else:
                rows = self.store.work_threads(tuple(statuses) if statuses else None, limit=max(0, int(limit)))
            extras = self._thread_extras(rows)
            return [{**self._thread_dict(t), **extras.get(int(t.id), {})} for t in rows]
        except Exception:
            return []

    def thread(self, ref: Any) -> dict | None:
        """One thread of work with what is linked to it: ``{**thread, "events": [...], "facts": [...], "episodes":
        [...], "calls": [...], "chats": [...], "links": [...]}`` (merged threads followed). ``None`` if unknown."""
        from yuki.memory import links as _links
        from yuki.memory.workthreads import describe_thread

        tid = self.thread_id_of(ref)
        tid = self.store.resolve_work_thread(tid) if tid is not None else None
        t = self.store.work_thread(tid) if tid is not None else None
        if t is None:
            return None
        d = describe_thread(self.store, t)
        secs = _links.seconds_of(self.store, d["links"])
        measured: set[int] = set()   # integ22 MEM-1: the links whose own visits were read (none: said so)
        own = _links.own_lasts(self.store, d["links"], measured=measured)   # S19 (mem22): by the user's own visits
        return {
            **self._thread_dict(t),
            **self._thread_extras([t]).get(int(t.id), {}),
            "events": [{"at": _iso(e["at"]), "action": e["action"], "app": e["app"], "host": e["host"],
                        "state": e["state"]} for e in d["events"]],
            "facts": [{"at": _iso(j.at), "app": j.app, "fact": j.fact, "nature": j.nature,
                       "source": describe_source(j.source, app=j.app, at=j.at)} for j in d["facts"]],
            "episodes": [{"start": _iso(e.started_at), "end": _iso(e.ended_at), "text": e.text} for e in d["episodes"]],
            "calls": [{"ref": f"call:{c.id}", "app": c.app, "start": _iso(c.recording_started_at or c.detected_at),
                       "end": _iso(c.ended_at)} for c in d["calls"]],
            "chats": [{"start": _iso(s.started_at), "end": _iso(s.ended_at), "text": s.text} for s in d["chats"]],
            "links": [{"url": link.url, "title": link.title,
                       "line": _links.describe(link, secs.get(link.id), own_last=own.get(int(link.id)),
                                               own_known=int(link.id) in measured)}
                      for link in d["links"]],
        }

    def close_thread(self, ref: Any, how: str) -> dict:
        """The user's own close of a thread of work (the feedback round 3, W1a), at once, no model: ``how`` = ``done``
        (finished), ``dismiss`` (not a piece of work: it stops being a thread, its facts stay) or ``reopen`` (either
        taken back exactly: the window's Undo and its Reopen). Returns ``{"ok", "ref", "status", "how"}``; ``ok`` False
        with ``error`` (plain words) when nothing could be done. Never raises."""
        from yuki.memory.workthreads import close_thread

        tid = self.thread_id_of(ref)
        if tid is None:
            return {"ok": False, "ref": str(ref or ""), "status": None, "how": str(how or ""), "error": "not a thread"}
        try:
            return close_thread(self.store, tid, how)
        except Exception as exc:
            return {"ok": False, "ref": f"thread:{tid}", "status": None, "how": str(how or ""),
                    "error": f"memory: {type(exc).__name__}"}

    def thread_continue(self, ref: Any) -> dict | None:
        """What a thread's Continue shows and does (the feedback round 3, W1d): ``{"ref", "title", "status", "shown"
        (what you were doing and where it stands, said to you; None until written), "next" (the concrete next step;
        None until written or when it only waits on someone), "open", "last_at", "last_label" ("Saturday 3 October"),
        "places": [{"kind": "page" | "window", "app", "title", "url" (pages), "last_at"}] (the windows and pages worth
        bringing back, newest first, at most 5), "ask" (the request to hand Yuki to continue it)}``. ``None`` for an
        unknown thread (merged ones followed). Read only, no model."""
        from yuki.memory.workthreads import continue_context

        tid = self.thread_id_of(ref)
        tid = self.store.resolve_work_thread(tid) if tid is not None else None
        t = self.store.work_thread(tid) if tid is not None else None
        if t is None:
            return None
        try:
            return continue_context(self.store, t)
        except Exception:
            return None

    def before(self, at: Any = None, event: str = "break", minutes: float = 90.0) -> dict:
        """What the user was doing before a moment: the break nearest ``at`` (or, with no ``at``, the latest
        break: "where I left off") and the stretch before it, as the coach's welcome back rebuilds it; or with
        ``event="time"`` the stretch that ended at ``at`` (:func:`yuki.memory.before.before`). Returns ``{"event",
        "break", "since", "until" (ISO), "text", "breaks": [...], "threads": [thread dicts], "ok"}``."""
        from yuki.memory import before as _before

        result = _before.before(self.store, _time_arg(at), event=event, minutes=float(minutes))
        brk = result.get("break")
        return {**result, "at": _iso(result.get("at")), "since": _iso(result.get("since")),
                "until": _iso(result.get("until")),
                "break": ({**brk, "start": _iso(brk["start"]), "end": _iso(brk.get("end"))} if brk else None),
                "breaks_text": _before.breaks_text(result.get("breaks") or []),
                "breaks": [{**b, "start": _iso(b["start"]), "end": _iso(b.get("end"))} for b in result.get("breaks") or []],
                "threads": [self._thread_dict(t) for t in result.get("threads") or []]}

    def links(self, query: str | None = None, since: Any = None, until: Any = None, how: str | None = None,
              limit: int = 10, embed_wait_s: float | None = None) -> list[dict]:
        """Links seen - pages the user had in front and links shared with them in conversations - best first
        (by meaning with ``query``, else most recently seen). Each: ``{"ref": "link:N", "url", "title", "host",
        "app", "how", "sender", "where", "first_seen", "last_seen", "own_last_seen", "seconds", "visits", "thread",
        "by_yuki", "line"}``; ``own_last_seen`` (S19): the end of the user's own last visit of a viewed page, never a
        sighting of a page Yuki opened or a live test's (``None`` when there is none). Never raises."""
        from yuki.memory import links as _links

        measured: set[int] = set()   # integ22 MEM-1: the links whose own visits were read (none: said so)
        try:
            found = _links.search(self.store, (lambda texts: self.embedder.embed(texts, embed_wait_s)),
                                  query, since=_time_arg(since), until=_time_arg(until), how=how, limit=limit)
            secs = _links.seconds_of(self.store, found)
            # S19 (mem22): how far the user themselves got on each page (never a sighting of Yuki's or a test's)
            own = _links.own_lasts(self.store, found, measured=measured)
        except Exception:
            return []

        def own_last(link: Any) -> float | None:
            return own.get(int(link.id))

        return [{"ref": f"link:{link.id}", "url": link.url, "title": link.title, "host": link.host, "app": link.app,
                 "how": link.how, "sender": link.sender, "where": link.where, "first_seen": _iso(link.first_seen),
                 "last_seen": _iso(link.last_seen), "own_last_seen": _iso(own_last(link)) if own_last(link) else None,
                 "seconds": round(secs.get(link.id, 0.0), 1), "visits": link.visits,
                 "thread": f"thread:{link.work_thread_id}" if link.work_thread_id else None, "by_yuki": link.by_yuki,
                 "score": link.score, "line": _links.describe(link, secs.get(link.id), own_last=own_last(link),
                                                              own_known=int(link.id) in measured)}
                for link in found]

    # ======================================================================================================
    # People and routes (revamp Phase B, migration 28, yuki.memory.contacts): who the user talks to and where,
    # the compact directory for cached prompt layers, the user's route preference, and what the task frame needs.
    # SQL only, no model call.
    # ======================================================================================================

    def _near_thread(self, near: Any) -> int | None:
        """A capture thread id from ``near``: an id, ``thread`` / ``capture_thread`` in a dict, or a screen dict
        (``app``, ``title``, ``url``, ``process``, ``scope``) found as memory's thread of that window."""
        if near is None:
            return None
        if isinstance(near, int) and not isinstance(near, bool):
            return int(near)
        if isinstance(near, dict):
            for key in ("thread_id", "capture_thread"):
                if isinstance(near.get(key), int):
                    return int(near[key])
            try:
                return self.store.find_thread(near.get("app"), near.get("title"), near.get("url"),
                                              process=near.get("process"), scope=near.get("scope"))
            except Exception:
                return None
        return None

    def contact(self, ref: Any, near: Any = None) -> dict | None:
        """One person the user talks to, by name, alias, a misheard form the user corrected, or ``contact:N`` /
        ``route:N``: ``{"found": True, "ref", "name", "relation", "aliases", "heard", "tone", "facts", "routes": [...],
        "margin", "close", "usual", "preferred", "calls", "text", "others"}`` - routes most used lately first with
        message counts both ways and the user's own last messages on each (decrypted now, for this person only);
        ``margin``/``close`` say how clearly the first route leads (``yuki.memory.contacts.ROUTE_MARGIN``). ``near``: the
        window in front (a capture thread id or a screen dict): its route is marked ``in_front``, and among several
        people the one with a route there comes first. ``{"found": False, "people": [...]}`` when nobody fits;
        ``None`` when memory cannot be read. Never raises."""
        from yuki.memory import contacts as _c

        try:
            near_thread = self._near_thread(near)
            found = _c.resolve(self.store, ref, near_thread=near_thread)
            if not found:
                return {"found": False, "ref": str(ref or ""), "people": [p.name for p in _c.people(self.store)][:20]}
            card = _c.card(self.store, found[0], near_thread=near_thread)
            try:   # the Yuki window's dossier: messages a day both ways, 14 days (revamp E11)
                card["rhythm"] = _c.rhythm(self.store, [found[0]]).get(found[0].id)
            except Exception:
                card["rhythm"] = None
            text = _c.card_text(card)   # the agent's card: every fact, in memory's form
            # the window's dossier shows one summary per person (``summary``) and only the other facts, said to the
            # user (report 38 §3: the same person fact was shown twice, once "you", once "the user")
            card["facts"] = list(card.pop("facts_shown", None) or [])
            return {"found": True, **card, "text": text, "others": [p.name for p in found[1:4]]}
        except Exception:
            return None

    def people_list(self, *, limit: int = 40) -> list[dict]:
        """The people the user talks to, as structured rows for the Yuki window's Me page (no message text is
        decrypted): ``[{"ref", "name", "relation", "aliases", "tone", "routes": [{"ref", "label", "human", "use",
        "app", "kind", "chat", "handle", "mine", "theirs", "last_mine", "last_theirs", "share", "preferred"}...],
        "usual", "usual_route", "usual_ref", "summary", "margin", "close", "last_contact", "rhythm"}]``, most recently in
        touch first;
        ``rhythm`` is their messages a day both ways over 14 days (``yuki.memory.contacts.rhythm``: ``days``, ``total``,
        ``known_days``, ``peak``...). ``[]`` when memory has no people index yet. Never raises.

        Since migration 33 (the 3 Oct feedback): ``usual`` is one plain line, never a ``route:N``
        (:func:`yuki.memory.contacts.usual_line`: "Slack DM. Code questions go on the PR.", or None) and
        ``usual_route`` its structured form (:func:`yuki.memory.contacts.usual_label`: ``{"ref", "label": "Discord
        DM", "sub", "use", "mine", "theirs", "last_at", "tone", "preferred"}`` or None; ``usual_ref`` is the bare
        ref); ``relation`` is the person fact's
        short label written to the user ("Your father") when there is one; ``summary`` is the one summary per person
        (``{"ref": "fact:N", "headline", "text" (shown form, may be None), "pinned"}`` or None); a route's ``use`` is
        one sentence of what the user uses that chat for with them (None until the contacts rebuild writes it)."""
        from yuki.memory import contacts as _c

        try:
            rows = []
            persons = _c.people(self.store)
            try:
                rhythms = _c.rhythm(self.store, persons)
            except Exception:
                rhythms = {}
            # read once for every card (review 34 M11): who the call digests saw, and the current person facts
            seen = _c.call_people_seen(self.store, limit=200) if persons else {}
            try:
                facts = {f.id: f for f in self.store.portrait_facts(["person"], include_corrections=False)} \
                    if persons else {}
            except Exception:
                facts = None
            for person in persons:
                c = _c.card(self.store, person, with_samples=0, calls_seen=seen, person_facts=facts)
                routes = [{k: r.get(k) for k in ("ref", "label", "human", "use", "app", "kind", "chat", "handle",
                                                 "mine", "theirs", "last_mine", "last_theirs", "share", "preferred")}
                          for r in c["routes"]]
                last = max((t for r in c["routes"] for t in (r.get("last_mine"), r.get("last_theirs")) if t),
                           default=None)
                rows.append({"ref": c["ref"], "name": c["name"], "relation": c["relation"], "aliases": c["aliases"],
                             "tone": c["tone"], "routes": routes, "usual": c["usual"], "usual_route": c["usual_route"],
                             "usual_ref": c["usual_ref"],
                             "summary": c["summary"], "margin": c["margin"],
                             "close": c["close"], "last_contact": last, "rhythm": rhythms.get(person.id)})
            rows.sort(key=lambda r: r["last_contact"] or 0, reverse=True)
            return rows[:limit]
        except Exception:
            return []

    def people_directory(self) -> str:
        """The compact people block (<= ~900 characters, ``yuki.memory.contacts.directory``): each person the user
        talks to with their relation, aliases (names speech recognition misheard too), where the user talks with them,
        and how the user writes to them, closest first: chats and calls outside work count most, work a quarter; then
        the group chats the user writes in most. For the cached profile layer of the typed agent, the voice thinker
        and the voice talker. A plain string, ``""`` when memory has no people index yet. Never raises."""
        from yuki.memory import contacts as _c

        try:
            return _c.directory(self.store)
        except Exception:
            return ""

    def prefer_route(self, ref: Any, route: Any, words: str, *, source: str = "agent") -> dict:
        """Store the user's lasting choice of route for a person ("no, the other one", "use WhatsApp for him"), in
        their own ``words``; ``route``: ``route:N`` as :meth:`contact` lists it. That route comes first for them from
        now on (it survives a rebuild of the index). ``{"ok", "id", "person", "route"}`` or ``{"ok": False, "error"}``.
        Never raises."""
        from yuki.memory import contacts as _c

        try:
            found = _c.resolve(self.store, ref)
            text = " ".join(str(route or "").split())
            head, _, num = text.partition(":")
            if not found:
                return {"ok": False, "error": f"no person {ref!r} in memory's people index"}
            if head.strip().lower() != "route" or not num.strip().isdigit():
                return {"ok": False, "error": "route must be route:N as about lists it"}
            person = found[0]
            target = next((r for r in person.routes if r.id == int(num)), None)
            if target is None:
                return {"ok": False, "error": f"{text} is not one of {person.name}'s routes"}
            out = _c.prefer_route(self.store, person, target, words, source=source)
            return {**out, "person": person.name, "route": target.label(with_service=True)}
        except Exception as exc:
            return {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

    def frame_facts(self, words: str = "", *, said: Any = (), screen: Any = None, apps: Any = (),
                    now: float | None = None, people_limit: int = 4) -> dict:
        """What the task frame needs from memory, in one read (``yuki.agent.frame`` builds the referent register and
        the frame from it; code only):

        * ``named``: people whose name or alias the request's ``words`` contain;
        * ``said``: people named earlier in this conversation (``said``: ``[{"at", "text", "who"}]``, the user's words
          and Yuki's replies), newest first, with the words as matched and who said them;
        * ``front``: the chat in front (``screen``: the window's app, title, url, process, scope) and the people with a
          route in it;
        * ``recent``: people whose chat was in front in the last minutes; ``call``: the call going on and the people
          around it;
        * ``routes``: for the people in play (at most ``people_limit``), their routes ranked with the margin;
        * ``pitfalls``: what did not work before in the apps in play (know-how's "Not:" part: the app in front, the
          ``apps`` given, the apps of the people's usual routes), newest first.

        Every person comes as ``{"id", "ref", "name", "relation", "heard"}``. A route and a group or channel nearby
        also carry ``tags`` / ``last_tag`` (how often the user named the person in their own messages there) and a
        place ``theirs`` (the person's own messages there); the usual route the user's last two lines (``samples``).
        Never raises (missing parts are empty)."""
        from dataclasses import replace as _dc_replace

        from yuki.memory import contacts as _c

        started = time.perf_counter()
        now = time.time() if now is None else float(now)
        out: dict[str, Any] = {"named": [], "said": [], "front": None, "recent": [], "call": None, "routes": {},
                               "pitfalls": [], "people": 0}

        def lite(c: Any) -> dict[str, Any]:
            return {"id": c.id, "ref": c.ref, "name": c.name, "relation": c.relation, "heard": c.heard()}

        try:
            everyone = _c.people(self.store)
        except Exception:
            everyone = []
        out["people"] = len(everyone)
        if not everyone:
            out["pitfalls"] = self._pitfalls(apps, screen, [])
            out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
            return out
        in_play: list[Any] = []
        try:
            for c, w in _c.named_in(self.store, words):
                out["named"].append({**lite(c), "words": w})
                in_play.append(c)
        except Exception:
            pass
        try:
            for item in sorted((s for s in said or [] if isinstance(s, dict)), key=lambda s: -float(s.get("at") or 0)):
                for c, w in _c.named_in(self.store, str(item.get("text") or "")):
                    if any(x["id"] == c.id for x in out["said"]):
                        continue
                    out["said"].append({**lite(c), "words": w, "at": float(item.get("at") or now),
                                        "who": str(item.get("who") or "")})
        except Exception:
            pass
        thread = self._near_thread(screen) if isinstance(screen, dict) else None
        if thread is not None:
            try:
                hits = _c.contacts_of_threads(self.store, [thread]).get(thread, [])
                place = next((r for r in _c.places(self.store) if thread in r.thread_ids), None)
                kind = hits[0][1].kind if hits else (place.kind if place else None)
                members = []
                for c, r in hits:
                    measured = _c.measure_route(self.store, r, c, now)
                    members.append({**lite(c), "route": measured.label(with_service=True), "route_ref": measured.ref,
                                    "last_theirs": measured.last_theirs, "theirs": measured.theirs})
                members.sort(key=lambda m: -(m["last_theirs"] or 0.0))
                if members or place is not None:
                    out["front"] = {"thread_id": thread, "kind": kind, "people": members,
                                    "chat": (place.chat if place else (hits[0][1].chat if hits else None))}
                    for m in members[:2]:
                        c = next((x for x in everyone if x.id == m["id"]), None)
                        if c is not None and kind == "dm":
                            in_play.append(c)
            except Exception:
                pass
        try:
            for item in _c.recent_chat_contacts(self.store, now=now):
                out["recent"].append({**lite(item["contact"]), "route": item["route"].label(with_service=True),
                                      "at": item["at"], "thread_id": item["thread_id"]})
        except Exception:
            pass
        try:
            call = _c.call_contacts(self.store, now=now)
            if call is not None:
                out["call"] = {"call_id": call["call_id"], "app": call["app"], "since": call["since"],
                               "people": [{**lite(p["contact"]), "at": p["at"], "how": p["how"]}
                                          for p in call["people"]]}
        except Exception:
            pass
        for group in (out["said"][:1], out["recent"][:1]):
            for p in group:
                c = next((x for x in everyone if x.id == p["id"]), None)
                if c is not None:
                    in_play.append(c)
        seen: set[int] = set()
        route_apps: list[str] = []
        place_rows: list[Any] | None = None
        for c in in_play:
            if c.id in seen or len(seen) >= people_limit:
                continue
            seen.add(c.id)
            try:
                ranking = _c.rank_routes(self.store, c, now=now)
            except Exception:
                continue
            # the group chats and channels on the same apps: first where the user tags this person (how often, when
            # last: "tag him in my usual channel" went to his DM in every eval run), then where the user writes most
            nearby: list[tuple[Any, int, float | None]] = []
            try:
                if place_rows is None:
                    place_rows = [_c.measure_route(self.store, p, None, now) for p in _c.places(self.store)]
                services = {r.service for r in ranking.routes}
                keys = {r.key for r in ranking.routes}
                same = [p for p in place_rows if p.service in services and p.key not in keys]
                tagged = []
                for p in same:
                    n, last = _c.tags_in(self.store, p, c)
                    # this person's own messages there (their sender names, as a group route with them measures it)
                    theirs = _c.measure_route(self.store, _dc_replace(p, thread_ids=list(p.thread_ids)), c, now).theirs
                    tagged.append((p, n, last, theirs))
                nearby = sorted((x for x in tagged if x[1] > 0 or x[3] > 0 or x[0].score > 0),
                                key=lambda x: (-x[1], -(x[2] or 0.0), -(x[3] > 0), -x[0].score))[:3]
            except Exception:
                nearby = []
            route_tags: dict[int, tuple[int, float | None]] = {}
            for r in ranking.routes[:4]:
                try:
                    route_tags[r.id] = _c.tags_in(self.store, r, c)
                except Exception:
                    route_tags[r.id] = (0, None)
            try:   # how the user writes to them: their own last lines on the usual route ("write it like me")
                said = [s["text"] for s in _c.samples(self.store, ranking.routes[0], 2)] if ranking.routes else []
            except Exception:
                said = []
            out["routes"][c.id] = {
                "name": c.name, "relation": c.relation, "margin": ranking.margin, "close": ranking.close,
                "preferred": ranking.preferred,
                "routes": [{"ref": r.ref, "label": r.label(with_service=True), "app": r.app, "kind": r.kind,
                            "mine": r.mine, "theirs": r.theirs, "last_mine": r.last_mine, "last_theirs": r.last_theirs,
                            "preferred": r.preferred, "thread_ids": r.thread_ids, "service": r.service,
                            "processes": self._route_processes(r.thread_ids),
                            # how the app knows them there when it is not the name the user calls them (a DM's own
                            # name, the sender name they write under): what to search for or mention
                            "known_as": next((n for n in (r.handle, r.chat if r.kind == "dm" else None)
                                              if n and _c.tokens(n) != _c.tokens(c.name)
                                              and not _c.span_in(c.name, n)), None),
                            "tags": route_tags.get(r.id, (0, None))[0], "last_tag": route_tags.get(r.id, (0, None))[1],
                            "in_front": thread is not None and thread in r.thread_ids} for r in ranking.routes[:4]],
                "places": [{"ref": p.ref, "label": p.label(with_service=True), "kind": p.kind, "mine": p.mine,
                            "tags": n, "last_tag": last, "theirs": theirs} for p, n, last, theirs in nearby],
                "samples": said}
            if ranking.usual is not None:
                route_apps += self._route_processes(ranking.usual.thread_ids)
        # what did not work before in the apps in play: the usual routes' apps when a person is in play, else the
        # window in front's (and the apps the caller names, always)
        out["pitfalls"] = self._pitfalls(apps, None if route_apps else screen, route_apps)
        out["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 1)
        return out

    # -- practice memory (migration 29, yuki.memory.practice; no model call here) -----------------------------------

    def record_run(self, run: dict) -> dict:
        """Keep one agent request's structured trace (``yuki.memory.practice.record_run``: one row per step, text
        secret-filtered and encrypted, 30-day TTL) and log the playbooks it was shown and cited. The memory service's
        practice worker learns from it once its follow-up window has passed. Never raises."""
        from yuki.memory import practice as _p

        return _p.record_run(self.store, run)

    def control_record(self, app: str | None, tool: str, control: str) -> dict:
        """How a control's earlier steps went in the run traces (revamp D, S2; ``yuki.memory.practice.control_record``):
        the brain's finish gate learns from it which controls are expected-unverifiable. No model; a few ms."""
        from yuki.memory import practice as _p

        return _p.control_record(self.store, app=app, tool=tool, control=control)

    def sent_messages(self, app: str | None, since: float, *, limit: int = 12) -> list[dict]:
        """The user's own messages memory's watcher captured since ``since`` (epoch s) in ``app``'s chats, newest
        first: ``{"text", "at", "first_seen", "thread", "status"}`` (revamp D, S2: the independent confirmation of a
        send, from ``messages.is_me``). Text is decrypted here, in-process, for the caller's similarity check only.
        Never raises (``[]``)."""
        from yuki.memory.store import app_key as _key

        proc = _key(app) if app else None
        try:
            rows = self.store._query(
                "SELECT m.thread_id, m.first_seen, m.at, m.status, m.text_ciphertext FROM messages m JOIN threads t"
                " ON t.id = m.thread_id WHERE m.is_me = 1 AND m.first_seen >= ? AND m.text_ciphertext IS NOT NULL"
                " AND (? IS NULL OR t.process = ? OR lower(t.app) = ?) ORDER BY m.first_seen DESC LIMIT ?",
                (float(since), proc, proc, proc, int(limit)))
        except Exception:  # noqa: BLE001 - an older database, or none
            return []
        out = []
        for r in rows:
            try:
                text = self.store._dec(r["text_ciphertext"]) or ""
            except Exception:  # noqa: BLE001
                continue
            out.append({"text": text, "at": r["at"], "first_seen": r["first_seen"], "thread": r["thread_id"],
                        "status": r["status"]})
        return out

    def record_hand_trace(self, session_id: str | None, name: str, args: dict | None, ok: bool, verified: bool | None,
                          report: str, ms: float, *, at: float | None = None, app: str | None = None,
                          target: str | None = None, words: str | None = None, why: str | None = None,
                          request_id: str | None = None, injected: Any = None, cited: Any = None,
                          control: Any = None) -> dict:
        """Keep one voice hand (the talker's instant single step) as a run of one action, so the learners see the hands
        too: ``name`` and ``args`` as the hand got them (window ids, element ids and coordinates are kept apart, never
        as meaning), ``ok`` / ``verified`` / ``report`` / ``ms`` as it measured, ``app`` the process it acted in,
        ``target`` the control's role and name, ``words`` what the user said for it; ``injected`` the playbooks shown
        to the talker for it and ``cited`` those it followed (ids or ``pb:N``: the ledger weighs them as for an agent
        run), ``control`` the control it acted on (``"role|name"`` or ``{"role", "name"}``). Fast (one small write);
        never raises."""
        from yuki.memory import practice as _p

        return _p.record_hand_trace(self.store, session_id=session_id, name=name, args=args, ok=ok, verified=verified,
                                    report=report, ms=ms, at=at, app=app, target=target, words=words, why=why,
                                    request_id=request_id, injected=injected, cited=cited, control=control)

    def workflows(self, *, skills: bool = False) -> dict:
        """Practice memory's ways (``yuki.memory.practice.workflows``) for the run card's chips (``skills`` is accepted
        and ignored: the Playbook page reads :meth:`playbook`). Never raises."""
        from yuki.memory import practice as _p

        return _p.workflows(self.store)

    def playbook(self) -> dict:
        """The Yuki window's Playbook page (audit 17 Oct; :func:`yuki.skills.playbook.page`): ``can_do`` (the
        capabilities, one line each, with how often the user used each), ``routines`` (with the card each leads to)
        and ``ways`` (the user's own ways, contract C4). The user's own things (sites, playlists, channels) are not on
        it (21 Oct, the user's ask): they stay Yuki's internal understanding. Never raises."""
        from yuki.skills.playbook import page

        try:
            return page(self.store)
        except Exception as exc:  # noqa: BLE001
            return {"available": False, "error": type(exc).__name__}

    def skill_edit(self, ref: Any, action: str, words: str = "") -> dict:
        """The user's say on the Playbook page (:func:`yuki.skills.playbook.skill_edit`): on a learned row
        (``skill:N``: a way of theirs, a routine's anchor) ``rename``, ``forget`` or ``not_mine``; any other ref is
        refused in plain words. One small write, never a model call and never a hand edit; the catalog and the menu
        follow on their next load. Never raises."""
        from yuki.skills.playbook import skill_edit

        try:
            return skill_edit(self.store, ref, action, words)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": f"Memory didn't take that ({type(exc).__name__})."}

    def launch_identities(self) -> dict:
        """What practice memory learned the apps of Start entries run as (``yuki.memory.practice.launch_identities``,
        appOpen): ``{entry name casefolded: [image names]}``, read back by ``launch_app`` so a game started through its
        launcher is recognised by its own window. Never raises (``{}``)."""
        from yuki.memory import practice as _p

        try:
            return _p.launch_identities(self.store)
        except Exception:
            return {}

    def workflow_edit(self, ref: Any, action: str, words: str = "") -> dict:
        """The user's say over one Playbook item (``yuki.memory.practice.workflow_edit``): ``save`` (it is right),
        ``edit`` (in their words: the practice worker re-distills it), ``disable`` / ``enable``, ``forget``; a
        ``question:N`` is answered with ``save`` and the words. Always through the ledger, never a hand edit. Never
        raises."""
        from yuki.memory import practice as _p

        return _p.workflow_edit(self.store, ref, action, words)

    def run_ledger(self, since: Any = None, until: Any = None, limit: int = 80) -> list[dict]:
        """What Yuki did, from the run traces (practice memory's ``runs``, migration 29), for the Yuki window's
        Activity ledger (revamp E14): one entry per request in ``[since, until)`` (default today), newest first::

            {"key", "source": "agent" | "hand", "lane", "at", "ended_at", "words" (what the user said), "outcome",
             "cost_usd", "ms", "calls", "steps": [{"step", "at", "tool", "kind", "app", "app_name", "target", "input",
             "ok", "verdict", "expected", "why", "result", "ms", "cited": [{"ref": "pb:N", "kind", "title"}]}]}

        Steps are the ones that did something (``kind`` action or memory; reads and questions are left out); ``cited``
        are the playbooks the step said it followed, with their kind (procedure / habit / pitfall / style) and title.
        Read only, decrypted here for the user's own window; ``[]`` without traces. Never raises."""
        try:
            s, u = self._window(since, until)
            heads = self.store._query("SELECT * FROM runs WHERE step=0 AND at >= ? AND at < ? ORDER BY at DESC LIMIT ?",
                                      (s, u, max(1, int(limit))))
        except Exception:
            return []
        if not heads:
            return []
        dec = self.store._dec
        keys = [h["run_key"] for h in heads]
        steps: dict[str, list[Any]] = {}
        try:
            for i in range(0, len(keys), 400):
                part = keys[i:i + 400]
                marks = ",".join("?" * len(part))
                for r in self.store._query(f"SELECT * FROM runs WHERE step > 0 AND kind IN ('action', 'memory') AND"
                                           f" run_key IN ({marks}) ORDER BY run_key, step", part):
                    steps.setdefault(r["run_key"], []).append(r)
        except Exception:
            steps = {}

        def cited_of(raw: Any) -> list[int]:
            try:
                return [int(v) for v in (json.loads(raw or "{}").get("cited") or []) if str(v).isdigit()]
            except (ValueError, TypeError, AttributeError):
                return []

        ids = sorted({i for rows in steps.values() for r in rows for i in cited_of(r["playbooks"])})
        books: dict[int, dict[str, Any]] = {}
        if ids:
            try:
                marks = ",".join("?" * len(ids))
                for r in self.store._query(f"SELECT id, kind, intent_ciphertext FROM playbooks WHERE id IN ({marks})", ids):
                    books[int(r["id"])] = {"ref": f"pb:{r['id']}", "kind": r["kind"], "title": dec(r["intent_ciphertext"]) or ""}
            except Exception:
                books = {}
        names: dict[str, str] = {}
        try:
            from yuki.core.appnames import app_key

            for r in self.store._query("SELECT process, app, count(*) n FROM threads WHERE process IS NOT NULL AND app"
                                       " IS NOT NULL GROUP BY process, app ORDER BY n"):
                names[app_key(r["process"])] = r["app"]   # the most seen display name wins (ordered by count)
        except Exception:
            names = {}
        out = []
        for h in heads:
            rows = []
            for r in steps.get(h["run_key"], []):
                meta = {}
                try:
                    meta = json.loads(r["playbooks"] or "{}")
                except (ValueError, TypeError):
                    meta = {}
                rows.append({"step": int(r["step"]), "at": float(r["at"]), "tool": r["tool"], "kind": r["kind"],
                             "app": r["app"], "app_name": names.get(r["app"] or "", r["app"] or ""),
                             "target": dec(r["target_ciphertext"]) or "", "input": dec(r["input_ciphertext"]) or "",
                             "ok": None if r["ok"] is None else bool(r["ok"]),
                             "verdict": None if r["verdict"] is None else bool(r["verdict"]),
                             "expected": bool(meta.get("expected")), "why": dec(r["why_ciphertext"]) or "",
                             "result": dec(r["result_ciphertext"]) or "", "ms": r["ms"],
                             "cited": [books[i] for i in cited_of(r["playbooks"]) if i in books]})
            out.append({"key": h["run_key"], "source": h["source"], "lane": h["lane"], "at": float(h["at"]),
                        "ended_at": h["ended_at"], "words": dec(h["input_ciphertext"]) or "", "outcome": h["outcome"],
                        "cost_usd": h["cost_usd"], "ms": h["ms"], "calls": h["calls"], "steps": rows})
        return out

    def _route_processes(self, thread_ids: Any) -> list[str]:
        ids = [int(t) for t in thread_ids or [] if isinstance(t, int)]
        if not ids:
            return []
        try:
            rows = self.store._query("SELECT DISTINCT process FROM threads WHERE id IN (%s)" % ",".join("?" * len(ids)),
                                     ids)
            return [r[0] for r in rows if r[0]]
        except Exception:
            return []

    def _pitfalls(self, apps: Any, screen: Any, route_apps: Any, limit: int = 3) -> list[dict]:
        """What did not work before in the apps in play: the "Not:" part of their current know-how (memory's own
        fixed format, ``remember_how``'s avoid), newest first: ``{"app", "text", "at"}``."""
        keys: list[str] = []
        for a in [*(apps or ()), (screen or {}).get("process") if isinstance(screen, dict) else None, *(route_apps or ())]:
            k = app_key(a) if a else None
            if k and k not in keys:
                keys.append(k)
        found = []
        for k in keys:
            try:
                rows = self.store.knowhow_current(k)
            except Exception:
                rows = []
            for row in rows:
                if row.app != k or " Not: " not in (row.text or ""):
                    continue
                avoid = row.text.split(" Not: ", 1)[1].strip()
                if avoid:
                    found.append({"app": k, "text": avoid, "at": float(row.valid_from or 0.0)})
        found.sort(key=lambda x: -x["at"])
        return found[:limit]

    # -- the action trace (capC1, report 96; yuki.memory.trace_store): read helpers and forgetting, content-free ------

    def trace_summary(self, hours: float = 1.0, *, until: float | None = None) -> dict:
        """What the trace noted in the last ``hours``: counts by app, kind, source and how the element was found.
        Counts only; never raises (``{"error"}``)."""
        from yuki.memory import trace_store

        end = time.time() if until is None else float(until)
        try:
            return trace_store.summary(self.store, since=end - float(hours) * 3600.0, until=end)
        except Exception as exc:  # noqa: BLE001 - an older database
            return {"error": type(exc).__name__}

    def trace_forget(self, *, since: float | None = None, until: float | None = None, app: str | None = None) -> dict:
        """Forget what the trace noted in a time range (an hour) and/or of one app (its name or process), with what
        was learned from it; ``{"actions", "elements", "files", "links", "diary_id"}``. Undo with
        :meth:`trace_forget_undo`."""
        from yuki.memory import trace_store
        from yuki.memory.store import app_key

        return trace_store.forget(self.store, since=since, until=until, process=app_key(app) if app else None)

    def trace_forget_undo(self, diary_id: int) -> dict:
        """Bring back what one :meth:`trace_forget` removed (rows still within the 30-day keep)."""
        from yuki.memory import trace_store

        return trace_store.undo_forget(self.store, int(diary_id))

    # -- capC2: tasks (report 78 §5.2-5.4, decision D5) ----------------------------------------------------------------

    def day_tasks(self, since: float, until: float) -> list[dict]:
        """capC2: the tasks the user did that started in ``[since, until)`` (``yuki.memory.trace_tasks.day_tasks``):
        ``[{"id", "ref", "start_at", "end_at", "goal", "apps", "outcome", "outcome_words", "verdict", "episode_id"}]``,
        oldest first. The Day page lists them inside each session. Read only; never raises (``[]``)."""
        from yuki.memory import trace_tasks

        return trace_tasks.day_tasks(self.store, float(since), float(until))

    def task_correct(self, task_id: Any, verdict: str, words: str = "") -> dict:
        """capC2: the user's correction of one task from the Day page: ``renamed`` (``words``: what it was, in their
        words), ``not_one`` (it was not one task) or ``not_mine`` (it isn't what they did). Stored as the task's
        ``user_verdict`` and shown to later task passes as a worked example; never a card, never spoken. ``{"ok",
        "verdict", "goal"}`` or ``{"ok": False, "error"}`` in plain words. Never raises."""
        from yuki.memory import trace_tasks

        try:
            tid = int(str(task_id).split(":")[-1])
        except (TypeError, ValueError):
            return {"ok": False, "error": "That can't be corrected here."}
        return trace_tasks.correct_task(self.store, tid, str(verdict or ""), str(words or ""))

    def task_correct_undo(self, task_id: Any) -> dict:
        """capC2: take a task's correction back (its own goal again, no verdict). Never raises."""
        from yuki.memory import trace_tasks

        try:
            tid = int(str(task_id).split(":")[-1])
        except (TypeError, ValueError):
            return {"ok": False, "error": "That can't be undone here."}
        return trace_tasks.undo_correction(self.store, tid)

    # -- capC4: ways learned from what the user did (report 78 §6.1-6.2) -----------------------------------------------

    def name_digests(self, process: str, role: str, names: Any) -> list[str]:
        """capC4: the keyed digest the action trace takes over an element's name
        (``yuki.memory.trace_demos.name_digest``), for each of ``names`` (the skill runner's live ``pick``: it matches a
        way's fixed item among a container's visible children without memory ever keeping the item's name). Each
        digest is cut to ``yuki.memory.practice.PICK_DIGEST_CHARS`` hex characters, as the way keeps it. Read only;
        never raises (``[]``)."""
        from yuki.memory import practice as _p
        from yuki.memory import trace_demos

        try:
            return [trace_demos.name_digest(self.store, str(process or ""), str(role or ""), str(n or ""))
                    [:_p.PICK_DIGEST_CHARS] for n in list(names or [])[:400]]
        except Exception:  # noqa: BLE001
            return []

    # -- capC6: the workflow graph's queries (report 78 §5.4, yuki.memory.trace_graph) ------------------------------
    # Read only; names only from the element registry's kept names; tasks the user rejected never shown as theirs.

    def done_evidence(self, since: float, until: float, *, limit: int = 12) -> list[dict]:
        """capC6: tasks whose outcome the PC checked inside ``[since, until)`` (learnable only: never unclear, never
        rejected), newest first, each citing its task and outcome action ids for the caller's records and saying it in
        plain words ("you saved a file (.pptx)", "4 PM") for the user. The Work page's evidence. Never raises."""
        from yuki.memory import trace_graph

        return trace_graph.done_evidence(self.store, float(since), float(until), limit=limit)

    def way_path(self, pid: Any) -> dict:
        """capC5: the recorded path a learned way is replayed against (``yuki.skills.uia_route.way_path``): per step
        the app it acted in, the site when it was a web page, whether it stayed in the window of the step before,
        whether a dialog came up after it. Machine facts only, no names. Read only; never raises."""
        from yuki.skills.uia_route import way_path

        return way_path(self.store, pid)

    # -- phase S: skills (yuki.skills; PLAN.md D13). The tray uses its own Store through these; none of the new tables
    # -- (skills, skill_routes, skill_runs, prefs, pref_events, tastes) is ever exposed by the MCP.

    def _skills_seeded(self) -> Any:
        """The catalog on this database (:func:`yuki.skills.catalog.load` with ``store=``: the capabilities from the
        shipped files, then the user's live learned skills, read again only when their rows changed: one count query),
        with the capabilities seeded into the database once per version per process (an upsert; learned rows are never
        seeded). A seed that failed is tried again on the next call (the version is marked seeded only on success).
        A file the habit-key refusal skipped, or a learned row the catalog could not read, is told to the skills log
        once (:meth:`set_skills_log`; lSplit review r1 L1: without the store no learned skill reaches a request)."""
        from yuki.skills import catalog, registry

        # no log set yet: none passed, so the catalog keeps each error to tell once a log is set (it marks one told
        # only when it hands it to a log)
        loaded = catalog.load(store=self.store,
                              log=self._skills_log if getattr(self, "_skills_log_fn", None) is not None else None)
        seen = getattr(self, "_skills_version", None)
        if seen != loaded.version and registry.has_tables(self.store):
            got = registry.seed(self.store, loaded)
            if isinstance(got, dict) and not got.get("error"):
                self._skills_version = loaded.version
        return loaded

    def set_skills_log(self, log: Any) -> None:
        """Where the catalog tells what it skipped (``log(type, **fields)``, content-free: ``connection_error`` with
        the file and a code word, ``learned_error`` with the row id and a code word; integ29 S2: ``knowhow_held`` with
        the know-how id, :meth:`remember_how`'s hold). The process that owns a log
        (the tray's UI log, the memory service's) sets it once; without one nothing is told."""
        self._skills_log_fn = log if callable(log) else None

    def _skills_log(self, type: str, **fields: Any) -> None:
        fn = getattr(self, "_skills_log_fn", None)
        if fn is None:
            return
        try:
            fn(type, **fields)
        except Exception:  # noqa: BLE001 - a log never breaks a load
            pass

    def skill_menu(self, audience: str = "talker", *, now: float | None = None, rank: str | None = "n_obs") -> str:
        """The skill menu for the talker (its cached prompt block) or the thinker (its ``run_skill`` tool), built by
        code from the connection files, the trust ledger and the user's taste (:func:`yuki.skills.menu.menu_text`).
        ``rank``: the taste order, names most seen first by default for both (sVoice r2 M2, sWire: the talker's menu
        sits in a 1-hour cached block, so an hour-ranked list would rewrite it; ``"hour"`` for the old order).
        ``""`` when no skill is loaded. Never raises."""
        from yuki.skills.menu import THINKER, TALKER, menu_text

        try:
            loaded = self._skills_seeded()
            return menu_text(self.store, audience=THINKER if audience == THINKER else TALKER, loaded=loaded, now=now,
                             rank=rank)
        except Exception:  # noqa: BLE001
            return ""

    def skill_menu_version(self) -> str:
        """What the menu depends on (it changes only when the registry does). Never raises (``""``)."""
        from yuki.skills import registry

        try:
            return registry.menu_version(self.store, self._skills_seeded())
        except Exception:  # noqa: BLE001
            return ""

    def skill_inputs(self, skill_key: str) -> dict:
        """One skill's trust as the ledger says it, per rung, and whether it may be offered (D14). Content-free; the
        resolver itself reads the same store in-process (:meth:`skill_resolve`). Never raises."""
        from yuki.skills import registry

        try:
            loaded = self._skills_seeded()
            skill = loaded.get(str(skill_key))
            if skill is None:
                return {"skill": str(skill_key), "known": False}
            table = registry.trust(self.store, skill.key)
            routes = {}
            for r in skill.routes:
                t = table.get(r.key)
                usable, why = registry.usable(r, t)
                routes[r.key] = {"ok": t.ok if t else 0, "failed": t.failed if t else 0,
                                 "unverified": t.unverified if t else 0, "streak": t.streak if t else 0,
                                 "seed_ok": t.seed_ok if t else 0, "usable": usable, "why": why}
            return {"skill": skill.key, "known": True, "kind": skill.kind, "routes": routes,
                    "offer_ready": registry.offer_ready(self.store, skill)}
        except Exception as exc:  # noqa: BLE001
            return {"skill": str(skill_key), "error": type(exc).__name__}

    def skill_resolve(self, skill_key: str, slots: Any, words: Any, *, live: Any = None, again: bool = False,
                      now: float | None = None) -> Any:
        """Code fills a skill's slots and picks its rungs on this store (:func:`yuki.skills.resolve.resolve`)."""
        from yuki.skills.resolve import resolve

        self._skills_seeded()
        return resolve(str(skill_key), slots if isinstance(slots, dict) else {}, words, store=self.store, live=live,
                       again=again, now=now)

    def record_skill_run(self, op: str, **fields: Any) -> Any:
        """The trust ledger (D18, D19): ``op`` ``start`` (the row at t0, before the first step: returns the run key),
        ``finish`` (outcome and the done-check's verdict), ``try`` (a rung that ran and was not the last), ``recheck``
        (the second probe's verdict; ``False`` marks a false done) or ``false_done`` (corrected or repeated within
        60 s). Content-free; never raises (``None``)."""
        from yuki.skills import registry

        try:
            if op == "start":
                return registry.start_run(self.store, **fields)
            if op == "finish":
                return registry.finish_run(self.store, **fields)
            if op == "try":
                return registry.record_route_try(self.store, **fields)
            if op == "recheck":
                return registry.set_recheck(self.store, fields["run_key"], fields.get("agrees"))
            if op == "false_done":
                return registry.mark_false_done(self.store, fields["run_key"])
        except Exception:  # noqa: BLE001
            return None
        return None

    def keep_usual(self, slot: str, option: str, answer: str, *, context: str = "", quote: str | None = None,
                   source: str | None = None) -> dict:
        """The user's answer to "Keep this as your usual?" (``yes`` / ``just_this_time``; S4.2). Never raises."""
        from yuki.skills.prefs import keep_usual

        try:
            return keep_usual(self.store, str(slot), str(option), str(answer), context=context, quote=quote,
                              source=source)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": type(exc).__name__}

    def prefs_choose(self, slot: str, *, context: str = "") -> dict | None:
        """The option the choice rule picks for a preference slot (usual > observed share >= 0.8 with n >= 3 > most
        recent, named aloud), or ``None`` (:func:`yuki.skills.prefs.choose`). Never raises."""
        from yuki.skills.prefs import choose

        try:
            got = choose(self.store, str(slot), context=context)
        except Exception:  # noqa: BLE001
            return None
        if got is None:
            return None
        return {"option": got.option, "source": got.source, "named": got.named, "share": got.share, "n": got.n,
                "still_usual": got.still_usual}


__all__ = [
    "MemoryClient", "service_running", "KNOWHOW_DUPLICATE_COSINE", "RULE_DUPLICATE_COSINE",
    "STANDING_CONTEXT_CHARS", "RESUME_CONTEXT_CHARS", "RESUME_EXCHANGES",
]


# -- change stamps for the Yuki window's live refresh (yuki.ui.web_data.page_stamps; 42-window-live-refresh.md) ------

#: One read-only query per table: the newest id and the latest change time it keeps (the newest row only, for the big
#: timeline). A closed table of machine columns; never content.
_STAMP_SQL: dict[str, str] = {
    "questions": "SELECT max(id), max(updated_at) FROM questions",
    "nudges": "SELECT max(id), max(shown_at), max(reacted_at), max(outcome_at) FROM nudges",
    "timeline": "SELECT id, updated_at FROM timeline ORDER BY id DESC LIMIT 1",
    "goals": "SELECT max(id), max(updated_at) FROM goals",
    "todos": "SELECT max(id), max(updated_at) FROM todos",
    "open_loops": "SELECT max(id), max(resolved_at), max(updated_at) FROM open_loops",
    "contacts": "SELECT max(id), max(updated_at) FROM contacts",
    "calls": "SELECT max(id), max(updated_at) FROM calls",
    "work_threads": "SELECT max(id), max(updated_at) FROM work_threads",
    "portraits": "SELECT max(id) FROM portraits",
    "portrait_facts": "SELECT max(id), max(valid_to), max(expired_at) FROM portrait_facts",
    # how many are active too: a commitment put back (Undo, Not done yet) clears its valid_to and moves no max
    "conversation_facts": "SELECT max(id), max(valid_to), max(expired_at), sum(status = 'active') FROM conversation_facts",
    # the Work page (migration 36): the judged chats, loops and meetings, and the user's marks
    "work_items": "SELECT max(id), max(updated_at) FROM work_items",
    # Needs your reply clears when your reply is captured (a newer message in that chat)
    "messages": "SELECT max(id) FROM messages",
    "playbooks": "SELECT max(id), max(updated_at) FROM playbooks",
    # the Playbook's things and ways (audit 17 Oct): a learned record or way written, renamed or marked
    "skills": "SELECT max(id), max(updated_at) FROM skills WHERE origin != 'capability'",
    # the Playbook's routines and the taste names it lists (audit 17 Oct)
    "routines": "SELECT max(id), max(updated_at) FROM routines",
    "tastes": "SELECT max(id), max(updated_at) FROM tastes",
    "standing_requests": "SELECT max(id), max(updated_at) FROM standing_requests",
    "knowhow": "SELECT max(id), max(valid_to) FROM knowhow",
    "runs": "SELECT max(id) FROM runs",
    "journal": "SELECT max(id) FROM journal",
    # capC2: the tasks inside the Day page's sessions (a new one, or a correction)
    "ui_tasks": "SELECT max(id), max(updated_at) FROM ui_tasks",
}


def change_stamps(client: MemoryClient, tables: tuple[str, ...] | list[str]) -> dict[str, list | None]:
    """``{table: [newest id, latest change, ...]}`` for each known table (``None`` when it can't be read): equal
    stamps mean nothing a page shows from that table changed. Reads only; each query is the table's max over the
    primary key or a short scan of a small table (under a few ms together)."""
    out: dict[str, list | None] = {}
    for table in tables:
        sql = _STAMP_SQL.get(table)
        if sql is None:
            continue
        try:
            rows = client.store._query(sql)
            out[table] = list(rows[0]) if rows else []
        except Exception:
            out[table] = None
    return out
