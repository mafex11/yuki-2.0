"""The ONE agent's per-turn budget (phase 4 design §2.3, addendum must-fix 6): the runaway guard, in code.

One :class:`TurnBudget` per user turn (voice) or request (typed). Every number is a code fact, never a reading of words:

* **Rounds**: model calls for the turn (voice 8, typed 12).
* **Wall time** from round 1 (or the turn's first tool call, whichever comes first), waits excluded (a send's countdown; on typed also asks, pauses and desk-lease waits:
  :meth:`TurnBudget.excluded`). Past the soft limit (voice 45 s, typed 120 s) an acting call is refused with
  :data:`LINE_SPENT` ("say what's left in one line and one other way"). At the hard stop (voice 60 s, the same as
  ``voice.hands.Hands.SKILL_HAND_MAX_S``, so a skill run started at round 1 is never cut before its own backstop; typed
  300 s of work) the driver interrupts the generation, says the owed line (:data:`LINE_REST_LEFT`, rule #149) and
  :meth:`TurnBudget.close` s the turn: every later result settles silently (:attr:`TurnBudget.closed`). A hard stop
  :meth:`TurnBudget.round` or :meth:`TurnBudget.before_call` finds first closes the turn itself.
* **Answers**: a turn with no act yet gets at most ``reads`` read calls (voice 8, typed 12) or ``answer_s`` of work.
* **Retry window**: after the first act that did not take effect, other undoable tries go for 8 s
  (``voice.hands.RETRY_BUDGET_S``), never after; a lasting act (a send, a join) that missed is never tried again in the
  turn.
* **Same failing step**: refused at once, keyed by :func:`step_key` (``voice.hands._step_key``'s identity).
* **Refusals**: the second refusal in a turn settles it: no model run (``run_llm=False``), code's rule-#149 line.

Every settle (the hard stop, the rounds, the second refusal) closes the turn: a later call in the same reply, or a
late result, settles silently, so code's line is said once. The driver calls :meth:`TurnBudget.round` before **every**
model call of the turn; the gate calls :meth:`TurnBudget.before_call` before every agent call (which starts the clock
too, so a driver that forgot :meth:`TurnBudget.round` still has its time limits).

Thread-safe; ``clock`` is injectable for checks. Logs nothing (the runner logs the content-free counts it reads here:
:meth:`TurnBudget.facts`).
"""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator, Mapping

#: Rule #149 (18 Oct): stuck, say so in one line and offer one other way, never loop. Code's own line, said when the
#: turn is settled by code (``voice.hands._REST_LEFT_LINE``'s words).
LINE_REST_LEFT = "I couldn't get to the rest of it. Want me to try another way?"
#: What an acting call returns once the turn's rounds or soft time are spent (to the model: one more line, no act).
LINE_SPENT = ("Not done: this turn's time for acting is spent, nothing was done. In one line say what is left of their "
              "request and offer one other way; no other call.")
#: What a read returns once the turn's reads or answer time are spent.
LINE_READS_SPENT = ("Not read: this turn's reads are spent. Answer from what you have, or say in one line what you "
                    "could not find and offer one other way.")
#: The same step as one that already failed this turn (rule #149: never the same failing step again).
LINE_SAME_STEP = ("Not tried: that very step already failed this turn, nothing was done. Try another way, or say in "
                  "one line what did not work and offer one other way.")
#: The retry window is over (``voice.hands._HALTED_NOTE``'s words).
LINE_RETRY_SPENT = ("Not tried: nothing was done; your time for other ways in this turn is spent. In one line say what "
                    "did not work and offer one other way, unless you said it already; no other call.")
#: A lasting act (a send, a join) is never tried again in a turn once one missed (it may have gone).
LINE_LASTING_RETRY = ("Not tried: a send or join already did not go through this turn and may have gone partly; it is "
                      "never tried again on its own. Say so in one line.")
#: The turn was closed by code (the hard stop): a late result settles with no words.
LINE_CLOSED = "The turn is over; nothing was done. Say nothing."


@dataclass(frozen=True)
class Limits:
    """One channel's numbers (seconds and counts)."""

    rounds: int
    soft_s: float
    hard_s: float
    reads: int
    answer_s: float
    retry_s: float = 8.0
    refusals: int = 2


#: Voice: the design's numbers (planner ``act_calls``/``answer_calls``/``answer_s``, ``hands.RETRY_BUDGET_S``,
#: ``hands.Hands.SKILL_HAND_MAX_S``).
VOICE = Limits(rounds=8, soft_s=45.0, hard_s=60.0, reads=8, answer_s=30.0)
#: Typed: its own budget, waits not counted (asks, pauses, lease waits; :meth:`TurnBudget.excluded`).
TYPED = Limits(rounds=12, soft_s=120.0, hard_s=300.0, reads=12, answer_s=90.0)


