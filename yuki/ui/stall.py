"""A debug-only watchdog for the GUI thread: it logs every stall longer than :data:`THRESHOLD_MS`, with the
GUI thread's stack taken while it is stuck.

A light timer on the GUI thread stamps a heartbeat; a background thread looks at it every few milliseconds.
When the heartbeat is older than the threshold, the checker grabs the GUI thread's Python stack (once per
stall, at its start) and, when the heartbeat comes back, writes one line: how long the thread was stuck and
where. Off unless ``YUKI_STALL_WATCH=1`` (or a test starts it): the heartbeat itself costs a little CPU, so
it never runs in normal use.

Lines go to ``logs/ui-stalls-<date>.jsonl`` (or the ``sink`` given), each
``{"at", "ms", "process", "stack"}``.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Callable

#: A stall is a GUI thread busy longer than this (two frames at 60 Hz).
THRESHOLD_MS = 32.0
#: The heartbeat's interval and how often the checker looks (ms).
BEAT_MS = 8
CHECK_MS = 4


class StallWatchdog:
    """Watch this process's GUI thread (call :meth:`start` on it, after the application exists)."""

    def __init__(self, *, threshold_ms: float = THRESHOLD_MS, sink: Callable[[dict], None] | None = None,
                 process: str = "") -> None:
        self.threshold_ms = threshold_ms
        self.sink = sink or _file_sink
        self.process = process or Path(sys.argv[0] if sys.argv else "yuki").stem
        self.stalls: list[dict] = []
        self._beat = time.perf_counter()
        self._running = False
        self._thread: threading.Thread | None = None
        self._timer = None
        self._gui_ident = threading.get_ident()

    def start(self) -> "StallWatchdog":
        from PySide6.QtCore import QTimer

        self._gui_ident = threading.get_ident()
        self._beat = time.perf_counter()
        self._timer = QTimer()
        self._timer.setInterval(BEAT_MS)
        self._timer.timeout.connect(self._stamp)
        self._timer.start()
        self._running = True
        self._thread = threading.Thread(target=self._watch, name="yuki-stall-watch", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._running = False
        if self._timer is not None:
            self._timer.stop()
        if self._thread is not None:
            self._thread.join(0.5)

    def _stamp(self) -> None:
        self._beat = time.perf_counter()

    def _watch(self) -> None:
        stuck_since: float | None = None
        stack: list[str] = []
        while self._running:
            time.sleep(CHECK_MS / 1000.0)
            beat = self._beat
            gap = (time.perf_counter() - beat) * 1000.0
            if gap > self.threshold_ms:
                if stuck_since is None:
                    stuck_since = beat
                    frame = sys._current_frames().get(self._gui_ident)
                    stack = traceback.format_stack(frame)[-14:] if frame is not None else []
                continue
            if stuck_since is not None:
                ms = (self._beat - stuck_since) * 1000.0 - BEAT_MS
                if ms > self.threshold_ms:
                    rec = {"at": datetime.now().isoformat(timespec="milliseconds"), "ms": round(ms, 1),
                           "process": self.process, "stack": [ln.rstrip() for ln in stack]}
                    self.stalls.append(rec)
                    try:
                        self.sink(rec)
                    except Exception:
                        pass
                stuck_since, stack = None, []

    @property
    def worst_ms(self) -> float:
        return max((s["ms"] for s in self.stalls), default=0.0)


def _file_sink(rec: dict) -> None:
    try:
        from yuki.core.paths import logs_dir

        folder = logs_dir()
    except Exception:
        folder = Path.cwd() / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    with open(folder / f"ui-stalls-{datetime.now():%Y-%m-%d}.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec) + "\n")


_ACTIVE: list[StallWatchdog] = []


def maybe_start(process: str = "") -> StallWatchdog | None:
    """Start the watchdog when ``YUKI_STALL_WATCH=1`` (debug only); once per process."""
    if os.environ.get("YUKI_STALL_WATCH") != "1" or _ACTIVE:
        return _ACTIVE[0] if _ACTIVE else None
    dog = StallWatchdog(process=process).start()
    _ACTIVE.append(dog)
    return dog
