"""Yuki's read/write door into memory (docs/MEMORY.md, "Yuki integration").

Yuki (the agent and the UI) runs in its own process and opens its own
:class:`~yuki.memory.store.Store` on the same database file as the running
``yuki-memory`` service; WAL plus the store's busy timeout make that safe. It
never talks to the service directly: the service is steered with flag files next
to the database (``paused``, ``refresh_portrait``) and observed through its named
mutex and the database itself.

Nothing here calls a model. The local embedder (for ``recall`` and know-how
similarity) loads lazily on first use, in this process.

Usage::

    from yuki.memory.api import MemoryClient
    memory = MemoryClient.open()              # default %LOCALAPPDATA%\\Yuki\\memory\\memory.db
    memory.portrait_text()
    memory.recall("what did Kenji ask me", since="2026-09-20")    # facts and episodes
    memory.activity(since="2026-09-24", group_by="site")          # time per site, visits, stretches
    memory.episodes(since="2026-09-24")                           # what the user was doing, told

Conversation memory (Yuki's own exchanges; extracted by the service, yuki.memory.conversations)::

    memory.log_turn(session_id, request_id, at, user_text, reply_text, actions, outcome) -> int
    memory.standing_context() -> str                  # active rules/preferences + open commitments, <= 1,200 chars
    memory.resume_context(within_hours=6.0) -> str | None   # last session's summary + last 6 exchanges, <= 4,000
    memory.remember_rule(text, source_turn=None) -> int      # immediate, the user's words; supersedes a near-duplicate
    memory.revoke_rule(text) -> int                          # immediate; id revoked or 0
    memory.recall(...)                                       # also kind "chat" (turns) and "session" (summaries)

To-dos and the coach (the service's nudge worker, yuki.memory.nudges)::

    memory.todos(include_done=False) -> list[dict]      # open loops + commitments + the user's own to-dos
    memory.add_todo(text, due=None) -> "todo:N"
    memory.complete_todo(ref) -> id | None              # an id or the item's text
    memory.pending_nudges() -> list[dict]               # unshown, oldest first; wait on Local\\YukiNudgeReady
    memory.ack_nudge(nudge_id, reaction)                # shown | dismissed | replied | snoozed
    memory.snooze_nudges(minutes);  memory.nudge_status() -> dict
"""

from __future__ import annotations

import ctypes
import threading
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.store import (
    PAUSE_FLAG,
    REFRESH_FLAG,
    STANDING_KINDS,
    ConversationFact,
    ConversationTurn,
    JournalEntry,
    Store,
    app_key,
    flag_path,
    service_mutex_name,
    signal_turns,
)

#: Know-how for the same app whose embedding is at least this close to a new
#: entry is treated as the same procedure restated, and superseded by it.
KNOWHOW_DUPLICATE_COSINE = 0.85
#: A rule given with :meth:`MemoryClient.remember_rule` whose embedding is at
#: least this close to an active rule or preference supersedes it.
RULE_DUPLICATE_COSINE = 0.85
#: :meth:`MemoryClient.revoke_rule` without a keyword match revokes the nearest
#: active rule or preference only when it is at least this close. Measured
#: 2026-09-24 (multilingual MiniLM): withdrawals of a rule in other words scored
#: 0.50-0.57 against it, unrelated requests and other rules 0.03-0.29.
REVOKE_MIN_COSINE = 0.45
#: Hard budgets (characters, ~4 per token) of the blocks Yuki attaches to requests.
STANDING_CONTEXT_CHARS = 1_200
RESUME_CONTEXT_CHARS = 4_000
RESUME_EXCHANGES = 6
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


