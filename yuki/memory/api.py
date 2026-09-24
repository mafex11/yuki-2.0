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
    memory.recall("what did Kenji ask me", since="2026-09-20")
"""

from __future__ import annotations

import ctypes
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import numpy as np

from yuki.memory.store import (
    PAUSE_FLAG,
    REFRESH_FLAG,
    JournalEntry,
    Store,
    app_key,
    flag_path,
    service_mutex_name,
)

#: Know-how for the same app whose embedding is at least this close to a new
#: entry is treated as the same procedure restated, and superseded by it.
KNOWHOW_DUPLICATE_COSINE = 0.85
#: Reciprocal-rank-fusion constant for merging vector and keyword hits.
_RRF_K = 60.0
_SYNCHRONIZE = 0x00100000


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
        """Journal facts relevant to ``query``, best first.

        Merges vector search (local embedder, loaded on first use) and keyword
        search (every whitespace-separated term present, case-insensitive) over
        the journal, by reciprocal rank fusion. ``since``/``until`` take epoch
        seconds, a datetime/date or an ISO string (naive = local time; ``since``
        inclusive, ``until`` exclusive). ``app`` takes either the display name
        the journal records ("Google Chrome", "Spotify") or a process name
        ("chrome.exe", "Spotify.exe"), case-insensitively: a fact matches when
        its app is one of the names that app goes by, or when it came from a
        window of that process. Each hit: ``{"at": ISO local time, "app": str,
        "host": str | None, "fact": str, "importance": int, "score": float}``
        (``score`` is the fused rank score; higher is better).
        """
        query = (query or "").strip()
        limit = max(0, int(limit))
        if not query or limit == 0:
            return []
        s, u = _time_arg(since), _time_arg(until)
        names = self._journal_app_names(app) if app and app.strip() else None
        process = app if names else None
        pool = max(limit * 2, 20)
        scores: dict[int, float] = {}
        entries: dict[int, JournalEntry] = {}
        vec = self._embed(query)
        if vec is not None:
            for rank, e in enumerate(self.store.search_journal(vec, s, u, names, pool, process=process)):
                entries[e.id] = e
                scores[e.id] = scores.get(e.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        for rank, e in enumerate(self.store.keyword_journal(query, s, u, names, pool, process=process)):
            entries.setdefault(e.id, e)
            scores[e.id] = scores.get(e.id, 0.0) + 1.0 / (_RRF_K + rank + 1)
        order = sorted(scores, key=lambda i: (-scores[i], -entries[i].at))[:limit]
        return [
            {
                "at": _iso(entries[i].at), "app": entries[i].app, "host": entries[i].host,
                "fact": entries[i].fact, "importance": entries[i].importance, "score": round(scores[i], 5),
            }
            for i in order
        ]

    # -- status and control ------------------------------------------------

    def status(self) -> dict:
        """Memory at a glance (content-free).

        ``{"service_running": bool, "paused": bool, "captures_today": int,
        "facts_today": int, "last_capture_at": ISO | None,
        "portrait_updated_at": ISO | None, "memory_cost_today_usd": float}``.
        "Today" is since local midnight; the cost is the estimate for the journal
        and portrait model calls (list prices, ``Settings.pricing``).
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
            "memory_cost_today_usd": round(today["journal_cost_usd"] + today["portrait_cost_usd"], 6),
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


__all__ = ["MemoryClient", "service_running", "KNOWHOW_DUPLICATE_COSINE"]
