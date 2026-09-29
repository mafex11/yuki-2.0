"""Now: the freshest picture of the screen, kept in RAM by the memory service and served to Yuki.

The watcher (:mod:`yuki.memory.watcher`) already reads the window in front on
every settle, and since 2026-09-29 every other window, popup, menu and
notification toast that can be seen (read on change only).  Until now those
reads only went to the database, and the live agent read the same window again
when the user spoke.  The :class:`NowBoard` keeps the latest read of each
visible window here, in memory, and :class:`NowServer` hands it to the tray over
a local pipe, so the brain's per-turn situation and the voice's desk block know
what is on screen the instant a request starts, with how old each read is.

* :class:`NowBoard` (service): per window the privacy-filtered, secret-redacted
  gist of its latest read (the same text the capture pipeline stores; a
  blocked app's entry holds no content), the layout (which windows are visible,
  how much of each, in Z-order), the popups seen recently, and for a window
  measured opaque the app's own data when a reader for it exists
  (:data:`APP_DATA_READERS`: Warp's saved tabs).  RAM only, capped: at most
  :data:`MAX_WINDOWS` windows and :data:`MAX_POPUPS` popups, text clipped.
* :class:`NowServer` / :class:`NowClient`: the pipe, in the shape of
  :mod:`yuki.memory.embed_ipc` (user-only DACL, remote clients refused, a key
  derived from memory's DPAPI-protected key proven both ways before anything
  crosses).  Requests and replies are JSON.  ``now`` returns the board,
  ``refresh`` asks the watcher to read a window again now (no wait).
* :func:`format_now`: the compact text the brain gets, capped.

API for the tray and voice (the voice builder's desk block uses the same)::

    from yuki.memory.now import shared_client, format_now
    board = shared_client().now(wait_s=0.05)      # dict, or None (no service / no answer in 50 ms)
    text = format_now(board, foreground=hwnd)     # "" when board is None
    shared_client().refresh(hwnd)                 # ask for a re-read (returns at once)

Nothing here reads the screen itself; it only keeps and serves what the watcher read.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

#: Windows kept on the board (the oldest reads of windows no longer visible go first).
MAX_WINDOWS = 24
#: Popups kept, and how long one stays "recent".
MAX_POPUPS = 12
POPUP_TTL_S = 900.0
#: Characters of text kept per window: the one in front, the others, a popup.
FRONT_CHARS = 6000
OTHER_CHARS = 1500
POPUP_CHARS = 600
#: Messages kept per conversation window, and characters per message.
MESSAGES_KEPT = 12
MESSAGE_CHARS = 300
#: Control names kept per window.
CONTROLS_KEPT = 30
#: A window no longer visible stays on the board this long (it may come back).
GONE_TTL_S = 600.0

#: Readers of an app's own local data for windows UI Automation cannot see into, by process image.
#: A data source per app, like the extractor profiles: it is consulted only for a window measured opaque
#: (its content coverage), never to decide anything about an app.  ``module:function`` taking
#: ``(rect, live_title, *, privacy)`` and returning a dict or None.
APP_DATA_READERS: dict[str, str] = {"warp.exe": "yuki.memory.warp:window_data"}


# ---------------------------------------------------------------------------
# The board (service side)
# ---------------------------------------------------------------------------


def gist_of(extraction: Any, *, front: bool, is_masked: Callable[[str], bool] | None = None) -> dict[str, Any]:
    """The text a board entry keeps from one (already redacted) extraction.

    Messages as ``sender: text`` (the newest :data:`MESSAGES_KEPT`), else the
    main body, clipped; masked lines (a hidden secret) left out.
    """
    masked = is_masked or (lambda _line: False)
    limit = FRONT_CHARS if front else OTHER_CHARS
    messages = []
    for m in list(getattr(extraction, "messages", None) or [])[-MESSAGES_KEPT:]:
        text = "\n".join(line for line in str(m.text or "").splitlines() if line.strip() and not masked(line))
        if not text:
            continue
        who = "the user" if getattr(m, "is_me", False) else (m.sender or "")
        messages.append({"sender": who, "text": text[:MESSAGE_CHARS], "time": m.time_label or ""})
    body = ""
    if not messages:
        lines = [" ".join(raw.split()) for raw in str(getattr(extraction, "body", "") or "").splitlines()]
        body = "\n".join(line for line in lines if line and not masked(line))
        if len(body) > limit:
            body = body[:limit] + "…"
    controls = [str(c)[:80] for c in list(getattr(extraction, "controls", None) or [])[:CONTROLS_KEPT]]
    return {
        "kind": str(getattr(extraction, "kind", "") or ""),
        "profile": str(getattr(extraction, "profile", "") or ""),
        "title": str(getattr(extraction, "title", "") or ""),
        "url": getattr(extraction, "url", None),
        "scope": str(getattr(extraction, "thread_scope", "") or "")[:200],
        "text": body,
        "messages": messages,
        "controls": controls,
    }


class NowBoard:
    """The latest read of every visible window, in RAM (service side).  Thread-safe."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._windows: dict[int, dict[str, Any]] = {}
        self._popups: deque[dict[str, Any]] = deque(maxlen=MAX_POPUPS)
        self._layout: dict[str, Any] = {"at": 0.0, "foreground": 0, "surfaces": [], "ms": 0.0}
        self.counters: dict[str, int] = {"reads": 0, "blocked": 0, "popups": 0, "served": 0, "layouts": 0}
        #: Bumped on every write, so a server can reuse the last snapshot it serialised.
        self.version = 0

    # -- writes (the watcher) ---------------------------------------------------

    def set_layout(self, surfaces: list[dict[str, Any]], *, foreground: int, at: float, ms: float) -> None:
        """What is on screen now: ``surfaces`` top first, each ``{hwnd, kind, title, rect, share, z, pid}``."""
        with self._lock:
            self._layout = {"at": at, "foreground": int(foreground or 0), "surfaces": list(surfaces),
                            "ms": round(ms, 2)}
            self.counters["layouts"] += 1
            self.version += 1
            visible = {int(s["hwnd"]) for s in surfaces}
            for hwnd, entry in list(self._windows.items()):
                if hwnd in visible:
                    entry.pop("gone_at", None)
                elif "gone_at" not in entry:
                    entry["gone_at"] = at
            self._prune(at)

    def note_title(self, hwnd: int, title: str, at: float) -> None:
        """The window's title changed (an OS event): the entry's read is older than what it shows now."""
        with self._lock:
            for surface in self._layout.get("surfaces") or []:
                if int(surface.get("hwnd") or 0) == int(hwnd):
                    surface["title"] = title  # newer than the layout it came with
            entry = self._windows.get(int(hwnd))
            if entry is not None and entry.get("live_title") != title:
                entry["live_title"] = title
                entry["changed_at"] = at
                self.version += 1

    def note_change(self, hwnd: int, at: float) -> None:
        """Something inside the window changed (content events): counted, for the entry's freshness."""
        with self._lock:
            entry = self._windows.get(int(hwnd))
            if entry is not None and at > float(entry.get("read_at") or 0.0):
                entry["changes"] = int(entry.get("changes") or 0) + 1
                self.version += 1

    def put(self, hwnd: int, entry: dict[str, Any]) -> None:
        """A read landed: ``entry`` holds :func:`gist_of`'s fields plus app, process, read_at, read_ms,
        trigger, foreground, visible_share, coverage, opaque (and app_data when known)."""
        with self._lock:
            old = self._windows.get(int(hwnd)) or {}
            entry = {**entry, "hwnd": int(hwnd), "changes": 0, "changed_at": None,
                     "live_title": entry.get("title") or old.get("live_title", "")}
            if "app_data" not in entry and old.get("app_data") is not None and entry.get("opaque"):
                entry["app_data"] = old["app_data"]
            self._windows[int(hwnd)] = entry
            self.counters["reads"] += 1
            self.version += 1
            self._prune(time.time())

    def block(self, hwnd: int, *, app: str, process: str, reason: str, at: float, foreground: bool) -> None:
        """The window may not be read (privacy): its entry keeps no content, only that and why."""
        with self._lock:
            self._windows[int(hwnd)] = {"hwnd": int(hwnd), "app": app, "process": process, "blocked": reason,
                                        "read_at": at, "foreground": foreground, "text": "", "messages": [],
                                        "controls": [], "title": "", "url": None, "changes": 0}
            self.counters["blocked"] += 1
            self.version += 1

    def forget(self, hwnd: int) -> None:
        with self._lock:
            self._windows.pop(int(hwnd), None)

    def set_app_data(self, hwnd: int, data: dict[str, Any] | None) -> None:
        with self._lock:
            entry = self._windows.get(int(hwnd))
            if entry is not None:
                entry["app_data"] = data
                self.version += 1

    def add_popup(self, entry: dict[str, Any]) -> None:
        """A popup (menu, toast, dialog, flyout) that appeared, as read: gist fields plus hwnd, app, at."""
        with self._lock:
            key = (entry.get("hwnd"), entry.get("text"), entry.get("title"))
            for i, old in enumerate(self._popups):
                if (old.get("hwnd"), old.get("text"), old.get("title")) == key:
                    del self._popups[i]
                    break
            self._popups.append(dict(entry))
            self.counters["popups"] += 1
            self.version += 1

    def _prune(self, now: float) -> None:
        for hwnd, entry in list(self._windows.items()):
            if entry.get("gone_at") and now - float(entry["gone_at"]) > GONE_TTL_S:
                del self._windows[hwnd]
        if len(self._windows) > MAX_WINDOWS:
            order = sorted(self._windows.items(), key=lambda kv: (kv[1].get("gone_at") is None,
                                                                  float(kv[1].get("read_at") or 0.0)))
            for hwnd, _entry in order[: len(self._windows) - MAX_WINDOWS]:
                del self._windows[hwnd]

    # -- reads (the pipe) -------------------------------------------------------

    def entry(self, hwnd: int) -> dict[str, Any] | None:
        with self._lock:
            entry = self._windows.get(int(hwnd))
            return dict(entry) if entry is not None else None

    def snapshot(self, *, now: float | None = None) -> dict[str, Any]:
        """The board as plain data: the visible windows top first (read or not), the recent popups."""
        now = time.time() if now is None else float(now)
        with self._lock:
            layout = dict(self._layout)
            windows = []
            for surface in layout.get("surfaces") or []:
                if surface.get("kind") != "window":
                    continue
                hwnd = int(surface["hwnd"])
                entry = dict(self._windows.get(hwnd) or {"hwnd": hwnd, "read_at": None})
                entry.update(z=surface.get("z"), share=surface.get("share"), rect=surface.get("rect"),
                             foreground=hwnd == layout.get("foreground"))
                entry.setdefault("live_title", surface.get("title") or "")
                if surface.get("title"):
                    entry["live_title"] = surface.get("title")
                read_at = entry.get("read_at")
                entry["age_s"] = round(now - float(read_at), 1) if read_at else None
                changed = entry.get("changed_at")
                entry["stale"] = bool(read_at is None or (changed and float(changed) > float(read_at)))
                windows.append(entry)
            popups = [dict(p, age_s=round(now - float(p.get("at") or now), 1)) for p in self._popups
                      if now - float(p.get("at") or 0.0) <= POPUP_TTL_S]
            self.counters["served"] += 1
            counters = dict(self.counters)
        return {"at": now, "foreground": layout.get("foreground"), "layout_at": layout.get("at"),
                "layout_ms": layout.get("ms"), "windows": windows, "popups": popups, "counters": counters}

    def sizes(self) -> dict[str, int]:
        """Content-free sizes, for the service's stats."""
        with self._lock:
            chars = sum(len(e.get("text") or "") + sum(len(m.get("text") or "") for m in e.get("messages") or [])
                        for e in self._windows.values())
            return {"windows": len(self._windows), "popups": len(self._popups), "chars": chars}


