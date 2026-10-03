"""The Yuki window as HTML in WebView2 (the Lattice design): its own short-lived process, 0 MB when closed.

``python -m yuki.ui.web_window`` (what a tray click starts unless ``ui.json`` says ``window_engine: "qt"``) hosts
``yuki/ui/web/index.html`` in a :class:`~yuki.ui.webview2.WebView2Host` and feeds it over the WebView2 message
channel. No localhost server, no host objects: the page asks with ``{op}`` messages and Python answers with
``{kind}`` messages (``yuki/ui/web/bridge.js``).

What happens when it opens (E4):

1. **Paint first from the snapshot.** The window's encrypted snapshot (``window-snapshot.bin``, memory's own key;
   the HTML window's pages in their own format, :data:`yuki.ui.window.snapshot.WEB_FORMAT`) is read on a thread with
   only the key (DPAPI) and the file: no memory store, no numpy. The page gets it in its ``boot`` and draws the last
   visit's pages at once.
2. **The pipe to the tray** (:mod:`yuki.ui.window_ipc`, the same key): the live chat thread (a
   :class:`~yuki.ui.chat_model.ChatModel` replica, sent to the page as item diffs), voice state, running requests,
   and every op only the tray can do (Stop, steer, an offer's button and Undo, the ask bar, autonomy).
3. **Memory**, opened on a worker thread (:class:`~yuki.memory.api.MemoryClient`, as the Qt window does): each page's
   data is built by :mod:`yuki.ui.web_data` (Home first, then the others one at a time) and replaces the snapshot's.
4. On close the pages are written back to the snapshot.

Nothing here reads what a message says to decide anything; every button is a closed action name with machine fields
(ids, kinds), turned into a memory call, a settings write or a pipe op.

Falls back to the Qt window (``yuki.ui.window``) by itself when the WebView2 runtime is missing or fails to start.
One window at a time, whichever engine: the same single-instance mutex and focus event as the Qt window.

Options for tests and measurements (all optional): ``--fixture`` (invented data, no memory, no pipe), ``--db``,
``--page``, ``--offscreen`` (a frameless tool window beyond every monitor that never activates), ``--profile``,
``--browser-args``, ``--query`` (preview knobs for the page, e.g. ``state=listening``), ``--assume-focus`` (animate
as if in front: the off-screen window never has focus), ``--t0``, ``--metrics``, ``--shot``, ``--exit-after``,
``--memory-low-after``.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from yuki.ui.webview2 import APP_HOST, FONT_HOST, HostOptions, WebView2Host


def _ui_dir() -> Path:
    """``yuki/ui``: next to this module in the source tree and in the PyInstaller folder alike."""
    here = Path(__file__).resolve().parent
    if (here / "web" / "index.html").exists():
        return here
    return Path(getattr(sys, "_MEIPASS", "") or here) / "yuki" / "ui"


UI_DIR = _ui_dir()
WEB_DIR = UI_DIR / "web"
FONTS_DIR = UI_DIR / "fonts"
FIXTURE = WEB_DIR / "fixtures" / "window.js"
FIXTURE_PREFIX = "window.YUKI_FIXTURE = "
START_URL = f"https://{APP_HOST}/index.html"
#: Chromium switches the window runs with unless ``--browser-args`` says otherwise. ``--disable-gpu`` won the E2
#: spike (25-build-E1-E2-E5.md §2): 134 MB private idle against 203 MB with the separate GPU process, the same first
#: paint, and the window never opens a device on the GPU a game is using.
DEFAULT_BROWSER_ARGS = "--disable-gpu"
#: Shared with the Qt window: one Yuki window at a time, whichever engine.
MUTEX_NAME = "Local\\YukiWindowSingleInstance"
FOCUS_EVENT = "Local\\YukiWindowFocus"
#: The pages, and the Qt window's page names (yuki.ui.own_window.PAGES: the agent's open_yuki tool) mapped to them.
PAGES = ("home", "work", "me", "playbook", "activity", "settings")
PAGE_ALIASES = {"today": "home", "status": "activity", "yuki": "playbook", "chat": "home", "day": "work"}
#: Home is read again this often while the window is open (the rest when shown, if older than STALE_S).
HOME_REFRESH_S = 60.0
STALE_S = 45.0
#: While yuki-ui is not answering, look for it again this often.
RECONNECT_S = 10.0
#: While a correction waits to be applied by memory, look this often (a read, no model).
PENDING_POLL_S = 15.0
#: The pipe's longest wait for one op.
OP_WAIT_S = 14.0
#: The user's words for a correction from Me, by its kind (memory's portrait fold decides what they change).
CORRECTION_WORDS = {"forget": "It's just wrong, forget it", "notmine": "That isn't about me. It's not mine.",
                    "confirm": "That's right"}


def default_profile() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Yuki" / "webview"


def load_fixture(path: Path = FIXTURE) -> dict[str, Any]:
    """The invented preview data (``fixtures/window.js``: ``window.YUKI_FIXTURE = {json};``, our own fixed format,
    written by ``scripts/make_web_fixture.py``)."""
    text = Path(path).read_text(encoding="utf-8")
    start = text.index(FIXTURE_PREFIX) + len(FIXTURE_PREFIX)
    data = json.loads(text[start:].rstrip().rstrip(";"))
    if not isinstance(data, dict) or data.get("schema") != "yuki-window/1":
        raise ValueError(f"{path} is not a yuki-window/1 fixture")
    return data


def appearance() -> dict[str, Any]:
    """The accent and motion the page follows (``ui.json``, read only)."""
    try:
        from yuki.ui import ui_settings

        s = ui_settings.load()
        return {"accent": s.accent, "reduce_motion": bool(s.reduce_motion)}
    except Exception:  # noqa: BLE001 - a broken settings file reads as defaults
        return {"accent": "green", "reduce_motion": False}


def page_name(page: str | None) -> tuple[str, bool]:
    """(the window's page for a requested name, whether to open the thread drawer): Qt names map to the new pages."""
    p = str(page or "").strip().casefold()
    return (p if p in PAGES else PAGE_ALIASES.get(p, "home")), p == "chat"


def default_db_path() -> Path:
    """Memory's database, as :func:`yuki.memory.store.default_db_path` says (``YUKI_MEMORY_DIR`` or
    ``%LOCALAPPDATA%\\Yuki\\memory``), without importing the store (numpy) before the first paint."""
    override = os.environ.get("YUKI_MEMORY_DIR")
    base = Path(override) if override else Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Yuki" / "memory"
    return base / "memory.db"


def cipher_for(db_path: Path) -> Any:
    """Memory's field cipher from its DPAPI-wrapped key next to the database (no store); ``None`` when there is no
    key yet (a fresh install: nothing to paint from, nothing to connect to)."""
    key_path = Path(db_path).parent / "memory.key"
    if not key_path.exists():
        return None
    from yuki.memory.crypto import FieldCipher, load_or_create_key

    return FieldCipher(load_or_create_key(key_path))


def set_clipboard(text: str) -> bool:
    """Put ``text`` on the clipboard (CF_UNICODETEXT). The page has no clipboard permission: WebView2 denies them all."""
    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    data = (text or "").encode("utf-16-le") + b"\x00\x00"
    for _ in range(5):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(data))   # GMEM_MOVEABLE
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        ctypes.memmove(ptr, data, len(data))
        kernel32.GlobalUnlock(handle)
        return bool(user32.SetClipboardData(13, handle))   # CF_UNICODETEXT; the clipboard owns it now
    finally:
        user32.CloseClipboard()


