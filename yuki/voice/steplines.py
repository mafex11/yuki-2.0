"""The thinker's live step lines, said in Yuki's one voice (docs/VOICE.md v3.14, "one voice narrates live steps").

The user asked (29 Sept 2026) for Yuki to talk like a person while it works, not "On it", seconds of silence, then
"Done." The thinker writes a few words on each action or slower read it makes for a voice request (``say_now``, the
first field of the call, :mod:`yuki.agent.tools`), and its ``say`` lines; this module decides *when* each is said,
by code, straight into the voice (no talker model call: that cost ~2.5 s and made v3.9's lines stale), and never
reads what they say:

* A step line is said while its step runs, only when a pause would be noticeable: the step has run :attr:`after_s`
  and nobody has spoken for :attr:`silence_s` (or the silence is already :attr:`long_silence_s`, then as the step
  starts). Thresholds from measured step durations (docs/VOICE.md v3.14: of 159 real thinker calls, a step still
  running at 1.0 s was still running 0.5 s later 77% of the time; most clicks and focus changes end in under 0.4 s).
* A line is never said once its step has returned (v3.14b): "Switching tabs." after the tab switched is a stale line,
  which the user's decision of 29 Sept rules out. A quick step (most clicks and keys end in under 0.4 s, measured) is
  therefore rarely narrated; the thinker's pauses between steps are covered by the one "still on it" line. (v3.14 said
  the line of a quick step that worked in a long silence after it; with the stubs' steps now taking the durations
  measured on the real PC, that is gone.)
* Never over the user (their speech, or the microphone hearing them before recognition does), never while Yuki is
  mid-sentence, never once the job's outcome or a question of it is known, never the same line twice in a
  conversation (the exact words, whitespace and case aside), never a line whose step has returned.
* A ``say`` line (the thinker's own choice to speak up: a finding, a delay) waits only for the user and for Yuki's
  own speech.

What is said goes into the talker's conversation as Yuki's own words (Pipecat appends what a ``TTSSpeakFrame``
played to the context), so the talker carries on from it and never repeats it; the spoken-line check
(:mod:`yuki.voice.speechguard`) still hears every line first. The voice log gets sizes and times only.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable


@dataclass
class _Line:
    """One line waiting to be said (content: never logged)."""

    job: int
    text: str
    kind: str                 # "step" | "say"
    seq: int                  # the job's step it belongs to (a say line: the step it came with)
    offered_at: float = field(default_factory=time.monotonic)
    step_started: float | None = None


def _key(text: str) -> str:
    """The words of a line for telling a repeat: case and spacing are formatting, never meaning."""
    return " ".join((text or "").casefold().split())


class StepLines:
    """Says the thinker's step and say lines at the right moments, by code, once each.

    Args:
        state: ``() -> dict`` with ``user_speaking``, ``user_sound``, ``bot_busy`` (speaking, generating or words on
            their way to the voice) and ``quiet_for`` (seconds nobody has spoken).
        speak: ``async (text)``: put the words into the voice now (a ``TTSSpeakFrame``).
        on_said: ``(job, kind, text)`` after a line went to the voice (the job log), or ``None``.
        log: The voice log (content-free), or ``None``.
    """

    def __init__(self, *, state: Callable[[], dict[str, Any]], speak: Callable[[str], Awaitable[None]],
                 on_said: Callable[[int, str, str], None] | None = None, log: Any = None,
                 silence_s: float = 2.5, after_s: float = 0.8, long_silence_s: float = 5.0,
                 max_wait_s: float = 12.0) -> None:
        self._state = state
        self._speak = speak
        self._on_said = on_said
        self.log = log
        self.silence_s = silence_s
        self.after_s = after_s
        self.long_silence_s = long_silence_s
        #: A line not said within this of being offered is let go (the moment passed).
        self.max_wait_s = max_wait_s
        self._pending: list[_Line] = []
        #: The step each job is on now, and when it started: ``{job: (seq, started_at)}``; a finished step is removed.
        self._running: dict[int, tuple[int, float]] = {}
        self._closed: set[int] = set()
        self._said: deque[str] = deque(maxlen=40)
        #: ``(job, seq, words)`` of every line sent: the same line offered again (the finished call after its streamed
        #: words) is the same line, not a second offer.
        self._sent_for: set[tuple[int, int, str]] = set()
        self._poke = asyncio.Event()
        #: The step line sent to the voice and not heard yet, as ``(job, seq)`` (``line_stale``).
        self._airing: tuple[int, int, str] | None = None
        self._task: asyncio.Task | None = None
        self.stats = {"offered": 0, "spoken": 0, "dropped": 0}

    # -- from the bridge (on the voice loop) ------------------------------------------

    def step_started(self, job: int, seq: int) -> None:
        """The job's step ``seq`` began running now (its tool call started)."""
        if job in self._closed:
            return
        # A line of an earlier step that was never said is past: the job is doing something else now.
        self._drop(lambda line: line.job == job and line.kind == "step" and line.seq < seq, "newer_step")
        self._running[job] = (seq, time.monotonic())
        for line in self._pending:
            if line.job == job and line.kind == "step" and line.seq == seq and line.step_started is None:
                line.step_started = self._running[job][1]
        self.poke()

    def step_ended(self, job: int, seq: int, ok: bool = True) -> None:
        """The job's step ``seq`` returned (worked or not): a line about it not said yet is past."""
        running = self._running.get(job)
        if running is not None and running[0] == seq:
            del self._running[job]
        self._drop(lambda line: line.job == job and line.kind == "step" and line.seq <= seq,
                   "step_ended" if ok else "step_failed")

    def offer(self, job: int, text: str, *, kind: str = "step", seq: int = 0) -> None:
        """A line the thinker wrote for step ``seq`` of ``job`` (or a ``say`` line)."""
        text = " ".join((text or "").split())
        if not text or job in self._closed:
            return
        if any(line.job == job and line.kind == kind and line.seq == seq and _key(line.text) == _key(text)
               for line in self._pending) or (job, seq, _key(text)) in self._sent_for:
            return   # the same line again (streamed first, then with the finished call): one line, not two
        self.stats["offered"] += 1
        running = self._running.get(job)
        started = running[1] if running is not None and running[0] == seq else None
        if kind == "step":
            # One step line waits per job: a newer step's line replaces an older one still waiting.
            self._drop(lambda line: line.job == job and line.kind == "step", "superseded")
        self._pending.append(_Line(job=job, text=text, kind=kind, seq=seq, step_started=started))
        self._log("offered", kind=kind, chars=len(text), step_running=started is not None)
        self._ensure()
        self.poke()

    def hold(self, job: int) -> None:
        """The job asks the user something now: its waiting lines go (its later steps may have their own)."""
        self._drop(lambda line: line.job == job, "question")

    def closed(self, job: int, why: str = "outcome") -> None:
        """The job's outcome (or a question of it) is known: nothing more of it is said as a step or say line."""
        self._closed.add(job)
        self._running.pop(job, None)
        self._drop(lambda line: line.job == job, why)

    def poke(self) -> None:
        self._poke.set()

    def line_stale(self, text: str | None = None) -> bool:
        """Asked by the voice right before a reply's first audio (once per line sent): the step line on its way
        belongs to a step that has returned, or to a job whose outcome is known, so it is dropped unheard (v3.14b).
        ``text``: the words about to be heard; another reply (the line was cut, the talker spoke first) is never
        taken for the line, whatever the step's state."""
        airing, self._airing = self._airing, None
        if airing is None:
            return False
        job, seq, words = airing
        if text is not None and _key(text) != words:
            return False
        running = self._running.get(job)
        stale = job in self._closed or running is None or running[0] != seq
        if stale:
            self.stats["dropped"] += 1
            self.stats["spoken"] = max(0, self.stats["spoken"] - 1)
            self._log("dropped", kind="step", why="step_ended_before_heard")
        return stale

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None
        if self.log is not None and self.stats["offered"]:
            self.log.event("step_line", op="summary", **self.stats)

    # -- deciding -------------------------------------------------------------------------

    def _ensure(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._run(), name="yuki-voice-steplines")

    def due(self, line: _Line, s: dict[str, Any], now: float) -> tuple[bool, str]:
        """Whether ``line`` may be said now, and why not (plumbing on times and states only)."""
        if s.get("user_speaking") or s.get("user_sound"):
            return False, "user"
        if s.get("bot_busy"):
            return False, "bot_busy"
        if line.kind == "say":
            return True, ""
        if s.get("call_mode"):
            return False, "call"   # on a call only what they asked about is said: its outcome, not the steps
        quiet = float(s.get("quiet_for") or 0.0)
        running = self._running.get(line.job)
        if running is None or running[0] != line.seq:
            return False, "step_not_running"
        if quiet >= self.long_silence_s:
            return True, ""
        if quiet < self.silence_s:
            return False, "not_quiet_yet"
        if now - running[1] < self.after_s:
            return False, "step_just_started"
        return True, ""

    async def _run(self) -> None:
        while True:
            self._poke.clear()
            if not self._pending:
                try:
                    await asyncio.wait_for(self._poke.wait(), timeout=30.0)
                except asyncio.TimeoutError:
                    return
                continue
            now = time.monotonic()
            self._drop(lambda line: now - line.offered_at > self.max_wait_s, "too_late")
            s = self._state()
            chosen = None
            for line in list(self._pending):
                ok, _why = self.due(line, s, now)
                if ok:
                    chosen = line
                    break
            if chosen is None:
                try:
                    await asyncio.wait_for(self._poke.wait(), timeout=0.1)
                except asyncio.TimeoutError:
                    pass
                continue
            self._pending.remove(chosen)
            key = _key(chosen.text)
            if key in self._said:
                self.stats["dropped"] += 1
                self._log("dropped", kind=chosen.kind, why="said_before", chars=len(chosen.text))
                continue
            self._said.append(key)
            self._sent_for.add((chosen.job, chosen.seq, key))
            self.stats["spoken"] += 1
            running = self._running.get(chosen.job)
            self._log("spoken", kind=chosen.kind, chars=len(chosen.text),
                      waited_ms=round((now - chosen.offered_at) * 1000),
                      step_ms=round((now - running[1]) * 1000) if running else None,
                      quiet_ms=round(float(s.get("quiet_for") or 0.0) * 1000))
            # The voice asks, right before the line's first audio, whether its step still runs (``line_stale``): the
            # first audio comes ~1 s after this (the voice's first byte), and offline 29 Sept with measured step
            # durations a line sent 0.81 s into a 0.84 s step was heard at 1.85 s, after the step had ended.
            self._airing = (chosen.job, chosen.seq, key) if chosen.kind == "step" else None
            try:
                await self._speak(chosen.text)
            except Exception as exc:  # noqa: BLE001 - a line that could not be said is only a line
                self._airing = None
                self._log("error", error=type(exc).__name__)
                continue
            if self._on_said is not None:
                try:
                    self._on_said(chosen.job, chosen.kind, chosen.text)
                except Exception:  # noqa: BLE001
                    pass

    def _drop(self, which: Callable[[_Line], bool], why: str) -> None:
        gone = [line for line in self._pending if which(line)]
        if not gone:
            return
        self._pending = [line for line in self._pending if line not in gone]
        for line in gone:
            self.stats["dropped"] += 1
            self._log("dropped", kind=line.kind, why=why, chars=len(line.text),
                      waited_ms=round((time.monotonic() - line.offered_at) * 1000))

    def _log(self, op: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event("step_line", op=op, **fields)
