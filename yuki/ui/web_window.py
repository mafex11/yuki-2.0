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

**Live refresh** (docs/research/revamp-2026-10-02/42-window-live-refresh.md, 44-build-appL-live-refresh.md): the page
open in the window follows memory by events, not timers. The tray forwards memory's named events (cards, the call
status) and its own (a request's end, the agent's memory tools by name, voice state) over the pipe; each is a closed
topic that marks the pages showing it (:data:`TOPIC_PAGES`). Only the open page is built, after a short quiet
(:data:`DEBOUNCE_S`: a burst builds once), and only while the window can be seen; data equal to what the page has is
never sent again (:func:`page_hash`). Pages with no event are checked by memory's change stamps (a few indexed reads)
on a slow fallback (:data:`FALLBACK_S`), and rebuilt at the next moment they change by the clock alone
(:func:`next_boundary`). Minimised, hidden or behind a full-screen app nothing is built or posted; one catch-up when
it can be seen again. The page patches only what changed (``app.js`` ``patchInto``). Every refresh is logged
content-free (:meth:`WebWindow.log`).

Nothing here reads what a message says to decide anything; every button is a closed action name with machine fields
(ids, kinds), turned into a memory call, a settings write or a pipe op.

**The startup splash** (the 06a planet, ``web/planet-splash.js``): the tray passes ``--splash`` on the first window of
each Yuki run only; the page gets ``splash=play`` (``still`` with reduced motion) in its address, holds a plain
background until the host answers its ready with ``{kind: "splash", on}`` (off when a full-screen app is in front,
:meth:`WebWindow.cover_now`), plays about 1.45 s over the first page's load and fades into it. A click or Esc skips it.

Falls back to the Qt window (``yuki.ui.window``) by itself when the WebView2 runtime is missing or fails to start.
One window at a time, whichever engine: the same single-instance mutex and focus event as the Qt window.

Options for tests and measurements (all optional): ``--fixture`` (invented data, no memory, no pipe), ``--db``,
``--page``, ``--offscreen`` (a frameless tool window beyond every monitor that never activates), ``--profile``,
``--browser-args``, ``--query`` (preview knobs for the page, e.g. ``state=listening``), ``--t0``, ``--metrics``, ``--shot``, ``--exit-after``,
``--memory-low-after``, ``--probe-frame`` (the off-screen frame: Windows' own hit test answers, 32-build-E11-E14.md §6).
"""

from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import queue
import sys
import threading
import time
import tomllib
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
#: The app icon (the badge, every size; packaging/make_icon.py keeps it equal to packaging/yuki.ico).
APP_ICON = UI_DIR / "yuki.ico"
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
PAGE_ALIASES = {"today": "home", "status": "activity", "yuki": "playbook", "chat": "home", "day": "work",
                "todos": "home"}
#: A place on a page an opener can ask for (the agent's open_yuki "todos": Home, scrolled to Your list): the page's
#: own sub-address (``#home/list``), which the page brings into view.
PAGE_SUBS = {"todos": "list"}
#: A page asked for (navigation) is checked against memory's change stamps if older than this.
STALE_S = 45.0
#: A want for the open page again (the same tab clicked, the window shown again): checked if older than this.
AGAIN_S = 5.0

# -- live refresh (42-window-live-refresh.md §6): event-driven, the open page only, coalesced, never while hidden --
#: Topic (a closed machine name: what changed in memory or the tray) -> the pages that show it. ``shell`` is the
#: rail (always on screen).
TOPIC_PAGES: dict[str, tuple[str, ...]] = {
    "cards": ("shell", "home", "activity"),          # a card or question arrived, or memory has a reaction to one
    "call": ("day", "activity"),                     # the tray's forwarded call status (rail and Home: patched)
    "request_end": ("home", "activity"),             # a request finished, failed or was replaced (the request log)
    "rules": ("me", "playbook"),
    "todos": ("home", "work", "activity"),           # Home's Your list, Work's To-dos (appTodo), the ledger
    "standing": ("playbook",),
    "portrait": ("me", "home"),
    "people": ("me", "home"),
    "voice": ("settings",),                          # the tray's voice, devices or hotkeys changed
}
#: After these, the pages may have changed (a run can add a to-do or teach a procedure): their change stamps are
#: read, and the page is built only if one moved.
CHECK_TOPICS: dict[str, tuple[str, ...]] = {"request_end": ("work", "playbook"), "practice": ("playbook",)}
#: The agent's memory tools (``tool_finished`` carries the name) -> topic. Every memory write the agent can make is
#: here (a test checks it against :data:`yuki.agent.tools.MEMORY_TOOLS`); ``about`` changes routes only with
#: ``prefer_route``, so it is a stamp check.
TOOL_TOPICS: dict[str, str] = {"remember_rule": "rules", "forget_rule": "rules", "todo": "todos",
                               "watch_for": "standing", "remember_how": "practice", "correct_memory": "portrait",
                               "answer_question": "portrait", "about": "people_check"}
CHECK_TOPICS["people_check"] = ("me", "home")
#: A burst of events builds once: the build waits this long after the last event (trailing), at most MAX_WAIT_S
#: after the first, and a page is built at most once per MIN_GAP_S by events (your own actions go at once).
DEBOUNCE_S = 1.5
MAX_WAIT_S = 4.0
MIN_GAP_S: dict[str, float] = {"home": 10.0, "shell": 5.0}
DEFAULT_GAP_S = 2.0
#: Data with no event (the day's timeline, people, the playbook, portrait facts, a tray that is down): the
#: open page's change stamps are read this often while it can be seen and no call records (a few indexed reads).
FALLBACK_S: dict[str, float] = {"home": 300.0, "work": 300.0, "day": 120.0, "activity": 300.0, "me": 600.0,
                                "playbook": 600.0}
#: Fields that change on every build without anything to show (wrong 4 of the verification): left out of the
#: "did it change" hash. The page's ticker draws the clock-driven ones itself.
VOLATILE: dict[str, tuple[str, ...]] = {"home": ("since",), "day": ("now_min",), "settings": ("usage",)}
#: The reasons that mean you asked for the page (opened it, pressed something): see :meth:`WebWindow.send_page`.
ASKED = frozenset({"want", "action"})
#: Messages held while the window can't be seen (minimised, hidden, covered by a full-screen app); one catch-up on show.
HOLD_KINDS = frozenset({"data", "live", "chat"})
#: The tray's state fields the Settings page shows (a change in any of them rebuilds Settings if it is open).
SETTINGS_STATE = ("wake", "mic", "speaker", "hotkeys", "devices", "tts_voice")
#: What is playing (the Now strip) is read again at most this often, after a Home build.
MEDIA_S = 60.0
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


def app_version(ui_dir: Path | None = None) -> str:
    """Yuki's version, for Settings -> About.

    An installed build carries ``build_version.txt`` (written by packaging/yuki.spec from build.ps1 -Version), so About
    says what the installer and the exe's properties say; from source it is ``yuki.__version__``.
    """
    try:
        built = ((ui_dir or UI_DIR) / "build_version.txt").read_text(encoding="utf-8").strip()
        if built:
            return built
    except OSError:
        pass
    try:
        from yuki import __version__

        return str(__version__)
    except Exception:  # noqa: BLE001
        return ""


def splash_query(asked: bool, appear: dict[str, Any] | None = None) -> str:
    """The page's startup-splash knob: ``splash=play`` on the first window of a Yuki run (the tray passes
    ``--splash``), ``splash=still`` when you chose reduced motion, ``""`` otherwise. The page still waits for the
    host's word on ready (:meth:`WebWindow.cover_now`) before it plays."""
    if not asked:
        return ""
    appear = appearance() if appear is None else appear
    return "splash=still" if appear.get("reduce_motion") else "splash=play"


def page_name(page: str | None) -> tuple[str, bool]:
    """(the window's page for a requested name, whether to open the thread drawer): Qt names map to the new pages."""
    p = str(page or "").strip().casefold()
    return (p if p in PAGES else PAGE_ALIASES.get(p, "home")), p == "chat"


def page_sub(page: str | None) -> str:
    """The place on the page a requested name means (:data:`PAGE_SUBS`), ``""`` for the page itself."""
    return PAGE_SUBS.get(str(page or "").strip().casefold(), "")


def page_hash(name: str, data: Any) -> str:
    """What a page's data says, as a short digest: equal digests need no post (the page's :data:`VOLATILE` fields,
    which change on every build with nothing new to show, left out)."""
    import hashlib

    if isinstance(data, dict) and VOLATILE.get(name):
        data = {k: v for k, v in data.items() if k not in VOLATILE[name]}
    text = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.blake2b(text.encode("utf-8"), digest_size=12).hexdigest()


#: Keys whose number is a moment the page changes by the clock alone (an Undo window closing, an offer running out,
#: Home's away window moving on): machine field names of the page data, never content.
CLOCK_KEYS = frozenset({"until", "undo_until", "expires_at", "next_at"})