class WebWindow:
    """One window's run: the page's requests, memory, the pipe, the snapshot, the timings.

    Threads: :meth:`on_message` runs on the WebView2 (UI) thread and only hands work on; page data is built by one
    worker thread (one read at a time); each action runs on a short thread of its own; the pipe has its reader."""

    def __init__(self, *, db_path: Path | None = None, page: str = "home", thread: bool = False,
                 fixture: dict[str, Any] | None = None, connect: bool = True, ui_path: Path | None = None,
                 t0: float | None = None, metrics_path: Path | None = None, shot: Path | None = None,
                 exit_after: float | None = None, memory_low_after: float | None = None,
                 assume_focus: bool = False, policy_path: Path | None = None, shot_delay: float = 0.0) -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.page, self.thread_open = page, thread
        self.fixture = fixture
        self.want_connect = connect and fixture is None
        self.ui_path = ui_path
        self.policy_path = policy_path
        self.assume_focus = assume_focus
        self.host: WebView2Host | None = None
        self.cipher: Any = None
        self.memory: Any = None
        self.client: Any = None
        self.ops: set[str] = set()
        self.model: Any = None
        self.tray_state: dict[str, Any] = {}
        self.pages: dict[str, Any] = {}
        self.stamps: dict[str, float] = {}
        self.fresh: set[str] = set()          # pages built from memory this run (only these are saved)
        self.shell: dict[str, Any] = {"connected": False}
        self.ready = threading.Event()        # the page said ready
        self.booted = False
        self._snapshot_done = threading.Event()
        self._jobs: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._queued: set[str] = set()
        self._lock = threading.Lock()
        self._closing = threading.Event()
        self._runs_sig = ""
        self._live_t: threading.Timer | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.metrics: dict[str, Any] = {"t0": t0, "pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False)),
                                        "python": sys.version.split()[0], "fixture": fixture is not None}
        self.metrics_path, self.shot, self.exit_after, self.memory_low_after = metrics_path, shot, exit_after, memory_low_after
        self.shot_delay = shot_delay
        self.events: list[dict[str, Any]] = []
        self.marks: dict[str, float] = {}

    # -- the page ------------------------------------------------------------------------------------------

    def post(self, message: dict[str, Any]) -> None:
        if self.host is not None:
            self.host.post(message)

    def on_message(self, message: Any) -> None:
        """A message from the page (UI thread): never waits on memory or the pipe."""
        if not isinstance(message, dict):
            return
        op = message.get("op")
        if op == "ready":
            self.marks["page_ready"] = time.time()
            self.ready.set()
            threading.Thread(target=self._boot_when_ready, name="yuki-web-boot", daemon=True).start()
        elif op == "painted":
            self.on_painted(message)
        elif op == "want":
            page = str(message.get("page") or "")
            if page in PAGES or page == "day":
                self.refresh(page, stale_s=STALE_S)
        elif op == "act":
            threading.Thread(target=self._act, args=(message,), name="yuki-web-act", daemon=True).start()

    def _boot_when_ready(self) -> None:
        """The page is up: send it the snapshot (or the fixture), the live state and the thread."""
        if self.fixture is not None:
            from yuki.ui.web_data import shift_times

            f = self.fixture
            delta = time.time() - float(f.get("now") or time.time())
            self.booted = True
            self.post({"kind": "boot", "source": "fixture", "shell": shift_times(f["shell"], delta),
                       "pages": shift_times(f["pages"], delta), "chat": shift_times(f.get("chat") or {}, delta),
                       "appearance": {**appearance(), "assume_focus": self.assume_focus}, "page": self.page})
            return
        self._snapshot_done.wait(1.5)   # read in parallel with the browser's start; ~0.1 s
        with self._lock:
            pages = dict(self.pages)
            self.booted = True
        chat = self.chat_payload() if self.model is not None else None
        self.post({"kind": "boot", "source": "snapshot", "shell": self.shell, "pages": pages, "chat": chat,
                   "appearance": {**appearance(), "assume_focus": self.assume_focus}, "page": self.page})
        if self.thread_open:
            self.post({"kind": "focus", "page": self.page, "thread": True})
        self.marks["boot_sent"] = time.time()

    def send_page(self, name: str, data: Any, source: str = "memory") -> None:
        with self._lock:
            self.pages[name] = data
            self.stamps[name] = time.time()
            if source == "memory":
                self.fresh.add(name)
            booted = self.booted
        if booted:
            self.post({"kind": "data", "page": name, "data": data, "source": source})

    def post_live(self) -> None:
        if self.booted:
            self.post({"kind": "live", "shell": self.shell})

    # -- start ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Begin the background work (before the window exists: it all runs while the browser starts)."""
        if self.fixture is not None:
            self._snapshot_done.set()
            return
        threading.Thread(target=self._early, name="yuki-web-early", daemon=True).start()
        threading.Thread(target=self._worker, name="yuki-web-data", daemon=True).start()

    def _early(self) -> None:
        """Key -> snapshot -> pipe, then memory. Each step's failure leaves the window working with less."""
        try:
            self.cipher = cipher_for(self.db_path)
        except Exception as exc:  # noqa: BLE001
            self.event("web_cipher_failed", error=type(exc).__name__)
        if self.cipher is not None:
            try:
                from yuki.ui.window import snapshot

                views = snapshot.load(snapshot.path_for(self.ui_path), self.cipher, db=str(self.db_path),
                                      fmt=snapshot.WEB_FORMAT)
                with self._lock:
                    for k, v in views.items():
                        if k == "shell" and isinstance(v, dict):
                            self.shell.update({kk: vv for kk, vv in v.items() if kk in ("user", "memory_line")})
                        elif k not in self.pages:
                            self.pages[k] = v
                self.marks["snapshot"] = time.time()
            except Exception as exc:  # noqa: BLE001
                self.event("web_snapshot_failed", error=type(exc).__name__)
        self._snapshot_done.set()
        if self.want_connect and self.cipher is not None:
            self._connect()
        self._open_memory()

    def _open_memory(self) -> None:
        try:
            from yuki.memory.api import MemoryClient

            self.memory = MemoryClient.open(self.db_path)
            self.marks["memory"] = time.time()
        except Exception as exc:  # noqa: BLE001
            self.event("web_memory_failed", error=f"{type(exc).__name__}: {exc}")
            if self.ready.wait(5.0):
                self.post({"kind": "memory_error", "text": f"Couldn't open memory: {type(exc).__name__}"})
            return
        if self.cipher is None:   # a key made just now (fresh install): the pipe can be found now
            self.cipher = getattr(self.memory.store, "cipher", None)
            if self.want_connect and self.cipher is not None and self.client is None:
                threading.Thread(target=self._connect, daemon=True).start()
        self._load_pending()
        for name in ("shell", "home", self.page if self.page != "home" else "work", "me", "work", "activity",
                     "settings", "playbook", "day"):
            self.refresh(name)
        threading.Thread(target=self._timers, name="yuki-web-timers", daemon=True).start()

    # -- page data (one worker, one read at a time) -------------------------------------------------------------

    def refresh(self, name: str, stale_s: float | None = None) -> None:
        if self.memory is None and self.fixture is None:
            return
        if stale_s is not None and time.time() - self.stamps.get(name, 0.0) < stale_s and name in self.fresh:
            return
        with self._lock:
            if name in self._queued:
                return
            self._queued.add(name)
        self._jobs.put(("page", name))

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            _kind, name = job
            with self._lock:
                self._queued.discard(name)
            if self.memory is None or self._closing.is_set():
                continue
            t = time.time()
            try:
                data = self.build(name)
            except Exception as exc:  # noqa: BLE001 - one page failing never stops the others
                self.event("web_page_failed", page=name, error=f"{type(exc).__name__}: {exc}")
                continue
            self.event("web_page", page=name, ms=round((time.time() - t) * 1000))
            if name == "shell":
                self.shell.update(data)
                self.post_live()
                with self._lock:
                    self.pages["shell"] = {k: data.get(k) for k in ("user", "memory_line")}
                    self.fresh.add("shell")
                continue
            self.send_page(name, data)
            if name == "home" and "media" not in self.marks:
                self.marks["media"] = time.time()
                threading.Thread(target=self._media, name="yuki-web-media", daemon=True).start()

    def requests_csv(self) -> Path | None:
        try:
            from yuki.config import Settings

            return Settings().requests_csv_path
        except Exception:  # noqa: BLE001
            return None

    def build(self, name: str) -> Any:
        from yuki.ui import web_data as W

        m = self.memory
        if name == "shell":
            return W.build_shell(m)
        if name == "home":
            source, people = self._people()
            lately = [{"ref": p["ref"], "name": p["name"], "route": p.get("route") or "", "last_at": p.get("last_at"),
                       "count": p.get("count"), **({"rhythm": p["rhythm"]} if p.get("rhythm") else {})}
                      for p in people if source == "index" and p.get("last_at")]
            data = W.build_home(m, requests_csv=self.requests_csv(), people=lately)
            playing = (self.pages.get("home") or {}).get("now", {}).get("playing")
            if playing:
                data["now"]["playing"] = playing
            return data
        if name == "work":
            return W.build_work(m)
        if name == "day":
            return W.build_day(m)
        if name == "me":
            return W.build_me(m, pending=self.pending, people=self._people())
        if name == "playbook":
            data = W.build_playbook(m)
            if self.client is not None and "workflows" in self.ops:   # Phase C's procedures, once the tray has them
                reply = self.request("workflows", wait_s=4.0) or {}
                if reply.get("ok") and reply.get("available"):
                    data["procedures"] = list(reply.get("workflows") or [])
                    data["workflows_available"] = True
            return data
        if name == "activity":
            return W.build_activity(m, requests_csv=self.requests_csv())
        if name == "settings":
            live = {k: self.tray_state.get(k) for k in ("wake", "mic", "speaker") if k in self.tray_state}
            return W.build_settings(m, live=live, devices=self._devices(), policy_path=self.policy_path)
        raise ValueError(name)

    def _people(self) -> tuple[str, list[dict[str, Any]]]:
        """The people: the tray's ``people`` op (memory's people index) when the tray serves it, else the same index
        through this window's own memory client, else the portrait's person facts."""
        from yuki.ui import web_data as W

        if self.client is not None and "people" in self.ops:
            reply = self.request("people", wait_s=6.0, limit=60) or {}
            if reply.get("ok") and reply.get("available") and isinstance(reply.get("people"), list):
                return "index", [W.person_from_index(p) for p in reply["people"] if isinstance(p, dict) and p.get("name")]
        return W.people_now(self.memory)

    def _devices(self) -> dict[str, Any] | None:
        reply = self.request("devices", wait_s=3.0) if self.client is not None else None
        if reply and reply.get("ok") and isinstance(reply.get("devices"), dict):
            return reply["devices"]
        return None

    def _media(self) -> None:
        from yuki.ui.web_data import media_now

        playing = media_now()
        with self._lock:
            home = self.pages.get("home")
        if home is not None and playing != (home.get("now") or {}).get("playing"):
            home = dict(home)
            home["now"] = {**(home.get("now") or {}), "playing": playing}
            self.send_page("home", home)

    def _timers(self) -> None:
        """Home every minute; the pending corrections every 15 s while there are some; the pipe again while down."""
        last_home, last_pending, last_connect = time.time(), 0.0, time.time()
        while not self._closing.wait(5.0):
            now = time.time()
            if now - last_home >= HOME_REFRESH_S:
                last_home = now
                self.refresh("home")
                self._jobs.put(("page", "shell"))
            if self.pending and now - last_pending >= PENDING_POLL_S:
                last_pending = now
                self._check_pending()
            if self.want_connect and self.client is None and self.cipher is not None and now - last_connect >= RECONNECT_S:
                last_connect = now
                self._connect()

    # -- the pipe to the tray -------------------------------------------------------------------------------------

    def _connect(self) -> None:
        from yuki.ui.window_ipc import WindowClient, auth_key, pipe_name

        try:
            key, address = auth_key(self.cipher), pipe_name(self.db_path)
        except Exception as exc:  # noqa: BLE001
            self.event("web_pipe_failed", error=type(exc).__name__)
            return
        client = WindowClient(key, address, on_event=self._on_tray_event, on_closed=self._on_tray_closed)
        if not client.connect(1.5):
            if not self.shell.get("connected") is False:
                self.shell["connected"] = False
                self.post_live()
            return
        self.client = client
        self.shell["connected"] = True
        self.marks.setdefault("pipe", time.time())
        hello = client.request("hello") or {}
        self.ops = set(hello.get("ops") or [])
        self.post_live()

    def _on_tray_closed(self) -> None:
        self.client = None
        self.shell.update(connected=False, runs=[])
        self.post_live()

    def _on_tray_event(self, msg: dict[str, Any]) -> None:
        """An unasked message from the tray (the pipe's reader thread)."""
        from yuki.ui.chat_model import ChatModel

        kind = msg.get("ev")
        if kind == "snapshot":
            if self.model is None:
                self.model = ChatModel()
                self.model.subscribe(self._on_chat_change)
            snap = msg.get("snapshot") or {}
            if snap.get("items"):
                self.model.load_snapshot(snap)
            else:
                self._history()
            self.tray_state.update(msg.get("state") or {})
            self._voice_from_state()
            self._runs_changed(force=True)
            if self.booted:
                self.post({"kind": "chat", "reset": True, **self.chat_payload()})
        elif kind == "chat" and self.model is not None:
            ev = msg.get("event")
            if isinstance(ev, dict):
                self.model.apply(ev)
                if ev.get("type") in ("nudge", "nudge_state"):
                    self.refresh("home")
        elif kind == "state":
            self.tray_state.update({k: v for k, v in msg.items() if k != "ev"})
            self._voice_from_state()
            self.post_live()
        elif kind == "focus":
            page, thread = page_name(msg.get("page")) if msg.get("page") else (None, False)
            if self.host is not None:
                self.host.invoke(self.host.bring_forward)
            self.post({"kind": "focus", **({"page": page} if page else {}), **({"thread": True} if thread else {})})
            self.refresh("home", stale_s=10.0)
        elif kind == "permissions":
            self.post({"kind": "permissions", "permissions": msg.get("permissions")})

    def _voice_from_state(self) -> None:
        st = self.tray_state
        self.shell["voice"] = {"state": st.get("voice_state") or "off", "wake": bool(st.get("wake")),
                               "conversation": bool(st.get("conversation")), "quiet_until": st.get("quiet_until") or 0}

    def _on_chat_change(self, what: str, item: Any) -> None:
        if not self.booted:
            return
        from yuki.ui.web_data import chat_item

        if what == "reset" or item is None:
            self.post({"kind": "chat", "reset": True, **self.chat_payload()})
        else:
            self.post({"kind": "chat", "what": what, "item": chat_item(item)})
        self._runs_changed()

    def _runs_changed(self, force: bool = False) -> None:
        """The running requests changed (a start, a step, an end): tell the page, at most four times a second."""
        from yuki.ui.web_data import runs_of

        runs = runs_of(self.model) if self.model is not None else []
        sig = json.dumps([[r["item"], r["line"]] for r in runs])
        if sig == self._runs_sig and not force:
            return
        self._runs_sig = sig
        self.shell["runs"] = runs
        if self._live_t is None or not self._live_t.is_alive():
            self._live_t = threading.Timer(0.25, self.post_live)
            self._live_t.daemon = True
            self._live_t.start()

    def chat_payload(self) -> dict[str, Any]:
        from yuki.ui.web_data import chat_item

        items = list(self.model.items)[-200:] if self.model is not None else []
        now = time.time()
        return {"items": [chat_item(i, now) for i in items], "connected": self.client is not None}

    def _history(self) -> None:
        """The live thread is empty (a new tray session): today's latest session log, else memory's last turns."""
        try:
            from datetime import datetime

            from yuki.core.paths import logs_dir
            from yuki.ui.chat_model import Item, Step, clean_request, history_from_session_log

            items: list[Any] = []
            logs = logs_dir() / "sessions"
            workers = sorted(logs.glob(f"{datetime.now():%Y%m%d}-*-worker.jsonl"), key=lambda p: p.stat().st_mtime)
            if workers:
                items = history_from_session_log(workers[-1], limit=20)
            if not items and self.memory is not None:
                for turn in self.memory.recent_turns(limit=12):
                    user = clean_request(turn["user"])
                    if user:
                        items.append(Item(f"m:{turn['id']}:u", "user", turn["at"], text=user, source="history"))
                    reply = Item(f"m:{turn['id']}:a", "assistant", turn["at"], text=turn["reply"], source="history")
                    reply.steps = [Step(f"m:{turn['id']}:{i}", "tool", a, a.replace("_", " "), turn["at"], ended=turn["at"],
                                        ok=True) for i, a in enumerate(turn["actions"] or [])]
                    items.append(reply)
            if items and self.model is not None:
                self.model.prepend_history(items)
        except Exception as exc:  # noqa: BLE001
            self.event("web_history_failed", error=type(exc).__name__)

    def request(self, op: str, wait_s: float = OP_WAIT_S, **args: Any) -> dict[str, Any] | None:
        client = self.client
        if client is None:
            return None
        return client.request(op, wait_s=wait_s, **args)

    # -- actions (each on its own short thread) --------------------------------------------------------------------

    def _act(self, message: dict[str, Any]) -> None:
        rid, action = message.get("id"), str(message.get("action") or "")
        handler = self.ACTIONS.get(action)
        try:
            if handler is None:
                reply = {"ok": False, "error": f"unknown action {action!r}"}
            elif self.fixture is not None:
                reply = dict((self.fixture.get("replies") or {}).get(action) or {"ok": True})
            else:
                reply = getattr(self, handler)(message) or {}
                reply.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001 - the page always gets an answer
            reply = {"ok": False, "error": f"That didn't work: {type(exc).__name__}"}
            self.event("web_act_failed", action=action, error=f"{type(exc).__name__}: {exc}")
        self.event("web_act", action=action, ok=bool(reply.get("ok")))
        if rid is not None:
            self.post({"kind": "reply", "id": rid, **reply})

    #: action -> method. Closed names; the fields are machine values (ids, kinds, levels) or the user's own words.
    ACTIONS: dict[str, str] = {
        "offer": "a_offer", "offer_undo": "a_offer_undo", "card_feedback": "a_card_feedback", "answer": "a_answer",
        "skip_question": "a_skip_question", "stop": "a_stop", "stop_call": "a_stop_call", "steer": "a_steer",
        "submit": "a_submit", "askbar": "a_askbar", "continue": "a_continue", "copy": "a_copy", "correct": "a_correct",
        "forget": "a_forget", "rule_edit": "a_rule_edit", "person": "a_person", "workflow_edit": "a_workflow_edit",
        "permissions_set": "a_permissions_set", "app_look": "a_app_look", "app_act": "a_app_act", "voice": "a_voice",
        "offers": "a_offers", "coach": "a_coach", "questions": "a_questions", "pause": "a_pause", "accent": "a_accent",
        "motion": "a_motion", "call_pref": "a_call_pref",
    }
    OFFLINE = "Yuki isn't running, so that can't happen right now. Start Yuki and try again."

    def _tray(self, op: str, wait_s: float = OP_WAIT_S, **args: Any) -> dict[str, Any]:
        """A tray op: its reply, or a plain-words refusal while the tray isn't running."""
        if self.client is None:
            return {"ok": False, "error": self.OFFLINE, "offline": True}
        if self.ops and op not in self.ops:
            return {"ok": False, "error": "The running Yuki is older than this window. Restart Yuki to use this."}
        reply = self.request(op, wait_s=wait_s, **args)
        if reply is None:
            return {"ok": False, "error": "Yuki didn't answer. Try again in a moment."}
        reply.pop("re", None)
        return reply

    def _memory(self) -> Any:
        if self.memory is None:
            raise RuntimeError("memory is not open")
        return self.memory

    def a_offer(self, m: dict[str, Any]) -> dict[str, Any]:
        reply = self._tray("offer", card=m.get("card"))
        self.refresh("home")
        self.refresh("activity")
        return reply

    def a_offer_undo(self, m: dict[str, Any]) -> dict[str, Any]:
        reply = self._tray("offer_undo", card=m.get("card"))
        self.refresh("home")
        self.refresh("activity")
        return reply

    def a_card_feedback(self, m: dict[str, Any]) -> dict[str, Any]:
        kind = str(m.get("kind") or "")
        if self.client is not None:
            reply = self._tray("card_feedback", card=m.get("card"), kind=kind)
        else:   # the tray is down: memory hears the same reaction the corner card would send
            reaction = {"less": "less", "dismissed": "dismissed", "skip": "dismissed", "never": "dismissed"}.get(kind)
            if reaction is None:
                return {"ok": False, "error": "unknown feedback"}
            self._memory().ack_nudge(int(str(m.get("card"))), reaction)
            reply = {"ok": True}
        self.refresh("home")
        self.refresh("activity")
        return reply

    def a_answer(self, m: dict[str, Any]) -> dict[str, Any]:
        words = " ".join(str(m.get("words") or "").split())
        if not words:
            return {"ok": False, "error": "Say something first."}
        result = self._memory().answer_question(str(m.get("ref") or ""), words) or {}
        self.refresh("home")
        return {"saved": bool(result.get("saved")), "why": result.get("why") or ""}

    def a_skip_question(self, m: dict[str, Any]) -> dict[str, Any]:
        self._memory().skip_question(str(m.get("ref") or ""), never=bool(m.get("never")))
        self.refresh("home")
        return {}

    def a_stop(self, m: dict[str, Any]) -> dict[str, Any]:
        job = m.get("job")
        return self._tray("stop", job=int(job)) if isinstance(job, (int, float)) else {"ok": False, "error": "no request to stop"}

    def a_stop_call(self, m: dict[str, Any]) -> dict[str, Any]:
        self._memory().stop_call()
        self.refresh("home")
        self._jobs.put(("page", "shell"))
        return {}

    def a_steer(self, m: dict[str, Any]) -> dict[str, Any]:
        job, text = m.get("job"), " ".join(str(m.get("text") or "").split())
        if not isinstance(job, (int, float)) or not text:
            return {"ok": False, "error": "nothing to add"}
        return self._tray("steer", job=int(job), text=text)

    def a_submit(self, m: dict[str, Any]) -> dict[str, Any]:
        text = str(m.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "empty"}
        return self._tray("submit", text=text)

    def _grant_foreground(self) -> None:
        try:
            ctypes.windll.user32.AllowSetForegroundWindow(ctypes.c_uint32(0xFFFFFFFF))   # ASFW_ANY: the ask bar
        except Exception:  # noqa: BLE001
            pass

    def a_askbar(self, m: dict[str, Any]) -> dict[str, Any]:
        self._grant_foreground()
        return self._tray("askbar", text=str(m.get("text") or ""))

    def a_continue(self, m: dict[str, Any]) -> dict[str, Any]:
        """Work's Continue: a plan for picking the thread up, put in the ask bar unsent (the user's request once
        they press Enter). Built from memory's own fields for the thread: its title, where it stands, what's open."""
        ref = str(m.get("ref") or "")
        thread = None
        if self.memory is not None:
            thread = self.memory.thread(ref)
        if not thread:
            thread = next((t for t in (self.pages.get("work") or {}).get("threads", []) if t.get("ref") == ref), None)
        if not thread:
            return {"ok": False, "error": "I can't find that thread any more."}
        text = plan_text(thread)
        self._grant_foreground()
        return self._tray("askbar", text=text)

    def a_copy(self, m: dict[str, Any]) -> dict[str, Any]:
        return {"ok": set_clipboard(str(m.get("text") or ""))}

    def a_correct(self, m: dict[str, Any]) -> dict[str, Any]:
        """Me's Correct / Forget / Not mine / Pin: the user's words about one entry, as a correction memory folds in
        (``correct_portrait``; exactly what the Qt window sends). Shown as pending until memory applies it."""
        from yuki.ui.window.actions import correction_text

        how = str(m.get("how") or "correct")
        words = " ".join(str(m.get("words") or "").split()) if how == "correct" else CORRECTION_WORDS.get(how, "")
        ref, text = str(m.get("ref") or ""), str(m.get("text") or "")
        if not words or not ref or not text:
            return {"ok": False, "error": "Nothing to correct."}
        fid = self._memory().correct_portrait(correction_text(str(m.get("title") or "this"), text, words))
        self._record_pending(ref, how, words if how == "correct" else "", text, str(m.get("title") or ""), fid)
        return {"correction_id": fid}

    def a_forget(self, m: dict[str, Any]) -> dict[str, Any]:
        kind, ref = str(m.get("kind") or ""), str(m.get("ref") or "")
        mem = self._memory()
        if kind == "rule":
            ok = bool(mem.revoke_rule(ref))
            self.refresh("me")
            self.refresh("playbook")
            return {"ok": ok} if ok else {"ok": False, "error": "That rule was already gone."}
        if kind == "standing":
            ok = bool(mem.cancel_standing_request(ref))
            self.refresh("playbook")
            return {"ok": ok} if ok else {"ok": False, "error": "That request wasn't open."}
        return self.a_correct({**m, "how": "forget"})

    def a_rule_edit(self, m: dict[str, Any]) -> dict[str, Any]:
        """A rule said again in the user's words: the old one revoked (history kept), the new one remembered."""
        words = " ".join(str(m.get("words") or "").split())
        if not words:
            return {"ok": False, "error": "Say the rule first."}
        mem = self._memory()
        mem.revoke_rule(str(m.get("ref") or ""))
        mem.remember_rule(words)
        self.refresh("me")
        self.refresh("playbook")
        return {}

    def a_person(self, m: dict[str, Any]) -> dict[str, Any]:
        """One person's card for Me's dossier: the tray's ``people`` op (``contact``) or this window's memory."""
        from yuki.ui.web_data import contact_card

        ref = str(m.get("ref") or "")
        card = None
        if self.client is not None and "people" in self.ops:
            reply = self.request("people", wait_s=6.0, person=ref) or {}
            if reply.get("ok") and reply.get("available") and isinstance(reply.get("person"), dict):
                card = reply["person"]
        if card is None:
            contact = getattr(self._memory(), "contact", None)
            if not callable(contact):
                return {"ok": False, "error": "not available"}
            card = contact(ref) or {}
        if not card.get("found", True):
            return {"ok": False, "error": "nobody by that name"}
        return {"person": contact_card(card)}

    def a_workflow_edit(self, m: dict[str, Any]) -> dict[str, Any]:
        if self.client is not None:
            return self._tray("workflow_edit", ref=m.get("ref"), verb=m.get("verb"), words=m.get("words"))
        return {"ok": False, "error": "Saving procedures comes with workflows, not built yet."}

    def a_permissions_set(self, m: dict[str, Any]) -> dict[str, Any]:
        kind, level = str(m.get("kind") or ""), str(m.get("level") or "")
        if self.client is not None:
            return self._tray("permissions_set", kind=kind, level=level)
        from yuki.ui import policy

        try:
            levels = policy.set_level(kind, level, self.policy_path)
        except policy.PolicyError as exc:
            return {"ok": False, "error": str(exc)}
        return {"permissions": policy.describe(levels)}

    def a_app_look(self, m: dict[str, Any]) -> dict[str, Any]:
        """App access "look": off adds the app to the privacy blocklist (the memory service re-reads the file)."""
        from yuki.ui.window import settings_io

        app = " ".join(str(m.get("app") or "").split())
        if not app:
            return {"ok": False, "error": "no app"}
        current = settings_io.blocked("apps")
        if m.get("look"):
            settings_io.set_blocked("apps", [v for v in current if v.casefold() != app.casefold()])
        else:
            settings_io.set_blocked("apps", current + [app])
        return {}

    def a_app_act(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.ui import policy

        policy.set_app_act(str(m.get("app") or ""), bool(m.get("act")), self.policy_path)
        return {}

    VOICE_KEYS = ("wake", "mic", "speaker", "end_tone", "tts_provider")

    def a_voice(self, m: dict[str, Any]) -> dict[str, Any]:
        changes = {k: m[k] for k in self.VOICE_KEYS if k in m}
        if not changes:
            return {"ok": False, "error": "nothing to change"}
        if self.client is not None:
            reply = self._tray("voice_set", **changes)
            if reply.get("ok") and isinstance(reply.get("state"), dict):
                self.tray_state.update(reply["state"])
                self._voice_from_state()
                self.post_live()
            return {"ok": bool(reply.get("ok")), **({"error": reply["error"]} if reply.get("error") else {})}
        from yuki.ui.window import settings_io

        settings_io.write_voice_prefs(changes)   # used when Yuki next starts
        return {"offline": True}

    def _privacy(self, key: str, value: Any) -> dict[str, Any]:
        from yuki.ui.window import settings_io

        settings_io.update_privacy("nudges", key, value)
        return {}

    def a_offers(self, m: dict[str, Any]) -> dict[str, Any]:
        """The offers toggle: ``[nudges] offers`` in the privacy file ("yuki_only" on, "off"), which memory reads
        for every card it writes and at every press (``offer_act`` refuses with "off")."""
        return self._privacy("offers", "yuki_only" if m.get("enabled") else "off")

    def a_coach(self, m: dict[str, Any]) -> dict[str, Any]:
        return self._privacy("enabled", bool(m.get("enabled")))

    def a_questions(self, m: dict[str, Any]) -> dict[str, Any]:
        return self._privacy("questions_enabled", bool(m.get("enabled")))

    def a_pause(self, m: dict[str, Any]) -> dict[str, Any]:
        self._memory().set_paused(bool(m.get("paused")))
        self._jobs.put(("page", "shell"))
        return {}

    def _ui(self, **changes: Any) -> dict[str, Any]:
        from yuki.ui import ui_settings

        ui_settings.update(self.ui_path, **changes)
        if self.client is not None:   # the tray's surfaces follow (accent, motion)
            self.request("ui_settings", wait_s=3.0)
        return {}

    def a_accent(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.ui import ui_settings

        name = str(m.get("accent") or "")
        if name not in ui_settings.ACCENT_NAMES:
            return {"ok": False, "error": "unknown colour"}
        return self._ui(accent=name)

    def a_motion(self, m: dict[str, Any]) -> dict[str, Any]:
        return self._ui(reduce_motion=bool(m.get("reduce")))

    def a_call_pref(self, m: dict[str, Any]) -> dict[str, Any]:
        decision = str(m.get("decision") or "ask")
        app = str(m.get("app") or "")
        rows = (self.pages.get("settings") or {}).get("calls") or []
        key = next((r.get("key") for r in rows if r.get("app") == app and r.get("key")), app)
        self._memory().set_call_pref(key, None if decision == "ask" else decision)
        return {}

    # -- corrections waiting to be applied (shared with the Qt window: window-pending.json) ------------------------

    def _pending_path(self) -> Path:
        from yuki.ui import ui_settings

        return (self.ui_path or ui_settings.settings_path()).parent / "window-pending.json"

    def _load_pending(self) -> None:
        try:
            raw = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raw = {}
        if isinstance(raw, dict) and raw.get("db") in (None, str(self.db_path)):
            self.pending = {str(k): v for k, v in (raw.get("items") or {}).items() if isinstance(v, dict)}

    def _save_pending(self) -> None:
        path = self._pending_path()
        try:
            if not self.pending:
                path.unlink(missing_ok=True)
                return
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"db": str(self.db_path), "items": self.pending}, indent=1), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            pass

    def _record_pending(self, ref: str, kind: str, words: str, old: str, subject: str, fid: Any) -> None:
        self.pending[ref] = {"kind": kind, "words": words, "at": time.time(), "old": old, "subject": subject,
                             **({"fid": fid} if fid is not None else {})}
        self._save_pending()

    def _check_pending(self) -> None:
        """Has memory applied the corrections? Reads only: ``correction_status`` for each, else whether the fact
        it answers changed. Applied ones leave the list and Me is read again."""
        mem = self.memory
        if mem is None or not self.pending:
            return
        status = getattr(mem, "correction_status", None)
        changed = False
        for ref, p in list(self.pending.items()):
            state = "none"
            if callable(status) and p.get("fid") is not None:
                try:
                    state = str((status(p["fid"]) or {}).get("state") or "none")
                except Exception:  # noqa: BLE001
                    state = "none"
            age = time.time() - float(p.get("at") or time.time())
            if state == "done" or (state == "none" and age > 8 * 86400):
                self.pending.pop(ref, None)
                changed = True
            elif age > 600 and not p.get("late"):
                p["late"] = True
                changed = True
        if changed:
            self._save_pending()
            self.refresh("me")
            self.refresh("home")

    # -- timings and the end -------------------------------------------------------------------------------------

    def on_painted(self, page: dict[str, Any]) -> None:
        host = self.host
        self.metrics["page"] = {k: v for k, v in page.items() if k != "op"}
        if host is not None:
            self.metrics["marks"] = {**dict(host.marks), **self.marks}
            self.metrics["browser_pid"] = host.browser_pid
            self.metrics["runtime"] = host.runtime
            fg = ctypes.windll.user32.GetForegroundWindow()
            self.metrics["took_focus"] = bool(fg) and int(fg) == host.hwnd and host.o.placement == "offscreen"
        self.metrics["derived"] = derived(self.metrics)
        self.write()
        if host is not None and self.shot is not None:
            shoot = lambda: host.capture(self.shot, lambda ok: self.metrics.__setitem__("shot", ok) or self.write())  # noqa: E731
            if self.shot_delay:
                threading.Timer(self.shot_delay, lambda: host.invoke(shoot)).start()
            else:
                shoot()
        if host is not None and self.exit_after is not None:
            threading.Timer(max(0.0, self.exit_after), host.close).start()
        if host is not None and self.memory_low_after is not None:
            def low() -> None:
                self.metrics["memory_low"] = {"ok": host.set_memory_low(True), "at": time.time()}
                self.write()
            threading.Timer(max(0.0, self.memory_low_after), lambda: host.invoke(low)).start()

    def event(self, kind: str, **fields: Any) -> None:
        self.events.append({"kind": kind, "at": round(time.time(), 3), **fields})
        self.events = self.events[-60:]
        self.metrics["events"] = self.events[-30:]

    def on_host_event(self, kind: str, **fields: Any) -> None:
        self.event(kind, **fields)

    def write(self) -> None:
        if self.metrics_path is None:
            return
        tmp = self.metrics_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.metrics, indent=1, default=str), encoding="utf-8")
        os.replace(tmp, self.metrics_path)

    def close(self) -> None:
        """The window is gone: stop the threads, save the pages for the next opening, let memory go."""
        self._closing.set()
        self._jobs.put(None)
        client = self.client
        if client is not None:
            client.close()
        if self.fixture is None and self.cipher is not None:
            try:
                from yuki.ui.window import snapshot

                with self._lock:
                    keep = {k: v for k, v in self.pages.items() if k in self.fresh}
                if keep:
                    snapshot.save(snapshot.path_for(self.ui_path), self.cipher, keep, db=str(self.db_path),
                                  pages=snapshot.WEB_PAGES, fmt=snapshot.WEB_FORMAT)
            except Exception:  # noqa: BLE001
                pass
        if self.memory is not None:
            try:
                self.memory.close()
            except Exception:  # noqa: BLE001
                pass


