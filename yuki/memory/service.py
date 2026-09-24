"""``yuki-memory``: the background memory process (docs/MEMORY.md, "Processes").

Runs the watcher (:mod:`yuki.memory.watcher`), the timeline recorder
(:mod:`yuki.memory.timeline`), the journal worker (:mod:`yuki.memory.journal`),
the Warp terminal-history reader (:mod:`yuki.memory.warp`, every 3 minutes),
the conversation worker (:mod:`yuki.memory.conversations`: rules, preferences,
commitments and journal facts from Yuki's own exchanges, and session summaries;
woken by Yuki's named turn event), the episode worker (:mod:`yuki.memory.episodes`), the coach
(:mod:`yuki.memory.nudges`: check-ins and reminders, written as nudges for the UI, which it wakes
with the named event ``Local\\YukiNudgeReady``) and the portrait scheduler
(:mod:`yuki.memory.portrait`) against one shared :class:`~yuki.memory.store.Store`
(the journal worker waits on the store's own "new capture" condition, which only
fires within one Store instance).  It is its own process so that nothing here
can take Yuki down, runs at below-normal priority, and keeps one instance per
session and database (named mutex).

Flag files next to the database (written by Yuki through
:class:`yuki.memory.api.MemoryClient`), checked about once a second:

* ``paused`` - while it exists the watcher captures nothing (one ``paused``
  event), the timeline records nothing, the Warp reader moves past new
  commands without reading them, and no scheduled portrait or episode run
  starts.
* ``refresh_portrait`` - wakes the portrait scheduler, which deletes it and
  rebuilds the portrait now.
* ``run_weekly_review`` - wakes the weekly-review scheduler (:mod:`yuki.memory.review`,
  thread ``yuki-memory-review``), which deletes it and writes a review of the last
  seven days now; the scheduled one runs Sunday evening (``[review]``).
* ``acting.json`` - Yuki is acting on the desktop for the user
  (:mod:`yuki.memory.acting`). Read when Yuki sets the named event
  ``Local\\YukiMemoryActing-<db digest>`` (this process creates it), and at
  least every 2 s; while it names a live request, the watcher's captures and
  the timeline's stretches are marked ``by_yuki`` with the request's text.

Stopping: Ctrl+C / Ctrl+Break / closing the console, ``WM_CLOSE`` to the
watcher's hidden window (class :data:`yuki.memory.watcher.WINDOW_CLASS`; this is
how the tray app stops it), end of the Windows session, or ``--duration``.

Logs: ``logs/memory-<YYYYMMDD-HHMMSS>.jsonl``, content-free - apps, triggers,
outcomes, sizes, timings, process CPU and memory - never captured text, window
titles or URLs.  The journal worker keeps its own log (``logs/memory/``).

Usage::

    uv run yuki-memory                     # the real store, journal on
    uv run yuki-memory --verbose           # one console line per capture
    uv run yuki-memory --db %TEMP%\\m\\memory.db --no-journal --duration 90
    uv run yuki-memory --no-portrait        # watcher + journal only (timeline and episodes still run)
    uv run yuki-memory --no-timeline        # no foreground timeline (and so no episodes)
    uv run yuki-memory --no-terminal        # do not read Warp's command history
    uv run yuki-memory --no-conversations   # do not extract memory from Yuki's own conversations
    uv run yuki-memory --no-nudges          # no coach: no check-ins, no reminders
    uv run yuki-memory --no-review          # no weekly review
"""

from __future__ import annotations

import argparse
import ctypes
import signal
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import psutil

from yuki.log.events import SessionLogger
from yuki.memory.privacy import PrivacyConfig
from yuki.memory.watcher import Watcher, WatcherSettings

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_ERROR_ALREADY_EXISTS = 183
#: Captures older than this are deleted (docs/MEMORY.md: captures TTL 30 days).
CAPTURE_TTL_DAYS = 30.0
_PRUNE_EVERY_S = 6 * 3600.0


