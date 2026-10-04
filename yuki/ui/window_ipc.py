"""The Yuki window's line to yuki-ui: a local, per-user, key-authenticated named pipe (plumbing).

The window (``pythonw -m yuki.ui.window``) is its own short-lived process, so it
costs nothing while closed. Memory it reads and writes straight through
:class:`~yuki.memory.api.MemoryClient`; what only the running tray has (the
agent runtime for chat, the voice session's devices and wake word, the model
and effort, the live chat thread) it reaches over this pipe.

Same pattern as :mod:`yuki.memory.embed_ipc`: the pipe is local only
(``PIPE_REJECT_REMOTE_CLIENTS``), its DACL grants only this Windows user, and
both ends prove they hold a key derived from memory's DPAPI-protected data key
(``multiprocessing.connection``'s HMAC challenge in both directions) before
anything else crosses. Every message is one JSON object (no pickle):

* request, window -> tray: ``{"op": str, "id": int, ...args}``
* reply, tray -> window: ``{"re": id, "ok": bool, ...}`` (``error`` when not ok)
* event, tray -> window, unasked: ``{"ev": str, ...}`` (the chat thread's events,
  live state such as the voice pill's state, "focus": bring the window forward)

:class:`WindowServer` runs in yuki-ui (one accept thread; per window a reader
and a writer thread, so the GUI never blocks on a slow window).
:class:`WindowClient` runs in the window (one reader thread; requests wait
for their reply with a bound).
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import threading
import time
from pathlib import Path
from typing import Any, Callable

#: Protocol version, sent in ``hello`` (a window from another version still gets its reply). 2: the Lattice
#: window's ops (Phase E5), listed in :data:`OPS` and sent in ``hello`` so a window can tell what the tray serves.
VERSION = 2

#: Every request op the tray serves (:meth:`yuki.ui.window_host.WindowHost._handle`). Version 1's, then the E5
#: ones: ``stop(job)``, ``steer(job, text)``, ``now``, ``plan(card)``, ``card_feedback(card, kind)``,
#: ``activity(filter)``, ``threads``, ``people``, ``workflows``, ``workflow_edit``, ``permissions_get``,
#: ``permissions_set(kind, level)``, ``forget(object)``; then the HTML window's (E3/E4): ``offer(card)`` and
#: ``offer_undo(card)`` (an offer's button and Undo, as the corner card's), ``askbar(text)`` (the ask bar with text
#: in it, unsent); then the Work page's ``start(text, shown)`` (a click's request, always a new one: never an answer
#: or a paused request's words). Unasked events the tray may send: ``snapshot``,
#: ``chat``, ``state``, ``focus`` and (E5) ``permissions`` (the policy changed).
OPS: tuple[str, ...] = (
    "hello", "state", "submit", "nudge_do", "cancel", "voice_set", "voice_toggle", "devices", "model", "effort",
    "ui_settings", "ensure_memory", "open_logs", "snooze",
    "stop", "steer", "now", "plan", "card_feedback", "activity", "threads", "people", "workflows",
    "workflow_edit", "permissions_get", "permissions_set", "forget",
    "offer", "offer_undo", "askbar", "start",
)
_BUF = 1 << 16
#: Largest message either side accepts.
MAX_MESSAGE = 16 << 20
CONNECT_WAIT_S = 1.0
REQUEST_WAIT_S = 6.0


def pipe_name(db_path: str | Path | None) -> str:
    """The pipe for the memory at ``db_path``: per database and per Windows user."""
    if db_path is None:   # imported only when needed: the store is a heavy import the HTML window avoids
        from yuki.memory.store import default_db_path

        db_path = default_db_path()
    db = Path(db_path)
    user = os.environ.get("USERNAME", "")
    digest = hashlib.sha1(f"{user}|{str(db.resolve()).lower()}".encode()).hexdigest()[:16]
    return rf"\\.\pipe\YukiWindow-{digest}"


def auth_key(cipher: Any) -> bytes:
    """The pipe's shared secret, derived from memory's data key (only this user can unwrap it)."""
    return bytes.fromhex(cipher.digest("yuki-window-ipc-v1"))


def _encode(msg: dict[str, Any]) -> bytes:
    return json.dumps(msg, ensure_ascii=False, default=str).encode("utf-8")


# -- the tray side --------------------------------------------------------------------------------


def _user_only_security() -> Any:
    """SECURITY_ATTRIBUTES whose DACL grants full access to the current user and no one else."""
    import ntsecuritycon
    import pywintypes
    import win32api
    import win32con
    import win32security

    token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
    sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
    dacl = win32security.ACL()
    dacl.AddAccessAllowedAce(win32security.ACL_REVISION, ntsecuritycon.FILE_ALL_ACCESS, sid)
    sd = win32security.SECURITY_DESCRIPTOR()
    sd.SetSecurityDescriptorDacl(1, dacl, 0)
    sa = pywintypes.SECURITY_ATTRIBUTES()
    sa.SECURITY_DESCRIPTOR = sd
    return sa


def _listener_class() -> type:
    from multiprocessing import connection

    class _UserPipeListener(connection.PipeListener):  # type: ignore[name-defined, misc]
        """``multiprocessing``'s pipe listener with a user-only DACL and remote clients refused."""

        def _new_handle(self, first: bool = False) -> int:
            import win32file
            import win32pipe

            flags = win32pipe.PIPE_ACCESS_DUPLEX | win32file.FILE_FLAG_OVERLAPPED
            if first:
                flags |= 0x00080000  # FILE_FLAG_FIRST_PIPE_INSTANCE: never share a name someone else holds
            handle = win32pipe.CreateNamedPipe(
                self._address, flags,
                win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_WAIT
                | win32pipe.PIPE_REJECT_REMOTE_CLIENTS,
                win32pipe.PIPE_UNLIMITED_INSTANCES, _BUF, _BUF, 0, _user_only_security(),
            )
            return int(handle.Detach())

    return _UserPipeListener