def plan_text(thread: dict[str, Any]) -> str:
    """Work's Continue, in the user's ask bar: pick the thread up from memory's own fields (title, where it stands,
    the first open item). The user edits it or presses Enter; nothing runs before."""
    title = " ".join(str(thread.get("title") or "this").split())
    state = " ".join(str(thread.get("state") or "").split())
    open_items = [str(x) for x in thread.get("open") or [] if str(x).strip()]
    parts = [f"Pick up “{title}” where I left off: bring back the windows and pages I had open for it."]
    if state:
        parts.append(f"Where it stands: {state}")
    if open_items:
        parts.append(f"Next: {open_items[0]}")
    return " ".join(parts)


def derived(m: dict[str, Any]) -> dict[str, float | None]:
    """Seconds from the click (``t0``) to each stage, and the page's own paint times."""
    t0 = m.get("t0")
    marks = m.get("marks") or {}
    page = m.get("page") or {}

    def since(epoch_s: Any) -> float | None:
        return round(float(epoch_s) - float(t0), 4) if t0 and isinstance(epoch_s, (int, float)) and epoch_s else None

    def since_ms(epoch_ms: Any) -> float | None:
        return since(epoch_ms / 1000.0) if isinstance(epoch_ms, (int, float)) and epoch_ms else None

    return {
        "python_started": since(marks.get("main")), "window": since(marks.get("window")),
        "environment": since(marks.get("environment")), "controller": since(marks.get("controller")),
        "navigation_completed": since(marks.get("navigation_completed")),
        "snapshot_read": since(marks.get("snapshot")), "pipe": since(marks.get("pipe")),
        "memory_open": since(marks.get("memory")),
        "page_ready": since_ms(page.get("t_ready")), "fcp": since_ms(page.get("fcp")),
        "snapshot_rendered": since_ms(page.get("t_snapshot")), "element_painted": since_ms(page.get("t_element")),
        "first_paint": since_ms(page.get("t_element") or page.get("t_painted")),
        "frame_after_data": since_ms(page.get("t_painted")),
    }


