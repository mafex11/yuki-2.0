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

Calls, just the user's side (the service's call worker, yuki.memory.calls)::

    memory.call_status() -> {"call": {...} | None, "recording": bool}   # wait on Local\\YukiCallReady
    memory.answer_call(call_id, "yes" | "no" | "always" | "never" | "none") -> bool
    memory.stop_call() -> int | None                     # "Stop remembering this call"
    memory.call_audio_waiting() -> dict | None           # "N calls waiting to be remembered"
    memory.erase_call_audio() -> int                     # "Erase waiting call audio"
    memory.call_prefs() -> list[dict];  memory.set_call_pref(app, "always" | "never" | None)

People and routes (migration 28, yuki.memory.contacts; SQL only, no model call)::

    memory.contact(ref, near=None) -> dict | None        # one person: routes ranked with counts and margin, their
                                                          # aliases and misheard names, the user's last messages each
    memory.people_directory() -> str                     # the compact people block (<= ~900 chars) for cached layers
    memory.prefer_route(ref, "route:N", words) -> dict   # the user's lasting route choice, in their own words
    memory.frame_facts(words, said=, screen=, apps=)     # what the task frame needs (yuki.agent.frame)

The personal brief and personal-ness (yuki.memory.brief; written by the nightly portrait render, migration 31)::

    memory.brief() -> dict | None                        # with_him, people, own_bits, now, routine, careful
    memory.brief_for("thinker" | "talker" | "cards") -> str   # the slice for one consumer ("" before the first)
    memory.brief_slice(consumer) -> {"text", "ids", "at"}     # the same with its line ids (the personal-ness log)
    memory.record_surfaced(kind, ref, brief_ids, fact_ids, session_id=, at=, brief_at=) -> int
        # which brief lines and memory items a reply or card was written from (ids only; the nightly judge)
    memory.activity_header(since=None, until=None) -> dict    # threads touched, things finished, dates ahead

