"""The "Yuki is acting" marker: what Yuki itself is doing on the desktop, for memory.

Contract: ``docs/MEMORY.md`` -> "Yuki's own actions".

While the agent carries out a request with its hands (it launched, clicked,
typed, scrolled, opened a URL...), what appears on screen is Yuki's doing, not
the user's browsing: a page Yuki opened at the user's request says nothing
about the user's interests.  The agent publishes a marker while such a request
runs; the memory service (watcher and timeline) tags what it captures during
that window ``by_yuki`` with the request's text, and the journal, episodes and
portrait treat it as "Yuki, at the user's request '...', opened ..." rather
than as the user's own activity.

Mechanism (plumbing only, no content decisions):

* a small JSON file next to the memory database (``acting.json``: the
  requests in progress, each with its text, start time, lane and the agent's
  process id), replaced atomically;
* a named auto-reset event ``Local\\YukiMemoryActing-<db digest>`` created by the
  service, set by the agent after every change so the service reacts within
  milliseconds (the service also re-reads the file on a bounded wait, so a
  missed signal or an agent that died mid-request is noticed: entries whose
  process is gone are ignored).

This module has no heavy imports: the agent side imports it on every request.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ACTING_FILE = "acting.json"
ACTING_EVENT_PREFIX = "Local\\YukiMemoryActing"
DB_FILENAME = "memory.db"

_lock = threading.Lock()


def _default_db_path() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "memory" / DB_FILENAME


def acting_path(db_path: str | Path | None) -> Path:
    """The marker file for the database at ``db_path`` (default location if ``None``)."""
    db = Path(db_path) if db_path is not None else _default_db_path()
    return db.parent / ACTING_FILE


def acting_event_name(db_path: str | Path | None) -> str:
    """Name of the event the agent sets after changing the marker (same digest as the service mutex)."""
    db = Path(db_path) if db_path is not None else _default_db_path()
    digest = hashlib.sha1(str(db.resolve()).lower().encode()).hexdigest()[:12]
    return f"{ACTING_EVENT_PREFIX}-{digest}"


@dataclass(frozen=True)
class Acting:
    """One request Yuki is carrying out with its hands."""

    token: str
    request: str
    started_at: float
    lane: str = ""
    pid: int = 0


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenProcess.restype = ctypes.c_void_p
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            ok = kernel32.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


def _read_raw(path: Path) -> list[dict[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = data.get("requests") if isinstance(data, dict) else None
    return [i for i in (items or []) if isinstance(i, dict) and i.get("token")]


def _write_raw(path: Path, items: list[dict[str, Any]]) -> None:
    if not items:
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"requests": items, "updated_at": time.time()}, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def _signal(db_path: str | Path | None) -> bool:
    """Set the service's event (microseconds); False when no service holds it."""
    try:
        kernel32 = ctypes.windll.kernel32
        kernel32.OpenEventW.restype = ctypes.c_void_p
        kernel32.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        handle = kernel32.OpenEventW(0x0002, False, acting_event_name(db_path))  # EVENT_MODIFY_STATE
        if not handle:
            return False
        try:
            return bool(kernel32.SetEvent(ctypes.c_void_p(handle)))
        finally:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
    except Exception:
        return False


def begin(db_path: str | Path | None, token: str, request: str, *, lane: str = "") -> bool:
    """Publish that Yuki is acting on ``request`` (idempotent per token). Never raises."""
    try:
        path = acting_path(db_path)
        with _lock:
            items = [i for i in _read_raw(path) if i.get("token") != token and _pid_alive(int(i.get("pid") or 0))]
            items.append({"token": token, "request": request, "started_at": time.time(), "lane": lane,
                          "pid": os.getpid()})
            _write_raw(path, items)
        _signal(db_path)
        return True
    except Exception:
        return False


def end(db_path: str | Path | None, token: str) -> bool:
    """Withdraw the marker for ``token``. Never raises."""
    try:
        path = acting_path(db_path)
        with _lock:
            before = _read_raw(path)
            items = [i for i in before if i.get("token") != token and _pid_alive(int(i.get("pid") or 0))]
            if len(items) != len(before) or not items:
                _write_raw(path, items)
        _signal(db_path)
        return True
    except Exception:
        return False


def current(db_path: str | Path | None) -> Acting | None:
    """The request Yuki is acting on now (the newest if several), or ``None``.

    Entries whose process has exited are ignored (an agent that died
    mid-request never leaves the marker standing).
    """
    live = []
    for i in _read_raw(acting_path(db_path)):
        try:
            pid = int(i.get("pid") or 0)
            if not _pid_alive(pid):
                continue
            live.append(Acting(token=str(i["token"]), request=str(i.get("request") or ""),
                               started_at=float(i.get("started_at") or 0.0), lane=str(i.get("lane") or ""),
                               pid=pid))
        except (TypeError, ValueError):
            continue
    return max(live, key=lambda a: a.started_at) if live else None


class ActingEvent:
    """The service's end of the signal: the named auto-reset event, created (and held) here."""

    def __init__(self, db_path: str | Path | None) -> None:
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateEventW.restype = ctypes.c_void_p
        kernel32.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._handle = kernel32.CreateEventW(None, False, False, acting_event_name(db_path))
        #: Without the named event (creation failed) the waits still end on set() or the timeout.
        self._fallback = threading.Event()

    def wait(self, timeout_s: float) -> bool:
        """True when signalled within ``timeout_s``."""
        if not self._handle:
            hit = self._fallback.wait(timeout_s)
            self._fallback.clear()
            return hit
        result = ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(self._handle), int(timeout_s * 1000))
        return result == 0

    def set(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.SetEvent(ctypes.c_void_p(self._handle))
        else:
            self._fallback.set()

    def close(self) -> None:
        if self._handle:
            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._handle))
            self._handle = None


__all__ = ["ACTING_FILE", "Acting", "ActingEvent", "acting_event_name", "acting_path", "begin", "current", "end"]