class _Log:
    """Thread-safe front for :class:`SessionLogger` plus an optional console line."""

    def __init__(self, logger: SessionLogger, *, verbose: bool) -> None:
        self._logger = logger
        self._lock = threading.Lock()
        self.verbose = verbose

    def __call__(self, type: str, **fields: Any) -> None:
        with self._lock:
            self._logger.log(type, **fields)
            if self.verbose:
                print(_console_line(type, fields), flush=True)

    def close(self) -> None:
        with self._lock:
            self._logger.close()

    @property
    def path(self) -> Path:
        return self._logger.path


def _console_line(type: str, f: dict[str, Any]) -> str:
    stamp = datetime.now().strftime("%H:%M:%S")
    if type == "capture":
        extra = f" +{f.get('delta_chars', 0)}" if f.get("outcome") == "captured" else ""
        reason = f" ({f['reason']})" if f.get("reason") else ""
        msgs = ""
        if f.get("kind") in ("conversation", "email"):
            msgs = f" msgs {f.get('messages', 0)} new {f.get('new_messages', 0)} hist {f.get('history_messages', 0)}"
        return (
            f"{stamp} {f.get('trigger', ''):<10} {f.get('app', '') or '-':<22.22} "
            f"{f.get('outcome', ''):<12}{reason} {f.get('chars', 0)} chars{extra}{msgs} "
            f"-{f.get('dropped_chars', 0)} chrome {f.get('ms', 0):.0f} ms "
            f"[{f.get('profile', '')}/{f.get('kind', '')}]"
        )
    if type == "stats":
        lat = f.get("watcher", {}).get("latency_ms", {})
        return (
            f"{stamp} stats  cpu {f.get('cpu_percent_total')}% (one core {f.get('cpu_percent_one_core')}%)"
            f"  rss {f.get('rss_mb')} MB  attempts/min {f.get('watcher', {}).get('attempts_per_min')}"
            f"  p50 {lat.get('p50')} ms  p90 {lat.get('p90')} ms"
        )
    if type == "error":
        return f"{stamp} error  {f.get('where', '')}: {f.get('error', '')}"
    brief = {k: v for k, v in f.items() if k not in ("traceback", "settings", "stats")}
    return f"{stamp} {type}  {brief}"


def _claim_mutex(db_path: Path) -> int | None:
    """One service per session and database; returns the handle to hold, or None.

    The name comes from :func:`yuki.memory.store.service_mutex_name`, which
    :func:`yuki.memory.api.service_running` also uses to see the service.
    """
    from yuki.memory.store import service_mutex_name

    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, service_mutex_name(db_path))
    if not handle:
        return None
    if kernel32.GetLastError() == _ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return int(handle)


class _ProcessMeter:
    """CPU and memory of this process, between successive readings."""

    def __init__(self) -> None:
        self._proc = psutil.Process()
        self._cpus = psutil.cpu_count() or 1
        self._t0 = time.perf_counter()
        self._c0 = self._cpu()
        self._start_t, self._start_c = self._t0, self._c0

    def _cpu(self) -> float:
        times = self._proc.cpu_times()
        return times.user + times.system

    def reading(self, *, since_start: bool = False) -> dict[str, Any]:
        now, cpu = time.perf_counter(), self._cpu()
        t0, c0 = (self._start_t, self._start_c) if since_start else (self._t0, self._c0)
        if not since_start:
            self._t0, self._c0 = now, cpu
        wall = max(now - t0, 1e-9)
        one_core = 100.0 * (cpu - c0) / wall
        mem = self._proc.memory_info()
        return {
            "window_s": round(wall, 1),
            "cpu_s": round(cpu - c0, 3),
            "cpu_percent_one_core": round(one_core, 2),
            "cpu_percent_total": round(one_core / self._cpus, 2),
            "rss_mb": round(mem.rss / 2**20, 1),
            "peak_mb": round(getattr(mem, "peak_wset", mem.rss) / 2**20, 1),
            "threads": self._proc.num_threads(),
        }


def _set_below_normal() -> None:
    try:
        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except (psutil.Error, OSError):
        pass