def app_data_for(process: str, rect: tuple[int, int, int, int], title: str, *, privacy: Any = None,
                 scales: tuple[float, ...] = (1.0,)) -> dict[str, Any] | None:
    """The app's own data about one of its windows (:data:`APP_DATA_READERS`), or ``None``.  Never raises."""
    target = APP_DATA_READERS.get((process or "").strip().lower())
    if not target:
        return None
    module_name, _, function_name = target.partition(":")
    try:
        import importlib

        reader = getattr(importlib.import_module(module_name), function_name)
        return reader(tuple(rect), title, privacy=privacy, scales=scales)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# What the brain reads
# ---------------------------------------------------------------------------


def _ago(seconds: float | None) -> str:
    if seconds is None:
        return "not read yet"
    if seconds < 90:
        return f"read {int(seconds)} s ago"
    if seconds < 5400:
        return f"read {int(round(seconds / 60))} min ago"
    return f"read {seconds / 3600:.1f} h ago"


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: max(limit - 1, 0)] + "…"


def _one(text: str, limit: int) -> str:
    return _clip(" ".join(str(text or "").split()), limit)


def _app_data_line(data: dict[str, Any] | None) -> str:
    if not data or not (data.get("tabs") or data.get("recent_titles")):
        return ""
    parts = []
    if data.get("tabs"):
        tabs = []
        for t in data["tabs"]:
            mark = " (showing)" if t.get("showing") else ""
            title = f" \"{_one(t['title'], 70)}\"" if t.get("title") else ""
            folder = f" in {t['folder']}" if t.get("folder") else ""
            tabs.append(f"tab {t.get('n')}{mark}{title}{folder}")
        saved = data.get("saved_at")
        when = f", saved {datetime.fromtimestamp(float(saved)).strftime('%H:%M')}" if saved else ""
        parts.append(f"from {data.get('source') or 'the app'}{when}: " + "; ".join(tabs))
    if data.get("recent_titles"):
        parts.append("titles its windows had lately (memory's timeline; a tab shows its title in front): "
                     + "; ".join(f'\"{_one(t, 70)}\"' for t in data["recent_titles"][:10]))
    return " | ".join(parts)