# -- one window at a time (shared with the Qt window) ---------------------------------------------------------------


def single_instance() -> int | None:
    """Hold the window's mutex; ``None`` when another Yuki window runs (it is asked to come forward)."""
    k = ctypes.windll.kernel32
    k.CreateMutexW.restype = ctypes.c_void_p
    handle = k.CreateMutexW(None, False, MUTEX_NAME)
    if not handle:
        return None
    if k.GetLastError() == 183:   # ERROR_ALREADY_EXISTS
        k.CloseHandle(ctypes.c_void_p(handle))
        k.OpenEventW.restype = ctypes.c_void_p
        ev = k.OpenEventW(0x0002, False, FOCUS_EVENT)   # EVENT_MODIFY_STATE
        if ev:
            k.SetEvent(ctypes.c_void_p(ev))
            k.CloseHandle(ctypes.c_void_p(ev))
        return None
    return int(handle)


def release_instance(handle: int | None) -> None:
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def watch_focus(on_focus: Callable[[], None]) -> None:
    """A second start sets the event: this window comes forward (one waiting thread, no polling)."""
    k = ctypes.windll.kernel32
    k.CreateEventW.restype = ctypes.c_void_p
    ev = k.CreateEventW(None, False, False, FOCUS_EVENT)
    if not ev:
        return

    def wait() -> None:
        while True:
            if k.WaitForSingleObject(ctypes.c_void_p(ev), 0xFFFFFFFF) == 0:
                on_focus()

    threading.Thread(target=wait, name="yuki-web-focus", daemon=True).start()