def limits_for(channel: str, settings: Any = None) -> Limits:
    """The channel's :class:`Limits`, each number overridable from the settings (``one_<channel>_<field>``, e.g.
    ``one_voice_hard_s``) for the final test; a bad value keeps the default."""
    base = VOICE if str(channel or "") == "voice" else TYPED
    if settings is None:
        return base
    out: dict[str, Any] = {}
    for name in ("rounds", "soft_s", "hard_s", "reads", "answer_s", "retry_s", "refusals"):
        raw = getattr(settings, f"one_{'voice' if base is VOICE else 'typed'}_{name}", None)
        if raw is None or isinstance(raw, bool):
            continue
        try:
            value = type(getattr(base, name))(raw)
        except (TypeError, ValueError):
            continue
        if value > 0:
            out[name] = value
    return Limits(**{**base.__dict__, **out}) if out else base


def step_key(name: str, args: Mapping[str, Any] | None) -> str:
    """What a call acts on, for telling one step from the same step again: its words for the user (``say_first``,
    ``say_when_done``, ``named``, ``then``) left out, an address and a name tidied (``voice.hands._step_key``)."""
    keep = {k: v for k, v in dict(args or {}).items() if k not in ("say_first", "say_when_done", "named", "then")}
    if isinstance(keep.get("url"), str):
        keep["url"] = keep["url"].strip().rstrip("/").casefold()
    if isinstance(keep.get("name"), str):
        keep["name"] = " ".join(keep["name"].split()).casefold()
    return str(name) + json.dumps(keep, sort_keys=True, default=str)


@dataclass(frozen=True)
class Spend:
    """The budget's answer for one call or round. ``ok``: go on. Else ``why`` (a code word: ``closed``, ``hard``,
    ``rounds``, ``soft``, ``reads``, ``answer_s``, ``same_step``, ``retry``, ``lasting_retry``, ``refusals``),
    ``line`` (the tool result for the model, or code's line when ``settle``), ``settle``: the turn ends by code with
    ``line`` said and no model run (``run_llm=False``), ``silent``: it ends with nothing said (a closed turn)."""

    ok: bool
    why: str = ""
    line: str = ""
    settle: bool = False
    silent: bool = False


OK = Spend(True)