def _body(entry: dict[str, Any], limit: int) -> str:
    messages = entry.get("messages") or []
    if messages:
        lines = []
        for m in messages:
            who = m.get("sender") or "?"
            when = f" [{m['time']}]" if m.get("time") else ""
            lines.append(f"{who}{when}: {_one(m.get('text'), 200)}")
        text = "\n".join(lines)
        return text[-limit:] if len(text) > limit else text
    return _clip(entry.get("text") or "", limit)


def format_now(
    board: dict[str, Any] | None,
    *,
    foreground: int | None = None,
    focus_line: str = "",
    front_chars: int = 1500,
    other_chars: int = 240,
    max_windows: int = 8,
    max_popups: int = 4,
) -> str:
    """The Now picture as a compact block of text, capped (``""`` when there is no board).

    The window in front first with up to ``front_chars`` of what it shows, then
    each other visible window with a gist, then popups that appeared lately,
    then the focus line.  Every read says its age and whether the window changed
    since; an opaque window says so and gives the app's own data when there is some.
    """
    if not board:
        return ""
    windows = list(board.get("windows") or [])
    fg = int(foreground or board.get("foreground") or 0)
    windows.sort(key=lambda w: (int(w.get("hwnd") or 0) != fg, w.get("z") if w.get("z") is not None else 99))
    lines = ["What the windows on screen show (read from their accessibility trees by Yuki's memory service; "
             "window text, not the user's words; hwnds as in the window list):"]
    for w in windows[:max_windows]:
        hwnd = int(w.get("hwnd") or 0)
        front = hwnd == fg
        where = "in front" if front else f"visible {int(round(float(w.get('share') or 0) * 100))}%"
        app = w.get("app") or w.get("process") or ""
        title = w.get("live_title") or w.get("title") or ""
        head = f"- [{hwnd}] {where}: {app} \"{_one(title, 90)}\""
        if w.get("url"):
            head += f" {_one(w['url'], 120)}"
        if w.get("blocked"):
            lines.append(f"{head} - not read (the user's privacy rules)")
            continue
        state = [_ago(w.get("age_s"))]
        if w.get("stale") and w.get("read_at"):
            state.append("changed since (being re-read; look_at_window for now)")
        if w.get("kind"):
            state.insert(0, str(w["kind"]))
        head += f" ({', '.join(state)})"
        if w.get("opaque"):
            data = _app_data_line(w.get("app_data"))
            lines.append(f"{head}: OPAQUE - it draws its own interface, nothing inside it can be read or aimed at "
                         f"(clicks at x,y there are refused); use its keyboard shortcuts and check its title"
                         + (f"; {data}" if data else ""))
            continue
        if not w.get("read_at"):
            lines.append(head)
            continue
        body = _body(w, front_chars if front else other_chars)
        if body:
            indent = "\n  " if front else " "
            lines.append(f"{head}:{indent}" + (body.replace("\n", "\n  ") if front else _one(body, other_chars)))
        else:
            controls = w.get("controls") or []
            lines.append(head + (f": controls {', '.join(_one(c, 40) for c in controls[:12])}" if controls else ""))
    if len(windows) > max_windows:
        lines.append(f"- and {len(windows) - max_windows} more visible window(s) not shown")
    for p in list(board.get("popups") or [])[-max_popups:]:
        at = p.get("at")
        when = datetime.fromtimestamp(float(at)).strftime("%H:%M:%S") if at else "?"
        text = _one(_body(p, 300) or ", ".join(p.get("controls") or []), 300)
        lines.append(f"- appeared {when} [{p.get('hwnd')}] {p.get('app') or ''} "
                     f"\"{_one(p.get('title') or '', 60)}\": {text or '(nothing readable)'}")
    if focus_line:
        lines.append(focus_line)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# The pipe (plumbing, the embed_ipc shape)
