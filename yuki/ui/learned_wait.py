"""appLearn review (4 Oct): the return card's late Learned box, driven by practice memory's signal.

Practice memory's learner writes a done run's way only after the run's follow-up window (90 s, then its 30 s tick and
one model pass; other runs' passes may come first). So the return card cannot know when to look: one fixed late read
missed the success when the learner was still busy. Instead the learner sets a named event
(:data:`yuki.memory.practice.LEARNED_EVENT_NAME`) when a run it read taught a way or marked one a success, and the
tray waits on it, off the GUI thread, only while a done run's learning is still to come (at most
:data:`yuki.ui.app.LEARNED_WAIT_S`): each signal is one ``workflows()`` read, and the wait ends as soon as the run's
Learned box is found, at the deadline (one last read then, in case a signal came before the event existed) or when a
newer run replaces it. No polling: the thread sleeps in ``WaitForMultipleObjects``.
"""

from __future__ import annotations

import ctypes
import threading
import time
from ctypes import wintypes
from typing import Any, Callable

#: The named auto-reset event practice memory's learner sets (:data:`yuki.memory.practice.LEARNED_EVENT_NAME`; the
#: tray keeps its own copy so it never imports the memory package).
LEARNED_EVENT_NAME = "Local\\YukiLearnedReady"

_WAIT_OBJECT_0 = 0x00000000
_WAIT_TIMEOUT = 0x00000102


class LearnedWaiter:
    """One wait at a time for the learner's signal (:meth:`arm`); a new arm ends the one before.

    Args:
        event_name: The named event to wait on (tests pass their own).
    """

    def __init__(self, event_name: str = LEARNED_EVENT_NAME) -> None:
        self.event_name = event_name
        self._k: Any = None
        self._ready = 0
        self._lock = threading.Lock()
        self._stop = 0
        self._thread: threading.Thread | None = None

    def prepare(self) -> bool:
        """Create (or open) the named event, so a signal from now on is kept until the wait reads it. ``False`` when
        events are unavailable (the caller falls back to one timed read)."""
        if self._ready:
            return True
        try:
            k = ctypes.WinDLL("kernel32", use_last_error=True)
            k.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
            k.CreateEventW.restype = wintypes.HANDLE
            k.SetEvent.argtypes = [wintypes.HANDLE]
            k.SetEvent.restype = wintypes.BOOL
            k.CloseHandle.argtypes = [wintypes.HANDLE]
            k.CloseHandle.restype = wintypes.BOOL
            k.WaitForMultipleObjects.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.BOOL,
                                                 wintypes.DWORD]
            k.WaitForMultipleObjects.restype = wintypes.DWORD
            ready = int(k.CreateEventW(None, False, False, self.event_name) or 0)   # auto-reset: one wake per signal
        except (AttributeError, OSError):
            return False
        if not ready:
            return False
        self._k, self._ready = k, ready
        return True

    @property
    def waiting(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def arm(self, check: Callable[[], bool], within_s: float) -> bool:
        """Wait for the learner's signal for at most ``within_s`` seconds; on each signal call ``check()`` (off the GUI
        thread), and stop once it returns ``True``. At the deadline ``check()`` runs once more. Ends any wait before
        it. ``False`` when events are unavailable (nothing armed)."""
        if not self.prepare():
            return False
        self.cancel()
        stop = int(self._k.CreateEventW(None, True, False, None) or 0)
        if not stop:
            return False
        with self._lock:
            self._stop = stop
        deadline = time.monotonic() + max(0.0, float(within_s))
        self._thread = threading.Thread(target=self._run, args=(check, deadline, stop), name="yuki-ui-learned-wait",
                                        daemon=True)
        self._thread.start()
        return True

    def cancel(self) -> None:
        """End the wait in progress, if any (its thread closes its own handle)."""
        with self._lock:   # under the lock: the wait's thread closes its handle under it too
            stop, self._stop = self._stop, 0
            if stop and self._k is not None:
                self._k.SetEvent(stop)

    def stop(self, timeout_s: float = 1.0) -> None:
        """End the wait and close the named event (the app is closing)."""
        self.cancel()
        if self._thread is not None:
            self._thread.join(timeout_s)
        if self._ready and not self.waiting:
            self._k.CloseHandle(self._ready)
            self._ready = 0

    def _run(self, check: Callable[[], bool], deadline: float, stop: int) -> None:
        k = self._k
        handles = (wintypes.HANDLE * 2)(stop, self._ready)
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._check(check)
                    return
                result = int(k.WaitForMultipleObjects(2, handles, False, int(remaining * 1000)))
                if result == _WAIT_OBJECT_0:
                    return   # cancelled: a newer run, or the app is closing
                if result == _WAIT_OBJECT_0 + 1:
                    if self._check(check):
                        return
                    continue
                if result == _WAIT_TIMEOUT:
                    self._check(check)
                return   # a failed wait: never spin
        finally:
            with self._lock:
                if self._stop == stop:
                    self._stop = 0
                k.CloseHandle(stop)

    @staticmethod
    def _check(check: Callable[[], bool]) -> bool:
        try:
            return bool(check())
        except Exception:   # the wait must outlive anything memory does
            return False


__all__ = ["LEARNED_EVENT_NAME", "LearnedWaiter"]