class TurnBudget:
    """One turn's budget (module docstring). ``channel``: ``voice`` or ``typed``. :meth:`start` at round 1 (the first
    :meth:`round` starts it too)."""

    def __init__(self, channel: str = "voice", limits: Limits | None = None, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.channel = "voice" if channel == "voice" else "typed"
        self.limits = limits or limits_for(self.channel)
        self._clock = clock
        self._lock = threading.Lock()
        self.started: float | None = None
        self._excluded_s = 0.0
        self._paused_at: float | None = None
        self._pauses = 0
        self.rounds = 0
        self.acts = 0
        self.reads = 0
        self.refusals = 0
        #: Counted seconds (:meth:`elapsed`) at the turn's first act that did not take effect; the retry window's start.
        self.first_miss: float | None = None
        self.failed: set[str] = set()
        self.lasting_missed = False
        self.closed = False
        self.closed_why = ""

    # -- time ---------------------------------------------------------------------------------------------------------

    def start(self) -> None:
        """Round 1 begins: the wall clock starts (idempotent)."""
        with self._lock:
            if self.started is None:
                self.started = self._clock()

    def pause(self) -> None:
        """A wait starts that the budget does not count (a countdown; typed: an ask, a pause, a lease wait). Nested
        pauses count once."""
        with self._lock:
            self._pauses += 1
            if self._pauses == 1:
                self._paused_at = self._clock()

    def resume(self) -> None:
        """The wait ended."""
        with self._lock:
            if self._pauses <= 0:
                return
            self._pauses -= 1
            if self._pauses == 0 and self._paused_at is not None:
                self._excluded_s += max(0.0, self._clock() - self._paused_at)
                self._paused_at = None

    @contextmanager
    def excluded(self) -> Iterator[None]:
        """``with budget.excluded(): ...``: the time inside is not counted."""
        self.pause()
        try:
            yield
        finally:
            self.resume()

    def elapsed(self) -> float:
        """Seconds counted since round 1 (waits excluded); 0 before it."""
        with self._lock:
            return self._elapsed()

    def _elapsed(self) -> float:
        if self.started is None:
            return 0.0
        now = self._clock()
        paused = (now - self._paused_at) if self._paused_at is not None else 0.0
        return max(0.0, now - self.started - self._excluded_s - paused)

    def hard_left(self) -> float:
        """Seconds of counted time until the hard stop (for the driver's timer; paused time does not run it down)."""
        with self._lock:
            return max(0.0, self.limits.hard_s - self._elapsed())

    def hard_due(self) -> bool:
        """The hard stop is reached (the driver interrupts the generation, says :data:`LINE_REST_LEFT` and
        :meth:`close` s the turn)."""
        with self._lock:
            return self.started is not None and self._elapsed() >= self.limits.hard_s

    def close(self, why: str = "closed") -> None:
        """The turn is over by code: every later call and result settles silently."""
        with self._lock:
            self._close(why)

    def _close(self, why: str) -> None:
        """:meth:`close` with the lock held."""
        if not self.closed:
            self.closed, self.closed_why = True, str(why or "closed")

    # -- rounds and calls ---------------------------------------------------------------------------------------------

    def round(self) -> Spend:
        """Before each model call of the turn (the driver's, every one). Over the rounds or past the hard stop: the
        turn settles by code with rule #149's line (no model call is made) and is closed."""
        self.start()
        with self._lock:
            if self.closed:
                return Spend(False, "closed", LINE_CLOSED, settle=True, silent=True)
            if self._elapsed() >= self.limits.hard_s:
                self._close("hard")
                return Spend(False, "hard", LINE_REST_LEFT, settle=True)
            if self.rounds >= self.limits.rounds:
                self._close("rounds")
                return Spend(False, "rounds", LINE_REST_LEFT, settle=True)
            self.rounds += 1
            return OK

    def before_call(self, name: str, args: Mapping[str, Any] | None = None, *, acts: bool = True,
                    lasting: bool = False) -> Spend:
        """Before one tool call (``acts``: it acts on the desktop; ``lasting``: a send or a join). The first call starts
        the clock when no :meth:`round` did; the hard stop closes the turn. Never raises."""
        self.start()
        with self._lock:
            if self.closed:
                return Spend(False, "closed", LINE_CLOSED, settle=True, silent=True)
            now_s = self._elapsed()
            if now_s >= self.limits.hard_s:
                self._close("hard")
                return Spend(False, "hard", LINE_REST_LEFT, settle=True)
            if not acts:
                if self.reads >= self.limits.reads:
                    return Spend(False, "reads", LINE_READS_SPENT)
                if self.acts == 0 and now_s >= self.limits.answer_s:
                    return Spend(False, "answer_s", LINE_READS_SPENT)
                self.reads += 1
                return OK
            if now_s >= self.limits.soft_s:
                return Spend(False, "soft", LINE_SPENT)
            key = step_key(name, args)
            if key in self.failed:
                return Spend(False, "same_step", LINE_SAME_STEP)
            if lasting and self.lasting_missed:
                return Spend(False, "lasting_retry", LINE_LASTING_RETRY)
            # oRun's note to oCore (phase 4 integration): the retry window runs on the counted clock, so a wait the
            # budget excludes (an ask, the send countdown, a typed lease wait) does not run it down
            if self.first_miss is not None and now_s - self.first_miss > self.limits.retry_s:
                return Spend(False, "retry", LINE_RETRY_SPENT)
            self.acts += 1
            return OK

    def lasting(self) -> Spend:
        """After the gate found a call lasting (a send with its countdown, a join): refused when one already missed
        this turn (it may have gone partly; never tried again)."""
        with self._lock:
            if self.closed:
                return Spend(False, "closed", LINE_CLOSED, settle=True, silent=True)
            if self.lasting_missed:
                return Spend(False, "lasting_retry", LINE_LASTING_RETRY)
            return OK

    def after_call(self, name: str, args: Mapping[str, Any] | None = None, *, acts: bool = True, ok: bool = True,
                   lasting: bool = False) -> None:
        """After one acting call: a call that did not take effect opens the retry window (its first miss) and is never
        tried again this turn; a lasting one that missed stops every later lasting act."""
        if not acts or ok:
            return
        with self._lock:
            if self.first_miss is None:
                self.first_miss = self._elapsed()   # counted seconds since round 1, as before_call reads it
            self.failed.add(step_key(name, args))
            if lasting:
                self.lasting_missed = True

    def refused(self, final_line: str = "") -> Spend:
        """A call was refused (by the gate or this budget). The second refusal of the turn settles it by code: no model
        run, ``final_line`` when the refusal is one said as it is (a game, Act off), else rule #149's line; the turn is
        closed (a later call of the same reply settles silently)."""
        with self._lock:
            self.refusals += 1
            if self.refusals >= self.limits.refusals:
                self._close("refusals")
                return Spend(False, "refusals", str(final_line or "") or LINE_REST_LEFT, settle=True)
            return OK

    def late(self) -> bool:
        """Whether a result arriving now settles silently (the turn was closed)."""
        return self.closed

    def facts(self) -> dict[str, Any]:
        """Content-free counts for the runner's log line."""
        with self._lock:
            return {"channel": self.channel, "rounds": self.rounds, "acts": self.acts, "reads": self.reads,
                    "refusals": self.refusals, "elapsed_s": round(self._elapsed(), 2), "missed": len(self.failed),
                    "closed": self.closed_why if self.closed else ""}


__all__ = ["LINE_CLOSED", "LINE_LASTING_RETRY", "LINE_READS_SPENT", "LINE_REST_LEFT", "LINE_RETRY_SPENT",
           "LINE_SAME_STEP", "LINE_SPENT", "Limits", "OK", "Spend", "TYPED", "TurnBudget", "VOICE", "limits_for",
           "step_key"]
