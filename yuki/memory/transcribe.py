"""Deferred transcription of the user's side of calls: only when the PC is genuinely free.

During a call nothing but capture, a cheap speech gate and encryption run
(:mod:`yuki.memory.calls`): the user's own speech waits in the spool as
encrypted Opus chunks. This module turns it into text later.

Two halves:

* :class:`TranscribeScheduler` - a thread in ``yuki-memory``
  (``yuki-memory-transcribe``). About once a minute, and only while audio is
  waiting, it looks at content-free facts: is a call going on, is the thing
  in front full screen (the timeline's fact, and Windows' own "busy"
  notification state: a game, a presentation), has the GPU been quiet for a
  while (NVML utilisation and free VRAM), has the CPU, and is the user away
  (no input for ``defer_idle_min``, or the session is locked) or is it night.
  When every gate holds it starts the worker process below; while it runs it
  keeps watching and, the moment the user comes back, a call or a game
  starts, or memory is paused, it asks the worker to stop after the chunk in
  hand (a named event), and ends it if it does not. It also enforces the
  spool's caps (age, total size) and removes orphans, logging counts only.
* ``python -m yuki.memory.transcribe`` - the worker process, at below-normal
  priority. It loads the speech-to-text model (:func:`yuki.memory.calls.load_backend`),
  transcribes the waiting chunks oldest first, stores each chunk's words
  (encrypted) and deletes its audio in that order, hands each finished call
  to the journal (``my_call_side``), and exits: the model, its RAM and its
  VRAM go with the process, at once. The service itself never loads a
  speech model or touches the GPU.

Logs are content-free: ``call_transcribe_start``/``_stop`` (why it ran, chunks,
seconds of audio, how it ended), ``call_spool_pruned`` (reason, chunks, bytes);
the worker's own ``logs/memory/transcribe-YYYYMMDD.jsonl`` has per-chunk timing,
device, model and GPU load - never words.
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from yuki.memory.store import CALL_OPEN_STATUSES, Store, default_db_path

BELOW_NORMAL_PRIORITY_CLASS = 0x00004000
CREATE_NO_WINDOW = 0x08000000
PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class DeferConfig:
    """The ``defer_*`` and ``spool_*`` keys of the privacy file's ``[calls]`` table."""

    check_s: float = 60.0
    idle_min: float = 5.0
    night: tuple[str, str] = ("01:00", "07:00")
    gpu_max_util: float = 10.0
    gpu_quiet_min: float = 3.0
    vram_free_mb: float = 2500.0
    cpu_max_percent: float = 40.0
    back_idle_s: float = 20.0
    stop_grace_s: float = 90.0
    retry_min: float = 30.0
    spool_max_age_days: float = 7.0
    spool_max_mb: float = 500.0

    @classmethod
    def from_dict(cls, table: dict | None) -> DeferConfig:
        t = table or {}
        d = cls()

        def num(key: str, default: float) -> float:
            try:
                return float(t.get(key, default))
            except (TypeError, ValueError):
                return default

        night = t.get("defer_night", list(d.night))
        if not (isinstance(night, list) and len(night) == 2):
            night = list(d.night)
        return cls(
            check_s=max(10.0, num("defer_check_s", d.check_s)), idle_min=num("defer_idle_min", d.idle_min),
            night=(str(night[0]), str(night[1])), gpu_max_util=num("defer_gpu_max_util", d.gpu_max_util),
            gpu_quiet_min=num("defer_gpu_quiet_min", d.gpu_quiet_min), vram_free_mb=num("defer_vram_free_mb", d.vram_free_mb),
            cpu_max_percent=num("defer_cpu_max_percent", d.cpu_max_percent),
            back_idle_s=num("defer_back_idle_s", d.back_idle_s), stop_grace_s=num("defer_stop_grace_s", d.stop_grace_s),
            retry_min=num("defer_retry_min", d.retry_min),
            spool_max_age_days=num("spool_max_age_days", d.spool_max_age_days),
            spool_max_mb=num("spool_max_mb", d.spool_max_mb),
        )


