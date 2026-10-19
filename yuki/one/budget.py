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
* **Same failing step**: refused at once, keyed by :func:`step_key` (``voice.hands._step_key``'s identity, on the
  resolved target: the window the gate found, whatever named it; one20, final-D one-core §3.4).
* **Refusals**: the second refusal in a turn settles it: no model run (``run_llm=False``), code's rule-#149 line.
* **Tidy steps** (one20): a step that undoes the turn's own side effect (the gate's row 8, an Escape on a window the
  turn's own act opened something in) is never "another way" after a miss: the retry window and the same-step bar do
  not apply to it (the gate's two-steps cap does).
* **The closing hold** (one20, contract C9: :meth:`TurnBudget.closing_hold`): the turn's one miss (an act that did not
  take, or a skill run that handed back with nothing done, :meth:`TurnBudget.unfinished_run`), no act tried after it,
  the retry window still open: a closing reply is held once by the voice driver with :data:`LINE_HOLD_MISS`.

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
#: One20 (contract C9, final-D S5): why a closing reply (a quiet, or words with no call) is held once: the turn's one
#: miss, nothing tried after it, time left for another way. The voice driver returns it to the agent as the refusal.
LINE_HOLD_MISS = ("Not done yet: one step of their request didn't take and nothing else was tried. Try one other way "
                  "now with an undoable act (never the same step), then say in a few words what is left.")


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


def _target_window(args: Mapping[str, Any], target: Mapping[str, Any] | None) -> int | None:
    """The window a call acts on as resolved: the target the gate was shown (``hwnd``), else the call's own
    ``window``; ``None``: no window (an address, a skill)."""
    for raw in ((target or {}).get("hwnd"), args.get("window")):
        if raw is None or isinstance(raw, bool):
            continue
        try:
            got = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if got:
            return got
    return None


def step_key(name: str, args: Mapping[str, Any] | None, target: Mapping[str, Any] | None = None) -> str:
    """What a call acts on, for telling one step from the same step again: its words for the user (``say_first``,
    ``say_when_done``, ``named``, ``then``) left out, an address and a name tidied (``voice.hands._step_key``).

    One20 (final-D one-core §3.4: the same tab close came again with ``app`` and ``window`` dropped, not caught):
    keyed on the resolved target when the gate found a window (``target["hwnd"]``, else the call's ``window``): that
    window stands for whatever named it (``window``, ``app``). A tab call keeps its ``named`` (folded): the tab is the
    target inside that window, and a browser's tab names carry live text (a memory figure) that changes between two
    reads, so the tab's own title is not the key."""
    raw = dict(args or {})
    keep = {k: v for k, v in raw.items() if k not in ("say_first", "say_when_done", "named", "then")}
    if isinstance(keep.get("url"), str):
        keep["url"] = keep["url"].strip().rstrip("/").casefold()
    if isinstance(keep.get("name"), str):
        keep["name"] = " ".join(keep["name"].split()).casefold()
    window = _target_window(raw, target)
    if window is not None:
        keep.pop("window", None)
        keep.pop("app", None)
        keep["@window"] = window
        if str(name) == "tab" and isinstance(raw.get("named"), str):
            keep["named"] = " ".join(raw["named"].split()).casefold()
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
        #: One20 (C9): skill runs that handed back with nothing on the PC touched (:meth:`unfinished_run`): not a miss
        #: of the retry window (no verb ran), but a part of their request still open.
        self.unfinished = 0
        #: :attr:`acts` when the newest miss (or unfinished run) came back: an act after it raises the count.
        self._acts_at_miss: int | None = None
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
                    lasting: bool = False, target: Mapping[str, Any] | None = None, tidy: bool = False) -> Spend:
        """Before one tool call (``acts``: it acts on the desktop; ``lasting``: a send or a join). The first call starts
        the clock when no :meth:`round` did; the hard stop closes the turn. ``target``: what the gate found it acts on
        (:func:`step_key`). ``tidy``: a step that undoes the turn's own side effect (the gate's reading, one20): the
        same-step bar and the retry window don't apply to it. Never raises."""
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
            if tidy and not lasting:
                # one20: undoing the turn's own side effect is never "another way" after a miss (the gate holds it to
                # its own windows and two steps)
                self.acts += 1
                return OK
            key = step_key(name, args, target)
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
                   lasting: bool = False, target: Mapping[str, Any] | None = None) -> None:
        """After one acting call: a call that did not take effect opens the retry window (its first miss) and is never
        tried again this turn (keyed on ``target`` as :meth:`before_call` read it); a lasting one that missed stops
        every later lasting act."""
        if not acts or ok:
            return
        with self._lock:
            if self.first_miss is None:
                self.first_miss = self._elapsed()   # counted seconds since round 1, as before_call reads it
            self.failed.add(step_key(name, args, target))
            self._acts_at_miss = self.acts
            if lasting:
                self.lasting_missed = True

    def unfinished_run(self) -> None:
        """One20 (C9): a skill run handed back with nothing on the PC touched (no route, a slot no route found, a rung
        that never read): a part of their request is still open. Not a miss of the retry window (no verb ran, the
        steps after it have their time), but :meth:`closing_hold` counts it."""
        with self._lock:
            self.unfinished += 1
            self._acts_at_miss = self.acts

    def closing_hold(self) -> str | None:
        """One20 (contract C9, final-D S5): why a closing reply (a quiet, or words with no call) should be held once:
        the turn has exactly one miss (an act that did not take, or one :meth:`unfinished_run`), no act was tried
        after it, it was no lasting act (never tried again), and the time for another way is still open (the retry
        window after a verb's miss; the soft limit). ``None``: no hold. Pure: reads counts only, changes nothing (the
        voice driver holds once). Never raises."""
        try:
            with self._lock:
                if self.closed or self.lasting_missed or self._acts_at_miss is None:
                    return None
                if len(self.failed) + self.unfinished != 1 or self.acts != self._acts_at_miss:
                    return None
                now_s = self._elapsed()
                if now_s >= self.limits.soft_s or now_s >= self.limits.hard_s:
                    return None
                if self.first_miss is not None and now_s - self.first_miss > self.limits.retry_s:
                    return None
                return LINE_HOLD_MISS
        except Exception:  # noqa: BLE001 - no hold
            return None

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
                    "unfinished": self.unfinished, "closed": self.closed_why if self.closed else ""}


#: Contract C9's name ("budget.py ``Turn.closing_hold()``"): the turn's budget. The gate's ``policy.Turn`` has a
#: ``closing_hold()`` too (this one's miss reason only, contract C12), so a driver may ask either through ``getattr``.
Turn = TurnBudget


__all__ = ["LINE_CLOSED", "LINE_HOLD_MISS", "LINE_LASTING_RETRY", "LINE_READS_SPENT", "LINE_REST_LEFT",
           "LINE_RETRY_SPENT", "LINE_SAME_STEP", "LINE_SPENT", "Limits", "OK", "Spend", "TYPED", "Turn", "TurnBudget",
           "VOICE", "limits_for", "step_key"]