class _Peer:
    """One connected window: its connection and its outgoing queue (drained by its writer thread)."""

    def __init__(self, conn: Any, n: int) -> None:
        self.conn = conn
        self.n = n
        self.out: queue.Queue[bytes | None] = queue.Queue()
        self.alive = True


class WindowServer:
    """Serves windows on the pipe (runs inside yuki-ui).

    Args:
        key: The shared secret (:func:`auth_key`).
        address: The pipe (:func:`pipe_name`).
        handler: ``handler(request) -> reply fields``; runs on the window's reader
            thread (the host moves GUI work to the GUI thread itself). An exception
            becomes ``{"ok": false, "error": ...}``.
        on_connect: Called with the peer number when a window has proved its key.
        log: ``log(kind, **fields)`` (content-free), or ``None``.
    """

    def __init__(self, key: bytes, address: str, handler: Callable[[dict[str, Any]], dict[str, Any]], *,
                 on_connect: Callable[[int], None] | None = None,
                 log: Callable[..., None] | None = None) -> None:
        self.address = address
        self._key = key
        self._handler = handler
        self._on_connect = on_connect
        self._log = log
        self._stop = threading.Event()
        self._peers: dict[int, _Peer] = {}
        self._lock = threading.Lock()
        self._n = 0
        self._listener: Any = None
        self._thread: threading.Thread | None = None
        self.stats = {"connections": 0, "requests": 0, "events": 0, "refused": 0, "errors": 0}

    def start(self) -> "WindowServer":
        self._listener = _listener_class()(self.address)
        self._thread = threading.Thread(target=self._accept_loop, name="yuki-window-ipc", daemon=True)
        self._thread.start()
        self._event("window_ipc", state="listening")
        return self

    def stop(self) -> None:
        self._stop.set()
        try:  # wake the blocking accept with one connection of our own
            from multiprocessing import connection

            connection.PipeClient(self.address).close()
        except Exception:  # noqa: BLE001
            pass
        if self._thread is not None:
            self._thread.join(2.0)
        with self._lock:
            peers = list(self._peers.values())
            self._peers.clear()
        for peer in peers:
            peer.alive = False
            peer.out.put(None)
            try:
                peer.conn.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._listener is not None:
                self._listener.close()
        except Exception:  # noqa: BLE001
            pass
        self._event("window_ipc", state="stopped", **self.stats)

    @property
    def clients(self) -> int:
        with self._lock:
            return len(self._peers)

    def broadcast(self, event: dict[str, Any]) -> int:
        """Queue ``event`` (``{"ev": ...}``) for every connected window; returns how many. Thread-safe."""
        with self._lock:
            peers = list(self._peers.values())
        if not peers:
            return 0
        data = _encode(event)
        for peer in peers:
            peer.out.put(data)
        self.stats["events"] += 1
        return len(peers)

    def send_to(self, peer_n: int, event: dict[str, Any]) -> bool:
        with self._lock:
            peer = self._peers.get(peer_n)
        if peer is None:
            return False
        peer.out.put(_encode(event))
        return True

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
            except Exception as exc:  # noqa: BLE001 - keep serving
                if self._stop.is_set():
                    return
                self.stats["errors"] += 1
                self._event("window_ipc", state="accept_error", error=f"{type(exc).__name__}: {exc}")
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
                    self._event("window_ipc", state="refused")
                    conn.close()
                    return
                with self._lock:
                    self._n += 1
                    peer = _Peer(conn, self._n)
                    self._peers[peer.n] = peer
                self.stats["connections"] += 1
                self._event("window_ipc", state="connected", peer=peer.n)
                threading.Thread(target=self._writer, args=(peer,), name="yuki-window-ipc-out",
                                 daemon=True).start()
                if self._on_connect is not None:
                    try:
                        self._on_connect(peer.n)
                    except Exception:  # noqa: BLE001
                        pass
                self._reader(peer)

            threading.Thread(target=handshake_and_serve, name="yuki-window-ipc-conn", daemon=True).start()

    def _reader(self, peer: _Peer) -> None:
        try:
            while not self._stop.is_set() and peer.alive:
                try:
                    raw = peer.conn.recv_bytes(MAX_MESSAGE)
                except (EOFError, OSError):
                    return
                try:
                    request = json.loads(raw.decode("utf-8"))
                    if not isinstance(request, dict):
                        raise ValueError("not an object")
                except ValueError:
                    self.stats["errors"] += 1
                    continue
                rid = request.get("id")
                request["_peer"] = peer.n
                try:
                    fields = self._handler(request) or {}
                    reply = {"re": rid, "ok": bool(fields.pop("ok", True)), **fields}
                except Exception as exc:  # noqa: BLE001 - one bad request must not end the connection
                    self.stats["errors"] += 1
                    reply = {"re": rid, "ok": False, "error": f"{type(exc).__name__}: {exc}"}
                self.stats["requests"] += 1
                peer.out.put(_encode(reply))
        finally:
            peer.alive = False
            peer.out.put(None)
            with self._lock:
                self._peers.pop(peer.n, None)
            try:
                peer.conn.close()
            except Exception:  # noqa: BLE001
                pass
            self._event("window_ipc", state="disconnected", peer=peer.n)

    def _writer(self, peer: _Peer) -> None:
        while True:
            data = peer.out.get()
            if data is None or not peer.alive:
                return
            try:
                peer.conn.send_bytes(data)
            except (EOFError, OSError):
                peer.alive = False
                return


