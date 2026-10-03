"""The loop breaker: measured signs that the brain is repeating itself, and a forced rethink.

It counts; it never decides what to do instead. The signals, per request:

* **exact repeat** - the same tool with the same input (canonical JSON) failed with the same
  result ``repeats`` times;
* **same failure, other input** - the same tool failed with the same result ``repeats`` times
  across different inputs (a click nudged a few pixels, a command re-quoted);
* **no progress** - ``failed_steps`` model steps in a row in which every tool call failed;
* **looking in circles** - the same look (same tool and input) returned the same summary
  ``repeats + 1`` times.

A trip adds a note to the next message (the loop does that with :meth:`LoopBreaker.take_note`):
stop, do not repeat, take a clearly different approach or ask the user. The inputs that tripped
it are refused from then on (:meth:`LoopBreaker.refuse`), so the note cannot simply be ignored.
At the ``max_trips``-th trip the breaker is **exhausted** (default: the first trip forces a rethink, the second stops all actions): only ``ask_user``, ``done``, ``say`` and
``note_to_self`` may run; the loop ends the request if the brain still tries anything else.
"""

from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

#: Tools the breaker never counts or refuses: the ways out of a loop.
EXEMPT: frozenset[str] = frozenset({"ask_user", "done", "say", "note_to_self"})

#: Input fields that are the brain's notes about a call, not what the call does.
#: (``say_now``: the few words Yuki's voice says as the step starts, v3.14; the same call with new words is the same
#: call. ``playbook``: the practice object the call follows, Phase C.)
NOTE_FIELDS: frozenset[str] = frozenset({"finishes_with", "finish_words", "risk", "say_now", "playbook"})


def signature(tool_input: dict[str, Any]) -> str:
    """Canonical JSON of what a call does (its note fields left out)."""
    return json.dumps({k: v for k, v in (tool_input or {}).items() if k not in NOTE_FIELDS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


@dataclass
class Trip:
    """One time the breaker tripped."""

    reason: str        # exact_repeat | same_failure | no_progress | looking_in_circles
    tool: str | None
    summary: str
    count: int


@dataclass
class LoopBreaker:
    """Per-request counters (``reset`` at each request)."""

    repeats: int = 3
    failed_steps: int = 4
    max_trips: int = 2
    trips: list[Trip] = field(default_factory=list)
    _exact_fail: Counter = field(default_factory=Counter)
    _tool_fail: Counter = field(default_factory=Counter)
    _looks: Counter = field(default_factory=Counter)
    _blocked: dict[tuple[str, str], str] = field(default_factory=dict)
    _failed_run: int = 0
    _pending: list[Trip] = field(default_factory=list)
    _tripped_keys: set = field(default_factory=set)

    def reset(self) -> None:
        self.trips.clear()
        self._exact_fail.clear()
        self._tool_fail.clear()
        self._looks.clear()
        self._blocked.clear()
        self._failed_run = 0
        self._pending.clear()
        self._tripped_keys.clear()

    @property
    def exhausted(self) -> bool:
        """The hard cap is reached: only the ways out (``EXEMPT``) may run."""
        return len(self.trips) >= self.max_trips

    # -- before a call -----------------------------------------------------------------------

    def refuse(self, name: str, tool_input: dict[str, Any]) -> str | None:
        """Why this call must not run (``None``: it may)."""
        if name in EXEMPT:
            return None
        if self.exhausted:
            return ("Not run: the loop breaker stopped this request's actions after the same thing kept "
                    "failing. Ask the user what to do (ask_user), or finish with done and say what went wrong.")
        failed = self._blocked.get((name, signature(tool_input)))
        if failed is not None:
            return (f"Not run: this exact call already failed {self.repeats} times with the same result "
                    f"(\"{failed}\"). Do something clearly different, or ask the user.")
        return None

    # -- after a call ------------------------------------------------------------------------

    def observe(self, name: str, tool_input: dict[str, Any], ok: bool, summary: str, *,
                perception: bool = False) -> Trip | None:
        """Count one finished call; returns the trip it caused, if any."""
        if name in EXEMPT:
            return None
        sig = signature(tool_input)
        summary = " ".join(str(summary or "").split())[:200]
        if not ok:
            self._exact_fail[(name, sig, summary)] += 1
            self._tool_fail[(name, summary)] += 1
            exact = self._exact_fail[(name, sig, summary)]
            same = self._tool_fail[(name, summary)]
            if exact >= self.repeats:
                self._blocked[(name, sig)] = summary
                return self._trip("exact_repeat", name, summary, exact, key=(name, sig, summary))
            if same >= self.repeats:
                # every input that produced this failure is refused from now on
                for (tool, s, result), _n in self._exact_fail.items():
                    if tool == name and result == summary:
                        self._blocked[(tool, s)] = summary
                return self._trip("same_failure", name, summary, same, key=(name, summary))
            return None
        if perception:
            self._looks[(name, sig, summary)] += 1
            count = self._looks[(name, sig, summary)]
            if count >= self.repeats + 1:
                return self._trip("looking_in_circles", name, summary, count, key=("look", name, sig, summary))
        return None

    def end_step(self, calls: int, failures: int) -> Trip | None:
        """Count one model step's tool calls (exempt tools excluded); returns a no-progress trip."""
        if calls <= 0:
            return None
        if failures >= calls:
            self._failed_run += 1
        else:
            self._failed_run = 0
        if self._failed_run >= self.failed_steps:
            self._failed_run = 0
            return self._trip("no_progress", None, f"{self.failed_steps} steps in a row with every call failed",
                              self.failed_steps, key=None)
        return None

    def _trip(self, reason: str, tool: str | None, summary: str, count: int, *, key: Any) -> Trip | None:
        if key is not None and key in self._tripped_keys:
            return None
        if key is not None:
            self._tripped_keys.add(key)
        trip = Trip(reason, tool, summary, count)
        self.trips.append(trip)
        self._pending.append(trip)
        return trip

    # -- the note for the next message --------------------------------------------------------

    def take_note(self) -> str | None:
        """The rethink note for the trips since the last call (``None`` when there were none)."""
        if not self._pending:
            return None
        pending, self._pending = self._pending, []
        what = []
        for t in pending:
            if t.reason == "exact_repeat":
                what.append(f"{t.tool} with the same input has failed {t.count} times with the same result "
                            f"(\"{t.summary}\")")
            elif t.reason == "same_failure":
                what.append(f"{t.tool} has failed {t.count} times with the same result (\"{t.summary}\") even "
                            f"with different input")
            elif t.reason == "looking_in_circles":
                what.append(f"{t.tool} has shown the same thing {t.count} times (\"{t.summary}\")")
            else:
                what.append(t.summary)
        if self.exhausted:
            return ("Loop breaker, hard stop: " + "; ".join(what) + ". No more actions will run in this request. "
                    "Ask the user how to go on (ask_user), or finish with done and say plainly what did not work.")
        return ("Loop breaker: " + "; ".join(what) + f". Stop and rethink before the next call (trip "
                f"{len(self.trips)} of {self.max_trips}). Do not repeat it: the calls that failed this way will be "
                "refused. Take a clearly different approach (another tool, another way in, check what is actually "
                "on screen), or ask the user.")