def _install_stop_handlers(stop: threading.Event) -> Callable[[], None]:
    """Ctrl+C, Ctrl+Break and console close all set ``stop``."""

    def on_signal(signum: int, frame: object) -> None:
        stop.set()

    signal.signal(signal.SIGINT, on_signal)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, on_signal)

    handler_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_uint)

    def on_console(event: int) -> int:
        # CTRL_CLOSE_EVENT(2), LOGOFF(5), SHUTDOWN(6): the console is going away.
        if event in (2, 5, 6):
            stop.set()
            return 1
        return 0  # Ctrl+C / Ctrl+Break: let Python's signal handlers run

    callback = handler_type(on_console)
    ctypes.windll.kernel32.SetConsoleCtrlHandler(callback, True)
    return lambda: callback  # keeps the callback alive for the process lifetime


def _parse(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="yuki-memory", description="Yuki's memory process.")
    parser.add_argument("--db", type=Path, default=None, help="database path (default: %%LOCALAPPDATA%%\\Yuki\\memory\\memory.db)")
    parser.add_argument("--privacy", type=Path, default=None, help="privacy rules file (default: %%LOCALAPPDATA%%\\Yuki\\memory\\privacy.toml)")
    parser.add_argument("--no-journal", action="store_true", help="capture only; no journal worker, no portrait")
    parser.add_argument("--no-portrait", action="store_true", help="do not run the portrait scheduler")
    parser.add_argument("--no-timeline", action="store_true", help="do not record the foreground timeline")
    parser.add_argument("--no-episodes", action="store_true", help="do not write episodes from the timeline")
    parser.add_argument("--no-terminal", action="store_true", help="do not read the Warp terminal's command history")
    parser.add_argument("--no-conversations", action="store_true",
                        help="do not extract memory from Yuki's own conversations (turns are still stored)")
    parser.add_argument("--no-nudges", action="store_true",
                        help="no coach: no check-ins and no reminders (to-dos are still kept)")
    parser.add_argument("--no-review", action="store_true", help="no weekly review (scheduled or requested)")
    parser.add_argument("--duration", type=float, default=None, help="stop after this many seconds")
    parser.add_argument("--stats-every", type=float, default=600.0, help="seconds between stats records (default 600)")
    parser.add_argument("--verbose", action="store_true", help="one console line per capture (content-free)")
    return parser.parse_args(argv)