def run_qt_window(page: str, db: Path | None) -> int:
    """The WebView2 runtime is missing or failed: open the Qt window instead (its own process, as a click would)."""
    import subprocess

    from yuki.core.procs import WINDOW, module_command

    qt_page = {"home": "today", "work": "today", "playbook": "yuki", "activity": "status"}.get(page, page)
    cmd = module_command(WINDOW, "--page", qt_page, *(["--db", str(db)] if db else []))
    try:
        subprocess.Popen(cmd, creationflags=0x00000008 | 0x00000200, close_fds=True, stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return 0
    except OSError:
        return 4


def main(argv: list[str] | None = None) -> int:
    t_main = time.time()
    ap = argparse.ArgumentParser(prog="python -m yuki.ui.web_window", description=__doc__.split("\n")[0])
    ap.add_argument("--page", default="home")
    ap.add_argument("--db", type=Path, default=None, help="memory database (default: the user's)")
    ap.add_argument("--fixture", nargs="?", const=str(FIXTURE), default=None,
                    help="render the invented preview data (no memory, no pipe)")
    ap.add_argument("--offscreen", action="store_true")
    ap.add_argument("--profile", type=Path, default=None)
    ap.add_argument("--browser-args", default=DEFAULT_BROWSER_ARGS,
                    help=f"Chromium switches (default {DEFAULT_BROWSER_ARGS!r}; an empty value: none)")
    ap.add_argument("--query", default="", help="preview knobs for the page (state=listening, motion=reduced, ...)")
    ap.add_argument("--assume-focus", action="store_true", help="animate as if in front (off-screen measurements)")
    ap.add_argument("--no-connect", action="store_true", help="never connect to the tray")
    ap.add_argument("--t0", type=float, default=None)
    ap.add_argument("--metrics", type=Path, default=None)
    ap.add_argument("--shot", type=Path, default=None)
    ap.add_argument("--shot-delay", type=float, default=0.0, help="take the shot this long after the paint")
    ap.add_argument("--exit-after", type=float, default=None)
    ap.add_argument("--memory-low-after", type=float, default=None)
    ap.add_argument("--dev-tools", action="store_true")
    ap.add_argument("--no-fallback", action="store_true", help="never open the Qt window instead")
    args = ap.parse_args(argv)
    page, thread = page_name(args.page)
    handle = None if args.offscreen else single_instance()
    if handle is None and not args.offscreen:
        return 0   # another Yuki window was asked to come forward
    from yuki.ui.webview2 import loader_path, runtime_version

    try:
        runtime = runtime_version(loader_path())
    except Exception:  # noqa: BLE001
        runtime = None
    if runtime is None:
        release_instance(handle)
        return 3 if args.no_fallback else run_qt_window(page, args.db)
    from yuki.ui import ui_settings

    ui = ui_settings.load()
    # another memory (a test copy, a measurement) keeps its window files (snapshot, pending corrections) beside it,
    # never in the user's own ui.json folder
    ui_path = None
    db = Path(args.db) if args.db is not None else default_db_path()
    users = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local") / "Yuki" / "memory" / "memory.db"
    if db.resolve() != users.resolve():
        ui_path = db.resolve().parent / "ui.json"
    win = WebWindow(db_path=args.db, page=page, thread=thread, ui_path=ui_path,
                    fixture=load_fixture(Path(args.fixture)) if args.fixture else None,
                    connect=not args.no_connect, t0=args.t0, metrics_path=args.metrics, shot=args.shot,
                    exit_after=args.exit_after, memory_low_after=args.memory_low_after, assume_focus=args.assume_focus,
                    shot_delay=args.shot_delay)
    win.start()
    url = START_URL + (("?" + args.query) if args.query else "")
    options = HostOptions(
        folders={APP_HOST: WEB_DIR, FONT_HOST: FONTS_DIR}, start_url=url,
        user_data_dir=args.profile or default_profile(), placement="offscreen" if args.offscreen else "normal",
        browser_args=args.browser_args, dev_tools=args.dev_tools, on_message=win.on_message,
        on_event=win.on_host_event, marks={"main": t_main},
        rect=tuple(ui.web_window) if ui.web_window and not args.offscreen else None,
        maximized=bool(ui.web_maximized) and not args.offscreen)
    host = WebView2Host(options)
    win.host = host
    if not args.offscreen:
        watch_focus(lambda: (host.invoke(host.bring_forward), win.post({"kind": "focus"})))
    placement: list[Any] = []

    def remember_place() -> None:
        placement.append(host.placement_now())
    host.on_closing = remember_place   # read before the window is destroyed
    code = host.run()
    if placement and placement[0][0] is not None:
        rect, maximized = placement[0]
        try:
            ui_settings.update(None, web_window=list(rect), web_maximized=bool(maximized))
        except Exception:  # noqa: BLE001
            pass
    win.metrics["exit_code"] = code
    win.close()
    win.write()
    release_instance(handle)
    if code == 2 and "page" not in win.metrics and not args.offscreen and not args.no_fallback:
        return run_qt_window(page, args.db)   # WebView2 could not start: the Qt window instead
    return code


if __name__ == "__main__":
    raise SystemExit(main())