def in_window(now: datetime, start: str, end: str) -> bool:
    """Whether local ``now`` is inside ``start``-``end`` (HH:MM, may wrap midnight)."""
    def minutes(text: str) -> int:
        h, _, m = text.partition(":")
        return int(h) * 60 + int(m or 0)

    try:
        a, b = minutes(start), minutes(end)
    except ValueError:
        return False
    x = now.hour * 60 + now.minute
    return a <= x < b if a <= b else (x >= a or x < b)


def screen_busy() -> bool:
    """Windows' own "not now" (``SHQueryUserNotificationState``): a full-screen app or game, presentation mode."""
    try:
        shell32 = ctypes.windll.shell32
        state = ctypes.c_int(0)
        if shell32.SHQueryUserNotificationState(ctypes.byref(state)) != 0:
            return False
        return state.value in (2, 3, 4)   # busy, d3d full screen, presentation
    except (AttributeError, OSError):
        return False


def stop_event_name(db_path: Path) -> str:
    import hashlib

    digest = hashlib.sha1(str(Path(db_path).resolve()).lower().encode()).hexdigest()[:12]
    return f"Local\\YukiTranscribeStop-{digest}"


class TranscribeScheduler:
    """Waits for a genuinely free PC, then runs the worker process (see the module docstring).

    Every fact comes through a replaceable function so the gates can be
    checked offline: ``live_fn() -> {"fullscreen": bool}``, ``idle_fn() -> s``,
    ``locked_fn() -> bool``, ``gpu_fn() -> {"gpu_util", "vram_used_mb", "vram_total_mb"} | None``,
    ``cpu_fn() -> system CPU % since the last call``, ``busy_fn() -> bool``,
    ``now_fn() -> datetime``, ``spawn(argv) -> Popen``.
    """

    def __init__(self, store: Store, *, db_path: Path | None = None, privacy: Any = None,
                 log: Callable[..., Any] | None = None, live_fn: Callable[[], dict] = lambda: {},
                 idle_fn: Callable[[], float] | None = None, locked_fn: Callable[[], bool] | None = None,
                 gpu_fn: Callable[[], dict | None] | None = None, cpu_fn: Callable[[], float] | None = None,
                 busy_fn: Callable[[], bool] = screen_busy, now_fn: Callable[[], datetime] = datetime.now,
                 spawn: Callable[[list[str]], Any] | None = None, config: DeferConfig | None = None) -> None:
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.db_path = Path(db_path) if db_path is not None else store.path
        self._privacy = privacy
        self._section = PrivacySection(privacy, "calls", DeferConfig.from_dict) if config is None else None
        self._config = config
        self._log_fn = log
        self.live_fn = live_fn
        if idle_fn is None or locked_fn is None:
            from yuki.memory.watcher import session_locked, user_idle_s

            idle_fn = idle_fn or user_idle_s
            locked_fn = locked_fn or session_locked
        self.idle_fn, self.locked_fn = idle_fn, locked_fn
        self._gpu = None
        self.gpu_fn = gpu_fn or self._nvml
        if cpu_fn is None:
            import psutil

            psutil.cpu_percent(None)
            cpu_fn = lambda: psutil.cpu_percent(None)   # noqa: E731 - since the previous call
        self.cpu_fn = cpu_fn
        self.busy_fn = busy_fn
        self.now_fn = now_fn
        self.spawn = spawn or self._spawn
        self.paused = False
        self._samples: deque[tuple[float, float, float, float]] = deque()   # (t, gpu util, vram free, cpu)
        self._proc: Any = None
        self._run_started = 0.0
        self._run_why = ""
        self._stopping: str | None = None
        self._stop_sent_at = 0.0
        self._retry_after = 0.0
        self._last_block: str | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._event_handle = None

    # -- plumbing ------------------------------------------------------------

    @property
    def cfg(self) -> DeferConfig:
        return self._config if self._config is not None else self._section.get()

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:
                pass

    def _nvml(self) -> dict | None:
        if self._gpu is None:
            from yuki.memory.calls import Gpu

            self._gpu = Gpu()
        return self._gpu.snapshot()

    def _spawn(self, argv: list[str]) -> Any:
        return subprocess.Popen(argv, cwd=str(PROJECT_ROOT), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=BELOW_NORMAL_PRIORITY_CLASS | CREATE_NO_WINDOW)

    def _stop_event(self) -> Any:
        if self._event_handle is None:
            k = ctypes.windll.kernel32
            k.CreateEventW.restype = ctypes.c_void_p
            k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
            self._event_handle = k.CreateEventW(None, True, False, stop_event_name(self.db_path))
        return self._event_handle

    def _signal_stop(self, on: bool) -> None:
        handle = self._stop_event()
        if handle:
            k = ctypes.windll.kernel32
            (k.SetEvent if on else k.ResetEvent)(ctypes.c_void_p(handle))

    # -- gates -----------------------------------------------------------------

    def _waiting(self) -> dict[str, Any]:
        rows = self.store.call_chunks()
        ready = [r for r in rows if r.get("call_status") == "ended"]
        return {"chunks": len(ready), "audio_s": round(sum(r["audio_s"] for r in ready), 1),
                "calls": len({r["call_id"] for r in ready}), "all_chunks": len(rows)}

    def _sample(self, now: float, cfg: DeferConfig) -> None:
        gpu = self.gpu_fn()
        util = float(gpu["gpu_util"]) if gpu else 0.0
        free = float(gpu["vram_total_mb"] - gpu["vram_used_mb"]) if gpu else float("inf")
        self._samples.append((now, util, free, float(self.cpu_fn())))
        keep = max(cfg.gpu_quiet_min * 60.0, cfg.check_s) + cfg.check_s
        while self._samples and now - self._samples[0][0] > keep:
            self._samples.popleft()

    def blocked(self, now: float, cfg: DeferConfig) -> tuple[str | None, str]:
        """Why a batch may not start now (``None`` when it may), and why it may (``away`` | ``locked`` | ``night``)."""
        if self.paused:
            return "paused", ""
        if self.store.calls(statuses=CALL_OPEN_STATUSES, limit=1):
            return "call_active", ""
        if bool((self.live_fn() or {}).get("fullscreen")) or self.busy_fn():
            return "fullscreen", ""
        window = cfg.gpu_quiet_min * 60.0
        span = [s for s in self._samples if now - s[0] <= window + 1.0]
        if not span or now - span[0][0] < window - cfg.check_s:
            return "gpu_not_yet_observed", ""
        if max(s[1] for s in span) > cfg.gpu_max_util:
            return "gpu_busy", ""
        if min(s[2] for s in span) < cfg.vram_free_mb:
            return "vram_low", ""
        if max(s[3] for s in span) > cfg.cpu_max_percent:
            return "cpu_busy", ""
        if self.locked_fn():
            return None, "locked"
        if self.idle_fn() >= cfg.idle_min * 60.0:
            return None, "away"
        if in_window(self.now_fn(), *cfg.night):
            return None, "night"
        return "user_present", ""

    def interrupt(self, cfg: DeferConfig) -> str | None:
        """Why a running batch must stop at its next chunk boundary, or None."""
        if self.paused:
            return "paused"
        if self.store.calls(statuses=CALL_OPEN_STATUSES, limit=1):
            return "call_started"
        if bool((self.live_fn() or {}).get("fullscreen")) or self.busy_fn():
            return "fullscreen"
        if self._run_why != "night" and not self.locked_fn() and self.idle_fn() < cfg.back_idle_s:
            return "user_back"
        return None

    # -- one pass --------------------------------------------------------------

    def sweep(self, cfg: DeferConfig) -> None:
        for reason, chunks, size in self.store.sweep_spool(max_age_s=cfg.spool_max_age_days * 86400.0,
                                                           max_bytes=int(cfg.spool_max_mb * 2**20)):
            if chunks:
                self._log("call_spool_pruned", reason=reason, chunks=chunks, bytes=size)

    def step(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else float(now)
        cfg = self.cfg
        if self._proc is not None:
            self._supervise(now, cfg)
            return
        self.sweep(cfg)
        waiting = self._waiting()
        if not waiting["all_chunks"]:
            self._samples.clear()
            return
        self._sample(now, cfg)
        if not waiting["chunks"] or now < self._retry_after:
            return
        block, why = self.blocked(now, cfg)
        if block is not None:
            if block != self._last_block:
                self._last_block = block
                self._log("call_transcribe_waiting", reason=block, **waiting)
            return
        self._last_block = None
        self._start(now, why, waiting)

    def _start(self, now: float, why: str, waiting: dict[str, Any]) -> None:
        self._signal_stop(False)
        argv = [sys.executable, "-m", "yuki.memory.transcribe", "--db", str(self.db_path),
                "--stop-event", stop_event_name(self.db_path)]
        path = getattr(self._privacy, "path", None)
        if path:
            argv += ["--privacy", str(path)]
        gpu = self._samples[-1] if self._samples else None
        self._proc = self.spawn(argv)
        self._run_started, self._run_why, self._stopping = now, why, None
        self._log("call_transcribe_start", why=why, pid=getattr(self._proc, "pid", None),
                  gpu_util=gpu[1] if gpu else None, vram_free_mb=gpu[2] if gpu else None, **waiting)

    def _supervise(self, now: float, cfg: DeferConfig) -> None:
        code = self._proc.poll()
        if code is None:
            if self._stopping is None:
                reason = self.interrupt(cfg)
                if reason is not None:
                    self._stopping, self._stop_sent_at = reason, now
                    self._signal_stop(True)
            elif now - self._stop_sent_at > cfg.stop_grace_s:
                self._proc.kill()   # each chunk is one transaction: nothing half-written
                self._log("call_transcribe_killed", reason=self._stopping)
            return
        waiting = self._waiting()
        self._log("call_transcribe_stop", exit_code=code, reason=self._stopping or ("done" if code == 0 else "error"),
                  seconds=round(now - self._run_started, 1), left=waiting["chunks"])
        if code not in (0, None) and self._stopping is None:
            self._retry_after = now + cfg.retry_min * 60.0
        self._proc = None
        self._samples.clear()    # the GPU was ours meanwhile: observe it quiet again before the next run

    def run(self, stop: threading.Event | None = None, pause_path: Path | None = None) -> None:
        stop = stop or self._stop
        self._stop = stop
        self._log("transcribe_scheduler_start", check_s=self.cfg.check_s)
        while not stop.is_set():
            self.paused = bool(pause_path is not None and pause_path.exists())
            try:
                self.step()
            except Exception as exc:
                self._log("error", where="transcribe.step", error=f"{type(exc).__name__}: {exc}")
            wait = 5.0 if self._proc is not None else self.cfg.check_s
            self._wake.wait(wait)
            self._wake.clear()
        if self._proc is not None and self._proc.poll() is None:
            self._signal_stop(True)
            try:
                self._proc.wait(30)
            except Exception:
                self._proc.kill()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()


# ---------------------------------------------------------------------------
# The worker process
# ---------------------------------------------------------------------------


def _stop_requested(handle: Any) -> bool:
    if not handle:
        return False
    return ctypes.windll.kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0


def transcribe_waiting(store: Store, cfg: Any, *, stop: Callable[[], bool], log: Callable[..., Any],
                       backend: Any = None, max_attempts: int = 3) -> dict[str, Any]:
    """Transcribe every waiting chunk of an ended call, oldest first, until done or ``stop()``.

    Per chunk: decrypt, decode (in memory), transcribe, store the words
    (encrypted) and delete the audio. A call with no chunk left waiting goes to
    the journal. Returns content-free totals.
    """
    from yuki.memory.calls import Gpu, decode_speech, load_backend

    done = failed = 0
    audio_s = 0.0
    ms_all: list[float] = []
    gpu = Gpu()
    rows = [r for r in store.call_chunks() if r.get("call_status") == "ended"]
    calls = sorted({r["call_id"] for r in rows})
    stopped = None
    if rows and backend is None:
        t0 = time.perf_counter()
        backend = load_backend(cfg, log)
        log("transcribe_model_ready", stt_model=backend.name, stt_device=backend.device,
            load_ms=round((time.perf_counter() - t0) * 1000), gpu=gpu.snapshot())
    try:
        for r in rows:
            if stop():
                stopped = "asked"
                break
            try:
                audio = decode_speech(store.read_spool(r["file"]), r["codec"] or "opus")
                t0 = time.perf_counter()
                segments, info = backend.transcribe(audio)
                ms = (time.perf_counter() - t0) * 1000.0
                utterances = [{"at": round(r["start_at"] + s.start, 2), "end": round(r["start_at"] + s.end, 2),
                               "text": s.text, "lang": s.lang} for s in segments]
                store.finish_call_chunk(r["id"], utterances, lang=info.get("lang"), ms=ms, model=backend.name,
                                        device=backend.device)
                done += 1
                audio_s += float(r["audio_s"])
                ms_all.append(ms)
                log("transcribe_chunk", chunk_id=r["id"], call_id=r["call_id"], audio_s=r["audio_s"], ms=round(ms),
                    segments=len(utterances), lang=info.get("lang"), gpu=gpu.snapshot())
                del audio
            except FileNotFoundError:
                store.remove_call_chunks([r["id"]], "lost")
            except Exception as exc:
                failed += 1
                store.fail_call_chunk(r["id"], f"{type(exc).__name__}: {exc}", max_attempts=max_attempts)
                log("transcribe_chunk_failed", chunk_id=r["id"], error=f"{type(exc).__name__}: {exc}")
    finally:
        journaled = 0
        for call_id in calls:
            try:
                if store.journal_call_chunks(call_id) is not None:
                    journaled += 1
            except Exception as exc:
                log("error", where="transcribe.journal", call_id=call_id, error=f"{type(exc).__name__}: {exc}")
        if backend is not None and hasattr(backend, "close"):
            backend.close()
    ms_all.sort()
    return {"chunks_done": done, "chunks_failed": failed, "audio_s": round(audio_s, 1), "calls_journaled": journaled,
            "stopped": stopped, "chunk_ms_p50": round(ms_all[len(ms_all) // 2]) if ms_all else None,
            "chunk_ms_max": round(ms_all[-1]) if ms_all else None}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.memory.transcribe")
    parser.add_argument("--db", type=Path, default=None)
    parser.add_argument("--privacy", type=Path, default=None)
    parser.add_argument("--stop-event", default="")
    args = parser.parse_args(argv)
    try:
        import psutil

        psutil.Process().nice(psutil.BELOW_NORMAL_PRIORITY_CLASS)
    except Exception:
        pass
    from yuki.log.events import _as_plain
    from yuki.memory.calls import CallConfig
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.timeline import PrivacySection

    log_dir = PROJECT_ROOT / "logs" / "memory"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"transcribe-{datetime.now():%Y%m%d}.jsonl"

    def log(type: str, **fields: Any) -> None:
        record = {"ts": time.time(), "pid": os.getpid(), "type": type, **{k: _as_plain(v) for k, v in fields.items()}}
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=_as_plain) + "\n")

    handle = None
    if args.stop_event:
        k = ctypes.windll.kernel32
        k.OpenEventW.restype = ctypes.c_void_p
        k.OpenEventW.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p]
        handle = k.OpenEventW(0x00100000, False, args.stop_event)   # SYNCHRONIZE
    cfg = PrivacySection(PrivacyConfig(args.privacy), "calls", CallConfig.from_dict).get()
    store = Store.open(args.db or default_db_path())
    t0 = time.perf_counter()
    log("transcribe_worker_start")
    try:
        result = transcribe_waiting(store, cfg, stop=lambda: _stop_requested(handle), log=log)
    except Exception as exc:
        log("transcribe_worker_error", error=f"{type(exc).__name__}: {exc}")
        store.close()
        return 3
    log("transcribe_worker_stop", seconds=round(time.perf_counter() - t0, 1), **result)
    store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