Practice memory (migration 29, yuki.memory.practice; learned by the service's practice worker, yuki.memory.practice_learn)::

    memory.record_run(run) -> dict                       # one agent request's step trace (the agent loop writes it)
    memory.record_hand_trace(session_id, name, args, ok, verified, report, ms, app=, target=, words=) -> dict
    memory.practice(words, apps=, open_apps=) -> dict    # procedures / habits and pitfalls for the apps in play
    memory.workflows() -> dict                           # the window's Playbook page
    memory.workflow_edit(ref, "save" | "edit" | "disable" | "enable" | "forget", words="") -> dict

What happened on calls and meetings (timeline digests written by the service, yuki.memory.calldigest)::

    memory.calls_in(since=None, until=None) -> list[dict]      # merged sessions: app, times, speech, gists
    memory.call_timeline(call, since=None, until=None) -> dict | None   # the session's blocks and dated lines
    memory.call_transcript(call, since=None, until=None, page=1, per_page=60) -> dict | None   # exact words
    memory.recall(...)            # a fact heard on a call with a digest carries "call": "call:<id>"
"""

from __future__ import annotations

import ctypes
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
        brief (the agent's profile layer), ``talker`` tone and names only, at most ~2,100 characters, labelled
        "tone and names, never news" (the voice talker's prompt, builder 3b), ``cards`` the whole brief as ABOUT THE
        USER lines."""
        return str(self.brief_slice(consumer).get("text") or "")

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

    def remember_how(self, app: str | None, text: str, source_request: str | None = None) -> int:
        """Store a procedure that worked (after a successful task); returns its id.

        ``app`` is the process name of the app it applies to ("arc.exe"; stored
        normalised as "arc", see :meth:`knowhow`), ``None`` for general
        know-how; ``text`` the procedure in one or two sentences;
        ``source_request`` the user request it came from. If a current entry for
        the same app says the same thing (embedding cosine >= 0.85, or identical
        text), that entry is superseded (its ``valid_to`` set, history kept)
        instead of piling up near-duplicates.
        """
        text = secret_filter.redact_text((text or "").strip())
        source_request = secret_filter.redact_text(source_request) if source_request else source_request
        if not text:
            raise ValueError("know-how text is empty")
        app = app_key(app)
        vec = self._embed(text)
        supersedes = self.store.knowhow_duplicate(app, text, vec, KNOWHOW_DUPLICATE_COSINE)
        model = getattr(self._embedder, "model_name", None) if vec is not None else None
        return self.store.add_knowhow(
            app, text, source_request=source_request, vector=vec, embed_model=model, supersedes=supersedes,
            origin="agent",
        )

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
        ``addressed``: who a voice turn was said to, from voice's ``turn_closed`` ("yuki" | "others");
        a turn said to others is kept for the record and never extracted (no rule, to-do or fact).
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
        """
        try:
            latest = self.store.latest_turn()
            now = time.time()
            if latest is None or now - latest.at > float(within_hours) * 3600.0:
                return None
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
        its window is still open and may be rewritten), "totals": dict}``.
        """
        s, u = self._window(since, until)
        rows = self.store.episodes_between(s, u)[-max(1, int(limit)):]
        return [
            {"start": _iso(e.started_at), "end": _iso(e.ended_at), "text": e.text, "final": e.final,
             "totals": (e.aggregates or {}).get("totals") or {}}
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
        # a check-in card's moment (day_start, followup, ...), as Yuki wrote it: the card's title comes from it
        moment = inputs.get("moment") if isinstance(inputs, dict) and n.trigger == "checkin" else None
        return {"id": n.id, "at": _iso(n.at), "kind": n.kind, "text": n.text, "reason": n.reason, "ref": n.ref,
                "trigger": n.trigger, "offer": card_offer_view(n), "outcome": getattr(n, "outcome", None),
                "moment": moment}

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
        ``url`` and ``title`` for the UI to open; ``draft``: ``text``; ``ask``: ``text`` for the ask bar, unsent) or
        ``{"ok": False, "why"}`` (``expired`` after 30 minutes, ``gone``, ``off``, ``already``, ``unknown``,
        ``no_offer``). Never a model call, never an agent run; a second press changes nothing."""
        from yuki.memory import offers

        try:
            return offers.act(self.store, int(nudge_id), enabled=self._nudges_config().offers != "off")
        except Exception as exc:
            return {"ok": False, "why": "error", "error": f"{type(exc).__name__}: {exc}"}

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
        VALORANT night as morning). Measured by the welcome back's own rule
        (:func:`yuki.memory.nudges.presence_sessions`: the first settled stretch after a
        break of ``resume_after_h``); the model decides what their words mean.
        """
        now = time.time() if now is None else float(now)
        try:
            from yuki.memory.nudges import NudgeConfig, presence_sessions
            from yuki.memory.timeline import _clip, format_duration

            cfg = NudgeConfig()
            since = now - 36 * 3600.0
            clips = _clip([r for r in self.store.timeline_states(since, now) if not r.by_yuki], since, now)
            sessions = [x for x in presence_sessions(clips, cfg) if x.settled]
            start, gap = None, 0.0
            for i, x in enumerate(sessions):
                g = x.start - (sessions[i - 1].end if i else since)
                if g >= cfg.resume_after_h * 3600.0:
                    start, gap = x.start, g
        except Exception:
            return None
        if start is None:
            return None
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

    # -- calls: the user's own side (yuki.memory.calls) ----------------------

    def call_status(self) -> dict:
        """The call going on now, for the UI's consent card and "remembering your side" indicator.

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
        ``unanswered`` | ``not_asked`` (nudges were snoozed) | ``stopped``.
        Content-free. The service sets ``Local\\YukiCallReady`` whenever it
        changes. Never raises.
        """
        try:
            from yuki.memory.calls import CallConfig
            from yuki.memory.store import CALL_OPEN_STATUSES

            waiting = self.call_audio_waiting()
            rows = self.store.calls(statuses=CALL_OPEN_STATUSES, limit=1)
            if not rows:
                return {"call": None, "recording": False, "waiting": waiting}
            c = rows[0]
            timeout = CallConfig().ask_timeout_s
            try:
                from types import SimpleNamespace

                from yuki.memory.privacy import user_config_path
                from yuki.memory.timeline import PrivacySection

                timeout = PrivacySection(SimpleNamespace(path=user_config_path()), "calls",
                                         CallConfig.from_dict).get().ask_timeout_s
            except Exception:
                pass
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
            }
            return {"call": call, "recording": c.status == "recording", "waiting": waiting}
        except Exception:
            return {"call": None, "recording": False, "waiting": None}

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
        audio"): the encrypted files go now, their words are never made. Returns the chunks erased. What was
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

    def _session_dict(self, span: dict, rows: dict, digests: dict, waiting: dict) -> dict:
        """One merged call for the tools: times, speech, what was recorded, digest state and gists."""
        ids = list(span["call_ids"])
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
                    gists.append({"call_id": cid, "gist": d["gist"]})
            elif r.recording_started_at is None:
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
        recorded = any(rows.get(c) is not None and rows[c].recording_started_at is not None for c in ids)
        from yuki.memory.calldigest import merge_people

        # who was on it and where, as the screen around the call showed (the digest's reading; migration 25)
        contexts = [(digests.get(c) or {}).get("context") or {} for c in ids]
        people = merge_people(*(c.get("people") or [] for c in contexts))
        where = list(dict.fromkeys(c["where"] for c in contexts if c.get("where")))
        return {
            "ref": f"call:{ids[0]}", "call_ids": ids, "app": span["app"], "service": span.get("service"),
            "start": _iso(span["start"]), "end": _iso(span["end"]), "ongoing": bool(span["ongoing"]),
            "seconds": round(sum(e - b for b, e in span["pieces"]), 1), "speech_s": round(speech, 1),
            "recorded": recorded, "digested": [c for c in ids if (digests.get(c) or {}).get("status") == "ok"],
            "gists": gists, "rows": parts, "people": people, "where": where,
        }

    def calls_in(self, since=None, until=None, limit: int = 30) -> list[dict]:
        """The calls and meetings overlapping ``[since, until)`` (default today), oldest first, rows of one
        continuous session merged (``yuki.memory.callspans``). Each::

            {"ref": "call:16", "call_ids": [16, ..., 21], "app", "service", "start": ISO, "end": ISO,
             "ongoing", "seconds", "speech_s" (the user's own speech), "recorded" (their side was),
             "digested": [ids with a timeline], "gists": [{"call_id", "gist"}],
             "rows": [{"call_id", "start", "end", "state"}]}

        plus meetings the timeline measured (an app in front holding the microphone or camera) that no call
        row covers: ``{"ref": None, "app", "service", "start", "end", "seconds", "speech_s": 0, "recorded":
        False, "transcript": False, "rows": [], ...}`` - hours only, no transcript. Nobody else's words are ever
        stored; ``people`` (``[{"name", "how"}]``) and ``where`` (``[str]``) are who was on the call and where, as
        the screen around it showed (the digest's reading, yuki.memory.callcontext). Content-free except the
        gists, people and where.
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
            out = [dict(self._session_dict(sp, rows, digests, waiting), transcript=True) for sp in spans]
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
                        "start": _iso(m["start"]), "end": _iso(m["end"]), "ongoing": False,
                        "seconds": m.get("seconds"), "speech_s": 0.0, "recorded": False, "transcript": False,
                        "digested": [], "gists": [], "rows": []})
        return out

    def call_timeline(self, call: Any, since=None, until=None) -> dict | None:
        """What happened on one call (the whole merged session the row ``call`` belongs to), timeline-wise, from
        the user's side: :meth:`calls_in`'s fields plus ``"blocks": [{"call_id", "start", "end", "topic",
        "lines": [{"at", "text", "nature"}]}]`` in time order (ISO times), cut to ``[since, until)`` when given,
        and ``"words_kept_until"`` (ISO: the exact transcript's 30-day TTL; None once gone). None: no such call.
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
        out = self._session_dict(span, rows, digests, waiting)
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
            b["lines"] = [{"at": _iso(ln["at"]), "text": ln["text"], "nature": ln.get("nature", "fact")}
                          for ln in b["lines"]]
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

    def call_transcript(self, call: Any, since=None, until=None, page: int = 1, per_page: int = 60) -> dict | None:
        """The user's exact words on one call (its merged session), as the recogniser heard them, a page at a time:
        ``{"ref", "app", "start", "end", "since", "until", "page", "pages", "total", "loops_left_out",
        "lines": [{"at": ISO, "call_id", "text", "lang", "unclear", "repeats"}]}``. An exact repeat of a recent
        line (the recogniser's loop on noise) is left out and counted on the line it repeats (``repeats``);
        ``to_yuki`` is "yes" when voice judged the line said to Yuki, "maybe" when Yuki was listening then;
        ``unclear`` is the recogniser's own low confidence. Kept for the 30-day capture TTL. None: no such call.
        """
        from yuki.memory.calldigest import _same_words, prepare, yuki_spans_for

        cid = self.call_id_of(call)
        span = self._call_session(cid) if cid is not None else None
        if span is None:
            return None
        s, u = _time_arg(since), _time_arg(until)
        lines: list[dict] = []
        loops = 0
        for i in span["call_ids"]:
            captures = self.store.call_side_captures(i)
            for utt in prepare(captures, yuki_spans_for(self.store, captures)):
                if (s is not None and utt.at < s) or (u is not None and utt.at >= u):
                    continue
                if utt.repeat:
                    loops += 1
                    words = _same_words(utt.text)
                    for prev in reversed(lines[-4:]):
                        if _same_words(prev["text"]) == words:
                            prev["repeats"] += 1
                            break
                    continue
                lines.append({"at": utt.at, "call_id": i, "text": utt.text[:500], "lang": utt.lang,
                              "unclear": utt.unclear, "repeats": 0, "to_yuki": utt.to_yuki})
        per_page = max(10, min(int(per_page or 60), 150))
        pages = max(1, -(-len(lines) // per_page))
        page = max(1, min(int(page or 1), pages))
        chunk = lines[(page - 1) * per_page: page * per_page]
        for ln in chunk:
            ln["at"] = _iso(ln["at"])
        return {"ref": f"call:{span['call_ids'][0]}", "app": span["app"], "start": _iso(span["start"]),
                "end": _iso(span["end"]), "since": _iso(s), "until": _iso(u), "page": page, "pages": pages,
                "total": len(lines), "loops_left_out": loops, "lines": chunk}

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
        return {"id": j.id, "at": j.at, "app": j.app, "text": j.fact, "nature": j.nature, "importance": j.importance,
                "by_yuki": bool(j.by_yuki), "milestone": bool(j.milestone)}

    def journal_entries(self, since: Any = None, until: Any = None) -> list[dict]:
        """Journal facts in ``[since, until)`` (decrypted), oldest first."""
        return [self._journal_dict(j) for j in self.store.journal_between(_time_arg(since), _time_arg(until))]

    def journal_by_ids(self, ids: Any) -> list[dict]:
        """Journal facts by id (the evidence behind a portrait fact)."""
        wanted = sorted({int(i) for i in ids or () if str(i).isdigit() or isinstance(i, int)})
        return [self._journal_dict(j) for j in self.store.journal_by_ids(wanted)] if wanted else []

    def portrait_facts_with_evidence(self, evidence_per_fact: int = 6) -> list[dict]:
        """The current portrait facts, each with the journal entries it rests on (newest first).

        Each: ``{"id", "ref": "fact:N", "kind", "subject", "text", "summary", "aspect", "origin", "confidence",
        "since", "evidence": [journal dict, ...]}``. ``summary`` is the app's short line for the fact (at most
        eight words; an open loop's is its title), model-written (migration 21, yuki.memory.titles), ``None``
        until written. Corrections waiting to be folded in are left out.
        """
        facts = self.store.portrait_facts(include_corrections=False)
        ids = sorted({i for f in facts for i in f.source_ids})
        entries = {j.id: j for j in self.store.journal_by_ids(ids)} if ids else {}
        out = []
        for f in facts:
            ev = sorted((entries[i] for i in f.source_ids if i in entries), key=lambda j: -j.at)
            out.append({"id": f.id, "ref": f"fact:{f.id}", "kind": f.kind, "subject": f.subject, "text": f.text,
                        "summary": f.summary, "aspect": f.aspect, "origin": f.origin, "confidence": f.confidence, "since": f.valid_from,
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

        Each: ``{"id", "session_id", "request_id", "at", "user", "reply", "actions", "outcome"}``.
        """
        if session_id is None:
            latest = self.store.latest_turn()
            if latest is None:
                return []
            session_id = latest.session_id
        turns = self.store.turns_for_session(session_id, limit=max(1, int(limit)))
        return [{"id": t.id, "session_id": t.session_id, "request_id": t.request_id, "at": t.at,
                 "user": t.user_text, "reply": t.reply_text or "", "actions": list(t.actions or []),
                 "outcome": t.outcome} for t in turns]

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
        heard = {a["heard"].casefold() for a in self.aliases()}
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
                "line": thread_line(t)}

    @staticmethod
    def thread_id_of(ref: Any) -> int | None:
        """``thread:12`` / ``W12`` / ``12`` -> 12 (the id formats the lists show)."""
        text = str(ref or "").strip().strip("[]")
        text = text.split(":")[-1].lstrip("Ww")
        return int(text) if text.isdigit() else None

    def threads(self, statuses: Any = ("active", "paused"), query: str | None = None, limit: int = 12,
                embed_wait_s: float | None = None) -> list[dict]:
        """The user's threads of work, most recently touched first (``query``: closest in meaning first, any
        status but merged). Each: ``{"ref": "thread:N", "id", "title", "status", "state", "open", "involves",
        "first_seen", "last_seen", "events", "importance", "line"}``. Never raises."""
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
            return [self._thread_dict(t) for t in rows]
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
        return {
            **self._thread_dict(t),
            "events": [{"at": _iso(e["at"]), "action": e["action"], "app": e["app"], "host": e["host"],
                        "state": e["state"]} for e in d["events"]],
            "facts": [{"at": _iso(j.at), "app": j.app, "fact": j.fact, "nature": j.nature,
                       "source": describe_source(j.source, app=j.app, at=j.at)} for j in d["facts"]],
            "episodes": [{"start": _iso(e.started_at), "end": _iso(e.ended_at), "text": e.text} for e in d["episodes"]],
            "calls": [{"ref": f"call:{c.id}", "app": c.app, "start": _iso(c.recording_started_at or c.detected_at),
                       "end": _iso(c.ended_at)} for c in d["calls"]],
            "chats": [{"start": _iso(s.started_at), "end": _iso(s.ended_at), "text": s.text} for s in d["chats"]],
            "links": [{"url": link.url, "title": link.title, "line": _links.describe(link, secs.get(link.id))}
                      for link in d["links"]],
        }

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
        "app", "how", "sender", "where", "first_seen", "last_seen", "seconds", "visits", "thread", "by_yuki",
        "line"}``. Never raises."""
        from yuki.memory import links as _links

        try:
            found = _links.search(self.store, (lambda texts: self.embedder.embed(texts, embed_wait_s)),
                                  query, since=_time_arg(since), until=_time_arg(until), how=how, limit=limit)
            secs = _links.seconds_of(self.store, found)
        except Exception:
            return []
        return [{"ref": f"link:{link.id}", "url": link.url, "title": link.title, "host": link.host, "app": link.app,
                 "how": link.how, "sender": link.sender, "where": link.where, "first_seen": _iso(link.first_seen),
                 "last_seen": _iso(link.last_seen), "seconds": round(secs.get(link.id, 0.0), 1), "visits": link.visits,
                 "thread": f"thread:{link.work_thread_id}" if link.work_thread_id else None, "by_yuki": link.by_yuki,
                 "score": link.score, "line": _links.describe(link, secs.get(link.id))} for link in found]

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
            return {"found": True, **card, "text": _c.card_text(card), "others": [p.name for p in found[1:4]]}
        except Exception:
            return None

    def people_list(self, *, limit: int = 40) -> list[dict]:
        """The people the user talks to, as structured rows for the Yuki window's Me page (no message is
        decrypted): ``[{"ref", "name", "relation", "aliases", "tone", "routes": [{"ref", "label", "app", "kind",
        "mine", "theirs", "last_mine", "last_theirs", "share", "preferred"}...], "usual", "margin", "close",
        "last_contact"}]``, most recently in touch first. ``[]`` when memory has no people index yet. Never raises."""
        from yuki.memory import contacts as _c

        try:
            rows = []
            for person in _c.people(self.store):
                c = _c.card(self.store, person, with_samples=0)
                routes = [{k: r.get(k) for k in ("ref", "label", "app", "kind", "mine", "theirs", "last_mine",
                                                 "last_theirs", "share", "preferred")} for r in c["routes"]]
                last = max((t for r in c["routes"] for t in (r.get("last_mine"), r.get("last_theirs")) if t),
                           default=None)
                rows.append({"ref": c["ref"], "name": c["name"], "relation": c["relation"], "aliases": c["aliases"],
                             "tone": c["tone"], "routes": routes, "usual": c["usual"], "margin": c["margin"],
                             "close": c["close"], "last_contact": last})
            rows.sort(key=lambda r: r["last_contact"] or 0, reverse=True)
            return rows[:limit]
        except Exception:
            return []

    def people_directory(self) -> str:
        """The compact people block (<= ~900 characters, ``yuki.memory.contacts.directory``): each person the user
        talks to with their relation, aliases (names speech recognition misheard too), where the user talks with them
        most used lately first, and how the user writes to them; then the group chats the user writes in most. For
        the cached profile layer of the typed agent, the voice thinker and the voice talker. A plain string, ``""``
        when memory has no people index yet. Never raises."""
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
                          request_id: str | None = None) -> dict:
        """Keep one voice hand (the talker's instant single step) as a run of one action, so the learners see the hands
        too: ``name`` and ``args`` as the hand got them (window ids, element ids and coordinates are kept apart, never
        as meaning), ``ok`` / ``verified`` / ``report`` / ``ms`` as it measured, ``app`` the process it acted in,
        ``target`` the control's role and name, ``words`` what the user said for it. Fast (one small write); never
        raises."""
        from yuki.memory import practice as _p

        return _p.record_hand_trace(self.store, session_id=session_id, name=name, args=args, ok=ok, verified=verified,
                                    report=report, ms=ms, at=at, app=app, target=target, words=words, why=why,
                                    request_id=request_id)

    def practice(self, words: str = "", *, apps: Any = (), open_apps: Any = (), embed_wait_s: float | None = None,
                 now: float | None = None) -> dict:
        """What practice memory holds for one request (``yuki.memory.practice.relevant``): ``{"ways": [procedures, then
        a habit or style note], "pitfalls": [...], "apps": [the apps in play], "named": [apps named in the words],
        "measured": bool, "ms"}``. The apps in play are the apps named in ``words`` (whole words of an app name memory
        has stored), ``apps`` (the caller's: apps the request names) and ``open_apps`` (the open windows' process
        names) - not the window in front alone. Ways clear a similarity floor to the words (the same vector as the
        related memories); pitfalls of an app in play always come. Never raises (empty lists)."""
        from yuki.memory import practice as _p

        started = time.perf_counter()
        try:
            text = " ".join(str(words or "").split())
            known = [str(a) for a in (*(open_apps or ()), *(apps or ())) if a]
            # the intent is matched without the app names (the apps are matched by scope): the words' own vector when
            # they name no app, else one more embedding of what is left (~20 ms on the service's pipe)
            intent = _p.intent_words(self.store, text, known) if text else ""
            vec = self._embed(intent or text, embed_wait_s) if text else None
            named = _p.apps_named(self.store, text, known)
            out = _p.relevant(self.store, query_vec=vec, apps_in_play=[*named, *known], now=now)
            out["named"] = named
            out["measured"] = vec is not None
        except Exception as exc:
            out = {"ways": [], "pitfalls": [], "apps": [], "named": [], "measured": False,
                   "error": f"{type(exc).__name__}: {exc}"}
        out["ms"] = round((time.perf_counter() - started) * 1000, 1)
        return out

    def workflows(self) -> dict:
        """The Yuki window's Playbook page (``yuki.memory.practice.workflows``): ``{"available", "procedures",
        "lessons", "proposals", "workflows", "learning"}`` - what Yuki learned to do on this PC, the lessons from its
        mistakes and what it is not sure of yet (tentative objects, teach-back questions), each with its uses, last use,
        confidence and state. Never raises."""
        from yuki.memory import practice as _p

        return _p.workflows(self.store)

    def workflow_edit(self, ref: Any, action: str, words: str = "") -> dict:
        """The user's say over one Playbook item (``yuki.memory.practice.workflow_edit``): ``save`` (it is right),
        ``edit`` (in their words: the practice worker re-distills it), ``disable`` / ``enable``, ``forget``; a
        ``question:N`` is answered with ``save`` and the words. Always through the ledger, never a hand edit. Never
        raises."""
        from yuki.memory import practice as _p

        return _p.workflow_edit(self.store, ref, action, words)

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


__all__ = [
    "MemoryClient", "service_running", "KNOWHOW_DUPLICATE_COSINE", "RULE_DUPLICATE_COSINE",
    "STANDING_CONTEXT_CHARS", "RESUME_CONTEXT_CHARS", "RESUME_EXCHANGES",
]
