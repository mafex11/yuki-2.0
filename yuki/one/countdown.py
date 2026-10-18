"""ONE send countdown host for voice and typed (phase 4 addendum, must-fix 5).

A send the gate allowed with a countdown (``Decision.countdown``) waits here before the runner presses anything: the
island shows ``SendPending("counting")`` with the exact text (the user's 3 Oct "Sending to X ... 3 s to stop";
``ui.runtime.SEND_COUNTDOWN_S``), voice says "Sending to <name>" first, as ``brain.agent.Brain._send_countdown`` does
today (its mechanism, push-style, with one more state: a pause).

* **Stop / Esc** (:meth:`Countdown.cancel`): ``cancelled``, nothing sent.
* **A chip** (:meth:`Countdown.swap`, who or where): ``changed`` with the swap; the call is not run.
* **The user starts speaking** (:meth:`Countdown.user_speaking`, called by the voice lane at Flux's start of turn): the
  countdown **pauses** at once (the island card closes: ``SendPending("paused")``). Their words then decide through the
  model: :meth:`Countdown.user_done` with words ends it ``changed`` (nothing sent; the model reads their words in its
  next round and sends again, changes it, or drops it); with no words (a cough, a false start) it counts down again
  for the time it had left. A pause nobody ends within :data:`PAUSE_MAX_S` ends ``changed``.
* **The time runs out**: the game and the Act switch are read again (``recheck``: the gate's
  ``Turn.recheck_send``); either stops it as ``changed`` with its line. Else ``sent``: the runner sends now.

``counted`` (the send grant's ``counted``, ``skills.uia_route.send_grant``): a countdown the user could stop ran: it
was open at least :data:`COUNTED_MIN_S` and ran out. ``seconds <= 0`` returns ``sent`` at once, not counted (a host
with no countdown: Yuki's own wording then needs their yes, review r1 L3).

Every method is thread-safe; :meth:`Countdown.run` blocks its caller's thread (the runner's worker), never the event
loop. Content-free logs (state, seconds, chars).
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

#: The countdown when the host gives none (s): ``ui.runtime.SEND_COUNTDOWN_S``, the executor's ``DEFAULT_COUNTDOWN_S``.
DEFAULT_SECONDS = 3.0
#: A countdown counts as one the user could stop only when it was open at least this long (``executor.COUNTED_MIN_S``).
COUNTED_MIN_S = 0.5
#: A pause (the user speaking) that nobody ends within this long ends the countdown, nothing sent.
PAUSE_MAX_S = 20.0
#: What voice says as the countdown opens (``brain.agent.Brain._send_countdown``'s words).
LINE_SENDING = "Sending to {who}"

SENT, CANCELLED, CHANGED = "sent", "cancelled", "changed"


@dataclass(frozen=True)
class CountdownResult:
    """``state``: ``sent`` (go: send it now), ``cancelled`` (Stop/Esc: nothing sent), ``changed`` (nothing sent: the
    user spoke, tapped a chip, or the game or Act switch changed). ``why``: a code word (``ran_out``, ``stop``,
    ``spoke``, ``pause_timeout``, ``swap``, ``game``, ``act_off``, ``no_countdown``, ``error``). ``line``: what the user
    hears for a refusal after the countdown (a game came up). ``counted``: a countdown they could stop ran out.
    ``swap``: ``{"kind", "value", "was"}`` for a chip. ``waited_s``: seconds it was open (the budget excludes them)."""

    state: str
    why: str = ""
    line: str = ""
    counted: bool = False
    swap: Mapping[str, str] | None = None
    waited_s: float = 0.0


@dataclass
class _Open:
    info: dict[str, Any]
    deadline: float
    left: float
    paused: bool = False
    paused_at: float = 0.0
    outcome: tuple[str, str] | None = None
    swap: dict[str, str] | None = None
    words_after_pause: bool | None = None
    wake: threading.Event = field(default_factory=threading.Event)


class Countdown:
    """The one countdown host (module docstring).

    Args:
        publish: ``publish(SendPending)``: to the island (the runtime's ``send_pending`` signal through the lane, or the
            voice's own relay). Called on the countdown's thread.
        seconds: The countdown (``Settings``/runtime's ``SEND_COUNTDOWN_S``); ``None``: :data:`DEFAULT_SECONDS`.
        say: ``say(text)``: voice only, "Sending to <name>" as it opens (``None``: typed, nothing said).
        said_name: Memory's own name for the recipient (``consent.spoken_name`` over the people index), else as given.
        log: ``log(event, **fields)``: content-free.
        clock: Injectable for checks.
    """

    def __init__(self, publish: Callable[[Any], None] | None = None, *, seconds: float | None = None,
                 say: Callable[[str], None] | None = None, said_name: Callable[[str], str] | None = None,
                 log: Callable[..., None] | None = None, clock: Callable[[], float] = time.monotonic) -> None:
        self._publish = publish
        self.seconds = DEFAULT_SECONDS if seconds is None else float(seconds)
        self._say = say
        self._said_name = said_name
        self._log = log
        self._clock = clock
        self._lock = threading.Lock()
        self._open: _Open | None = None

    # -- the user's side (any thread) ----------------------------------------------------------------------------------

    @property
    def counting(self) -> bool:
        """A countdown is open (counting or paused)."""
        with self._lock:
            return self._open is not None

    def cancel(self) -> bool:
        """Stop / Esc: the open countdown ends ``cancelled``. ``False``: none was open."""
        return self._end(CANCELLED, "stop")

    def swap(self, kind: str, value: str) -> bool:
        """A chip tapped (``who`` / ``where``, one of the alternatives shown): ends ``changed`` with the swap."""
        with self._lock:
            cur = self._open
            if cur is None or cur.outcome is not None or kind not in ("who", "where") or not str(value or "").strip():
                return False
            was = cur.info.get("who") if kind == "who" else (cur.info.get("where") or cur.info.get("app"))
            cur.swap = {"kind": kind, "value": " ".join(str(value).split()), "was": str(was or "")}
            cur.outcome = (CHANGED, "swap")
            cur.wake.set()
            return True

    def user_speaking(self) -> bool:
        """The user started speaking (the voice lane's start of turn): the countdown pauses at once. ``False``: none
        was counting."""
        with self._lock:
            cur = self._open
            if cur is None or cur.outcome is not None or cur.paused:
                return False
            now = self._clock()
            cur.left = max(0.0, cur.deadline - now)
            cur.paused, cur.paused_at = True, now
            cur.wake.set()
            return True

    def user_done(self, words: bool) -> bool:
        """The user's turn after a pause ended: with ``words`` the countdown ends ``changed`` (the model decides from
        their words); without (nothing recognised) it counts down again for the time it had left."""
        with self._lock:
            cur = self._open
            if cur is None or cur.outcome is not None or not cur.paused:
                return False
            cur.words_after_pause = bool(words)
            cur.wake.set()
            return True

    def _end(self, state: str, why: str) -> bool:
        with self._lock:
            cur = self._open
            if cur is None or cur.outcome is not None:
                return False
            cur.outcome = (state, why)
            cur.wake.set()
            return True

    # -- the runner's side ---------------------------------------------------------------------------------------------

    def run(self, *, who: str = "", where: str = "", app: str = "", text: str = "", typed: bool = False,
            window: str = "", who_alts: tuple[str, ...] = (), where_alts: tuple[str, ...] = (),
            from_memory: bool = False, recheck: Callable[[], tuple[str, str] | None] | None = None
            ) -> CountdownResult:
        """One countdown, blocking this thread until it ends. ``recheck()`` after it ran out: ``None`` (go on) or
        ``(why, line)`` (a game came up, Act was switched off: ``changed``, nothing sent). Never raises."""
        seconds = float(self.seconds)
        if seconds <= 0:
            return self._after(CountdownResult(SENT, "no_countdown"), recheck)
        try:
            if self._said_name is not None and who:
                who = str(self._said_name(who) or who)
        except Exception:  # noqa: BLE001 - the name as given
            pass
        info = {"who": who, "where": where, "app": app, "text": text, "typed": bool(typed), "window": window,
                "who_alts": tuple(who_alts or ()), "where_alts": tuple(where_alts or ()),
                "from_memory": bool(from_memory)}
        started = self._clock()
        cur = _Open(info=info, deadline=started + seconds, left=seconds)
        with self._lock:
            if self._open is not None:
                return CountdownResult(CANCELLED, "busy")   # one countdown at a time: never two sends counting
            self._open = cur
        try:
            self._emit_log("one_countdown", state="counting", seconds=seconds, chars=len(text or ""), typed=bool(typed))
            if self._say is not None and who:
                self._safe(self._say, LINE_SENDING.format(who=who))
            self._emit("counting", seconds, info)
            state, why = self._wait(cur, seconds, info)
        except Exception:  # noqa: BLE001 - nothing is sent on an error
            state, why = CHANGED, "error"
        finally:
            with self._lock:
                self._open = None
        waited = max(0.0, self._clock() - started)
        if state == SENT:
            self._emit("going", seconds, info)
        elif state == CANCELLED:
            self._emit("stopped", seconds, info)
        elif why == "swap" and cur.swap is not None:
            # the chosen alternative leaves the chips and the name it replaced takes its place (at most two), as
            # ``brain.agent.Brain._send_countdown`` does
            event = dict(info)
            value = cur.swap["value"]
            if cur.swap["kind"] == "who":
                event["who"] = value
                event["who_alts"] = tuple(x for x in (info["who"], *info["who_alts"]) if x and x != value)[:2]
            else:
                event["where"] = value
                event["where_alts"] = tuple(x for x in (info["where"], *info["where_alts"])
                                            if x and x != value)[:2]
            event["window"] = ""
            self._emit("swapped", seconds, event)
        else:
            self._emit("stopped", seconds, info)
        self._emit_log("one_countdown", state=state, why=why, after_ms=round(waited * 1000))
        counted = state == SENT and waited >= COUNTED_MIN_S
        result = CountdownResult(state, why, counted=counted, swap=dict(cur.swap) if cur.swap else None,
                                 waited_s=waited)
        return self._after(result, recheck) if state == SENT else result

    def _wait(self, cur: _Open, seconds: float, info: dict[str, Any]) -> tuple[str, str]:
        """Until an outcome: counting down, or paused while the user speaks (then their words decide, or it counts down
        again for the time it had left). The wake is cleared before each look, so a set during the look is never
        lost; each wait is at most 0.25 s."""
        told = False
        while True:
            cur.wake.clear()
            emit: tuple[str, float] | None = None
            with self._lock:
                if cur.outcome is not None:
                    return cur.outcome
                now = self._clock()
                if cur.paused:
                    if cur.words_after_pause is True:
                        cur.outcome = (CHANGED, "spoke")
                        return cur.outcome
                    if cur.words_after_pause is False:
                        cur.paused, cur.words_after_pause, told = False, None, False
                        cur.deadline = now + cur.left
                        emit, wait = ("counting", cur.left), 0.0
                    elif now - cur.paused_at >= PAUSE_MAX_S:
                        cur.outcome = (CHANGED, "pause_timeout")
                        return cur.outcome
                    else:
                        if not told:
                            told, emit = True, ("paused", seconds)
                        wait = PAUSE_MAX_S - (now - cur.paused_at)
                else:
                    wait = cur.deadline - now
                    if wait <= 0:
                        return SENT, "ran_out"
            if emit is not None:
                self._emit(emit[0], emit[1], info)
                self._emit_log("one_countdown", state=emit[0])
            if wait > 0:
                cur.wake.wait(min(wait, 0.25))

    def _after(self, result: CountdownResult, recheck: Callable[[], tuple[str, str] | None] | None
               ) -> CountdownResult:
        """The game and the Act switch read again once the countdown ran out (``recheck``); a refusal stops it."""
        if recheck is None:
            return result
        try:
            stop = recheck()
        except Exception:  # noqa: BLE001 - the check could not run: nothing is sent
            stop = ("error", "")
        if not stop:
            return result
        why, line = stop
        self._emit_log("one_countdown", state="changed", why=str(why or "recheck"))
        return CountdownResult(CHANGED, str(why or "recheck"), line=str(line or ""), counted=False,
                               waited_s=result.waited_s)

    # -- plumbing ------------------------------------------------------------------------------------------------------

    def _emit(self, state: str, seconds: float, info: Mapping[str, Any]) -> None:
        if self._publish is None:
            return
        try:
            from yuki.log.events import SendPending

            event = SendPending(state, seconds=float(seconds), **{k: info[k] for k in (
                "who", "where", "app", "text", "typed", "window", "who_alts", "where_alts", "from_memory")})
        except Exception:  # noqa: BLE001 - presentation only
            return
        self._safe(self._publish, event)

    @staticmethod
    def _safe(fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception:  # noqa: BLE001 - the island and the voice never stop a countdown
            pass

    def _emit_log(self, event: str, **fields: Any) -> None:
        if self._log is None:
            return
        try:
            self._log(event, **fields)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["CANCELLED", "CHANGED", "COUNTED_MIN_S", "Countdown", "CountdownResult", "DEFAULT_SECONDS", "LINE_SENDING",
           "PAUSE_MAX_S", "SENT"]