def run(args: argparse.Namespace) -> int:
    from yuki.memory.store import PAUSE_FLAG, REFRESH_FLAG, REVIEW_FLAG, Store, default_db_path, flag_path

    db_path = Path(args.db) if args.db else default_db_path()
    mutex = _claim_mutex(db_path)
    if mutex is None:
        print(f"yuki-memory is already running for {db_path}", file=sys.stderr)
        return 1
    _set_below_normal()

    stop = threading.Event()
    keep_alive = _install_stop_handlers(stop)
    session_id = "memory-" + datetime.now().strftime("%Y%m%d-%H%M%S")
    log = _Log(SessionLogger(PROJECT_ROOT / "logs", session_id=session_id, quiet=True), verbose=args.verbose)
    meter = _ProcessMeter()
    started = time.monotonic()
    watcher: Watcher | None = None
    journal = None
    journal_thread: threading.Thread | None = None
    portrait = None
    portrait_thread: threading.Thread | None = None
    timeline = None
    episodes = None
    episodes_thread: threading.Thread | None = None
    warp = None
    warp_thread: threading.Thread | None = None
    conversations = None
    conversations_thread: threading.Thread | None = None
    acting_thread: threading.Thread | None = None
    acting_event = None
    nudges = None
    nudges_thread: threading.Thread | None = None
    review = None
    review_thread: threading.Thread | None = None
    pause_path = flag_path(db_path, PAUSE_FLAG)
    refresh_path = flag_path(db_path, REFRESH_FLAG)
    review_path = flag_path(db_path, REVIEW_FLAG)
    store = None
    code = 0
    try:
        store = Store.open(db_path)
        privacy = PrivacyConfig(args.privacy)
        log(
            "service_start",
            pid=psutil.Process().pid,
            db=str(db_path),
            privacy=str(privacy.path),
            privacy_error=privacy.last_error,
            journal=not args.no_journal,
            portrait=not (args.no_journal or args.no_portrait),
            timeline=not args.no_timeline,
            episodes=not (args.no_journal or args.no_timeline or args.no_episodes),
            terminal=not (args.no_journal or args.no_terminal),
            conversations=not (args.no_journal or args.no_conversations),
            nudges=not (args.no_journal or args.no_timeline or args.no_nudges),
            review=not (args.no_journal or args.no_review),
            duration_s=args.duration,
        )
        try:
            removed = store.prune(CAPTURE_TTL_DAYS)
            log("prune", captures_deleted=removed, older_than_days=CAPTURE_TTL_DAYS)
        except Exception as exc:
            log("error", where="prune", error=f"{type(exc).__name__}: {exc}")
        last_prune = time.monotonic()

        shared = store
        from yuki.config import Settings

        watcher = Watcher(
            lambda: shared, privacy=privacy, log=log, settings=WatcherSettings(), on_close=stop.set,
            user_names=Settings().user_names,
        )
        paused = pause_path.exists()
        if paused:
            watcher.set_paused(True)  # before start: nothing is read, not even the first window
        log("pause_flag", paused=paused, at_start=True)
        watcher.start()

        if not args.no_timeline:
            try:
                from yuki.memory.timeline import TimelineRecorder

                timeline = TimelineRecorder(
                    store, privacy=privacy, log=log,
                    # a page that changed address without a title change (a reel, a feed): read it now
                    on_page_change=lambda hwnd: watcher.request_capture(hwnd, "page"),
                )
                timeline.set_paused(paused)  # before start: nothing is recorded, not even the first window
                timeline.start()
            except Exception as exc:  # capture and journal run on without it
                log("error", where="timeline_start", error=f"{type(exc).__name__}: {exc}")
                timeline = None

        try:
            from yuki.memory import acting as _acting

            acting_event = _acting.ActingEvent(db_path)
            acting_thread = threading.Thread(
                target=_guarded(_follow_acting, log, "acting"),
                args=(stop, acting_event, db_path, watcher, lambda: timeline, log),
                name="yuki-memory-acting", daemon=True,
            )
            acting_thread.start()
        except Exception as exc:  # capture runs on; Yuki's own actions are then not marked
            log("error", where="acting_start", error=f"{type(exc).__name__}: {exc}")

        if not args.no_journal:
            try:
                from yuki.memory.journal import JournalWorker

                journal = JournalWorker(store)
                journal_thread = threading.Thread(
                    target=_guarded(journal.run, log, "journal"), args=(stop,), name="yuki-memory-journal", daemon=True
                )
                journal_thread.start()
            except Exception as exc:  # the watcher runs on without it
                log("error", where="journal_start", error=f"{type(exc).__name__}: {exc}")
                journal = None

        if not (args.no_journal or args.no_terminal):
            try:
                from yuki.memory.warp import WarpReader

                warp = WarpReader(store, privacy=privacy, log=log)
                warp.set_paused(paused)
                warp_thread = threading.Thread(
                    target=_guarded(warp.run, log, "warp"), args=(stop, pause_path), name="yuki-memory-warp",
                    daemon=True,
                )
                warp_thread.start()
            except Exception as exc:  # everything else runs on without it
                log("error", where="warp_start", error=f"{type(exc).__name__}: {exc}")
                warp = None

        if not (args.no_journal or args.no_conversations):
            try:
                from yuki.memory.conversations import ConversationWorker

                conversations = ConversationWorker(store, log=log)
                conversations_thread = threading.Thread(
                    target=_guarded(conversations.run, log, "conversations"), args=(stop,),
                    name="yuki-memory-conversations", daemon=True,
                )
                conversations_thread.start()
            except Exception as exc:  # everything else runs on without it
                log("error", where="conversations_start", error=f"{type(exc).__name__}: {exc}")
                conversations = None

        if not (args.no_journal or args.no_portrait):
            try:
                from yuki.memory.portrait import PortraitScheduler, PortraitWorker
                from yuki.memory.watcher import user_idle_s

                portrait = PortraitScheduler(
                    store, PortraitWorker(store), db_path=db_path, log=log, idle_fn=user_idle_s
                )
                portrait_thread = threading.Thread(
                    target=_guarded(portrait.run, log, "portrait"), args=(stop,), name="yuki-memory-portrait",
                    daemon=True,
                )
                portrait_thread.start()
            except Exception as exc:  # capture and journal run on without it
                log("error", where="portrait_start", error=f"{type(exc).__name__}: {exc}")
                portrait = None

        if timeline is not None and not (args.no_journal or args.no_episodes):
            try:
                from yuki.memory.episodes import EpisodeWorker

                episodes = EpisodeWorker(store, log=log)
                episodes_thread = threading.Thread(
                    target=_guarded(episodes.run, log, "episodes"), args=(stop, pause_path),
                    name="yuki-memory-episodes", daemon=True,
                )
                episodes_thread.start()
            except Exception as exc:  # everything else runs on without it
                log("error", where="episodes_start", error=f"{type(exc).__name__}: {exc}")
                episodes = None

        if timeline is not None and not (args.no_journal or args.no_nudges):
            try:
                from yuki.memory import acting as _acting_mod
                from yuki.memory.nudges import NudgeWorker
                from yuki.memory.watcher import session_locked

                def live_state() -> dict[str, Any]:
                    # the recorder's open stretch (written to the store every 30 s) and Yuki's own marker
                    tl = timeline
                    st = tl.stats() if tl is not None and tl.alive else {}
                    return {"meeting": bool(st.get("meeting")), "fullscreen": bool(st.get("fullscreen")),
                            "acting": _acting_mod.current(db_path) is not None}

                nudges = NudgeWorker(store, db_path=db_path, privacy=privacy, locked_fn=session_locked,
                                     live_fn=live_state, log=log)
                nudges_thread = threading.Thread(
                    target=_guarded(nudges.run, log, "nudges"), args=(stop, pause_path),
                    name="yuki-memory-nudges", daemon=True,
                )
                nudges_thread.start()
            except Exception as exc:  # everything else runs on without it
                log("error", where="nudges_start", error=f"{type(exc).__name__}: {exc}")
                nudges = None

        if not (args.no_journal or args.no_review):
            try:
                from yuki.memory.review import ReviewScheduler, ReviewWorker
                from yuki.memory.watcher import session_locked, user_idle_s

                review = ReviewScheduler(store, ReviewWorker(store, log=log), db_path=db_path, privacy=privacy,
                                         log=log, idle_fn=user_idle_s, locked_fn=session_locked)
                review_thread = threading.Thread(
                    target=_guarded(review.run, log, "review"), args=(stop,), name="yuki-memory-review",
                    daemon=True,
                )
                review_thread.start()
            except Exception as exc:  # everything else runs on without it
                log("error", where="review_start", error=f"{type(exc).__name__}: {exc}")
                review = None

        if not args.verbose:
            print(f"yuki-memory running (log {log.path}); Ctrl+C to stop", flush=True)
        next_stats = time.monotonic() + args.stats_every
        privacy_error = privacy.last_error
        while not stop.is_set():
            now = time.monotonic()
            deadline = next_stats
            if args.duration is not None:
                deadline = min(deadline, started + args.duration)
            # Bounded so Ctrl+C is seen promptly (signals run between waits).
            stop.wait(min(max(deadline - now, 0.0), 1.0))
            now = time.monotonic()
            if args.duration is not None and now >= started + args.duration:
                log("service_stop_requested", via="duration")
                break
            if not watcher.alive:
                log("error", where="watcher", error="hook thread exited")
                code = 2
                break
            if timeline is not None and not timeline.alive:
                log("error", where="timeline", error="timeline thread exited")  # not fatal
                timeline = None
            now_paused = pause_path.exists()
            if now_paused != paused:
                paused = now_paused
                watcher.set_paused(paused)
                if timeline is not None:
                    timeline.set_paused(paused)
                if warp is not None:
                    warp.set_paused(paused)
                log("pause_flag", paused=paused)
            if portrait is not None and refresh_path.exists():
                portrait.wake()
            if review is not None and review_path.exists():
                review.wake()
            if privacy.last_error != privacy_error:
                privacy_error = privacy.last_error
                log("privacy_config", error=privacy_error, reloads=privacy.reloads)
            if now >= next_stats:
                next_stats = now + args.stats_every
                log("stats", **meter.reading(), watcher=watcher.stats(),
                    timeline=timeline.stats() if timeline is not None else None,
                    warp=warp.stats() if warp is not None else None)
            if now - last_prune >= _PRUNE_EVERY_S:
                last_prune = now
                try:
                    log("prune", captures_deleted=store.prune(CAPTURE_TTL_DAYS), older_than_days=CAPTURE_TTL_DAYS)
                except Exception as exc:
                    log("error", where="prune", error=f"{type(exc).__name__}: {exc}")
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        import traceback

        log("error", where="service", error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())
        code = 2
    finally:
        stop.set()
        if acting_event is not None:
            acting_event.set()
        if journal is not None:
            try:
                journal.stop()
            except Exception:
                pass
        if portrait is not None:
            portrait.wake()
        if review is not None:
            review.wake()
        if episodes is not None:
            episodes.stop()
        if warp is not None:
            warp.stop()
        if conversations is not None:
            conversations.stop()
        if nudges is not None:
            nudges.stop()
        if watcher is not None:
            watcher.stop()
        if timeline is not None:
            timeline.stop()  # writes the open stretch first
        if acting_thread is not None:
            acting_thread.join(3.0)
        if acting_event is not None:
            acting_event.close()
        if journal_thread is not None:
            journal_thread.join(10.0)
        if warp_thread is not None:
            warp_thread.join(5.0)
        if conversations_thread is not None:
            conversations_thread.join(10.0)  # a call in flight is abandoned (daemon); its turns stay pending
        if nudges_thread is not None:
            nudges_thread.join(10.0)  # a call in flight is abandoned (daemon); nothing half-written
        if episodes_thread is not None:
            episodes_thread.join(10.0)  # a run mid-call is abandoned (daemon); its row stays "running"
        if portrait_thread is not None:
            portrait_thread.join(10.0)  # a run mid-call is abandoned (daemon); its row stays "running"
        if review_thread is not None:
            review_thread.join(10.0)  # a run mid-call is abandoned (daemon); its row stays "running"
        totals = meter.reading(since_start=True)
        log("service_stop", **totals, watcher=watcher.stats() if watcher else None,
            timeline=timeline.stats() if timeline is not None else None, exit_code=code)
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
        log.close()
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(mutex))
        keep_alive()
    return code


