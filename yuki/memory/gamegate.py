"""The hard game gate (report 78 §4.5): knowing, at any moment, that a game is running.

A later capture phase (recording how you work, only once you say yes) must stop completely while a game runs:
in front or not, windowed or full screen, at the launch screen and on Alt+Tab. That rule is not a setting. This
module is the gate it will obey. In phase C0 it only **decided and logged** (content-free) so its decisions could be
checked for three days; nothing here records input or reads a window. From phase C1 the action trace
(:mod:`yuki.memory.trace`) obeys it: while it holds, the trace drops input at its first step, unhooks its per-app
window events and asks UI Automation nothing (zero rows, zero lookups). The learned set is kept in
``ui_game_processes`` (migration 38) with its evidence, and mirrored to the C0 ``nudge_state`` row.

The gate holds while any of these is true:

* **a game program runs** (in front or not). A game program is
  - one of the fixed safety list, ``[trace] game_processes`` in the packaged privacy defaults (games with kernel
    anti-cheat: VALORANT and the like), plus any the user adds to their own privacy file. The user can add, never
    remove: the packaged list always counts. It keys on the **game programs**, never on anti-cheat services:
    Vanguard's tray (``vgtray.exe``) runs on this PC all the time and would hold the gate forever;
  - or a program **seen full screen while drawing like a game**: its own share of the GPU's compute at least
    :data:`yuki.memory.avsignals.GAME_GPU_SHARE`, the same measured fact the call worker uses for a game's voice
    chat, in at least :data:`LEARN_AFTER_STRETCHES` separate full-screen stretches. The evidence (a count per
    program, with the last share) is kept in ``nudge_state`` (:data:`LEARNED_STATE`) and corrects itself: a later
    stretch measured full screen with a low share at every check takes one back, and at zero the program is
    forgotten, so one GPU-heavy moment of a music app's visualiser cannot make it a game for good. A full-screen
    app that does not draw like a game (FL Studio maximised without a caption, a video, a slide show) is not
    learned, and with no GPU reading nothing is learned at all;
* the shell reports **Direct3D exclusive full screen** (``QUNS_RUNNING_D3D_FULL_SCREEN``), read live by
  :meth:`GameGate.holds`;
* the call worker has a **game voice session** open or pending (``calls.game`` / ``calls._gaming``), read live.

**Fails closed.** A capture phase asks :func:`gate_holds` (``gate_holds(None)`` is True: a gate that failed to start
holds), and a started gate whose thread is gone (it died or was stopped) holds.

Lightweight, event-driven: one thread that sleeps in ``MsgWaitForMultipleObjectsEx`` on the foreground-change
WinEvent and on the handles of the game processes it knows are running (a game's exit wakes it). The process list
is read once at start and then at most every :data:`SCAN_EVERY_S`, and only when the foreground changes. While an
unknown program is full screen in front, its GPU share is read at most :data:`CHECKS_PER_STRETCH` times
(:data:`CHECK_AFTER_S`). No polling loop: D3D full screen and the game voice session, which no window event shows,
are re-read by :meth:`GameGate.beat` from the memory service's existing one-second loop, at most every
:data:`BEAT_EVERY_S`.

Log events (the memory log, content-free: process image names of measured programs only, flags, numbers):

* ``game_gate``: the decision changed (``hold``, ``reasons``, ``games`` running, ``front`` = the game in front or
  "", ``front_fullscreen``, ``trigger``: start / foreground / game_exit / check / d3d / game_voice / beat); written
  on every change, so Alt+Tab in and out of a game shows;
* ``game_gate_learned``: a program joined the learned set (``game``, ``gpu_share``, ``stretches``);
* ``game_gate_unlearned``: low stretches took a learned program out again (``app``, ``gpu_share``, ``stretches``);
* ``game_gate_evidence``: a stretch counted for or against a program without changing whether it is a game
  (``app``, ``gpu_share``, ``stretches``, ``step``);
* ``game_gate_not_game``: a full-screen program was measured and is not a game (``app``, ``gpu_share``, ``why``),
  once per program per run: the three-day check that FL Studio never counts reads these.

Public API::

    GameGate(store=None, privacy_path=None, log=None, game_session_fn=None).start()
        .holds() -> bool                 # the gate now (cheap, any thread; fails closed)
        .beat() -> Decision | None       # from an existing beat: re-read D3D and the voice session, log a change
        .decision() -> Decision          # why
        .stats() -> dict                 # counters, content-free
        .stop()
    gate_holds(gate_or_None) -> bool     # what capture must ask: None (no gate) holds
    decide(view, games) -> Decision      # the pure rule (tests use it with invented process data)
    judge_learn(app, fullscreen=, gpu_share=, own=) -> (learn, why)
    GameSet.load(privacy_path, learned)  # the fixed list (packaged + the user's additions) and the learned set
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import tomllib
from collections import Counter
from collections.abc import Callable, Iterable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuki.memory.avsignals import GAME_GPU_SHARE

#: ``nudge_state`` row holding the evidence per measured program (candidates and learned games):
#: ``{"<image stem>": {"stretches": n, "first_seen": ts, "last_seen": ts, "gpu_share": x}}``.
LEARNED_STATE = "game_gate:learned"

#: The process list is read again at most this often, and only on a foreground change (a game started without ever
#: coming to the front is caught within this, at the next window switch).
SCAN_EVERY_S = 300.0

#: An unknown program full screen in front: its GPU share is read this long after it came to the front (the share
#: is the mean of NVML's last 10 s, so a loading screen is not judged at once), at most this many times a stretch.
CHECK_AFTER_S = (20.0, 90.0)
CHECKS_PER_STRETCH = len(CHECK_AFTER_S)

#: Game handles waited on at once (``MsgWaitForMultipleObjectsEx`` takes at most 63 with the message queue).
MAX_HANDLES = 32

#: Shell notification state for Direct3D exclusive full screen (``SHQueryUserNotificationState``).
QUNS_RUNNING_D3D_FULL_SCREEN = 3

#: A program becomes a learned game only after this many separate full-screen stretches drew like a game (one
#: GPU-heavy moment of a music app's visualiser or a video editor is not enough), and each later stretch measured
#: full screen with a low share takes one back: at zero the program is forgotten. Evidence is capped so a game
#: played for months can still be corrected by a few low stretches.
LEARN_AFTER_STRETCHES = 2
EVIDENCE_CAP = 3

#: :meth:`GameGate.holds` reads Direct3D exclusive full screen live, reusing a reading this fresh (it may be asked
#: on every input event in a later phase).
D3D_FRESH_S = 1.0

#: :meth:`GameGate.beat` (called from the memory service's existing one-second loop) re-reads D3D full screen and the
#: game voice session at most this often, so a change the window events cannot see is logged too.
BEAT_EVERY_S = 5.0


def stem(image: str | None) -> str:
    """A process image name as the gate compares it: lower case, without ``.exe`` (``VALORANT.exe`` -> ``valorant``)."""
    name = " ".join(str(image or "").split()).lower()
    return name[:-4] if name.endswith(".exe") else name


def stretches(entry: object) -> int:
    """The evidence count of one learned-state entry (an entry from before counts were kept: 1, a candidate)."""
    if not isinstance(entry, dict) or not entry:
        return 0
    try:
        return max(0, int(entry.get("stretches", 1)))
    except (TypeError, ValueError):
        return 1


def confirmed(evidence: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """The learned games: entries with at least :data:`LEARN_AFTER_STRETCHES` stretches of evidence."""
    return {k: v for k, v in evidence.items() if stretches(v) >= LEARN_AFTER_STRETCHES}


def gate_holds(gate: GameGate | None) -> bool:
    """What a capture phase must ask: no gate (it failed to start) holds, so does a gate whose thread is gone."""
    return True if gate is None else gate.holds()


def _stems(values: object) -> frozenset[str]:
    if not isinstance(values, list):
        return frozenset()
    return frozenset(s for s in (stem(str(v)) for v in values) if s)


def _list_in(path: Path | None) -> frozenset[str]:
    if path is None:
        return frozenset()
    try:
        with Path(path).open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
        return frozenset()
    trace = data.get("trace") if isinstance(data.get("trace"), dict) else {}
    return _stems(trace.get("game_processes"))


@dataclass(frozen=True)
class GameSet:
    """Which programs are games: the fixed list (packaged defaults and the user's additions) and the learned set."""

    fixed: frozenset[str] = frozenset()
    learned: dict[str, dict[str, Any]] = field(default_factory=dict)

    @classmethod
    def load(cls, privacy_path: Path | None = None, learned: dict[str, dict[str, Any]] | None = None) -> GameSet:
        from yuki.memory.privacy import DEFAULT_PATH

        return cls(fixed=_list_in(DEFAULT_PATH) | _list_in(privacy_path), learned=dict(learned or {}))

    def source(self, app: str) -> str | None:
        """``"list"``, ``"learned"`` or ``None`` (not a game)."""
        key = stem(app)
        if not key:
            return None
        if key in self.fixed:
            return "list"
        if key in self.learned:
            return "learned"
        return None

    def is_game(self, app: str) -> bool:
        return self.source(app) is not None


@dataclass(frozen=True)
class GateView:
    """What the gate sees at one moment (invented in tests, measured by :class:`GameGate`)."""

    front: str = ""                     # the image name of the program in front ("" unknown)
    front_fullscreen: bool = False      # the window in front is full screen (``watcher.is_fullscreen_front``)
    d3d_fullscreen: bool = False        # Direct3D exclusive full screen (the shell's own report)
    running: frozenset[str] = frozenset()   # image names of the programs running that the gate looked at
    game_session: bool = False          # the call worker has a game voice session open or pending


@dataclass(frozen=True)
class Decision:
    hold: bool = False
    reasons: tuple[str, ...] = ()       # "game_running", "d3d_fullscreen", "game_voice"
    games: tuple[str, ...] = ()         # game programs running (stems), sorted
    front: str = ""                     # the game in front (stem), "" when the program in front is not a game
    front_fullscreen: bool = False
    sources: tuple[str, ...] = ()       # "list" / "learned", per game in ``games``

    def key(self) -> tuple:
        return (self.hold, self.reasons, self.games, self.front, self.front_fullscreen)

    def facts(self) -> dict[str, Any]:
        return {"hold": self.hold, "reasons": list(self.reasons), "games": list(self.games), "front": self.front,
                "front_fullscreen": self.front_fullscreen, "sources": list(self.sources)}


def decide(view: GateView, games: GameSet) -> Decision:
    """The gate's rule: hold while a game program runs (in front or not), in D3D exclusive full screen, or while a
    game voice session is open. Nothing else counts: an anti-cheat service, a full-screen app that is not a game."""
    running = sorted({stem(p) for p in (*view.running, view.front) if games.is_game(p)})
    reasons = []
    if running:
        reasons.append("game_running")
    if view.d3d_fullscreen:
        reasons.append("d3d_fullscreen")
    if view.game_session:
        reasons.append("game_voice")
    front = stem(view.front) if games.is_game(view.front) else ""
    return Decision(hold=bool(reasons), reasons=tuple(reasons), games=tuple(running), front=front,
                    front_fullscreen=bool(view.front_fullscreen),
                    sources=tuple(games.source(g) or "" for g in running))


def judge_learn(app: str, *, fullscreen: bool, gpu_share: float | None, own: bool = False) -> tuple[bool, str]:
    """Whether a program in front joins the learned game set, and why (a content-free code).

    Only a measured fact: full screen AND its own GPU compute share at least ``GAME_GPU_SHARE``. Without a GPU
    reading nothing is learned (unlike the call worker's voice-chat test, which then assumes a game): a wrong entry
    here would hold the gate for that program for good."""
    if own or not stem(app):
        return False, "own_or_unknown"
    if not fullscreen:
        return False, "not_fullscreen"
    if gpu_share is None:
        return False, "no_gpu_reading"
    if gpu_share < GAME_GPU_SHARE:
        return False, "low_gpu"
    return True, "draws_like_a_game"


# ---------------------------------------------------------------------------
# The gate's thread
# ---------------------------------------------------------------------------

_EVENT_SYSTEM_FOREGROUND = 0x0003
_WINEVENT_OUTOFCONTEXT = 0x0000
_WINEVENT_SKIPOWNPROCESS = 0x0002
_QS_ALLINPUT = 0x04FF
_MWMO_INPUTAVAILABLE = 0x0004
_PM_REMOVE = 0x0001
_WM_QUIT = 0x0012
_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102
_INFINITE = 0xFFFFFFFF
_SYNCHRONIZE = 0x00100000
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_GA_ROOT = 2


def _default_fullscreen(hwnd: int) -> bool:
    from yuki.memory.watcher import is_fullscreen_front

    return bool(is_fullscreen_front(hwnd))


def _default_d3d() -> bool:
    from yuki.memory.watcher import notification_state

    return notification_state() == QUNS_RUNNING_D3D_FULL_SCREEN


def _default_gpu(pids: Iterable[int]) -> float | None:
    from yuki.memory.avsignals import gpu_share

    return gpu_share(pids)


def _default_scan() -> list[tuple[int, str]]:
    """``(pid, image name)`` of every process (one read of the process list)."""
    import psutil

    out = []
    for proc in psutil.process_iter(["name"]):
        name = proc.info.get("name") or ""
        if name:
            out.append((int(proc.pid), str(name)))
    return out


def _default_image(pid: int) -> str:
    import psutil

    try:
        return str(psutil.Process(int(pid)).name() or "")
    except Exception:  # noqa: BLE001 - gone or not ours to read
        return ""


def _default_own(pid: int) -> bool:
    try:
        from yuki.memory.watcher import _ProcessCache

        return bool(_ProcessCache().get(int(pid)).is_own)
    except Exception:  # noqa: BLE001
        return int(pid) == os.getpid()


class GameGate:
    """The gate, kept current by its own event-driven thread (see the module docstring)."""

    def __init__(
        self,
        *,
        store: Any = None,
        privacy_path: Path | None = None,
        log: Callable[..., None] | None = None,
        game_session_fn: Callable[[], bool] | None = None,
        fullscreen_fn: Callable[[int], bool] = _default_fullscreen,
        d3d_fn: Callable[[], bool] = _default_d3d,
        gpu_fn: Callable[[Iterable[int]], float | None] = _default_gpu,
        scan_fn: Callable[[], list[tuple[int, str]]] = _default_scan,
        image_fn: Callable[[int], str] = _default_image,
        own_fn: Callable[[int], bool] = _default_own,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._privacy_path = Path(privacy_path) if privacy_path else None
        self._log_fn = log
        self._game_session_fn = game_session_fn
        self._fullscreen_fn = fullscreen_fn
        self._d3d_fn = d3d_fn
        self._gpu_fn = gpu_fn
        self._scan_fn = scan_fn
        self._image_fn = image_fn
        self._own_fn = own_fn
        self._clock = clock
        self._lock = threading.RLock()
        self._evidence = self._read_learned()           # every measured program: candidates and learned games
        self._games = GameSet.load(self._privacy_path, confirmed(self._evidence))
        self._privacy_stamp = self._stamp()
        self._d3d_live: tuple[float, bool] | None = None   # (when read, value) for holds()
        self._last_beat = float("-inf")
        self._running: dict[int, str] = {}       # pid -> image name, game programs known to be running
        self._handles: dict[int, int] = {}       # pid -> process handle (SYNCHRONIZE), waited on by the thread
        self._front = ""
        self._front_pid = 0
        self._front_hwnd = 0
        self._front_fullscreen = False
        self._d3d = False
        self._stretch: tuple[int, int] | None = None   # (hwnd, pid) of the unknown full-screen program in front
        self._checks: list[float] = []                  # when its GPU share is read next (monotonic)
        self._not_game: set[str] = set()                # logged once per program per run
        self._last_scan = float("-inf")
        self._decision = Decision()
        self._held_since: float | None = None
        self._held_total = 0.0
        self.counters: Counter[str] = Counter()
        self._thread: threading.Thread | None = None
        self._thread_id = 0
        self._ready = threading.Event()
        self._start_error: BaseException | None = None

    # -- reading ------------------------------------------------------------------------------------------------

    def holds(self) -> bool:
        """Whether the gate holds now: a game program running, D3D exclusive full screen or a game voice session.

        Fails closed: once started, a gate whose thread is gone (it stopped or died) holds. The game voice session
        and D3D full screen are read live (D3D reused for :data:`D3D_FRESH_S`), as neither changes the window in
        front; the thread's last decision covers game programs."""
        if self._thread is not None and not self._thread.is_alive():
            return True
        with self._lock:
            if self._decision.hold:
                return True
        return self._session() or self._d3d_now()

    def beat(self) -> Decision | None:
        """Called from an existing beat (the memory service's loop): at most every :data:`BEAT_EVERY_S`, re-read
        D3D full screen and the game voice session and log a change no window event showed. ``None``: not due."""
        now = self._clock()
        with self._lock:
            if now - self._last_beat < BEAT_EVERY_S:
                return None
            self._last_beat = now
        d3d = bool(self._safe(self._d3d_fn, False))
        session = self._session()
        with self._lock:
            self._d3d_live = (now, d3d)
            if session != ("game_voice" in self._decision.reasons):
                trigger = "game_voice"
            elif d3d != self._d3d:
                trigger = "d3d"
            else:
                trigger = "beat"
            self._d3d = d3d
            self.counters["beats"] += 1
            return self._evaluate(trigger, session=session)

    def decision(self) -> Decision:
        with self._lock:
            return self._decision

    def stats(self) -> dict[str, Any]:
        with self._lock:
            held = self._held_total
            if self._held_since is not None:
                held += self._clock() - self._held_since
            return {**dict(self.counters), "hold": self._decision.hold, "games_running": len(self._running),
                    "learned": len(self._games.learned),
                    "candidates": len(self._evidence) - len(self._games.learned), "held_s": round(held, 1)}

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- the rule, fed by events (the thread calls these; tests call them directly) ------------------------------

    def on_foreground(self, hwnd: int, pid: int, *, image: str | None = None, trigger: str = "foreground") -> Decision:
        """The window in front changed (or the thread started): note it, track a game program, plan GPU checks."""
        now = self._clock()
        image = image if image is not None else (self._image_fn(pid) if pid else "")
        with self._lock:
            self._reload_if_changed()
            self.counters["foreground"] += 1
            self._front, self._front_pid, self._front_hwnd = image, int(pid or 0), int(hwnd or 0)
            self._front_fullscreen = bool(hwnd) and self._safe(self._fullscreen_fn, False, hwnd)
            self._d3d = bool(self._safe(self._d3d_fn, False))
            self._d3d_live = (now, self._d3d)
            if pid and self._games.is_game(image):
                self._track(int(pid), image)
            if now - self._last_scan >= SCAN_EVERY_S:
                self._scan(now)
            else:
                self._drop_exited()
            stretch = (self._front_hwnd, self._front_pid)
            # measured: a program not on the fixed list (an unknown one may be learned, a learned one un-learned)
            if self._front_fullscreen and pid and self._games.source(image) != "list":
                if self._stretch != stretch:
                    self._stretch = stretch
                    self._checks = [now + s for s in CHECK_AFTER_S]
            else:
                self._stretch, self._checks = None, []
            return self._evaluate(trigger)

    def on_exit(self, pid: int) -> Decision:
        """A game program exited (its handle was signalled)."""
        with self._lock:
            self._untrack(int(pid))
            self.counters["game_exits"] += 1
            return self._evaluate("game_exit")

    def on_check(self) -> Decision:
        """A planned GPU check of the full-screen program in front (not on the fixed list): a stretch that draws like
        a game adds one to its evidence (learned at :data:`LEARN_AFTER_STRETCHES`); a stretch measured low at every
        check takes one away (forgotten at zero)."""
        now = self._clock()
        with self._lock:
            if self._checks and self._checks[0] <= now:
                self._checks.pop(0)
            stretch = self._stretch
            if stretch is None:
                self._checks = []
                return self._evaluate("check")
            hwnd, pid = stretch
            fullscreen = bool(self._safe(self._fullscreen_fn, False, hwnd)) and hwnd == self._front_hwnd
            self._d3d = bool(self._safe(self._d3d_fn, False))
            image = self._front
        own = bool(self._safe(self._own_fn, False, pid))
        share = self._safe(self._gpu_fn, None, [pid]) if fullscreen and not own else None
        learn, why = judge_learn(image, fullscreen=fullscreen, gpu_share=share, own=own)
        with self._lock:
            self.counters["gpu_checks"] += 1
            key = stem(image)
            if learn:
                # one stretch of evidence (a stretch is judged once: its remaining checks are dropped)
                self._evidence_step(image, share, +1)
                if self._games.is_game(image):
                    self._track(pid, image)
                self._stretch, self._checks = None, []
            elif not self._checks and why in ("low_gpu", "no_gpu_reading"):
                # the last check of the stretch and none drew like a game
                if why == "low_gpu" and key in self._evidence:
                    self._evidence_step(image, share, -1)
                elif key and key not in self._evidence and key not in self._not_game:
                    self._not_game.add(key)   # said once per program per run
                    self._log("game_gate_not_game", app=key, why=why,
                              gpu_share=None if share is None else round(float(share), 1))
            if not fullscreen:
                self._stretch, self._checks = None, []
            return self._evaluate("check")

    def next_check_in(self) -> float | None:
        """Seconds until the next planned GPU check (``None``: none planned)."""
        with self._lock:
            if not self._checks:
                return None
            return max(0.0, self._checks[0] - self._clock())

    # -- internals ----------------------------------------------------------------------------------------------

    def _session(self) -> bool:
        if self._game_session_fn is None:
            return False
        return bool(self._safe(self._game_session_fn, False))

    def _d3d_now(self) -> bool:
        now = self._clock()
        with self._lock:
            cached = self._d3d_live
            if cached is not None and now - cached[0] < D3D_FRESH_S:
                return cached[1]
        value = bool(self._safe(self._d3d_fn, False))
        with self._lock:
            self._d3d_live = (now, value)
        return value

    @staticmethod
    def _safe(fn: Callable[..., Any], default: Any, *args: Any) -> Any:
        try:
            return fn(*args)
        except Exception:  # noqa: BLE001 - a failed reading is "not known", never a crash of the gate
            return default

    def _stamp(self) -> tuple[int, int] | None:
        try:
            st = self._privacy_path.stat() if self._privacy_path else None
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size) if st else None

    def _reload_if_changed(self) -> None:
        stamp = self._stamp()
        if stamp != self._privacy_stamp:
            self._privacy_stamp = stamp
            self._games = GameSet.load(self._privacy_path, self._games.learned)
            self.counters["list_reloads"] += 1

    def _read_learned(self) -> dict[str, dict[str, Any]]:
        if self._store is None:
            return {}
        if hasattr(self._store, "_write"):
            # capC1: the learned set lives in ui_game_processes (migration 38), with its evidence; the C0 row in
            # nudge_state is read once when the table is still empty
            try:
                from yuki.memory.trace_store import load_game_evidence

                table = load_game_evidence(self._store)
                if table:
                    return {stem(k): v for k, v in table.items() if stem(k) and isinstance(v, dict)}
            except Exception:  # noqa: BLE001
                pass
        try:
            raw = self._store.nudge_state(LEARNED_STATE)
            data = json.loads(raw) if raw else {}
        except Exception:  # noqa: BLE001
            return {}
        return {stem(k): v for k, v in data.items() if stem(k) and isinstance(v, dict)} if isinstance(data, dict) else {}

    def _evidence_step(self, image: str, share: float | None, delta: int) -> None:
        """One stretch of evidence for (+1) or against (-1) a program being a game; kept with the numbers."""
        key = stem(image)
        was_game = key in self._games.learned
        evidence = dict(self._evidence)
        entry = dict(evidence.get(key) or {})
        count = min(EVIDENCE_CAP, max(0, stretches(entry) + delta))
        gpu = None if share is None else round(float(share), 1)
        if count <= 0:
            evidence.pop(key, None)
        else:
            entry.setdefault("first_seen", round(time.time(), 1))
            entry.update({"stretches": count, "gpu_share": gpu, "last_seen": round(time.time(), 1),
                          "evidence": "fullscreen_gpu"})
            evidence[key] = entry
        self._evidence = evidence
        self._games = GameSet(fixed=self._games.fixed, learned=confirmed(evidence))
        is_game = key in self._games.learned
        if is_game and not was_game:
            self.counters["learned"] += 1
            self._log("game_gate_learned", game=key, gpu_share=gpu, stretches=count)
        elif was_game and not is_game:
            self.counters["unlearned"] += 1
            self._log("game_gate_unlearned", app=key, gpu_share=gpu, stretches=count)
            for pid in [p for p, name in self._running.items() if stem(name) == key]:
                self._untrack(pid)
        else:
            self.counters["evidence"] += 1
            self._log("game_gate_evidence", app=key, gpu_share=gpu, stretches=count, step=delta)
        if self._store is not None:
            try:
                self._store.set_nudge_state(LEARNED_STATE, json.dumps(evidence, sort_keys=True))
                if hasattr(self._store, "_write"):
                    from yuki.memory.trace_store import save_game_evidence

                    save_game_evidence(self._store, evidence)     # ui_game_processes (capC1), with its evidence
            except Exception as exc:  # noqa: BLE001 - kept in memory for this run
                self._log("error", where="game_gate_store", error=type(exc).__name__)

    def _track(self, pid: int, image: str) -> None:
        if pid in self._running:
            return
        self._running[pid] = image
        handle = _open_wait_handle(pid) if self._thread is not None else 0
        if handle:
            if len(self._handles) < MAX_HANDLES:
                self._handles[pid] = handle
            else:
                _close(handle)

    def _untrack(self, pid: int) -> None:
        self._running.pop(pid, None)
        handle = self._handles.pop(pid, 0)
        if handle:
            _close(handle)

    def _drop_exited(self) -> None:
        """Games tracked without a handle to wait on (Windows refused one): checked on a window switch."""
        for pid in [p for p in self._running if p not in self._handles]:
            if stem(self._safe(self._image_fn, "", pid)) != stem(self._running[pid]):
                self._untrack(pid)

    def _scan(self, now: float) -> None:
        self._last_scan = now
        self.counters["scans"] += 1
        seen = self._safe(self._scan_fn, None)
        if seen is None:
            return
        alive = set()
        for pid, image in seen:
            if self._games.is_game(image):
                alive.add(int(pid))
                self._track(int(pid), image)
        for pid in [p for p in self._running if p not in alive]:
            self._untrack(pid)   # gone (a handle we could not open is only checked here)

    def _evaluate(self, trigger: str, *, session: bool | None = None) -> Decision:
        view = GateView(front=self._front, front_fullscreen=self._front_fullscreen, d3d_fullscreen=self._d3d,
                        running=frozenset(self._running.values()),
                        game_session=self._session() if session is None else session)
        decision = decide(view, self._games)
        if decision.key() != self._decision.key():
            now = self._clock()
            if decision.hold and self._held_since is None:
                self._held_since = now
                self.counters["holds"] += 1
            elif not decision.hold and self._held_since is not None:
                self._held_total += now - self._held_since
                self._held_since = None
            self.counters["changes"] += 1
            self._log("game_gate", trigger=trigger, **decision.facts())
        self._decision = decision
        return decision

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:  # noqa: BLE001
            pass

    # -- the thread ---------------------------------------------------------------------------------------------

    def start(self, timeout_s: float = 10.0) -> GameGate:
        self._thread = threading.Thread(target=self._main, name="yuki-memory-gamegate", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout_s):
            raise TimeoutError("game gate thread did not start")
        if self._start_error is not None:
            raise self._start_error
        return self

    def stop(self, timeout_s: float = 5.0) -> None:
        if self._thread_id:
            _U.PostThreadMessageW(self._thread_id, _WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(timeout_s)

    def _main(self) -> None:
        msg = wintypes.MSG()
        hook = None
        try:
            self._thread_id = int(_K.GetCurrentThreadId())
            _U.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)   # make this thread's message queue now
            self._callback = _WINEVENTPROC(self._on_winevent)
            hook = _U.SetWinEventHook(_EVENT_SYSTEM_FOREGROUND, _EVENT_SYSTEM_FOREGROUND, None, self._callback, 0, 0,
                                      _WINEVENT_OUTOFCONTEXT | _WINEVENT_SKIPOWNPROCESS)
            if not hook:
                raise OSError(ctypes.get_last_error(), "SetWinEventHook(EVENT_SYSTEM_FOREGROUND) failed")
            self._foreground_now("start")
            self._log("game_gate_start", fixed=len(self._games.fixed), learned=len(self._games.learned),
                      decision=self._decision.facts())
        except BaseException as exc:
            self._start_error = exc
            self._ready.set()
            if hook:
                _U.UnhookWinEvent(hook)
            return
        self._ready.set()
        try:
            while True:
                with self._lock:
                    pids = list(self._handles)
                    handles = (wintypes.HANDLE * max(1, len(pids)))(*[self._handles[p] for p in pids])
                wait = self.next_check_in()
                timeout = _INFINITE if wait is None else int(wait * 1000) + 1
                result = _U.MsgWaitForMultipleObjectsEx(len(pids), ctypes.cast(handles, ctypes.c_void_p) if pids
                                                        else None, timeout, _QS_ALLINPUT, _MWMO_INPUTAVAILABLE)
                if _WAIT_OBJECT_0 <= result < _WAIT_OBJECT_0 + len(pids):
                    self.on_exit(pids[result - _WAIT_OBJECT_0])
                    continue
                quit_seen = False
                while _U.PeekMessageW(ctypes.byref(msg), None, 0, 0, _PM_REMOVE):
                    if msg.message == _WM_QUIT:
                        quit_seen = True
                        break
                    _U.TranslateMessage(ctypes.byref(msg))
                    _U.DispatchMessageW(ctypes.byref(msg))
                if quit_seen:
                    break
                due = self.next_check_in()
                if due is not None and due <= 0:
                    self.on_check()
        except Exception as exc:  # noqa: BLE001
            self._log("error", where="game_gate", error=type(exc).__name__)
        finally:
            _U.UnhookWinEvent(hook)
            with self._lock:
                for pid in list(self._handles):
                    _close(self._handles.pop(pid))
            self._log("game_gate_stop", stats=self.stats())

    def _on_winevent(self, hook, event, hwnd, id_object, id_child, thread, ms) -> None:   # noqa: ANN001
        try:
            if event == _EVENT_SYSTEM_FOREGROUND and id_object == 0:
                self._foreground_now("foreground", int(hwnd or 0))
        except Exception as exc:  # noqa: BLE001 - a raising callback must not take the thread down
            self._log("error", where="game_gate_foreground", error=type(exc).__name__)

    def _foreground_now(self, trigger: str, hwnd: int | None = None) -> None:
        hwnd = int(hwnd if hwnd is not None else (_U.GetForegroundWindow() or 0))
        if hwnd:
            hwnd = int(_U.GetAncestor(hwnd, _GA_ROOT) or hwnd)
        pid = wintypes.DWORD(0)
        if hwnd:
            _U.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        self.on_foreground(hwnd, int(pid.value), trigger=trigger)


# Private user32/kernel32 handles, so the signatures set here never collide with other modules' ``ctypes.windll``.
_U = ctypes.WinDLL("user32", use_last_error=True)
_K = ctypes.WinDLL("kernel32", use_last_error=True)
_WINEVENTPROC = ctypes.WINFUNCTYPE(None, wintypes.HANDLE, wintypes.DWORD, wintypes.HWND, wintypes.LONG,
                                   wintypes.LONG, wintypes.DWORD, wintypes.DWORD)
_U.SetWinEventHook.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.HMODULE, _WINEVENTPROC, wintypes.DWORD,
                               wintypes.DWORD, wintypes.DWORD]
