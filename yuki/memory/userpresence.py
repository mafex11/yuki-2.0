"""The user's presence for the memory service: seconds since their last real input, a vibrating mouse left out.

Why (presence, 5 Oct 2026; report 73 release 6): the user's mouse sensor vibrates by itself and reports 20-30 moves a
second of 0-1 px with nobody at the PC. The memory service judged presence from ``GetLastInputInfo`` and the cursor
(:class:`yuki.memory.watcher._PresenceSampler`), whose rule "3 idle-timer resets within 1.5 s is a person" always held
at 20-30 resets a second: :func:`yuki.memory.watcher.user_idle_s` stayed near 0 for a whole minute with nobody there.
So the timeline never wrote "away", the jobs that wait for the user to step away (the portrait, the weekly review's
catch-up, call transcription on the idle GPU) never ran, and cards timed on stepping away or coming back were wrong.

What counts as the user (the same facts the tray's input gate and takeover read, :mod:`yuki.actions.userinput`):

* a key going down, a mouse button going down, the wheel turning;
* the mouse travelling the user's takeover distance (``ui.json`` ``takeover_move_px``, 40 by default) within 0.3 s,
  read from the mouse's own reports (:data:`yuki.actions.userinput.DEVICE`): a twitching sensor's 0-1 count
  back-and-forth never sums that far, a hand does at once, and Yuki's cursor jumps (``SetCursorPos``) send no report;
* unlocking the session (the password is typed on the secure desktop, which Raw Input does not see).

Never the user: anything carrying Yuki's input stamp (:data:`yuki.core.inputtag.YUKI_INPUT_TAG`); untagged injected
input (dictation, Deskflow) is the user's.

How: the process's Raw Input reader (:func:`yuki.actions.userinput.shared`, posted messages on a message-only window,
never a hook, so it can never lag the mouse) is held from the first question to the end of the process, with
``track_cursor`` off: a mouse report costs a few additions, no cursor read. Nothing polls. Counts and times only: never
which key, never where the cursor was. If Raw Input is unavailable (no desktop), the old sampler answers
(``fallback``), and the reason is in :meth:`Presence.facts`.

Semantics kept for the callers: :meth:`Presence.idle_s` is seconds since the user's last real input, never less than
Windows' own idle time (``GetLastInputInfo``: no input of any kind for that long means no real input either).
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable

#: How often the user's takeover distance is read again from their settings (s): it can change while the service runs.
SETTINGS_EVERY_S = 60.0
#: The kinds of input that are the user (keys, buttons, wheel, the mouse's own travel; the cursor's travel is the tray's).
PRESENCE_KINDS = ("keys", "click", "wheel", "device")


class Presence:
    """Seconds since the user's last real input, from the Raw Input reader.

    Args:
        raw_idle: Windows' idle time, any input (s): a floor, and the start's baseline.
        fallback: Answers when Raw Input is unavailable (the old sampler).
        monitor: The input monitor (default: the process's :func:`yuki.actions.userinput.shared`).
        clock: The monitor's clock (``time.monotonic``).

    Attributes:
        source: ``"raw_input"`` once listening, ``"fallback"`` when it could not, ``""`` before the first question.
        error: Why Raw Input is unavailable.
    """

    def __init__(self, *, raw_idle: Callable[[], float], fallback: Callable[[], float],
                 monitor: Any = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._raw_idle = raw_idle
        self._fallback = fallback
        self._monitor = monitor
        self._clock = clock
        self._lock = threading.Lock()
        self.source = ""
        self.error = ""
        self._baseline = 0.0       # the start, less Windows' idle time then: no real input known before it
        self._unlock_at = 0.0
        self._settings_at = 0.0

    def _get_monitor(self) -> Any:
        if self._monitor is None:
            from yuki.actions import userinput

            self._monitor = userinput.shared()
        return self._monitor

    def _ensure(self) -> Any:
        """The listening monitor, starting it on the first question; ``None`` when Raw Input is unavailable."""
        if self.source == "raw_input":
            monitor = self._monitor
            if monitor.running:
                return monitor
            with self._lock:   # the reader stopped (its thread failed): the fallback from now on
                if self.source == "raw_input":
                    self.source, self.error = "fallback", monitor.error or "the input reader stopped"
            return None
        if self.source == "fallback":
            return None
        with self._lock:
            if self.source == "":
                try:
                    monitor = self._get_monitor()
                    monitor.track_cursor = False
                    raw = float(self._raw_idle())
                    if monitor.acquire():          # held for the life of the process: never released
                        self._baseline = self._clock() - raw
                        self._settings_at = self._clock()
                        self.source = "raw_input"
                    else:
                        self.source, self.error = "fallback", monitor.error or "the input reader did not start"
                except Exception as exc:  # noqa: BLE001 - not on Windows, no desktop: the fallback
                    self.source, self.error = "fallback", f"{type(exc).__name__}: {exc}"
        return self._monitor if self.source == "raw_input" else None

    def last_input_at(self) -> float | None:
        """When the user's last real input came (the monitor's clock), ``None`` when Raw Input is unavailable."""
        monitor = self._ensure()
        if monitor is None:
            return None
        at, _kind = monitor.latest(PRESENCE_KINDS)
        return max(at, self._baseline, self._unlock_at)

    def idle_s(self) -> float:
        """Seconds since the user's last real input, never less than Windows' idle time."""
        last = self.last_input_at()
        if last is None:
            return float(self._fallback())
        now = self._clock()
        if now - self._settings_at >= SETTINGS_EVERY_S:
            self._settings_at = now
            self._monitor.move_px = None        # the reader reads the user's takeover distance again on its next move
        try:
            raw = float(self._raw_idle())
        except Exception:  # noqa: BLE001
            raw = 0.0
        return max(raw, now - last)

    def note_unlock(self) -> None:
        """The session was unlocked: the user is at the PC (their password went to the secure desktop)."""
        self._unlock_at = self._clock()

    def facts(self) -> dict[str, Any]:
        """Counts and numbers only, for a log line."""
        out: dict[str, Any] = {"source": self.source or "not_started"}
        if self.error:
            out["error"] = self.error
        monitor = self._monitor
        if self.source == "raw_input" and monitor is not None:
            counts = dict(monitor.counts)
            out["counts"] = counts
            out["max_device_travel"] = round(float(monitor.max_device_travel), 1)
            out["move_px"] = monitor.move_px
        return out


__all__ = ["PRESENCE_KINDS", "Presence", "SETTINGS_EVERY_S"]
