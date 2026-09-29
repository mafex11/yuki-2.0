"""Python's cyclic garbage collector and the voice loop (docs/VOICE.md v3.14, "no dead air from stalls").

A full collection walks every object the process holds while it holds the interpreter lock, so no other thread runs
meanwhile: in the tray process (the window, the agent, UI Automation trees, memory caches, Pipecat) that is the
one thing that can freeze the voice loop for seconds with the loop itself idle. Live 2026-09-29 01:29 the loop sat in
``select`` for 11.5 s while the one other busy thread was inside a weakref callback (``weakref.py:remove``, which
the collector runs), and the PC's RAM was 80%+ used (a collection then pages the heap back in).

* :func:`freeze_heap` collects once and moves every object alive then into the permanent generation
  (``gc.freeze``), so later collections only walk what came after (measured: a full collection of a freshly
  imported tray-sized heap, 195k objects, 150-170 ms before, 57-87 ms after with 200k new objects). Called once per
  process, off every loop, after the voice prewarm has imported everything (VoiceSession's prewarm).
* :func:`watch` records every collection of 100 ms or more (generation, ms; content-free), so a loop stall's log
  line can say whether a collection overlapped it (:func:`pauses_between`).

Plumbing only: nothing here decides anything about what Yuki says or does.
"""

from __future__ import annotations

import gc
import threading
import time
from collections import deque
from typing import Any

#: Collections at least this long are kept (ms).
REPORT_MS = 100.0
_pauses: deque[tuple[float, float, int, int]] = deque(maxlen=64)   # (started, ended, generation, collected)
_started: dict[int, tuple[float, int]] = {}
_installed = False
_frozen = False
_lock = threading.Lock()


def _callback(phase: str, info: dict[str, Any]) -> None:
    """``gc.callbacks`` hook: as cheap as it can be (it runs on every collection, on whichever thread)."""
    if phase == "start":
        _started[threading.get_ident()] = (time.monotonic(), int(info.get("generation", -1)))
        return
    begun = _started.pop(threading.get_ident(), None)
    if begun is None:
        return
    now = time.monotonic()
    if (now - begun[0]) * 1000.0 >= REPORT_MS:
        _pauses.append((begun[0], now, begun[1], int(info.get("collected", 0) or 0)))


def watch() -> None:
    """Record long collections from now on (once per process)."""
    global _installed
    with _lock:
        if not _installed:
            gc.callbacks.append(_callback)
            _installed = True


def pauses_between(start: float, end: float) -> list[dict[str, Any]]:
    """The long collections that overlapped ``[start, end]`` (monotonic), content-free."""
    return [{"gen": g, "ms": round((b - a) * 1000.0), "collected": n}
            for a, b, g, n in list(_pauses) if b >= start and a <= end]


def recent(within_s: float = 3600.0) -> list[dict[str, Any]]:
    """The long collections of the last ``within_s`` seconds."""
    now = time.monotonic()
    return pauses_between(now - within_s, now)


def freeze_heap() -> dict[str, Any]:
    """Collect once, then freeze what is alive (once per process). Blocking: call it off every loop."""
    global _frozen
    with _lock:
        if _frozen:
            return {"frozen": "already"}
        _frozen = True
    started = time.perf_counter()
    before = len(gc.get_objects())
    collected = gc.collect()
    collect_ms = round((time.perf_counter() - started) * 1000)
    gc.freeze()
    return {"objects": before, "collected": collected, "collect_ms": collect_ms, "frozen": gc.get_freeze_count()}


__all__ = ["freeze_heap", "watch", "pauses_between", "recent", "REPORT_MS"]