# -- the window side ------------------------------------------------------------------------------


class WindowClient:
    """The window's end: requests with bounded waits, and a callback for the tray's events.

    Args:
        key: The shared secret (:func:`auth_key`).
        address: The pipe (:func:`pipe_name`).
        on_event: Called on the reader thread with each ``{"ev": ...}`` message
            (the window turns it into a queued Qt signal).
        on_closed: Called once when the connection drops.
    """

    def __init__(self, key: bytes, address: str, *, on_event: Callable[[dict[str, Any]], None] | None = None,
                 on_closed: Callable[[], None] | None = None) -> None:
        self.address = address
        self._key = key
        self._on_event = on_event
        self._on_closed = on_closed
        self._conn: Any = None
        self._send_lock = threading.Lock()
        self._ids = 0
        self._waiting: dict[int, tuple[threading.Event, dict[str, Any]]] = {}
        self._wlock = threading.Lock()
        self._reader_thread: threading.Thread | None = None

    @property
    def connected(self) -> bool:
        return self._conn is not None

    def connect(self, wait_s: float = CONNECT_WAIT_S) -> bool:
        """Connect and prove the key; ``False`` when yuki-ui is not serving (or not this user's)."""
        if self._conn is not None:
            return True
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
        except OSError:
            return False
        try:
            if not conn.poll(max(0.05, wait_s - (time.monotonic() - started))):   # the server speaks first
                raise TimeoutError("no challenge")
            connection.answer_challenge(conn, self._key)
            connection.deliver_challenge(conn, self._key)
        except (AuthenticationError, TimeoutError, EOFError, OSError):
            conn.close()
            return False
        self._conn = conn
        self._reader_thread = threading.Thread(target=self._reader, name="yuki-window-client", daemon=True)
        self._reader_thread.start()
        return True

    def request(self, op: str, wait_s: float = REQUEST_WAIT_S, **args: Any) -> dict[str, Any] | None:
        """Send ``op`` and wait for its reply (``None`` when not connected or no reply in time)."""
        conn = self._conn
        if conn is None:
            return None
        with self._wlock:
            self._ids += 1
            rid = self._ids
            slot: tuple[threading.Event, dict[str, Any]] = (threading.Event(), {})
            self._waiting[rid] = slot
        try:
            with self._send_lock:
                conn.send_bytes(_encode({"op": op, "id": rid, **args}))
        except (EOFError, OSError):
            self._closed()
            return None
        ok = slot[0].wait(wait_s)
        with self._wlock:
            self._waiting.pop(rid, None)
        return slot[1] if ok else None

    def send(self, op: str, **args: Any) -> bool:
        """Fire and forget (the reply, when it comes, is dropped)."""
        conn = self._conn
        if conn is None:
            return False
        with self._wlock:
            self._ids += 1
            rid = self._ids
        try:
            with self._send_lock:
                conn.send_bytes(_encode({"op": op, "id": rid, **args}))
            return True
        except (EOFError, OSError):
            self._closed()
            return False

    def _reader(self) -> None:
        conn = self._conn
        while conn is not None:
            try:
                raw = conn.recv_bytes(MAX_MESSAGE)
            except (EOFError, OSError):
                break
            try:
                msg = json.loads(raw.decode("utf-8"))
            except ValueError:
                continue
            if not isinstance(msg, dict):
                continue
            if "re" in msg:
                with self._wlock:
                    slot = self._waiting.get(msg["re"])
                if slot is not None:
                    slot[1].update(msg)
                    slot[0].set()
            elif "ev" in msg and self._on_event is not None:
                try:
                    self._on_event(msg)
                except Exception:  # noqa: BLE001 - a bad handler must not kill the reader
                    pass
        self._closed()

    def _closed(self) -> None:
        conn, self._conn = self._conn, None
        if conn is None:
            return
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
        with self._wlock:
            for ev, _box in self._waiting.values():
                ev.set()
        if self._on_closed is not None:
            try:
                self._on_closed()
            except Exception:  # noqa: BLE001
                pass

    def close(self) -> None:
        self._closed()