_U.SetWinEventHook.restype = wintypes.HANDLE
_U.UnhookWinEvent.argtypes = [wintypes.HANDLE]
_U.UnhookWinEvent.restype = wintypes.BOOL
_U.MsgWaitForMultipleObjectsEx.argtypes = [wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                           wintypes.DWORD]
_U.MsgWaitForMultipleObjectsEx.restype = wintypes.DWORD
_U.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT]
_U.PeekMessageW.restype = wintypes.BOOL
_U.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
_U.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
_U.DispatchMessageW.restype = ctypes.c_ssize_t
_U.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
_U.PostThreadMessageW.restype = wintypes.BOOL
_U.GetForegroundWindow.restype = wintypes.HWND
_U.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
_U.GetAncestor.restype = wintypes.HWND
_U.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
_U.GetWindowThreadProcessId.restype = wintypes.DWORD
_K.GetCurrentThreadId.restype = wintypes.DWORD
_K.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
_K.OpenProcess.restype = wintypes.HANDLE
_K.CloseHandle.argtypes = [wintypes.HANDLE]
_K.CloseHandle.restype = wintypes.BOOL


def _open_wait_handle(pid: int) -> int:
    """A handle that is signalled when the process exits (0 when it cannot be opened: then a scan notices)."""
    try:
        return int(_K.OpenProcess(_SYNCHRONIZE | _PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid)) or 0)
    except OSError:
        return 0


def _close(handle: int) -> None:
    try:
        _K.CloseHandle(handle)
    except OSError:
        pass


__all__ = ["GameGate", "GameSet", "GateView", "Decision", "decide", "judge_learn", "stem", "LEARNED_STATE",
           "SCAN_EVERY_S", "CHECK_AFTER_S", "LEARN_AFTER_STRETCHES", "EVIDENCE_CAP", "BEAT_EVERY_S", "D3D_FRESH_S",
           "gate_holds", "confirmed", "stretches"]