def next_boundary(data: Any, now: float) -> float:
    """The next moment ``data`` changes with time alone: the earliest future value under :data:`CLOCK_KEYS`, or
    the next local midnight ("today", the date, the day's timeline), whichever comes first."""
    from datetime import datetime, timedelta

    best = (datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(days=1, seconds=1)).timestamp()

    def walk(v: Any) -> None:
        nonlocal best
        if isinstance(v, dict):
            for k, x in v.items():
                if k in CLOCK_KEYS and isinstance(x, (int, float)) and not isinstance(x, bool):
                    if now < x < best:
                        best = float(x)
                else:
                    walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(data)
    return best


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
                 policy_path: Path | None = None, shot_delay: float = 0.0, splash: bool = False,
                 sub: str = "") -> None:
        self.db_path = Path(db_path) if db_path is not None else default_db_path()
        self.sub = sub                         # the place on the first page asked for (``todos``: Home's Your list)
        self.splash = splash                   # the startup splash was asked for (the tray's first window of a run)
        self.splash_answered = False           # the page's first ready got its answer (play or not): only once
        self.page, self.thread_open = page, thread
        self.open_page = page                  # the page open now ("day" for Work's Day tab), from the page's want
        self.fixture = fixture
        self.want_connect = connect and fixture is None
        self.ui_path = ui_path
        self.policy_path = policy_path
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
        self.call_status: dict[str, Any] | None = None   # the tray's last forwarded call_status()
        # live refresh (42 §6): pages marked for a build (dirty) or a stamp check (maybe), with the topics that marked
        # them; when each is due (debounced); what was last posted (a hash) and the stamps it was built at
        self._cv = threading.Condition()
        self.dirty: dict[str, set[str]] = {}
        self.maybe: dict[str, set[str]] = {}
        self._due: dict[str, float] = {}
        self._first: dict[str, float] = {}
        self._last_build: dict[str, float] = {}
        self.posted: dict[str, str] = {}
        self.posted_full: dict[str, str] = {}  # the same, volatile fields included (sent on a build you asked for)
        self.seen_stamps: dict[str, Any] = {}
        self._clock_at: dict[str, float] = {}
        self._fallback_at = float("inf")
        self._connecting = threading.Lock()   # one connect at a time (review F1: three at once on a restore)
        self._connect_soon = False            # catch_up asks the scheduler to look for the tray now
        self._scheduler_on = False
        self.covered = False                 # a full-screen app is in front (from _watch_cover)
        self._held: set[str] = set()          # kinds of messages held while the window can't be seen
        self._held_pages: set[str] = set()
        self._settings_sig = self._settings_state()
        self.playing: dict[str, Any] | None = None
        self._media_at = 0.0
        self._logger: Any = None             # the refresh log (log): opened on its first line
        self._log_lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.fixed: dict[str, dict[str, Any]] = {}   # corrections that landed lately ("Fixed" on their rows)
        self.metrics: dict[str, Any] = {"t0": t0, "pid": os.getpid(), "frozen": bool(getattr(sys, "frozen", False)),
                                        "python": sys.version.split()[0], "fixture": fixture is not None}
        self.metrics_path, self.shot, self.exit_after, self.memory_low_after = metrics_path, shot, exit_after, memory_low_after
        self.shot_delay = shot_delay
        self.events: list[dict[str, Any]] = []
        self.marks: dict[str, float] = {}
        self.probe_frame = False              # measurement: Windows' own answers about the frame (--probe-frame)

    # -- the page ------------------------------------------------------------------------------------------

    def post(self, message: dict[str, Any]) -> None:
        """To the page. Page data, live state and the thread wait while the window can't be seen (minimised,
        hidden, covered by a full-screen app): nothing wakes the page's JS then; :meth:`catch_up` sends one of each."""
        if self.host is None:
            return
        kind = message.get("kind")
        if kind in HOLD_KINDS and self.booted and not self.may_refresh():
            with self._lock:
                first = not self._held and not self._held_pages
                if kind == "data":
                    self._held_pages.add(str(message.get("page")))
                else:
                    self._held.add(str(kind))
            if first:
                self.log("web_hold", page=self.open_page, reason="hidden" if not self.window_shown() else "covered")
            return
        self.host.post(message)

    def on_message(self, message: Any) -> None:
        """A message from the page (UI thread): never waits on memory or the pipe."""
        if not isinstance(message, dict):
            return
        op = message.get("op")
        if op == "ready":
            self.marks["page_ready"] = time.time()
            if self.splash and self.host is not None:   # the page holds its splash until this answer (UI thread)
                # once per window: a page loaded again (a renderer restart) gets "off" at once, never a second play
                covered = self.cover_now() if not self.splash_answered else False
                on = not covered and not self.splash_answered
                self.splash_answered = True
                self.host.post({"kind": "splash", "on": on})
                self.log("web_splash", on=on, **({} if on else {"reason": "covered" if covered else "again"}))
            self.ready.set()
            threading.Thread(target=self._boot_when_ready, name="yuki-web-boot", daemon=True).start()
        elif op == "painted":
            self.on_painted(message)
        elif op == "want":
            page = str(message.get("page") or "")
            if page in PAGES or page == "day":
                self.want(page, again=bool(message.get("again")), shown=bool(message.get("shown")))
        elif op == "act":
            threading.Thread(target=self._act, args=(message,), name="yuki-web-act", daemon=True).start()
        elif op == "caption" and self.host is not None:   # where the page drew its title bar (Windows' hit test)
            self.host.set_caption(message)
        elif op == "window" and self.host is not None:    # a caption button or the title bar, on this (UI) thread
            self.host.window_op(str(message.get("do") or ""))
        elif op == "perf":   # the page's redraw counters (a measurement knob)
            self.metrics.setdefault("perf", []).append({"at": time.time(), "draws": message.get("draws"),
                                                        "ticks": message.get("ticks"), "ms": message.get("ms")})
            self.metrics["perf"] = self.metrics["perf"][-40:]
            self.write()

    def _boot_when_ready(self) -> None:
        """The page is up: send it the snapshot (or the fixture), the live state and the thread."""
        if self.fixture is not None:
            from yuki.ui.web_data import shift_times

            f = self.fixture
            delta = time.time() - float(f.get("now") or time.time())
            self.booted = True
            self.post({"kind": "boot", "source": "fixture", "shell": shift_times(f["shell"], delta),
                       "pages": shift_times(f["pages"], delta), "chat": shift_times(f.get("chat") or {}, delta),
                       "appearance": appearance(), "page": self.page, "sub": self.sub, "version": app_version()})
            return
        self._snapshot_done.wait(1.5)   # read in parallel with the browser's start; ~0.1 s
        with self._lock:
            pages = dict(self.pages)
            self.booted = True
            for k, v in pages.items():   # what the page has: fresh data equal to it is not sent again
                self.posted.setdefault(k, page_hash(k, v))
                self.posted_full.setdefault(k, page_hash("", v))
        chat = self.chat_payload() if self.model is not None else None
        self.post({"kind": "boot", "source": "snapshot", "shell": self.shell, "pages": pages, "chat": chat,
                   "appearance": appearance(), "page": self.page, "sub": self.sub, "version": app_version()})
        if self.thread_open:
            self.post({"kind": "focus", "page": self.page, "thread": True})
        self.marks["boot_sent"] = time.time()

    def send_page(self, name: str, data: Any, source: str = "memory", built_at: float | None = None,
                  asked: bool = False) -> bool:
        """Keep ``data`` as the page's and send it, unless the page already has exactly this (the hash of the data,
        less its :data:`VOLATILE` fields). A build you asked for (``asked``: you opened the page, or pressed
        something) also sends a change in the volatile fields alone, so Settings' "Yuki right now" and Home's "since"
        are current on a revisit; an event or a fallback never sends those alone. Returns whether it was sent."""
        digest = page_hash(name, data)
        full = page_hash("", data) if VOLATILE.get(name) else digest
        with self._lock:
            self.pages[name] = data
            self.stamps[name] = time.time()
            if source == "memory":
                self.fresh.add(name)
            if name == "home" and isinstance(data, dict):
                self.shell["needs"] = len(data.get("needs") or [])
            booted = self.booted
            if self.posted.get(name) == digest and not (asked and self.posted_full.get(name) != full):
                return False
            if booted:
                self.posted[name] = digest
                self.posted_full[name] = full
        if booted:
            self.post({"kind": "data", "page": name, "data": data, "source": source,
                       **({"built_at": round(built_at, 3)} if built_at else {})})
        return booted

    def post_live(self) -> None:
        if self.booted:
            # a snapshot (a dict copy is one step under the GIL): other threads update the shell while this posts
            # (review 34 L6: "dictionary changed size" killed the timer thread and lost an update)
            self.post({"kind": "live", "shell": dict(self.shell)})

    # -- start ---------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Begin the background work (before the window exists: it all runs while the browser starts)."""
        threading.Thread(target=self._watch_cover, name="yuki-web-cover", daemon=True).start()
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
        # the one pass at open: every page from memory once (navigation then shows fresh data at once); after it,
        # only the open page is built, and only when something changed
        for name in ("shell", "home", self.page if self.page != "home" else "work", "me", "work", "activity",
                     "settings", "playbook", "day"):
            self._enqueue(name, "open")
        self._arm_fallback()
        self._scheduler_on = True
        threading.Thread(target=self._scheduler, name="yuki-web-refresh", daemon=True).start()

    # -- page data (one worker, one read at a time) -------------------------------------------------------------

    def refresh(self, name: str, stale_s: float | None = None, reason: str = "action") -> None:
        """``name`` changed by something the user just did (a button here, a correction applied): built at once
        if it is open (or the rail), else marked to be built when it is next opened."""
        if self.memory is None and self.fixture is None:
            return
        if stale_s is not None and time.time() - self.stamps.get(name, 0.0) < stale_s and name in self.fresh:
            return
        self.mark(name, reason, urgent=True)

    def _enqueue(self, name: str, reason: str, topics: tuple[str, ...] = (), check: bool = False) -> None:
        with self._lock:
            key = ("check:" if check else "") + name
            if key in self._queued:
                return
            self._queued.add(key)
        self._jobs.put(("check" if check else "page", name, reason, topics))

    def _worker(self) -> None:
        while True:
            job = self._jobs.get()
            if job is None:
                return
            kind, name = job[0], job[1]
            reason, topics = (job[2], job[3]) if len(job) > 3 else ("refresh", ())
            if kind == "media":   # what is playing, put on the Home it was read for, in turn with the builds
                self._apply_media(job[2])
                continue
            if kind == "call":    # the tray's call status on Home: a payload patch, no memory read
                self._apply_call(job[2])
                continue
            with self._lock:
                self._queued.discard(("check:" if kind == "check" else "") + name)
            if self.memory is None or self._closing.is_set():
                continue
            if kind == "check":
                self._check(name, reason, topics)
            else:
                self._build_and_send(name, reason, topics)

    def _check(self, name: str, reason: str, topics: tuple[str, ...]) -> None:
        """Read the page's change stamps (a few indexed reads); build it only if one moved since its last build."""
        t = time.time()
        stamps = self._stamps(name)
        if stamps is not None and stamps == self.seen_stamps.get(name) and name in self.fresh:
            if name == self.open_page:
                self._arm_fallback()
            self.log("web_page", page=name, reason=reason, topic=",".join(topics), ms=round((time.time() - t) * 1000),
                     result="unchanged_stamps")
            return
        self._build_and_send(name, reason + "+stamps" if stamps is not None else reason, topics, stamps=stamps)

    def _build_and_send(self, name: str, reason: str, topics: tuple[str, ...], stamps: Any = None) -> None:
        t = time.time()
        if stamps is None:
            stamps = self._stamps(name)   # read before the build: a write during it moves them for the next check
        try:
            data = self.build(name)
        except Exception as exc:  # noqa: BLE001 - one page failing never stops the others
            self.event("web_page_failed", page=name, error=f"{type(exc).__name__}: {exc}")
            self.log("web_page_failed", page=name, reason=reason, error=type(exc).__name__)
            if name == self.open_page:   # the fallback was disarmed for this build: armed again (review F7)
                self._arm_fallback()
            return
        ms = round((time.time() - t) * 1000)
        self.event("web_page", page=name, ms=ms)
        self._last_build[name] = time.time()
        self.seen_stamps[name] = stamps
        if name == "shell":
            changed = {k: v for k, v in data.items() if self.shell.get(k) != v}
            self.shell.update(data)
            if changed:
                self.post_live()
            with self._lock:
                self.pages["shell"] = {k: data.get(k) for k in ("user", "memory_line")}
                self.fresh.add("shell")
            self.log("web_page", page=name, reason=reason, topic=",".join(topics), ms=ms,
                     result="posted" if changed else "unchanged")
            return
        asked = bool(ASKED & set(topics)) or reason.split("+")[0] in ASKED
        posted = self.send_page(name, data, built_at=t, asked=asked)
        self._clock_at[name] = next_boundary(data, time.time())
        if name == self.open_page:
            self._arm_fallback()
        self.log("web_page", page=name, reason=reason, topic=",".join(topics), ms=ms,
                 result="posted" if posted else "unchanged")
        if name == "home" and time.time() - self._media_at >= MEDIA_S:
            self._media_at = time.time()
            threading.Thread(target=self._media, name="yuki-web-media", daemon=True).start()

    def _stamps(self, name: str) -> Any:
        if self.memory is None:
            return None
        from yuki.ui.web_data import page_stamps

        try:
            return page_stamps(self.memory, name, requests_csv=self.requests_csv())
        except Exception:  # noqa: BLE001 - no stamps: the page is built
            return None

    def requests_csv(self) -> Path | None:
        """The agent's request log; a test memory (its own ``ui_path``) reads the one beside it, never the user's."""
        if self.ui_path is not None:
            return Path(self.ui_path).parent / "requests.csv"
        try:
            from yuki.config import Settings

            return Settings().requests_csv_path
        except Exception:  # noqa: BLE001
            return None

    def build(self, name: str) -> Any:
        from yuki.ui import web_data as W

        m = self.memory
        if name == "shell":
            return W.build_shell(m, call_status=self.call_status)
        if name == "home":
            source, people = self._people()
            lately = [{"ref": p["ref"], "name": p["name"], "route": p.get("route") or "", "last_at": p.get("last_at"),
                       "count": p.get("count"), **({"rhythm": p["rhythm"]} if p.get("rhythm") else {})}
                      for p in people if source == "index" and p.get("last_at")]
            data = W.build_home(m, requests_csv=self.requests_csv(), people=lately, call_status=self.call_status,
                                pending=self.shown_pending())
            if self.playing:
                data["now"]["playing"] = self.playing
            return data
        if name == "work":
            return W.build_work(m, pending=self.shown_pending())
        if name == "day":
            return W.build_day(m, people=self._people_ids(), pending=self.shown_pending())
        if name == "me":
            return W.build_me(m, pending=self.shown_pending(), people=self._people())
        if name == "playbook":   # practice memory's workflows(), read by this window's own memory client
            return W.build_playbook(m)
        if name == "activity":   # the people index names a call's friends once (two handles, one friend)
            return W.build_activity(m, requests_csv=self.requests_csv(), people=self._people_ids())
        if name == "settings":
            live = {k: self.tray_state.get(k) for k in ("wake", "mic", "speaker") if k in self.tray_state}
            data = W.build_settings(m, live=live, devices=self._devices(), policy_path=self.policy_path)
            keys = self.tray_state.get("hotkeys") if isinstance(self.tray_state.get("hotkeys"), dict) else {}
            if keys.get("voice"):
                data["voice"]["hotkey"] = W.hotkey_words(str(keys["voice"]))
            return data
        raise ValueError(name)

    def _people(self) -> tuple[str, list[dict[str, Any]]]:
        """The people: the tray's ``people`` op (memory's people index) when the tray serves it, else the same index
        through this window's own memory client, else the portrait's person facts. An empty index is no index (review
        34 M5): the portrait's people show until the nightly run builds it."""
        from yuki.ui import web_data as W

        if self.client is not None and "people" in self.ops:
            reply = self.request("people", wait_s=6.0, limit=60) or {}
            if reply.get("ok") and reply.get("available") and isinstance(reply.get("people"), list):
                people = [W.person_from_index(p) for p in reply["people"] if isinstance(p, dict) and p.get("name")]
                if people:
                    return "index", people
                return W.people_now(self.memory, use_index=False)
        return W.people_now(self.memory)

    def _people_ids(self) -> list[dict[str, Any]]:
        """Who's who for a call's people (each person's names and handles), kept a minute: Day and Activity name a
        call's friends once on every build without reading the people index each time."""
        now = time.time()
        cache = getattr(self, "_ids_cache", None)
        if cache is None or now - cache[0] > 60.0:
            cache = (now, [{"name": p.get("name"), "ids": p.get("ids") or []} for p in self._people()[1]])
            self._ids_cache = cache
        return cache[1]

    def _devices(self) -> dict[str, Any] | None:
        reply = self.request("devices", wait_s=3.0) if self.client is not None else None
        if reply and reply.get("ok") and isinstance(reply.get("devices"), dict):
            return reply["devices"]
        return None

    def _media(self) -> None:
        """What is playing (WinRT, ~0.1 s), read off the worker; put on Home by the worker itself, so it never lands
        on an older Home than the latest build (42 verification, missing 8)."""
        from yuki.ui.web_data import media_now

        self._jobs.put(("media", "home", media_now(), ()))

    def _apply_media(self, playing: Any) -> None:
        if playing == self.playing:
            return
        self.playing = playing
        with self._lock:
            home = self.pages.get("home")
        if isinstance(home, dict) and playing != (home.get("now") or {}).get("playing"):
            home = dict(home)
            home["now"] = {**(home.get("now") or {}), "playing": playing}
            posted = self.send_page("home", home, built_at=time.time())
            self.log("web_page", page="home", reason="media", topic="media", ms=0,
                     result="posted" if posted else "unchanged")

    def _apply_call(self, status: dict[str, Any]) -> None:
        from yuki.ui.web_data import call_view, home_call

        with self._lock:
            home = self.pages.get("home")
        if not isinstance(home, dict):
            return
        now_call, running = home_call(call_view(status), status.get("waiting"))
        patched = dict(home)
        patched["now"] = {**(home.get("now") or {}), "call": now_call}
        patched["running"] = running
        t = time.time()
        posted = self.send_page("home", patched, built_at=t)
        self.log("web_page", page="home", reason="call", topic="call", ms=round((time.time() - t) * 1000),
                 result="posted" if posted else "unchanged")

    # -- what changed, and when the open page is built (42 §6.2) ---------------------------------------------------

    def mark(self, name: str, reason: str, *, urgent: bool = False, check: bool = False,
             topics: tuple[str, ...] = ()) -> None:
        """Mark ``name`` for a build (or, ``check``, a stamp check). The open page and the rail are scheduled:
        at once when ``urgent`` (the user's own action), else after a quiet DEBOUNCE_S (a burst builds once) and no
        sooner than the page's MIN_GAP_S after its last build. Other pages wait until they are opened."""
        topics = topics or (reason,)
        now = time.time()
        with self._cv:
            if check and name not in self.dirty:
                self.maybe.setdefault(name, set()).update(topics)
            else:
                self.dirty.setdefault(name, set()).update(topics)
            if name == self.open_page or name == "shell":
                self._schedule(name, now, urgent)
                self._cv.notify()

    def _schedule(self, name: str, now: float, urgent: bool) -> None:
        """(``self._cv`` held) when ``name`` is built: a trailing debounce, capped, and the page's least gap."""
        current = self._due.get(name)
        if urgent:
            self._due[name] = now if current is None else min(current, now)
            return
        if current is not None and current <= now:
            return
        first = self._first.setdefault(name, now)
        due = min(now + DEBOUNCE_S, first + MAX_WAIT_S)
        due = max(due, self._last_build.get(name, 0.0) + MIN_GAP_S.get(name, DEFAULT_GAP_S))
        self._due[name] = due

    def topic(self, topic: str, why: str = "") -> None:
        """Something changed that pages show (a closed topic name): its pages marked, the open one scheduled."""
        for name in TOPIC_PAGES.get(topic, ()):
            self.mark(name, why or topic, topics=(topic,))
        for name in CHECK_TOPICS.get(topic, ()):
            self.mark(name, why or topic, check=True, topics=(topic,))

    def want(self, page: str, again: bool = False, shown: bool = False) -> None:
        """The page asked for its data (opened, the same tab clicked again, the window shown again)."""
        moved = page != self.open_page
        self.open_page = page   # the host knows which page is open only from here (42 verification, missing 6)
        if moved:
            self._arm_fallback()
        if self.memory is None and self.fixture is None:
            return
        if self._clock_at.get(page, float("inf")) <= time.time():
            self.mark(page, "clock", urgent=True)
        with self._cv:
            dirty, maybe = page in self.dirty, page in self.maybe
        if dirty or page not in self.fresh:
            self.mark(page, "want", urgent=True)
        elif maybe or time.time() - self.stamps.get(page, 0.0) >= (AGAIN_S if again or shown else STALE_S):
            self.mark(page, "want", urgent=True, check=True)
        if shown:
            self.catch_up("shown")

    def may_refresh(self) -> bool:
        """The one gate: the window can be seen (not minimised, not hidden, not covered by a full-screen app)."""
        return not self.covered and self.window_shown()

    def catch_up(self, reason: str) -> None:
        """The window can be seen again (restored, brought to the front, a full-screen app gone): one of each held
        message, and the open page if anything marked it meanwhile (or its fallback came due)."""
        if not self.booted or not self.may_refresh():
            return
        with self._lock:
            held, self._held = self._held, set()
            pages, self._held_pages = self._held_pages, set()
        if "live" in held:
            self.post_live()
        if "chat" in held and self.model is not None:
            self.post({"kind": "chat", "reset": True, **self.chat_payload()})
        for name in sorted(pages):
            with self._lock:
                data = self.pages.get(name)
            if data is not None:
                self.post({"kind": "data", "page": name, "data": data, "source": "memory"})
        now = time.time()
        scheduled: list[str] = []
        with self._cv:
            if self._clock_at.get(self.open_page, float("inf")) <= now:   # a time boundary passed while hidden
                self._clock_at[self.open_page] = float("inf")
                self.dirty.setdefault(self.open_page, set()).add("clock")
            for name in (self.open_page, "shell"):
                if name in self.dirty or name in self.maybe:
                    self._schedule(name, now, True)
                    scheduled.append(name)
            if self.want_connect and self.client is None and self.cipher is not None:
                self._connect_soon = True   # the scheduler (woken here) looks for the tray now, one connect at a time
            self._cv.notify()
        if self._connect_soon and not self._scheduler_on:   # no memory, so no scheduler: a thread of its own
            self._connect_soon = False
            threading.Thread(target=self._connect, name="yuki-web-connect", daemon=True).start()
        if held or pages or scheduled:   # an alt-tab with nothing waiting is not worth a line
            self.log("web_restore", page=self.open_page, reason=reason, held=sorted(held), pages=sorted(pages),
                     scheduled=scheduled)

    def _arm_fallback(self) -> None:
        every = FALLBACK_S.get(self.open_page)
        with self._cv:
            self._fallback_at = time.time() + every if every else float("inf")
            self._cv.notify()

    def _recording(self) -> bool:
        """A call records now: the tray's last forwarded status while it is connected (it forwards every change).
        The tray is what records, so with no tray nothing does (review F2: a stale chip once stopped every read)."""
        status = self.call_status
        return bool(self.client is not None and isinstance(status, dict) and status.get("recording"))

    def _scheduler(self) -> None:
        """The refresh clock: due builds (debounced), the open page's fallback stamp check, its next time boundary,
        the pending corrections and the pipe while it is down. Sleeps until the next of them; with nothing due it
        sleeps until an event wakes it. Nothing is built or read while the window can't be seen."""
        last_pending = last_connect = 0.0
        while not self._closing.is_set():
            now = time.time()
            visible = self.may_refresh()
            run: list[tuple[str, str, tuple[str, ...], bool]] = []
            with self._cv:
                if visible:
                    for name, due in list(self._due.items()):
                        if due > now:
                            continue
                        del self._due[name]
                        self._first.pop(name, None)
                        if name in self.dirty:
                            topics = tuple(sorted(self.dirty.pop(name) | self.maybe.pop(name, set())))
                            run.append((name, topics[0] if len(topics) == 1 else "events", topics, False))
                        elif name in self.maybe:
                            topics = tuple(sorted(self.maybe.pop(name)))
                            run.append((name, topics[0] if len(topics) == 1 else "events", topics, True))
                    clock = self._clock_at.get(self.open_page, float("inf"))
                    if clock <= now:
                        self._clock_at[self.open_page] = float("inf")
                        self.dirty.setdefault(self.open_page, set()).add("clock")
                        self._schedule(self.open_page, now, True)   # picked up on the next turn, at once
                    if self._fallback_at <= now:
                        if self._recording():   # during a call: no reads, look again later
                            self._fallback_at = now + FALLBACK_S.get(self.open_page, 300.0)
                        else:
                            self._fallback_at = float("inf")   # re-armed when the check or build is done
                            run.append((self.open_page, "fallback", ("fallback",), self.open_page not in self.dirty))
                else:
                    # held: marks stay, catch_up schedules them on show; the fallback waits for it too
                    if self._fallback_at <= now:
                        self._fallback_at = float("inf")
                        self.maybe.setdefault(self.open_page, set()).add("fallback")
            for name, reason, topics, check in run:
                self._enqueue(name, reason, topics, check=check)
            if visible and self.pending and now - last_pending >= PENDING_POLL_S:
                last_pending = now
                self._check_pending()
            if visible and self.want_connect and self.client is None and self.cipher is not None \
                    and (self._connect_soon or now - last_connect >= RECONNECT_S):
                last_connect = now
                self._connect_soon = False
                self._connect()
            with self._cv:
                waits = [self._fallback_at]
                if visible:
                    waits += list(self._due.values()) + [self._clock_at.get(self.open_page, float("inf"))]
                    if self.pending:
                        waits.append(last_pending + PENDING_POLL_S)
                    if self.want_connect and self.client is None and self.cipher is not None:
                        waits.append(last_connect + RECONNECT_S)
                wait = min(waits) - time.time()
                if self._closing.is_set():
                    return
                # not visible: due builds wait for catch_up's notify (no wake while hidden but the fallback mark)
                self._cv.wait(None if wait == float("inf") else max(0.05, min(wait, 3600.0)))

    def _watch_cover(self) -> None:
        """While a full-screen app (a game, a presentation, a full-screen video) is in front and this window is not,
        the window can't be seen: the page draws one still frame (``quiet``), and nothing is built or posted until
        that ends (:meth:`may_refresh`; then :meth:`catch_up`). Minimised or hidden, WebView2 marks the page hidden
        and its loop stops by itself. Windows' own answer, checked every 2 s."""
        while not self._closing.wait(2.0):
            host = self.host
            if host is None or not host.hwnd or not self.booted:
                continue
            self.set_covered(self.cover_now())

    def cover_now(self) -> bool:
        """Whether a full-screen app (a game, a presentation, a full-screen video) is in front and this window is
        not: Windows' own answer (``SHQueryUserNotificationState``), read once. ``False`` when Windows cannot say."""
        host = self.host
        hwnd = int(getattr(host, "hwnd", 0) or 0) if host is not None else 0
        state = ctypes.c_int(0)
        try:
            busy = ctypes.windll.shell32.SHQueryUserNotificationState(ctypes.byref(state)) == 0 and state.value in (2, 3, 4)
            return busy and int(ctypes.windll.user32.GetForegroundWindow() or 0) != hwnd
        except Exception:  # noqa: BLE001
            return False

    def set_covered(self, covered: bool) -> None:
        if covered == self.covered:
            return
        self.covered = covered
        if self.host is not None:
            self.host.post({"kind": "quiet", "on": covered})   # never held: it is what stills the page
        self.event("web_quiet", on=covered)
        self.log("web_quiet", on=covered, page=self.open_page)
        if covered:
            return
        self.catch_up("uncovered")
        with self._cv:
            self._cv.notify()

    def on_shown(self, reason: str = "restore") -> None:
        """The host's restore / activate hook (UI thread): hand on, never wait."""
        threading.Thread(target=self.catch_up, args=(reason,), name="yuki-web-shown", daemon=True).start()
        with self._cv:
            self._cv.notify()

    def window_shown(self) -> bool:
        """Whether the window can be seen: not minimised and not hidden (Windows' own answer for its hwnd). ``True``
        before it exists or when Windows cannot say, so nothing is skipped by mistake."""
        host = self.host
        hwnd = int(getattr(host, "hwnd", 0) or 0) if host is not None else 0
        if not hwnd:
            return True
        try:
            user32 = ctypes.windll.user32
            return bool(user32.IsWindowVisible(hwnd)) and not bool(user32.IsIconic(hwnd))
        except Exception:  # noqa: BLE001
            return True

    def log(self, kind: str, **fields: Any) -> None:
        """One content-free line in the window's own small log (page names, topics, reasons, times; never data):
        every refresh, skip, hold and catch-up, so a refresh can be proven from disk (42 R8). Written through the
        same writer and record shape as the tray's UI log (:class:`yuki.ui.uilog.UiLog`: a quiet
        :class:`~yuki.log.events.SessionLogger`, ``type="ui"``, ``event=<kind>``), in a session file of this window
        process's own (``<sessions>/<start time>-window.jsonl``: two processes never share one file); beside a test
        memory's ``ui.json`` as ``window-refresh.jsonl``."""
        self.event(kind, **fields)
        with self._log_lock:
            try:
                if self._logger is None:
                    from yuki.log.events import SessionLogger

                    if self.ui_path is not None:
                        where, sid = Path(self.ui_path).parent, "window-refresh"
                    else:
                        where, sid = self._sessions_dir(), time.strftime("%Y%m%d-%H%M%S") + "-window"
                    self._logger = SessionLogger(where, session_id=sid, quiet=True)
                self._logger.log("ui", event=kind, pid=os.getpid(), **fields)
            except (OSError, ValueError):
                pass

    @staticmethod
    def _sessions_dir() -> Path:
        try:
            from yuki.config import Settings

            return Settings().sessions_dir
        except Exception:  # noqa: BLE001
            from yuki.core.paths import logs_dir

            return logs_dir() / "sessions"

    # -- the pipe to the tray -------------------------------------------------------------------------------------

    def _connect(self) -> None:
        """Find the tray's pipe. One attempt at a time (review F1): a restore used to start up to three, each kept
        a live client and every chat event arrived several times. A caller that finds one running returns."""
        if not self._connecting.acquire(blocking=False):
            return
        try:
            if self.client is not None:
                return
            self._connect_once()
        finally:
            self._connecting.release()

    def _connect_once(self) -> None:
        from yuki.ui.window_ipc import WindowClient, auth_key, pipe_name

        try:
            key, address = auth_key(self.cipher), pipe_name(self.db_path)
        except Exception as exc:  # noqa: BLE001
            self.event("web_pipe_failed", error=type(exc).__name__)
            return
        box: list[Any] = []
        client = WindowClient(key, address, on_event=self._on_tray_event,
                              on_closed=lambda: self._on_tray_closed(box[0] if box else None))
        box.append(client)
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

    def _on_tray_closed(self, client: Any = None) -> None:
        if client is not None and self.client is not None and client is not self.client:
            return   # an older connection's goodbye: the current one stays
        self.client = None
        self.call_status = None   # no more forwards: pages read the call from memory again
        # the tray records calls: with it gone nothing records, and the forwarded chip is stale (review F2). The
        # chip goes now; the rail and the call's pages are read from memory again
        self.shell.update(connected=False, runs=[], call=None)
        self.post_live()
        self.mark("shell", "tray_closed", topics=("tray_closed",))
        self.topic("call", "tray_closed")
        self.mark("home", "tray_closed", topics=("tray_closed",))

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
            self._settings_sig = self._settings_state()
            self._voice_from_state()
            self._runs_changed(force=True)
            if self.booted:
                self.post({"kind": "chat", "reset": True, **self.chat_payload()})
        elif kind == "chat" and self.model is not None:
            ev = msg.get("event")
            if isinstance(ev, dict):
                self.model.apply(ev)
                self._chat_topic(ev)
        elif kind == "state":
            self.tray_state.update({k: v for k, v in msg.items() if k != "ev"})
            self._voice_from_state()
            self.post_live()
            sig = self._settings_state()
            if sig != self._settings_sig:   # only what Settings shows: voice state changes many times a minute
                self._settings_sig = sig
                self.topic("voice")
        elif kind == "focus":
            page, thread = page_name(msg.get("page")) if msg.get("page") else (None, False)
            self.come_forward(page, thread, page_sub(msg.get("page")))
        elif kind == "permissions":
            self.post({"kind": "permissions", "permissions": msg.get("permissions")})
        elif kind == "call":   # the tray's call status, forwarded once per change: no memory read for the rail
            self.on_call_status(msg.get("status") if isinstance(msg.get("status"), dict) else {})
        elif kind == "memory":   # memory's named events, forwarded by the tray (a closed topic name)
            self.on_memory_topic(str(msg.get("topic") or ""))

    def come_forward(self, page: str | None = None, thread: bool = False, sub: str = "") -> None:
        """A tray click, the agent's open_yuki or a second start: the window comes forward and the page that will
        be open is refreshed (not Home whatever is open: 42 R5). ``sub``: a place on that page to bring into view
        (``list``: Home's Your list, for open_yuki's ``todos``)."""
        if self.host is not None:
            self.host.invoke(self.host.bring_forward)
        self.post({"kind": "focus", **({"page": page} if page else {}), **({"thread": True} if thread else {}),
                   **({"sub": sub} if page and sub else {})})
        if page and page != self.open_page:
            return   # the page goes there and asks for it (want)
        self.refresh(self.open_page, stale_s=10.0)

    def on_call_status(self, status: dict[str, Any]) -> None:
        """The tray's call status: the rail's chip patched from it at once (no memory read); Home, Day and Activity
        marked (Home is built from this status, without reading the call)."""
        from yuki.ui.web_data import call_view

        self.call_status = status
        call = call_view(status)
        if call != self.shell.get("call"):
            self.shell["call"] = call
            self.post_live()
        self._jobs.put(("call", "home", status, ()))   # Home's call tile and rows, patched in turn with the builds
        self.topic("call")

    def on_memory_topic(self, topic: str) -> None:
        if topic in TOPIC_PAGES or topic in CHECK_TOPICS:
            self.topic(topic)

    #: Chat events that mean a request is over (its row is in the request log a moment later).
    REQUEST_ENDS = frozenset({"finished", "failed", "withdrawn"})

    def _chat_topic(self, ev: dict[str, Any]) -> None:
        """The thread's events that change pages: a card in the thread or its state, a request's end, and the
        agent's memory tools by their fixed names (never what they said)."""
        kind = ev.get("type")
        if kind in ("nudge", "nudge_state"):
            self.topic("cards")
        elif kind in self.REQUEST_ENDS:
            self.topic("request_end")
        elif kind == "tool_finished" and ev.get("ok", True):
            name = TOOL_TOPICS.get(str(ev.get("name") or ""))
            if name:
                self.topic(name)

    def _settings_state(self) -> str:
        return json.dumps({k: self.tray_state.get(k) for k in SETTINGS_STATE}, sort_keys=True, default=str)

    def _voice_from_state(self) -> None:
        from yuki.ui.web_data import hotkey_words

        st = self.tray_state
        self.shell["voice"] = {"state": st.get("voice_state") or "off", "wake": bool(st.get("wake")),
                               "conversation": bool(st.get("conversation")), "quiet_until": st.get("quiet_until") or 0}
        keys = st.get("hotkeys") if isinstance(st.get("hotkeys"), dict) else {}
        if keys.get("toggle"):
            self.shell["hotkey_words"] = hotkey_words(str(keys["toggle"]))

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
                from yuki.ui.web_data import shift_times

                canned = dict((self.fixture.get("replies") or {}).get(action) or {"ok": True})
                by_ref = canned.pop("by_ref", None)   # a reply that depends on what was asked for (a person's card)
                if isinstance(by_ref, dict) and str(message.get("ref") or "") in by_ref:
                    canned = dict(by_ref[str(message.get("ref"))])
                reply = shift_times(canned, time.time() - float(self.fixture.get("now") or time.time()))
            else:
                reply = getattr(self, handler)(message) or {}
                reply.setdefault("ok", True)
        except Exception as exc:  # noqa: BLE001 - the page always gets an answer
            reply = {"ok": False, "error": f"That didn't work: {type(exc).__name__}"}
            self.event("web_act_failed", action=action, error=f"{type(exc).__name__}: {exc}")
        self.event("web_act", action=action, ok=bool(reply.get("ok")))
        if action in self.SETTINGS_ACTIONS and self.fixture is None:
            # Settings' controls flip the page's own switch; its data is read again so a later patch shows the
            # same (42 verification, missing 3: files and the tray's state, which no memory stamp sees)
            self.refresh("settings")
        if rid is not None:
            self.post({"kind": "reply", "id": rid, **reply})

    #: action -> method. Closed names; the fields are machine values (ids, kinds, levels) or the user's own words.
    ACTIONS: dict[str, str] = {
        "offer": "a_offer", "offer_undo": "a_offer_undo", "card_feedback": "a_card_feedback", "answer": "a_answer",
        "skip_question": "a_skip_question", "stop": "a_stop", "stop_call": "a_stop_call", "steer": "a_steer",
        "submit": "a_submit", "askbar": "a_askbar", "copy": "a_copy", "correct": "a_correct",
        "forget": "a_forget", "rule_edit": "a_rule_edit", "person": "a_person", "workflow_edit": "a_workflow_edit",
        "pin": "a_pin", "resolve_heard": "a_resolve_heard", "review_mark": "a_review_mark",
        "thread_days": "a_thread_days", "chat_day": "a_chat_day", "day_answer": "a_day_answer",
        "permissions_set": "a_permissions_set", "app_look": "a_app_look", "app_act": "a_app_act", "voice": "a_voice",
        "offers": "a_offers", "coach": "a_coach", "questions": "a_questions", "pause": "a_pause", "accent": "a_accent",
        "motion": "a_motion", "call_pref": "a_call_pref", "quiet": "a_quiet",
        # the feedback round 3 (W3): Correct from your own words with its Undo
        "correct_item": "a_correct_item", "cancel_correction": "a_cancel_correction",
        # the to-do list (appTodo): Done / Not needed, the Undo of a Done, Add from the window (Correct is correct_item)
        "todo_end": "a_todo_end", "todo_reopen": "a_todo_reopen", "todo_add": "a_todo_add",
        # Settings → AI (report 67): each job's service and model, a key, the other service's address, a model list,
        # a one-call test. Written here (ai.json, the DPAPI key file): no tray needed, every process follows
        "ai_role": "a_ai_role", "ai_key": "a_ai_key", "ai_custom": "a_ai_custom", "ai_models": "a_ai_models",
        "ai_test": "a_ai_test",
    }
    #: Settings' own controls (each changes what the Settings page shows).
    SETTINGS_ACTIONS = frozenset({"permissions_set", "app_look", "app_act", "voice", "offers", "coach", "questions",
                                  "pause", "accent", "motion", "call_pref", "quiet", "ai_role", "ai_key", "ai_custom"})
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
        self.refresh("work")   # an "Add to list" offer puts a to-do on Work's list too
        return reply

    def a_offer_undo(self, m: dict[str, Any]) -> dict[str, Any]:
        reply = self._tray("offer_undo", card=m.get("card"))
        self.refresh("home")
        self.refresh("activity")
        self.refresh("work")
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
        self.refresh("shell")
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

    #: Which page shows the items of a ref kind (its Correct's live refresh). Threads are not shown in the window (the
    #: 4 Oct decision), so a thread ref is nothing here.
    REF_PAGES = {"fact": ("me", "home"), "journal": ("me",), "episode": ("day", "work"),
                 "todo": ("home", "work"), "commit": ("home", "work"), "loop": ("home", "work")}

    def a_correct_item(self, m: dict[str, Any]) -> dict[str, Any]:
        """Correct (the feedback round 3, W3): the user's few words about what is wrong with one item (``fact:N``, a
        person's summary, a routine; ``todo:N``; ``episode:N``; ``journal:N``, "It was..." on a misheard entry), to
        memory's ``correct_item``, which records it at once and wakes the correction pass. The row says "Got it,
        fixing it" until the pass lands (``_check_pending``), then shows the corrected text."""
        ref, words = str(m.get("ref") or ""), " ".join(str(m.get("words") or "").split())
        if not ref or not words:
            return {"ok": False, "error": "Say what’s wrong in a few words."}
        fn = getattr(self._memory(), "correct_item", None)
        if not callable(fn):   # an older memory: the fixed-format correction (facts only)
            return self.a_correct({**m, "how": "correct", "words": words})
        r = fn(ref, words) or {}
        if not r.get("ok"):
            return {"ok": False, "error": str(r.get("error") or "That can’t be corrected here.")}
        cid = r.get("correction_id")
        self._record_pending(ref, "correct", words, str(m.get("text") or ""), str(m.get("title") or ""), cid)
        for page in self.REF_PAGES.get(ref.split(":")[0], ("me",)):
            self.refresh(page)
        return {"correction_id": cid, "ref": ref}

    #: How long a to-do you marked done here can be put back (memory's ``reopen_todo`` window).
    TODO_UNDO_S = 10 * 60.0

    def _todo_refresh(self) -> None:
        for page in ("home", "work", "activity"):
            self.refresh(page)

    def a_todo_end(self, m: dict[str, Any]) -> dict[str, Any]:
        """A to-do's Done or Not needed (the list on Home and Work): memory's ``end_todo`` by the item's id, at once,
        no model. ``undo``: whether its Undo can put it back (a to-do you added, within :data:`TODO_UNDO_S`)."""
        ref, how = str(m.get("ref") or ""), str(m.get("how") or "")
        if how not in ("done", "not_needed") or not ref:
            return {"ok": False, "error": "Unknown choice."}
        mem = self._memory()
        ended = mem.end_todo(ref, how)
        self._todo_refresh()
        if not ended:
            return {"ok": False, "error": "That’s already off your list."}
        can_undo = ref.startswith("todo:") and callable(getattr(mem, "reopen_todo", None))
        return {"ref": ended, "how": how, "undo": can_undo,
                **({"until": time.time() + self.TODO_UNDO_S} if can_undo else {})}

    def a_todo_reopen(self, m: dict[str, Any]) -> dict[str, Any]:
        """The Undo of a Done or Not needed on a to-do you added: memory's ``reopen_todo``; it is open again, as it was."""
        fn = getattr(self._memory(), "reopen_todo", None)
        back = fn(str(m.get("ref") or "")) if callable(fn) else None
        self._todo_refresh()
        if not back:
            return {"ok": False, "error": "Too late to put it back (10 minutes)."}
        return {"ref": back}

    def a_todo_add(self, m: dict[str, Any]) -> dict[str, Any]:
        """Add from the window: your words as the to-do (memory's ``add_todo``, as the voice adds one), with the due
        day and time from the page's own date and time fields (machine values ``YYYY-MM-DD`` and ``HH:MM``, checked
        here; nothing is read out of the words)."""
        from datetime import datetime

        text = " ".join(str(m.get("text") or "").split())
        if not text:
            return {"ok": False, "error": "Say the to-do in a few words."}
        day, clock = str(m.get("day") or "").strip(), str(m.get("time") or "").strip()
        due = None
        try:
            if day:
                datetime.strptime(day, "%Y-%m-%d")
                due = day
                if clock:
                    datetime.strptime(clock, "%H:%M")
                    due = f"{day} {clock}"
        except ValueError:
            return {"ok": False, "error": "That date isn’t one I can read. Pick it again."}
        ref = self._memory().add_todo(text, due)
        self._todo_refresh()
        return {"ref": ref}

    def a_cancel_correction(self, m: dict[str, Any]) -> dict[str, Any]:
        """The Undo of a Correct while it waits (memory's ``cancel_correction``); once the pass started it is too late
        and the correction stands."""
        ref, cid = str(m.get("ref") or ""), m.get("correction_id")
        if not isinstance(cid, int):
            cid = (self.pending.get(ref) or {}).get("fid")
        if not isinstance(cid, int):
            return {"ok": False, "error": "There’s nothing to undo."}
        r = self._memory_op("cancel_correction")(cid) or {}
        if not r.get("ok"):
            return {"ok": False, "too_late": True, "error": self.TOO_LATE}
        self.pending.pop(ref, None)
        self._save_pending()
        for page in self.REF_PAGES.get(ref.split(":")[0], ("me",)):
            self.refresh(page)
        return {}

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
        """Playbook (and Home's Learned) Save / Edit / No on one practice object, through memory's own
        ``workflow_edit`` (the ledger; an edit is queued in the user's words and the practice worker re-distills it).
        A teach-back question is answered (Save / Edit with words) or skipped (No)."""
        ref, words = str(m.get("ref") or ""), " ".join(str(m.get("words") or "").split())
        action = workflow_action(ref, str(m.get("verb") or ""), words)
        if action is None:
            return {"ok": False, "error": "Say it in your words first." if m.get("verb") == "edit" else "Unknown choice."}
        edit = getattr(self._memory(), "workflow_edit", None)
        if not callable(edit):
            return {"ok": False, "error": "This memory has no playbook yet."}
        result = edit(ref, action, words) or {}
        self.refresh("playbook")
        self.refresh("home")
        return {"ok": bool(result.get("ok")), "error": result.get("error") or "", "state": result.get("state"),
                "queued": bool(result.get("queued"))}

    #: Memory's "too late" answer to an Undo after its correction pass started, said by Yuki itself.
    TOO_LATE = "Too late to undo: I’m already updating my memory from your answer."

    def _memory_op(self, name: str) -> Any:
        fn = getattr(self._memory(), name, None)
        if not callable(fn):
            raise RuntimeError(f"memory has no {name}")
        return fn

    def a_pin(self, m: dict[str, Any]) -> dict[str, Any]:
        """Pin and Unpin on a fact, a routine or a person's summary: its own flag in memory (``pin_fact``; one write,
        no model), so the button toggles and the next build shows the same."""
        ref, on = str(m.get("ref") or ""), bool(m.get("on", True))
        if not ref:
            return {"ok": False, "error": "Nothing to pin."}
        r = self._memory_op("pin_fact")(ref, on) or {}
        self.refresh("me")
        if not r.get("ok"):
            return {"ok": False, "error": "That changed since you opened it. I’ll show it again."}
        return {"pinned": bool(r.get("pinned"))}

    def _answer(self, op: str, m: dict[str, Any], verdicts: tuple[str, ...]) -> dict[str, Any]:
        ref, verdict = str(m.get("ref") or ""), str(m.get("verdict") or "")
        if not ref or verdict not in verdicts:
            return {"ok": False, "error": "Unknown answer."}
        cid = m.get("correction_id")
        r = self._memory_op(op)(ref, verdict, correction_id=int(cid) if isinstance(cid, int) else None) or {}
        self.refresh("me")
        if r.get("ok"):
            return {"correction_id": r.get("correction_id"), "verdict": verdict}
        too_late = verdict == "undo" and str(r.get("error") or "").startswith("Too late")
        return {"ok": False, "too_late": too_late,
                "error": self.TOO_LATE if too_late else "That changed since you opened it. I’ll show it again."}

    def a_resolve_heard(self, m: dict[str, Any]) -> dict[str, Any]:
        """Maybe misheard: Yes (``guess_right``: Yuki's guess is right; the correction pass rewrites the entry), No
        (``wrong``) and, on an entry without a guess, Right (``right``: taken as heard), at once (``resolve_heard``, no
        model on the click), Undo inside the row's 5 s; an Undo after the correction pass started is too late and the
        item stays resolved. "It was..." in your words is :meth:`a_correct_item`."""
        return self._answer("resolve_heard", m, ("right", "guess_right", "wrong", "undo"))

    def a_review_mark(self, m: dict[str, Any]) -> dict[str, Any]:
        """To review: Right / Wrong / Not mine at once (``review_mark``), Undo inside the row's 5 s."""
        return self._answer("review_mark", m, ("right", "wrong", "notmine", "undo"))

    def a_thread_days(self, m: dict[str, Any]) -> dict[str, Any]:
        """The thread's days (a new chat every day, the 3 Oct item 15): the connector line about your last day
        (memory's day digest said to you, with what was left open) and the earlier days with exchanges, for the
        drawer's separators and one-line folds. Reads only."""
        mem = self._memory()
        conn = getattr(mem, "day_connector", None)
        days_fn = getattr(mem, "chat_days", None)
        c = (conn() if callable(conn) else None) or None
        connector = None
        if isinstance(c, dict) and str(c.get("text") or "").strip():
            from datetime import datetime

            try:   # midday of that day (a time, so the page finds its day the way it finds every other)
                day_at = datetime.fromisoformat(str(c.get("day"))).timestamp() + 12 * 3600
            except (TypeError, ValueError):
                day_at = None
            connector = {"day": c.get("day"), "day_at": day_at, "label": c.get("label") or "", "text": " ".join(str(c["text"]).split()),
                         "open": [" ".join(str(o.get("text") or "").split()) for o in c.get("open") or []
                                  if isinstance(o, dict) and str(o.get("text") or "").strip()][:3]}
        days = []
        for d in (days_fn(days=14) if callable(days_fn) else []) or []:
            if isinstance(d, dict) and d.get("day"):
                days.append({"day": str(d["day"]), "count": int(d.get("count") or 0), "first_at": d.get("first"),
                             "last_at": d.get("last")})
        return {"connector": connector, "days": days}

    def a_chat_day(self, m: dict[str, Any]) -> dict[str, Any]:
        """One earlier day's exchanges, when you open its fold in the drawer (``turns_on``): what you said (Yuki's
        own context blocks taken off) and what Yuki answered."""
        from yuki.ui.chat_model import clean_request

        fn = getattr(self._memory(), "turns_on", None)
        if not callable(fn):
            return {"ok": False, "error": "This memory keeps no earlier days yet."}
        turns = []
        for t in fn(str(m.get("day") or "")) or []:
            if isinstance(t, dict):
                turns.append({"id": f"t{t.get('id')}", "at": t.get("at"), "user": clean_request(str(t.get("user") or "")),
                              "reply": " ".join(str(t.get("reply") or "").split()), "outcome": t.get("outcome") or ""})
        return {"turns": turns}

    def a_day_answer(self, m: dict[str, Any]) -> dict[str, Any]:
        """Your answer to the connector line ("Yesterday you were fixing X. Still on it?"). "Still on it" keeps it
        open: nothing in memory is wrong, so nothing is sent. "Done" reaches memory the way your words about what it
        knows always do, as a correction its fold applies (memory has no direct "done" for a day's open item yet);
        sent after the line's Undo window, like Forget. The day is the connector's own (memory's label for it: it can
        be an older day when yesterday has no digest yet), and the press is said as a press, never as words you typed
        (review F5)."""
        verdict, item = str(m.get("verdict") or ""), " ".join(str(m.get("item") or "").split())
        if verdict == "still":
            return {"sent": False}
        if verdict != "done" or not item:
            return {"ok": False, "error": "Nothing to mark."}
        mem, day = self._memory(), str(m.get("day") or "")
        conn = getattr(mem, "day_connector", None)
        c = (conn(day=day) if callable(conn) and day else None) or {}
        label = " ".join(str((c.get("label") if isinstance(c, dict) else "") or m.get("label") or day).split())[:40]
        when = "yesterday" if label.casefold() in ("", "yesterday") else f"on {label}"
        fid = mem.correct_portrait(f'About "what was still open {when}" (memory had: "{item}"), the user pressed Done '
                                   f'on it: it is finished now. They typed no words of their own.')
        self.refresh("me")
        return {"sent": True, "correction_id": fid}

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

    #: Quiet hours when they are first turned on (the memory service's own defaults, NudgeConfig).
    QUIET_DEFAULT = ("00:30", "08:00")

    def a_quiet(self, m: dict[str, Any]) -> dict[str, Any]:
        """Quiet hours (the 4 Oct item W10): ``[nudges] quiet_start`` / ``quiet_end`` in the privacy file, the setting
        the memory service's card gates read (:func:`yuki.memory.nudges.in_quiet_hours`, the shared attention rules:
        check-ins, questions, observation cards and offers are held; reminders and "tell me when" requests are not).
        ``{"on": false}`` turns them off (an equal start and end is none, as memory reads it) and remembers the hours;
        ``{"on": true}`` brings the remembered hours back; ``{"start", "end"}`` ("HH:MM") sets them. Both keys go in
        one write of the file, so memory never reads half a change."""
        from yuki.ui import ui_settings
        from yuki.ui.window import settings_io

        nudges = settings_io.read_privacy().get("nudges", {}) or {}
        cur = (str(nudges.get("quiet_start", self.QUIET_DEFAULT[0])), str(nudges.get("quiet_end", self.QUIET_DEFAULT[1])))
        if "start" in m or "end" in m:
            start, end = _hhmm(m.get("start", cur[0])), _hhmm(m.get("end", cur[1]))
            if start is None or end is None:
                return {"ok": False, "error": "That isn't a time."}
            if start == end:
                return {"ok": False, "error": "Pick a different end time; the same time means no quiet hours."}
        elif m.get("on"):
            last = ui_settings.load(self.ui_path).quiet_last or []
            start, end = (_hhmm(last[0]), _hhmm(last[1])) if len(last) >= 2 else (None, None)
            if start is None or end is None or start == end:
                start, end = self.QUIET_DEFAULT
        else:
            start = end = cur[0]
        if start != end:   # remembered, so off and on again brings these hours back
            ui_settings.update(self.ui_path, quiet_last=[start, end])
        elif cur[0] != cur[1]:
            ui_settings.update(self.ui_path, quiet_last=list(cur))
        path = settings_io.privacy_path()
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        new = settings_io.set_toml_key(settings_io.set_toml_key(text, "nudges", "quiet_start", start), "nudges", "quiet_end", end)
        table = tomllib.loads(new).get("nudges", {})   # never write a file the service could not read
        if (table.get("quiet_start"), table.get("quiet_end")) != (start, end):
            return {"ok": False, "error": "The privacy file didn't take the change."}
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(new, encoding="utf-8")
        os.replace(tmp, path)
        return {"quiet_start": start, "quiet_end": end}

    def a_pause(self, m: dict[str, Any]) -> dict[str, Any]:
        self._memory().set_paused(bool(m.get("paused")))
        self.refresh("shell")
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

    # -- Settings → AI (report 67) ---------------------------------------------------------------------------------

    def a_ai_role(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.agent.providers import summary

        provider, model = str(m.get("provider") or "default"), str(m.get("model") or "")
        backup = m.get("backup_provider")
        try:
            line = summary.set_role(str(m.get("role") or ""), provider, model,
                                    None if backup is None else str(backup), str(m.get("backup_model") or ""),
                                    price=summary.openrouter_price(model) if provider == "openrouter" else None)
        except ValueError as exc:
            return {"ok": False, "error": str(exc)[:1].upper() + str(exc)[1:]}
        return {"role": line, "ai": summary.page()}

    def a_ai_key(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.agent.providers import summary

        try:   # the key itself is never logged or echoed: the reply carries only where it now comes from
            status = summary.set_key(str(m.get("provider") or ""), str(m.get("key") or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        except Exception:  # noqa: BLE001 - DPAPI refused (another account's file): plain words
            return {"ok": False, "error": "The key couldn't be saved on this PC."}
        return {**status, "ai": summary.page()}

    def a_ai_custom(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.agent.providers import summary

        try:
            summary.set_custom(str(m.get("base_url") or ""), str(m.get("label") or ""))
        except ValueError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ai": summary.page()}

    def a_ai_models(self, m: dict[str, Any]) -> dict[str, Any]:
        """A service's models (network or the local Ollama, on this action's own thread)."""
        from yuki.agent.providers import discover

        provider = str(m.get("provider") or "")
        models = discover.list_models(provider, refresh=bool(m.get("refresh")))
        out: dict[str, Any] = {"models": discover.describe_list(provider, models) or []}
        if provider == "ollama":
            out["running"] = models is not None
            out["status"] = ("Ollama isn't running" if models is None else
                             f"{len(models)} model{'s' if len(models) != 1 else ''} on this PC" if models
                             else "Ollama is running but has no models yet")
        return out

    def a_ai_test(self, m: dict[str, Any]) -> dict[str, Any]:
        from yuki.agent.providers import discover

        works, words = discover.test(str(m.get("provider") or ""), str(m.get("model") or ""))
        return {"works": works, "words": words}

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
            if state in ("done", "failed") or (state == "none" and age > 8 * 86400):
                self.pending.pop(ref, None)
                if state in ("done", "failed") and p.get("kind") == "correct":   # the row says "Fixed" a while
                    # a corrected session is a new episode row (the old one superseded): its "Fixed" goes on the new
                    # ref, the one Day's rebuilt row has
                    shown_ref = self._current_episode_ref(ref) if state == "done" else ref
                    self.fixed[shown_ref] = {**p, "state": "fixed" if state == "done" else "failed",
                                             "done_at": time.time()}
                changed = True
            elif age > 600 and not p.get("late"):
                p["late"] = True
                changed = True
        if changed:
            self._save_pending()
            for page in ("me", "home", "work", "day"):
                self.refresh(page)

    def _current_episode_ref(self, ref: str) -> str:
        """``episode:N`` after a correction: memory supersedes the episode with a new row, so follow ``superseded_by``
        to the current one (a short read by id; any other ref, or one that can't be read, stays as it is)."""
        kind, _, num = ref.partition(":")
        by_ids = getattr(getattr(self.memory, "store", None), "episodes_by_ids", None)
        if kind != "episode" or not num.isdigit() or not callable(by_ids):
            return ref
        eid = int(num)
        for _ in range(8):   # a few corrections in a row at most
            try:
                rows = by_ids([eid])
            except Exception:  # noqa: BLE001
                return ref
            nxt = getattr(rows[0], "superseded_by", None) if rows else None
            if not nxt:
                break
            eid = int(nxt)
        return f"episode:{eid}"

    def shown_pending(self) -> dict[str, dict[str, Any]]:
        """The corrections the pages show on their rows: the ones waiting (``pending``, shared with the Qt window)
        and, for a while, the ones that landed (``fixed``, this window's own, never written to the shared file)."""
        cutoff = time.time() - 15 * 60
        for ref in [r for r, p in self.fixed.items() if float(p.get("done_at") or 0) < cutoff]:
            self.fixed.pop(ref, None)
        return {**self.fixed, **self.pending}

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
        if host is not None and self.probe_frame:
            threading.Timer(1.0, lambda: host.invoke(lambda: self._probe_frame(host))).start()
        if host is not None and self.exit_after is not None:
            threading.Timer(max(0.0, self.exit_after), host.close).start()
        if host is not None and self.memory_low_after is not None:
            def low() -> None:
                self.metrics["memory_low"] = {"ok": host.set_memory_low(True), "at": time.time()}
                self.write()
            threading.Timer(max(0.0, self.memory_low_after), lambda: host.invoke(low)).start()

    def _probe_frame(self, host: WebView2Host) -> None:
        """Measurement (``--probe-frame``, the off-screen window only): what Windows itself answers about the
        frameless frame - the style bits, the client area against the window, and for points over the page's caption
        buttons, its title bar, its top edge, the page and the left resize border: the window's own ``WM_NCHITTEST``
        and the window ``WindowFromPoint`` gives the pointer to (ours, or WebView2's). Sends no input. UI thread."""
        u = ctypes.windll.user32
        hwnd = wintypes.HWND(host.hwnd)
        win, cli = wintypes.RECT(), wintypes.RECT()
        u.GetWindowRect(hwnd, ctypes.byref(win))
        u.GetClientRect(hwnd, ctypes.byref(cli))
        origin = wintypes.POINT(0, 0)
        u.ClientToScreen(hwnd, ctypes.byref(origin))
        u.GetWindowLongW.restype = ctypes.c_long
        style = u.GetWindowLongW(hwnd, -16) & 0xFFFFFFFF
        u.WindowFromPoint.restype = wintypes.HWND
        u.WindowFromPoint.argtypes = [wintypes.POINT]
        u.SendMessageW.restype = ctypes.c_ssize_t
        u.SendMessageW.argtypes = [wintypes.HWND, ctypes.c_uint, wintypes.WPARAM, wintypes.LPARAM]
        u.GetClassNameW.argtypes = [wintypes.HWND, ctypes.c_wchar_p, ctypes.c_int]
        lay = host.caption
        points: dict[str, tuple[int, int]] = {f"{p}_button": (r[0] + r[2] // 2, r[1] + r[3] // 2) for p, r in lay.buttons.items()}
        points.update({"title_bar": (cli.right // 2, max(lay.top + 2, lay.height // 2)), "top_edge": (cli.right // 2, 1),
                       "page": (cli.right // 2, cli.bottom // 2)})
        out: dict[str, Any] = {}
        for name, (x, y) in points.items():
            sx, sy = origin.x + x, origin.y + y
            lp = ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)
            under = u.WindowFromPoint(wintypes.POINT(sx, sy))
            cls = ctypes.create_unicode_buffer(64)
            if under:
                u.GetClassNameW(under, cls, 64)
            out[name] = {"nchittest": int(u.SendMessageW(hwnd, 0x0084, 0, lp)),
                         "pointer_goes_to": "this window" if under and int(under) == host.hwnd else (cls.value or "none")}
        sx, sy = win.left + 2, (win.top + win.bottom) // 2   # the invisible left resize border
        out["left_border"] = {"nchittest": int(u.SendMessageW(hwnd, 0x0084, 0, ((sy & 0xFFFF) << 16) | (sx & 0xFFFF)))}
        self.metrics["frame"] = {
            "style": hex(style), "thick_frame": bool(style & 0x00040000), "caption_style": bool(style & 0x00C00000),
            "maximize_box": bool(style & 0x00010000), "window": [win.left, win.top, win.right - win.left, win.bottom - win.top],
            "client_origin": [origin.x - win.left, origin.y - win.top], "client": [cli.right, cli.bottom],
            "caption": {"h": lay.height, "top": lay.top, "buttons": {k: list(v) for k, v in lay.buttons.items()}},
            "non_client_regions": host.non_client_regions, "points": out}
        self.write()

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
        with self._cv:
            self._cv.notify_all()
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
        with self._log_lock:
            if self._logger is not None:
                self._logger.close()   # a late line from a thread finds it closed and is dropped (log)


def workflow_action(ref: str, verb: str, words: str = "") -> str | None:
    """The page's Save / Edit / No on a Playbook item as practice memory's ``workflow_edit`` action
    (``yuki.memory.practice.EDIT_ACTIONS``): on a playbook (``pb:N``) Save keeps it (``save``), Edit is the user's own
    words re-distilled (``edit``, words needed), No retires it (``forget``, its history kept); on a teach-back question
    (``question:N``) Save or Edit answers it in the user's words (``save``) and No skips it (``disable``). ``None`` for
    anything else."""
    question = str(ref or "").lower().startswith("question:")
    verb = str(verb or "").strip().lower()
    if verb == "no":
        return "disable" if question else "forget"
    if verb == "edit" or (question and verb == "save"):
        return "save" if question and words else ("edit" if words else None)
    if verb == "save":
        return "save"
    return None


def _hhmm(value: Any) -> str | None:
    """A clock time as the privacy file keeps it ("HH:MM", 24-hour), or ``None`` when it isn't one."""
    parts = str(value or "").strip().split(":")
    if len(parts) != 2 or not all(p.isdigit() and 1 <= len(p) <= 2 for p in parts):
        return None
    h, mi = int(parts[0]), int(parts[1])
    return f"{h:02d}:{mi:02d}" if 0 <= h < 24 and 0 <= mi < 60 else None


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
    ap.add_argument("--no-connect", action="store_true", help="never connect to the tray")
    ap.add_argument("--t0", type=float, default=None)
    ap.add_argument("--metrics", type=Path, default=None)
    ap.add_argument("--shot", type=Path, default=None)
    ap.add_argument("--shot-delay", type=float, default=0.0, help="take the shot this long after the paint")
    ap.add_argument("--exit-after", type=float, default=None)
    ap.add_argument("--memory-low-after", type=float, default=None)
    ap.add_argument("--dev-tools", action="store_true")
    ap.add_argument("--no-fallback", action="store_true", help="never open the Qt window instead")
    ap.add_argument("--probe-frame", action="store_true", help="measure the frameless frame (off-screen)")
    ap.add_argument("--splash", action="store_true",
                    help="play the startup splash (the tray asks on the first window of each Yuki run)")
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
                    exit_after=args.exit_after, memory_low_after=args.memory_low_after,
                    shot_delay=args.shot_delay, splash=bool(args.splash), sub=page_sub(args.page))
    win.probe_frame = bool(args.probe_frame and args.offscreen)
    win.start()
    query = "&".join(q for q in (args.query, splash_query(bool(args.splash))) if q)
    url = START_URL + (("?" + query) if query else "")
    options = HostOptions(
        folders={APP_HOST: WEB_DIR, FONT_HOST: FONTS_DIR}, start_url=url, icon=APP_ICON,
        user_data_dir=args.profile or default_profile(), placement="offscreen" if args.offscreen else "normal",
        browser_args=args.browser_args, dev_tools=args.dev_tools, on_message=win.on_message,
        on_event=win.on_host_event, on_shown=win.on_shown, marks={"main": t_main},
        rect=tuple(ui.web_window) if ui.web_window and not args.offscreen else None,
        maximized=bool(ui.web_maximized) and not args.offscreen)
    host = WebView2Host(options)
    win.host = host
    if not args.offscreen:
        watch_focus(win.come_forward)
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
