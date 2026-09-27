"""One embedder for the whole PC: the memory service serves it, Yuki's tray asks it (plumbing).

The local embedder (:mod:`yuki.memory.embed`) costs ~550 MB loaded and 1-2 s to
load. The ``yuki-memory`` service already has it loaded while the user is
active (the journal, episodes and conversations embed with it), so the tray
process (recall, know-how, the voice grounding lookup) asks the service over a
small local named pipe instead of loading a second copy (measured 2026-09-25:
a know-how lookup loaded it cold in the tray, grew the tray by ~570 MB and
held a brain request 2.3-3.9 s).

* :class:`EmbedServer` runs in the service: one accept thread plus one short
  thread per connection, each request answered from the service's process-wide
  :func:`~yuki.memory.embed.get_embedder`.
* :class:`EmbedClient` is what :class:`~yuki.memory.api.MemoryClient` embeds
  with: the service's embedder when the pipe answers; otherwise (the service is
  down, or an older one without the pipe) the local embedder, loaded lazily in
  the background. Every call takes a wait bound, and a caller that cannot wait
  (the know-how lookup on a request's path) gets ``None`` instead of waiting
  for a load, so it goes on without.

Both ends embed with the model the database's vectors are made with
(:func:`yuki.memory.embed.active_model`, read from the same database file, so they
agree across a re-embed's flip); every reply names the model its vectors came from,
and the client tags what it stores with it.

Security: the pipe is local only (``PIPE_REJECT_REMOTE_CLIENTS``), its DACL
grants only this Windows user, and both ends prove they hold a key derived
from memory's DPAPI-protected data key (``multiprocessing.connection``'s HMAC
challenge in both directions) before anything else crosses. Requests are
JSON; replies are a JSON header line and raw float32 vectors (no pickle).
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable

import numpy as np

#: The wait a caller gets when it passes none (tools, rule saving: a vector matters there).
DEFAULT_WAIT_S = 10.0
#: How long connecting to the pipe (and the handshake) may take before the local path is used.
CONNECT_WAIT_S = 0.5
#: After the pipe could not be reached, how long before it is tried again (the service may come up).
RETRY_AFTER_S = 5.0
_BUF = 1 << 16
_DIM_FALLBACK = 384


def pipe_name(db_path: str | Path | None) -> str:
    """The pipe for the database at ``db_path``: per database and per Windows user."""
    from yuki.memory.store import default_db_path

    db = Path(db_path) if db_path is not None else default_db_path()
    user = os.environ.get("USERNAME", "")
    digest = hashlib.sha1(f"{user}|{str(db.resolve()).lower()}".encode()).hexdigest()[:16]
    return rf"\\.\pipe\YukiEmbed-{digest}"


def auth_key(cipher: Any) -> bytes:
    """The pipe's shared secret, derived from memory's data key (only this user can unwrap it)."""
    return bytes.fromhex(cipher.digest("yuki-embed-ipc-v1"))


# ---------------------------------------------------------------------------
# The service side
# ---------------------------------------------------------------------------


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


class EmbedServer:
    """Serves the process's embedder on the database's pipe (run inside ``yuki-memory``).

    Args:
        cipher: The store's :class:`~yuki.memory.crypto.FieldCipher` (for the shared secret).
        db_path: The database the pipe is named for.
        embedder: Returns the embedder (default: the process-wide one).
        log: ``log(type, **fields)`` (content-free), or ``None``.
    """

    def __init__(self, cipher: Any, db_path: str | Path | None, *,
                 embedder: Callable[[], Any] | None = None, log: Callable[..., None] | None = None) -> None:
        self.address = pipe_name(db_path)
        self._key = auth_key(cipher)
        self._embedder = embedder or _process_embedder
        self._log = log
        if embedder is None:
            _follow_database(db_path)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: Any = None
        self.stats = {"connections": 0, "requests": 0, "texts": 0, "refused": 0, "errors": 0}

    def start(self) -> "EmbedServer":
        self._listener = _listener_class()(self.address)
        self._thread = threading.Thread(target=self._accept_loop, name="yuki-memory-embed-ipc", daemon=True)
        self._thread.start()
        self._event("embed_ipc", state="listening")
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
        try:
            if self._listener is not None:
                self._listener.close()
        except Exception:  # noqa: BLE001
            pass
        self._event("embed_ipc", state="stopped", **self.stats)

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
                self._event("embed_ipc", state="accept_error", error=f"{type(exc).__name__}: {exc}")
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

            threading.Thread(target=handshake_and_serve, name="yuki-memory-embed-conn", daemon=True).start()

    def _serve(self, conn: Any) -> None:
        try:
            while not self._stop.is_set():
                try:
                    raw = conn.recv_bytes(8 << 20)
                except (EOFError, OSError):
                    return
                try:
                    request = json.loads(raw.decode("utf-8"))
                    reply, body = self._answer(request)
                except Exception as exc:  # noqa: BLE001 - one bad request must not end the connection
                    self.stats["errors"] += 1
                    reply, body = {"id": None, "ok": False, "error": f"{type(exc).__name__}"}, b""
                try:
                    conn.send_bytes(json.dumps(reply).encode("utf-8") + b"\n" + body)
                except (EOFError, OSError):
                    return  # the client gave up waiting and closed: fine
        finally:
            conn.close()

    def _answer(self, request: dict[str, Any]) -> tuple[dict[str, Any], bytes]:
        op = request.get("op")
        rid = request.get("id")
        embedder = self._embedder()
        if op == "embed":
            texts = [str(t) for t in (request.get("texts") or [])][:256]
            started = time.perf_counter()
            vecs = np.asarray(embedder.embed(texts), dtype="<f4")
            self.stats["requests"] += 1
            self.stats["texts"] += len(texts)
            return ({"id": rid, "ok": True, "shape": list(vecs.shape), "model": embedder.model_name,
                     "ms": round((time.perf_counter() - started) * 1000, 1)}, vecs.tobytes())
        if op == "warm":
            loaded = bool(embedder.stats().get("loaded"))
            if not loaded:  # load it now, in the background: the caller does not wait for it
                threading.Thread(target=lambda: embedder.embed([""]), name="yuki-memory-embed-warm",
                                 daemon=True).start()
            return {"id": rid, "ok": True, "loaded": loaded}, b""
        if op == "stats":
            return {"id": rid, "ok": True, **embedder.stats()}, b""
        return {"id": rid, "ok": False, "error": "unknown op"}, b""


def _process_embedder() -> Any:
    from yuki.memory.embed import get_embedder

    return get_embedder()


def _follow_database(db_path: str | Path | None) -> None:
    """The process-wide embedder embeds with the model this database's vectors are made with.

    The server and every client read it from the same database (``embed_state.active_model``),
    so they agree; a vector served over the pipe also says its model (``model`` in the reply).
    """
    if db_path is None:
        return
    try:
        from yuki.memory.embed import use_database

        use_database(db_path)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Yuki's side
# ---------------------------------------------------------------------------


class EmbedClient:
    """Embeddings for :class:`~yuki.memory.api.MemoryClient`: the service's embedder, else a local one.

    Thread-safe. ``embed(texts, wait_s)`` never waits longer than ``wait_s``
    (plus a connect bound the first time); it returns ``None`` when no vector
    could be had in time, and the caller goes on without one.

    Args:
        cipher: The store's cipher (the pipe's shared secret).
        db_path: The database whose service to ask.
        local: Returns the local embedder for the fallback (default: the process-wide one).
    """

    def __init__(self, cipher: Any, db_path: str | Path | None, *,
                 local: Callable[[], Any] | None = None) -> None:
        self.address = pipe_name(db_path)
        self._key = auth_key(cipher)
        self._local_factory = local or _process_embedder
        if local is None:
            _follow_database(db_path)
        self._conn: Any = None
        self._lock = threading.Lock()
        self._ids = 0
        self._next_try = 0.0
        self._local_loading: threading.Thread | None = None
        #: Whether the last connection attempt found no service at all (then the local embedder is used).
        self.absent = False
        #: The model the last vector came from (for the stored rows).
        self.model_name: str | None = None
        #: ``remote`` / ``local`` / ``none``: where the last call's vector came from; and counters.
        self.last_source = "none"
        self.stats = {"remote": 0, "local": 0, "skipped": 0, "remote_ms_max": 0.0}

    # -- remote ---------------------------------------------------------------

    def _connect(self, wait_s: float = CONNECT_WAIT_S) -> Any:
        """A connection to the service's pipe; ``None`` when there is none now.

        :attr:`absent` says why: ``True`` when no service serves the pipe (the local
        embedder is then the fallback), ``False`` when it is there but busy (a
        slow handshake while it loads its model): the caller then goes without
        rather than loading a second copy here.
        """
        if self._conn is not None:
            return self._conn
        if time.monotonic() < self._next_try:
            return None
        import _winapi
        from multiprocessing import AuthenticationError, connection

        try:
            started = time.monotonic()
            _winapi.WaitNamedPipe(self.address, max(1, int(wait_s * 1000)))
            handle = _winapi.CreateFile(self.address, _winapi.GENERIC_READ | _winapi.GENERIC_WRITE, 0,
                                        _winapi.NULL, _winapi.OPEN_EXISTING, _winapi.FILE_FLAG_OVERLAPPED,
                                        _winapi.NULL)
            _winapi.SetNamedPipeHandleState(handle, _winapi.PIPE_READMODE_MESSAGE, None, None)
            conn = connection.PipeConnection(handle)
        except OSError as exc:
            # ERROR_FILE_NOT_FOUND: no service (or an older one without the pipe). Anything else
            # (all instances busy, a wait that timed out): it is there, just not free this instant.
            self.absent = getattr(exc, "winerror", None) == 2
            self._next_try = time.monotonic() + (RETRY_AFTER_S if self.absent else 0.0)
            return None
        try:
            if not conn.poll(max(0.02, wait_s - (time.monotonic() - started))):  # the server speaks first
                raise TimeoutError("no challenge yet")
            connection.answer_challenge(conn, self._key)
            connection.deliver_challenge(conn, self._key)
        except AuthenticationError:
            conn.close()  # not our service (another key): treat as absent for a while
            self.absent = True
            self._next_try = time.monotonic() + RETRY_AFTER_S
            return None
        except Exception:  # noqa: BLE001 - there, but too slow to answer right now
            conn.close()
            self.absent = False
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

    def _ask(self, request: dict[str, Any], wait_s: float) -> tuple[dict[str, Any], bytes] | None:
        """One request/reply over the pipe; ``None`` when it cannot be had within ``wait_s``."""
        deadline = time.monotonic() + wait_s
        with self._lock:
            conn = self._connect(min(CONNECT_WAIT_S, max(0.05, wait_s)))
            if conn is None:
                return None
            self._ids += 1
            request = {**request, "id": self._ids}
            try:
                conn.send_bytes(json.dumps(request).encode("utf-8"))
                if not conn.poll(max(0.0, deadline - time.monotonic())):
                    self._drop()  # the reply may still come: this connection is no longer in step
                    return ("timeout", b"")  # type: ignore[return-value]
                raw = conn.recv_bytes()
            except (EOFError, OSError):
                self._drop()
                return None
        head, _, body = raw.partition(b"\n")
        reply = json.loads(head.decode("utf-8"))
        if reply.get("id") != request["id"]:
            self._drop()
            return None
        return reply, body

    def service_available(self) -> bool:
        """Whether the service's embedder answers on the pipe (connects if needed)."""
        with self._lock:
            return self._connect(CONNECT_WAIT_S) is not None

    def warm(self, *, allow_local: bool = True) -> str:
        """Ask for the embedder to be loaded ahead of use, without waiting. Returns where: remote/local/none.

        ``allow_local=False``: only the service's; with no service nothing loads here.
        """
        answer = self._ask({"op": "warm"}, CONNECT_WAIT_S)
        if isinstance(answer, tuple) and answer[0] != "timeout":
            return "remote"
        if answer is None and self.absent:
            if not allow_local:
                return "none"
            self._start_local_load()
            return "local"
        return "none"

    # -- local fallback ---------------------------------------------------------

    def _start_local_load(self) -> None:
        local = self._local_factory()
        if local.stats().get("loaded"):
            return
        with self._lock:
            if self._local_loading is not None and self._local_loading.is_alive():
                return
            self._local_loading = threading.Thread(target=lambda: local.embed([""]), name="yuki-embed-load",
                                                   daemon=True)
            self._local_loading.start()

    def _local(self, texts: list[str], wait_s: float) -> np.ndarray | None:
        local = self._local_factory()
        if not local.stats().get("loaded"):
            self._start_local_load()
            loader = self._local_loading
            if loader is not None:
                loader.join(max(0.0, wait_s))
            if not local.stats().get("loaded"):
                return None
        try:
            vecs = local.embed(texts)
        except Exception:  # noqa: BLE001
            return None
        self.model_name = local.model_name
        return vecs

    # -- the call ---------------------------------------------------------------

    def embed(self, texts: list[str], wait_s: float | None = None) -> np.ndarray | None:
        """``(n, DIM)`` float32 L2-normalised vectors for ``texts``, or ``None`` if not within ``wait_s``."""
        wait = DEFAULT_WAIT_S if wait_s is None else float(wait_s)
        started = time.perf_counter()
        answer = self._ask({"op": "embed", "texts": list(texts)}, wait)
        if isinstance(answer, tuple) and answer[0] == "timeout":
            self.last_source = "none"
            self.stats["skipped"] += 1
            return None
        if answer is not None:
            reply, body = answer
            if reply.get("ok"):
                shape = tuple(reply.get("shape") or (len(texts), _DIM_FALLBACK))
                vecs = np.frombuffer(body, dtype="<f4").reshape(shape).astype(np.float32)
                self.model_name = reply.get("model") or self.model_name
                self.last_source = "remote"
                self.stats["remote"] += 1
                self.stats["remote_ms_max"] = max(self.stats["remote_ms_max"],
                                                  round((time.perf_counter() - started) * 1000, 1))
                return vecs
        if not self.absent:
            # The service is there but could not answer in time (it is loading its model):
            # go without this once rather than load a second copy in this process.
            self.last_source = "none"
            self.stats["skipped"] += 1
            return None
        remaining = wait - (time.perf_counter() - started)
        vecs = self._local(list(texts), remaining)
        self.last_source = "local" if vecs is not None else "none"
        self.stats["local" if vecs is not None else "skipped"] += 1
        return vecs

    def close(self) -> None:
        with self._lock:
            self._drop()