def _follow_acting(
    stop: threading.Event, event: Any, db_path: Path, watcher: Watcher, timeline_now: Callable[[], Any], log: _Log,
) -> None:
    """Keep the watcher and the timeline told whether Yuki is acting (yuki.memory.acting).

    Wakes on Yuki's event, or every 2 s at the latest (a missed signal, an
    agent that died mid-request: :func:`yuki.memory.acting.current` ignores
    entries whose process is gone).
    """
    from yuki.memory import acting as _acting

    current_token: str | None = None
    while not stop.is_set():
        event.wait(2.0)
        if stop.is_set():
            break
        try:
            now = _acting.current(db_path)
        except Exception:
            now = None
        token = now.token if now is not None else None
        if token == current_token:
            continue
        current_token = token
        watcher.set_acting(now)
        timeline = timeline_now()
        if timeline is not None:
            timeline.set_acting(now)
        log("acting", acting=now is not None, lane=now.lane if now is not None else None)


def _guarded(fn: Callable[..., Any], log: _Log, where: str) -> Callable[..., None]:
    def runner(*args: Any) -> None:
        try:
            fn(*args)
        except BaseException as exc:
            import traceback

            log("error", where=where, error=f"{type(exc).__name__}: {exc}", traceback=traceback.format_exc())

    return runner


def main(argv: list[str] | None = None) -> int:
    # Started by the tray under pythonw.exe (no console), stdout/stderr can be
    # None; the status lines printed below must not crash the service then.
    import os

    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    return run(_parse(argv))


if __name__ == "__main__":
    sys.exit(main())