def _file_description(exe: str) -> str:
    """An executable's FileDescription ("Google Chrome"): what the watcher records as the app name."""
    try:
        import win32api

        for lang, codepage in win32api.GetFileVersionInfo(exe, "\\VarFileInfo\\Translation") or []:
            value = win32api.GetFileVersionInfo(exe, f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription")
            if value and str(value).strip():
                return str(value).strip()
    except Exception:
        pass
    return ""


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

    def _embed(self, text: str) -> np.ndarray | None:
        """Embedding of ``text`` with the local model, or ``None`` if the model cannot load."""
        with self._embedder_lock:
            if self._embedder is None:
                from yuki.memory.embed import get_embedder

                self._embedder = get_embedder()
        try:
            return self._embedder.embed_one(text)
        except Exception:
            return None

    def _journal_app_names(self, app: str) -> list[str]:
        """Display names the journal may carry for ``app`` (a display name or a process name).

        The name as given, its :func:`app_key` form ("spotify" matches the app
        "Spotify"), and the FileDescription of any running process with that
        image name ("chrome.exe" -> "Google Chrome"; found names are cached).
        Journal facts are also matched through ``threads.process``.
        """
        key = app_key(app) or ""
        names = {app.strip(), key} | self._app_names.get(key, set())
        try:
            import psutil

            for proc in psutil.process_iter(["name", "exe"]):
                if app_key(proc.info.get("name")) == key and proc.info.get("exe"):
                    desc = _file_description(proc.info["exe"])
                    if desc:
                        self._app_names.setdefault(key, set()).add(desc)
                        names.add(desc)
                        break
        except Exception:
            pass
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

    def correct_portrait(self, text: str) -> int:
        """Record a user-confirmed correction to the portrait; returns its fact id.

        ``text`` is the correction in plain words, as the user confirmed it
        ("The user left Layerpath in August and now works at Acme"). It is
        stored as a portrait fact of kind ``correction`` with origin ``user`` and
        confidence 1.0. The next render follows it over anything inferred; the
        next portrait run folds it into the facts it concerns (superseding the
        contradicted ones) and retires it. Also asks the service for a
        re-render soon (same flag as :meth:`refresh_portrait`).
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("correction text is empty")
        fact_id = self.store.add_correction(text)
        self.refresh_portrait()
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

    # -- know-how ----------------------------------------------------------

    @staticmethod
    def _format_knowhow(app: str | None, text: str) -> str:
        return f"[{app}] {text}" if app else text

    def knowhow(self, app: str | None = None, query: str | None = None, limit: int = 8) -> list[str]:
        """Current (valid) procedures that worked on this PC, as short strings.

        ``app`` is the foreground window's process name ("arc.exe",
        "Spotify.exe"); it is matched in its normalised form (casefolded, no
        ``.exe``: "arc"), so "Arc.exe", "arc.exe" and "arc" are the same app.
        Order: entries for ``app`` first (newest first), then entries most
        similar to ``query`` (local embeddings, any app). With no query the
        rest is general know-how (entries without an app), or with no app
        either, every current entry. Each string is the procedure, prefixed with
        ``[app]`` when it belongs to one: "[arc] pass the URL as a launch
        argument". At most ``limit`` entries.
        """
        limit = max(0, int(limit))
        if limit == 0:
            return []
        picked: list[tuple[str | None, str]] = []
        seen: set[int] = set()
        if app:
            for k in self.store.knowhow_current(app):
                if len(picked) >= limit:
                    break
                picked.append((k.app, k.text))
                seen.add(k.id)
        if len(picked) < limit and query:
            vec = self._embed(query)
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
        text = (text or "").strip()
        if not text:
            raise ValueError("know-how text is empty")
        app = app_key(app)
        vec = self._embed(text)
        candidates = (
            self.store.knowhow_current(app) if app else self.store.knowhow_current(None, any_app=False)
        )
        supersedes: int | None = None
        best = -1.0
        for k in candidates:
            if k.text.casefold() == text.casefold():
                supersedes, best = k.id, 1.0
                break
        if supersedes is None and vec is not None and candidates:
            stored = self.store.knowhow_vectors([k.id for k in candidates])
            for k in candidates:
                other = stored.get(k.id)
                if other is None or other.shape != vec.shape:
                    continue
                score = float(np.dot(vec, other) / ((np.linalg.norm(other) * np.linalg.norm(vec)) or 1.0))
                if score >= KNOWHOW_DUPLICATE_COSINE and score > best:
                    supersedes, best = k.id, score
        model = getattr(self._embedder, "model_name", None) if vec is not None else None
        return self.store.add_knowhow(
            app, text, source_request=source_request, vector=vec, embed_model=model, supersedes=supersedes
        )

    # -- recall ------------------------------------------------------------

    def recall(
        self, query: str, since=None, until=None, app: str | None = None, limit: int = 15
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
        Each hit: ``{"kind": "fact" | "episode" | "chat" | "session", "at": ISO
        local time, "until": ISO | None (an episode's or session's end), "app":
        str | None, "host": str | None, "fact": str, "importance": int | None,
        "score": float}``. ``fact`` is the journal fact, the episode text, the
        session summary, or for a chat one exchange: ``The user said: "..." Yuki
        replied: "..." Yuki did: ...`` (long messages cut after whole
        sentences). ``score`` is the fused rank score; higher is better.
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

        vec = self._embed(query)
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
        for rank, e in enumerate(self.store.keyword_journal(query, s, u, names, pool, process=process)):
            add("fact", rank, e)
        if names is None:
            for rank, ep in enumerate(self.store.keyword_episodes(query, s, u, pool)):
                add("episode", rank, ep)
            for rank, t in enumerate(self.store.keyword_turns(query, s, u, pool)):
                add("chat", rank, t)
            for rank, sm in enumerate(self.store.keyword_summaries(query, s, u, pool)):
                add("session", rank, sm)

        def at(key: tuple[str, int]) -> float:
            item = entries[key]
            return item.at if key[0] in ("fact", "chat") else item.started_at

        order = sorted(scores, key=lambda k: (-scores[k], -at(k)))[:limit]
        out = []
        for key in order:
            item = entries[key]
            score = round(scores[key], 5)
            if key[0] == "fact":
                out.append({
                    "kind": "fact", "at": _iso(item.at), "until": None, "app": item.app, "host": item.host,
                    "fact": item.fact, "importance": item.importance, "score": score,
                })
            elif key[0] == "chat":
                out.append({
                    "kind": "chat", "at": _iso(item.at), "until": None, "app": None, "host": None,
                    "fact": self._chat_text(item), "importance": None, "score": score,
                })
            else:
                out.append({
                    "kind": key[0], "at": _iso(item.started_at), "until": _iso(item.ended_at), "app": None,
                    "host": None, "fact": item.text, "importance": None, "score": score,
                })
        return out

    @staticmethod
    def _message(text: str | None, limit: int) -> str:
        clipped = _clip_sentences(text, limit)
        if clipped is None:
            return f"(a long message of {len(text or '')} characters, not shown)"
        return clipped

    @classmethod
    def _chat_text(cls, t: ConversationTurn) -> str:
        """One exchange as a recall line."""
        out = f'The user said: "{cls._message(t.user_text, 600)}"'
        if t.reply_text:
            out += f' Yuki replied: "{cls._message(t.reply_text, 600)}"'
        if t.actions:
            out += f" Yuki did: {cls._actions_line(t.actions, 300)}."
        return out

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
        actions: list[str], outcome: str,
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
        """
        try:
            turn_id = self.store.add_turn(session_id, request_id, at, user_text, reply_text, actions or (), outcome)
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
        if f.kind == "commitment":
            if text is None:
                return None
            due = f"due {datetime.fromtimestamp(f.due_at):%Y-%m-%d %H:%M}".replace(" 00:00", "") if f.due_at else ""
            dates = ", ".join(x for x in (due, f"asked {_day(f.valid_from)}") if x)
            return f"- {text} ({dates})"
        if text is None and quote is None:
            return None
        if text is None or (quote and normalize_quote(quote) == normalize_quote(text)):
            return f'- "{quote}" (the user\'s words, {_day(f.valid_from)})'
        words = f' Their words: "{quote}"' if quote else ""
        return f"- {text}{words} ({_day(f.valid_from)})"

    def standing_context(self) -> str:
        """The user's active rules and preferences for Yuki, and Yuki's open commitments, as one block.

        Rules and preferences are how the user told Yuki to talk or behave,
        each in the user's own words with the date it was said; commitments are
        what the user asked Yuki to do later or Yuki promised, with the due date
        when one was given and the date asked. Hard cap 1,200 characters (~300
        tokens): when it is over, the oldest items are dropped whole (never cut
        mid-sentence). ``""`` when there is nothing (or memory cannot be read).
        Meant to be attached to every request after the cached portrait.
        """
        try:
            facts = self.store.conversation_facts(STANDING_KINDS)
            items = [(f, line) for f in facts if (line := self._standing_line(f))]
        except Exception:
            return ""
        headings = {
            "rule": "Rules the user set for Yuki:",
            "preference": "The user's preferences about how Yuki talks or works:",
            "commitment": "Open commitments (what Yuki is to do later):",
        }

        def render(chosen: list[tuple[ConversationFact, str]]) -> str:
            lines = ["[Standing rules and open commitments, from Yuki's conversations with the user]"]
            for kind in ("rule", "preference", "commitment"):
                rows = [line for f, line in chosen if f.kind == kind]
                if rows:
                    lines += [headings[kind], *rows]
            return "\n".join(lines)

        kept = sorted(items, key=lambda item: (item[0].valid_from, item[0].id))
        while kept and len(render(kept)) > STANDING_CONTEXT_CHARS:
            kept.pop(0)   # the oldest item goes first
        return render(kept) if kept else ""

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

    def remember_rule(self, text: str, source_turn: int | None = None) -> int:
        """Store a standing rule the user just gave, in the user's words; returns its id.

        ``text``: the rule as the user said it ("call me babe always");
        ``source_turn``: the :meth:`log_turn` id it came from, if known. Stored
        at once (kind ``rule``, origin ``user``), so it shows in
        :meth:`standing_context` from the next request. An active rule or
        preference that says the same thing (identical words, or embedding
        cosine >= 0.85) is superseded by it (bi-temporal: the old version ends,
        history kept). Raises ``ValueError`` for an empty text.
        """
        from yuki.memory.conversations import normalize_quote

        text = " ".join((text or "").split())
        if not text:
            raise ValueError("rule text is empty")
        vec = self._embed(text)
        candidates = self.store.conversation_facts(("rule", "preference"))
        supersedes: int | None = None
        best = -1.0
        wanted = normalize_quote(text)
        for f in candidates:
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
        return self.store.add_conversation_fact(
            "rule", text, quote=text, subject="", origin="user",
            source_turn_ids=[int(source_turn)] if source_turn else (), vector=vec, embed_model=model,
            supersedes=supersedes,
        )

    def revoke_rule(self, text: str) -> int:
        """Mark the active rule (or preference) that ``text`` refers to as revoked; returns its id, or 0.

        ``text`` is the rule, or the user's words withdrawing it ("stop
        calling me babe"). The rule whose text or quote contains every word of
        ``text`` is chosen (the newest if several); otherwise the nearest by
        local embedding, if its cosine is at least 0.45. The rule is kept as
        history (status ``revoked``, ``valid_to`` now, the words kept as the
        reason). 0 when nothing matches.
        """
        from yuki.memory.conversations import normalize_quote

        text = " ".join((text or "").split())
        if not text:
            return 0
        try:
            candidates = self.store.conversation_facts(("rule", "preference"))
        except Exception:
            return 0
        if not candidates:
            return 0
        terms = normalize_quote(text).split()
        target: ConversationFact | None = None
        keyword = [f for f in candidates
                   if all(t in normalize_quote(f"{f.text} {f.quote or ''}") for t in terms)]
        if keyword:
            target = keyword[-1]
        else:
            vec = self._embed(text)
            if vec is not None:
                stored = self.store.conversation_fact_vectors([f.id for f in candidates])
                best = REVOKE_MIN_COSINE
                for f in candidates:
                    other = stored.get(f.id)
                    if other is None or other.shape != vec.shape:
                        continue
                    score = float(np.dot(vec, other) / ((np.linalg.norm(other) * np.linalg.norm(vec)) or 1.0))
                    if score >= best:
                        target, best = f, score
        if target is None:
            return 0
        ok = self.store.end_conversation_fact(target.id, "revoked", note=f'the user: "{text}"')
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
             "background_media": [{"app", "seconds"}]}

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
        agg = aggregate(self.store.timeline_between(s, u), s, u, group_by, limit=max(1, int(limit)))
        agg["since"], agg["until"] = _iso(s), _iso(u)
        totals = agg["totals"]
        totals["first_at"], totals["last_at"] = _iso(totals["first_at"]), _iso(totals["last_at"])
        for item in agg["items"]:
            item.pop("key", None)
            for k in ("longest_start", "longest_end", "first_at", "last_at"):
                item[k] = _iso(item[k])
        return agg

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
             "source": "loop" | "commitment" | "user",
             "due": ISO local time with offset ("2026-09-25T15:00:00+05:30"), or a
                    date "2026-09-25" when no time was given, or None,
             "status": "open" | "done",
             "evidence": str | None,     # a loop: who asked, where, when ("asked by Vinay in
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

        ``ref`` is an id from :meth:`todos` (``"loop:12"``, ``"commit:5"``,
        ``"todo:3"``) or the item's text in the user's words: the open item
        whose text contains every word of it (the newest if several), else the
        nearest by local embedding if close enough (cosine >= 0.45).

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
            return complete_item(self.store, ref, embed=self._embed)
        except Exception:
            return None

    @staticmethod
    def _nudge_dict(n: Any) -> dict:
        return {"id": n.id, "at": _iso(n.at), "kind": n.kind, "text": n.text, "reason": n.reason}

    def pending_nudges(self) -> list[dict]:
        """Nudges the service wrote that the UI has not shown yet, oldest first.

        Each: ``{"id": int, "at": ISO, "kind": "praise" | "nudge" | "reminder",
        "text": str, "reason": str | None}``. ``text`` is what to show the user
        (one or two short sentences, already in their register and following
        their rules); ``reason`` is the coach's one-line why, for a tooltip or
        the log, not for the user. A praise or nudge not shown within 15
        minutes (``[nudges] deliver_within_min``) expires and is no longer
        returned; reminders never expire. The service sets the named
        auto-reset event ``Local\\YukiNudgeReady`` after writing one, so the UI
        can wait on it and call this at once. Call :meth:`ack_nudge` with
        ``"shown"`` when one is on screen. Never raises.
        """
        try:
            from yuki.memory.nudges import NudgeConfig
            from yuki.memory.timeline import PrivacySection

            window = NudgeConfig().deliver_within_min
            try:   # the user's [nudges] table, read only (the service applies the same window)
                from types import SimpleNamespace

                from yuki.memory.privacy import user_config_path

                if self._nudge_section is None:
                    self._nudge_section = PrivacySection(SimpleNamespace(path=user_config_path()), "nudges",
                                                         NudgeConfig.from_dict)
                window = self._nudge_section.get().deliver_within_min
            except Exception:
                pass
            cutoff = time.time() - window * 60.0
            return [self._nudge_dict(n) for n in self.store.nudges(pending=True)
                    if n.kind == "reminder" or n.at >= cutoff]
        except Exception:
            return []

    def ack_nudge(self, nudge_id: int, reaction: str) -> None:
        """Record what happened to a nudge: ``"shown"`` | ``"dismissed"`` | ``"replied"`` | ``"snoozed"``.

        ``shown`` when it appears on screen (it leaves :meth:`pending_nudges`);
        then, if the user acts on it, ``dismissed`` / ``replied`` / ``snoozed``
        (a later reaction replaces ``shown``; ``shown`` never replaces a real
        reaction). ``snoozed`` only records the reaction: call
        :meth:`snooze_nudges` for the quiet period itself. The coach reads the
        reactions (it gets sparser after dismissals) and the nightly portrait
        gets their counts for its Relationship section. Raises ``ValueError``
        for another reaction; an unknown id is ignored.
        """
        self.store.react_to_nudge(int(nudge_id), str(reaction))

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
                               "today": {"praise": 0, "nudge": 0, "reminder": 0}}
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

    # -- status and control ------------------------------------------------

    def status(self) -> dict:
        """Memory at a glance (content-free).

        ``{"service_running": bool, "paused": bool, "captures_today": int,
        "facts_today": int, "last_capture_at": ISO | None,
        "portrait_updated_at": ISO | None, "memory_cost_today_usd": float}``.
        "Today" is since local midnight; the cost is the estimate for the journal,
        portrait, episode, conversation and coach model calls (list prices, ``Settings.pricing``).
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
                + today.get("conversation_cost_usd", 0.0) + today.get("nudge_cost_usd", 0.0), 6
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


__all__ = [
    "MemoryClient", "service_running", "KNOWHOW_DUPLICATE_COSINE", "RULE_DUPLICATE_COSINE", "REVOKE_MIN_COSINE",
    "STANDING_CONTEXT_CHARS", "RESUME_CONTEXT_CHARS", "RESUME_EXCHANGES",
]