# ---------------------------------------------------------------------------

#: How long connecting (and the handshake) may take on a caller's path.
CONNECT_WAIT_S = 0.05
#: After the pipe could not be reached, how long before it is tried again.
RETRY_AFTER_S = 5.0
_BUF = 1 << 16


def pipe_name(db_path: str | Path | None) -> str:
    """The Now pipe for the database at ``db_path``: per database and per Windows user."""
    from yuki.memory.store import default_db_path

    db = Path(db_path) if db_path is not None else default_db_path()
    user = os.environ.get("USERNAME", "")
    digest = hashlib.sha1(f"{user}|{str(db.resolve()).lower()}".encode()).hexdigest()[:16]
    return rf"\\.\pipe\YukiNow-{digest}"


def auth_key(cipher: Any) -> bytes:
    """The pipe's shared secret, derived from memory's data key."""
    return bytes.fromhex(cipher.digest("yuki-now-ipc-v1"))


class NowServer:
    """Serves a :class:`NowBoard` on the database's pipe (inside ``yuki-memory``).

    Args:
        board: The board.
        cipher: The store's cipher (the shared secret).
        db_path: The database the pipe is named for.
        on_refresh: ``(hwnd or None) -> bool``: ask the watcher for a read now.
        log: ``log(type, **fields)``, content-free.
    """

    def __init__(self, board: NowBoard, cipher: Any, db_path: str | Path | None, *,
                 on_refresh: Callable[[int | None], bool] | None = None,
                 log: Callable[..., None] | None = None) -> None:
        self.board = board
        self.address = pipe_name(db_path)
        self._key = auth_key(cipher)
        self._on_refresh = on_refresh
        self._log = log
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: Any = None
        self.stats = {"connections": 0, "requests": 0, "refused": 0, "errors": 0, "bytes": 0, "ms_max": 0.0}

    def start(self) -> "NowServer":
        from yuki.memory.embed_ipc import _listener_class

        self._listener = _listener_class()(self.address)
        self._thread = threading.Thread(target=self._accept_loop, name="yuki-memory-now-ipc", daemon=True)
        self._thread.start()
        self._event("now_ipc", state="listening")
        return self

    def stop(self) -> None:
        self._stop.set()
        try:
            from multiprocessing import connection

            connection.PipeClient(self.address).close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(2.0)
        try:
            if self._listener is not None:
                self._listener.close()
        except Exception:  # noqa: BLE001
            pass
        self._event("now_ipc", state="stopped", **self.stats)

    def _event(self, kind: str, **fields: Any) -> None:
        if self._log is not None:
            try:
                self._log(kind, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _accept_loop(self) -> None:
        from multiprocessing import AuthenticationError, connection

        while not self._stop.is_set():
            try:
                conn = self._listener.accept()
            except Exception as exc:  # noqa: BLE001
                if self._stop.is_set():
                    return
                self.stats["errors"] += 1
                self._event("now_ipc", state="accept_error", error=f"{type(exc).__name__}: {exc}")
                time.sleep(0.2)  # an accept that keeps failing must not spin a core
                continue
            if self._stop.is_set():
                conn.close()
                return

            def handshake_and_serve(conn: Any = conn) -> None:
                try:
                    connection.deliver_challenge(conn, self._key)
                    connection.answer_challenge(conn, self._key)
                except (AuthenticationError, EOFError, OSError):
                    self.stats["refused"] += 1
                    conn.close()
                    return
                self.stats["connections"] += 1
                self._serve(conn)

            threading.Thread(target=handshake_and_serve, name="yuki-memory-now-conn", daemon=True).start()

    def _serve(self, conn: Any) -> None:
        try:
            while not self._stop.is_set():
                try:
                    raw = conn.recv_bytes(1 << 20)
                except (EOFError, OSError):
                    return
                started = time.perf_counter()
                try:
                    reply = self._answer(json.loads(raw.decode("utf-8")))
                except Exception as exc:  # noqa: BLE001 - one bad request must not end the connection
                    self.stats["errors"] += 1
                    reply = {"id": None, "ok": False, "error": type(exc).__name__}
                data = json.dumps(reply, ensure_ascii=False, default=str).encode("utf-8")
                self.stats["bytes"] += len(data)
                self.stats["ms_max"] = max(self.stats["ms_max"], round((time.perf_counter() - started) * 1000, 2))
                try:
                    conn.send_bytes(data)
                except (EOFError, OSError):
                    return
        finally:
            conn.close()

    #: A serialised snapshot is reused while the board is unchanged, for at most this long (ages drift by it).
    REUSE_S = 1.0

    def _answer(self, request: dict[str, Any]) -> dict[str, Any]:
        op, rid = request.get("op"), request.get("id")
        self.stats["requests"] += 1
        if op == "now":
            version, now = self.board.version, time.monotonic()
            cached = getattr(self, "_cached", None)
            if cached is not None and cached[0] == version and now - cached[1] <= self.REUSE_S:
                board = cached[2]
            else:
                board = self.board.snapshot()
                self._cached = (version, now, board)
            return {"id": rid, "ok": True, "board": board}
        if op == "refresh":
            hwnd = request.get("hwnd")
            queued = bool(self._on_refresh(int(hwnd) if hwnd else None)) if self._on_refresh else False
            return {"id": rid, "ok": True, "queued": queued}
        if op == "stats":
            return {"id": rid, "ok": True, **self.stats, **self.board.sizes()}
        return {"id": rid, "ok": False, "error": "unknown op"}


class NowClient:
    """The tray's side of the pipe.  Thread-safe; never waits longer than asked; never raises."""

    def __init__(self, cipher: Any, db_path: str | Path | None) -> None:
        self.address = pipe_name(db_path)
        self._key = auth_key(cipher)
        self._conn: Any = None
        self._lock = threading.Lock()
        self._ids = 0
        self._next_try = 0.0
        self.absent = False
        self.stats = {"ok": 0, "timeout": 0, "absent": 0, "ms_max": 0.0, "last_ms": 0.0}

    def _connect(self, wait_s: float) -> Any:
        if self._conn is not None:
            return self._conn
        if time.monotonic() < self._next_try:
            return None
        import _winapi
        from multiprocessing import AuthenticationError, connection

        started = time.monotonic()
        try:
            _winapi.WaitNamedPipe(self.address, max(1, int(wait_s * 1000)))
            handle = _winapi.CreateFile(self.address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                                        _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED,
                                        _winapi.NULL)
            _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
            conn = connection.PipeConnection(handle)
        except OSError as exc:
            self.absent = getattr(exc, "winerror", None) == 2
            self._next_try = time.monotonic() + (RETRY_AFTER_S if self.absent else 0.0)
            return None
        try:
            if not conn.poll(max(0.02, wait_s - (time.monotonic() - started))):
                raise TimeoutError("no challenge yet")
            connection.answer_challenge(conn, self._key)
            connection.deliver_challenge(conn, self._key)
        except AuthenticationError:
            conn.close()
            self.absent = True
            self._next_try = time.monotonic() + RETRY_AFTER_S
            return None
        except Exception:  # noqa: BLE001 - there, but not answering this instant
            conn.close()
            return None
        self.absent = False
        self._conn = conn
        return conn

    def _drop(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass

    def _ask(self, request: dict[str, Any], wait_s: float) -> dict[str, Any] | None:
        started = time.perf_counter()
        deadline = time.monotonic() + max(wait_s, 0.0)
        if not self._lock.acquire(timeout=max(wait_s, 0.0)):
            self.stats["timeout"] += 1
            return None
        try:
            conn = self._connect(max(min(CONNECT_WAIT_S, wait_s), 0.01))
            if conn is None:
                self.stats["absent" if self.absent else "timeout"] += 1
                return None
            self._ids += 1
            request = {**request, "id": self._ids}
            try:
                conn.send_bytes(json.dumps(request).encode("utf-8"))
                reply = None
                while reply is None:
                    if not conn.poll(max(0.0, deadline - time.monotonic())):
                        # The answer may still come: the connection stays, and a late answer to this request
                        # is recognised by its id and dropped by the next call (no new handshake).
                        self.stats["timeout"] += 1
                        return None
                    answer = json.loads(conn.recv_bytes().decode("utf-8"))
                    if answer.get("id") == request["id"]:
                        reply = answer
            except (EOFError, OSError, ValueError):
                self._drop()
                return None
        finally:
            self._lock.release()
        ms = (time.perf_counter() - started) * 1000.0
        self.stats["ok"] += 1
        self.stats["last_ms"] = round(ms, 2)
        self.stats["ms_max"] = max(self.stats["ms_max"], round(ms, 2))
        return reply

    def now(self, wait_s: float = 0.05) -> dict[str, Any] | None:
        """The board (:meth:`NowBoard.snapshot`), or ``None`` without an answer within ``wait_s``."""
        reply = self._ask({"op": "now"}, wait_s)
        return reply.get("board") if reply and reply.get("ok") else None

    def refresh(self, hwnd: int | None = None, wait_s: float = 0.05) -> bool:
        """Ask the watcher to read ``hwnd`` (default: the window in front) again now; returns at once."""
        reply = self._ask({"op": "refresh", "hwnd": int(hwnd) if hwnd else None}, wait_s)
        return bool(reply and reply.get("queued"))

    def server_stats(self, wait_s: float = 0.2) -> dict[str, Any] | None:
        reply = self._ask({"op": "stats"}, wait_s)
        return reply if reply and reply.get("ok") else None

    def close(self) -> None:
        with self._lock:
            self._drop()


_clients: dict[str, NowClient | None] = {}
_clients_lock = threading.Lock()


def shared_client(db_path: str | Path | None = None) -> NowClient | None:
    """One :class:`NowClient` per database for this process, or ``None`` when memory has no key yet.

    Reads memory's DPAPI-protected key once (it never creates one: a missing key
    means no memory service has ever run for this database).
    """
    from yuki.memory.store import KEY_FILENAME, default_db_path

    db = Path(db_path) if db_path is not None else default_db_path()
    key = str(db.resolve()).lower()
    with _clients_lock:
        if key in _clients:
            return _clients[key]
        client: NowClient | None = None
        key_path = db.parent / KEY_FILENAME
        if key_path.exists():
            try:
                from yuki.memory.crypto import FieldCipher, load_or_create_key

                client = NowClient(FieldCipher(load_or_create_key(key_path)), db)
            except Exception:  # noqa: BLE001 - no Now then; everything else goes on
                client = None
        if client is not None:  # no key yet (the service has never run): asked again next time
            _clients[key] = client
        return client


@dataclass
class NowTiming:
    """Content-free numbers about one Now read, for the tray's logs."""

    ok: bool
    ms: float
    windows: int
    popups: int
    front_age_s: float | None
    front_stale: bool


def timing_of(board: dict[str, Any] | None, ms: float) -> NowTiming:
    windows = list((board or {}).get("windows") or [])
    fg = (board or {}).get("foreground")
    front = next((w for w in windows if w.get("hwnd") == fg), None)
    return NowTiming(ok=board is not None, ms=round(ms, 2), windows=len(windows),
                     popups=len((board or {}).get("popups") or []),
                     front_age_s=front.get("age_s") if front else None,
                     front_stale=bool(front.get("stale")) if front else False)


__all__ = ["APP_DATA_READERS", "NowBoard", "NowClient", "NowServer", "NowTiming", "app_data_for", "format_now",
           "gist_of", "pipe_name", "shared_client", "timing_of"]
