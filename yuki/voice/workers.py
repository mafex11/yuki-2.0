"""Background work for the voice layer on threads that already exist (docs/VOICE.md v3.14, "no dead air").

Measured 2026-09-29 in the offline replays: the voice loop stalled 0.4-3.4 s inside ``Thread.start()`` five times in
one run, each where it started a thread for a bit of background work (the hands' tab and screen-name reads, the
default executor growing for ``asyncio.to_thread``). ``start()`` waits until the new thread has run, and a new
thread first has to get the interpreter lock, which other busy threads hold. Starting threads is therefore never
done from the loop: :func:`shared` is a small pool started once, off every loop (when the bridge is made on the GUI
thread), and :meth:`Workers.submit` only puts work on its queue. :func:`prestart_default_executor` gives a
conversation's loop an executor whose threads all exist before anything is live.
"""

from __future__ import annotations

import asyncio
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable


class Workers:
    """A few daemon threads that run submitted callables in order of arrival; ``submit`` never blocks."""

    def __init__(self, count: int = 3, name: str = "yuki-voice-bg") -> None:
        self._queue: queue.SimpleQueue[Callable[[], Any]] = queue.SimpleQueue()
        self._threads = [threading.Thread(target=self._run, name=f"{name}_{i}", daemon=True) for i in range(count)]
        for thread in self._threads:
            thread.start()

    def submit(self, fn: Callable[[], Any]) -> None:
        self._queue.put(fn)

    def _run(self) -> None:
        while True:
            fn = self._queue.get()
            try:
                fn()
            except Exception:  # noqa: BLE001 - background hints only; each caller handles its own errors
                pass


_shared: Workers | None = None
_ordered: Workers | None = None
_lock = threading.Lock()


def shared() -> Workers:
    """The voice layer's pool (made on first call: call it off every loop first, e.g. when the bridge is made)."""
    global _shared, _ordered
    with _lock:
        if _shared is None:
            _shared = Workers()
        if _ordered is None:
            _ordered = Workers(count=1, name="yuki-voice-ordered")
        return _shared


def ordered() -> Workers:
    """One thread for work that must keep its order (memory's turn log: its first call starts memory's own writer
    thread, measured 0.5 s on the loop in an offline replay of 29 Sept)."""
    shared()
    assert _ordered is not None
    return _ordered


#: Threads of a conversation loop's default executor (``asyncio.to_thread``: the desktop summary, the hands, memory).
DEFAULT_EXECUTOR_THREADS = 8


async def prestart_default_executor(loop: asyncio.AbstractEventLoop,
                                    threads: int = DEFAULT_EXECUTOR_THREADS) -> float:
    """Give ``loop`` a default executor with all its threads started now (before the conversation is live).

    Returns the ms it took. Every later ``asyncio.to_thread`` finds an idle thread instead of starting one.
    """
    started = time.perf_counter()
    executor = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="yuki-voice-io")
    loop.set_default_executor(executor)
    await asyncio.gather(*(loop.run_in_executor(executor, time.sleep, 0.02) for _ in range(threads)))
    return (time.perf_counter() - started) * 1000.0


__all__ = ["Workers", "shared", "ordered", "prestart_default_executor"]
