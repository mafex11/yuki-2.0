"""One anti-spam framework for everything Yuki says or shows unasked (coach, reminders, voice).

Yuki's proactive outputs used to each carry their own gates: the coach's budget, card gap, quiet
hours and meeting / full-screen checks in ``yuki.memory.nudges``, the card queue in
``yuki.ui.nudges``, the voice narrator's cooldown and settle in ``yuki.voice.narrator``. A rule
tuned in one place silently diverged from the others. :class:`Attention` is the one place those
rules live, per **channel** (``coach``, ``reminder``, ``standing_request``, ``question``,
``voice_progress``, ``notice``, ``checkin`` (:mod:`yuki.memory.checkins`), the observation cards of
:mod:`yuki.memory.presence`: ``stuck``, ``recap``, ``followup``, ``recall``, ``thread``, all counted together
in ``presence``, and ``routine``, the "Your usual" card of :mod:`yuki.memory.routine_offers`), each with a
:class:`ChannelRule`:

* **budget** - at most ``budget`` deliveries per ``window_s`` (rolling);
* **cooldown** - at least ``cooldown_s`` after the channel's last delivery;
* **card gap** - channels that show a card (``card=True``) keep ``card_gap_s`` from any other card (a channel with
  ``card_gap=False``, the routine card, skips it for itself but still moves it for the others);
* **quiet hours** - ``quiet_start``-``quiet_end`` local time (wrapping midnight), for channels
  with ``quiet=True``;
* **full-screen / meeting gates** - measured facts (``facts()["fullscreen"]`` /
  ``facts()["meeting"]``), for channels with ``fullscreen=True`` / ``meeting=True``;
* **one pending card** - while a card is up (:meth:`card_shown` until :meth:`card_closed`), no
  other card channel may show one;
* **snooze** - :meth:`snooze` until a time (the tray's "Quiet for 1 hour").

``urgent=True`` (a reminder due at an exact time) skips the budget, cooldown and card gap, never
the one-pending-card rule, the snooze or the meeting gate.

Nothing here looks at *what* is said; the decision is made from counts, clocks and measured
facts only. It is small and in-process: the caller that shows the output owns one instance and
calls :meth:`check` before and :meth:`record` after delivering. Every decision can be logged.

Usage::

    attention = Attention(facts=lambda: {"fullscreen": ..., "meeting": ...}, log=log)
    decision = attention.check("coach")
    if decision.allowed:
        show(card); attention.record("coach"); attention.card_shown("coach:17")
    else:
        retry_at(decision.retry_at)          # decision.reason: budget | cooldown | card_gap | ...
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Callable


@dataclass(frozen=True)
class ChannelRule:
    """How often one kind of unasked output may happen, and what holds it back."""

    budget: int = 0              # deliveries per window_s; 0 = no budget
    window_s: float = 3600.0
    cooldown_s: float = 0.0
    card: bool = False           # shows a card (card gap + one pending card apply)
    quiet: bool = True           # held in quiet hours
    fullscreen: bool = True      # held while a full-screen app is in front
    meeting: bool = True         # held during a meeting or call
    #: Keeps the card gap after another card (``card=True`` only). The routine card sets it False (PLAN.md Phase R,
    #: critique M20): a welcome back at 08:48 would otherwise hide the whole 08:50-08:57 window. It never skips the
    #: one-pending-card rule, and its own delivery still moves the gap for every other card.
    card_gap: bool = True


#: Defaults, matching what the coach and the voice did on their own before (docs/HANDBOOK.md §6):
#: one coach card per 30 min; cards 10 min apart; reminders on time but never over another card;
#: spoken progress at most 4 lines a minute with the narrator's 1.5 s cooldown, never gated by a
#: full-screen game (the user is talking to Yuki) but held in a meeting.
#:
#: The observation cards (yuki.memory.presence, 2026-09-29: "offers on cards only", about 8-12 cards on a
#: full day in all, spread out): each kind has its own day budget - ``notice`` (something the user just
#: finished), ``stuck`` (something keeps failing), ``recap`` (a call's digest is ready), ``followup`` (a
#: personal event reached its moment), ``recall`` (the screen meets something memory holds: an answer
#: they are looking for, an open item), ``thread`` (a thread of work changed or went stale) - and all of
#: them together go through ``presence``: at most two an hour, 25 minutes apart, so they never come in a
#: burst. The user's "Less of these" on a kind lowers that kind's budget (the presence worker tunes it
#: from their reactions); nothing mutes the others. Budgets start here and are counted from the stored
#: cards (:meth:`Attention.load`), so a service restart does not reset them.
DAY_S = 86400.0
DEFAULT_RULES: dict[str, ChannelRule] = {
    "coach": ChannelRule(budget=1, window_s=1800.0, cooldown_s=90.0, card=True),
    "question": ChannelRule(budget=3, window_s=3600.0, cooldown_s=1200.0, card=True),
    "reminder": ChannelRule(budget=0, card=True, quiet=False),
    "standing_request": ChannelRule(budget=6, window_s=3600.0, cooldown_s=60.0, card=True, quiet=False),
    "notice": ChannelRule(budget=3, window_s=DAY_S, cooldown_s=1800.0, card=True, quiet=True, fullscreen=True),
    "stuck": ChannelRule(budget=3, window_s=DAY_S, cooldown_s=3600.0, card=True),
    "recap": ChannelRule(budget=3, window_s=DAY_S, cooldown_s=0.0, card=True),
    "followup": ChannelRule(budget=2, window_s=DAY_S, cooldown_s=3 * 3600.0, card=True),
    "recall": ChannelRule(budget=3, window_s=DAY_S, cooldown_s=3600.0, card=True),
    "thread": ChannelRule(budget=2, window_s=DAY_S, cooldown_s=4 * 3600.0, card=True),
    # a plan of the user's within a day (yuki.memory.presence._plans, revamp 09 P3.1): at most one a day
    "plans": ChannelRule(budget=1, window_s=DAY_S, cooldown_s=0.0, card=True),
    "presence": ChannelRule(budget=2, window_s=3600.0, cooldown_s=1500.0, card=True),
    # Yuki's check-ins about the user's goals and their day (yuki.memory.checkins, 2026-10-03): at most three a day
    # (the [nudges] checkins_per_day tunes it), an hour apart at least; their own pacing by presence comes on top.
    "checkin": ChannelRule(budget=3, window_s=DAY_S, cooldown_s=3600.0, card=True),
    "voice_progress": ChannelRule(budget=4, window_s=60.0, cooldown_s=1.5, quiet=False, fullscreen=False,
                                  meeting=True),
    # the "Your usual" routine card (yuki.memory.routine_offers, PLAN.md Phase R): at most two a day, held in quiet
    # hours and over a full screen; the lane holds it itself on any open call, meeting audio, a game in front, away
    # and Yuki acting (D11), so the meeting fact is not this rule's; it skips the card gap (never a card showing)
    "routine": ChannelRule(budget=2, window_s=DAY_S, cooldown_s=0.0, card=True, quiet=True, fullscreen=True,
                           meeting=False, card_gap=False),
}
#: The observation channels (yuki.memory.presence), each also counted in ``presence``.
PRESENCE_CHANNELS: tuple[str, ...] = ("notice", "stuck", "recap", "followup", "recall", "thread", "plans")


@dataclass(frozen=True)
class Decision:
    """Whether an output may go now; if not, why (content-free) and when to look again."""

    allowed: bool
    reason: str | None = None
    retry_at: float | None = None


def _minutes(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


@dataclass
class Attention:
    """The shared gates (see the module docstring). Thread-safe."""

    rules: dict[str, ChannelRule] = field(default_factory=lambda: dict(DEFAULT_RULES))
    quiet_start: str = "00:30"
    quiet_end: str = "08:00"
    card_gap_s: float = 600.0
    facts: Callable[[], dict[str, Any]] | None = None
    log: Callable[..., Any] | None = None
    clock: Callable[[], float] = time.time
    _history: dict[str, deque] = field(default_factory=dict)
    _last_card: float = 0.0
    _pending_card: str | None = None
    _snooze_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- configuration ---------------------------------------------------------------------

    def rule(self, channel: str) -> ChannelRule:
        return self.rules.get(channel) or ChannelRule()

    def tune(self, channel: str, **changes: Any) -> None:
        """Change one channel's rule (e.g. from the coach's ``[nudges]`` table)."""
        self.rules[channel] = replace(self.rule(channel), **changes)

    def in_quiet_hours(self, at: float | None = None) -> bool:
        start, end = _minutes(self.quiet_start), _minutes(self.quiet_end)
        if start == end:
            return False
        t = datetime.fromtimestamp(self.clock() if at is None else at)
        now = t.hour * 60 + t.minute
        return start <= now < end if start < end else (now >= start or now < end)

    def snooze(self, until: float) -> None:
        with self._lock:
            self._snooze_until = float(until)

    # -- the decision ------------------------------------------------------------------------

    def check(self, channel: str, *, urgent: bool = False, now: float | None = None,
              facts: dict[str, Any] | None = None) -> Decision:
        """May ``channel`` deliver now? Never raises (a failing facts read counts as no facts)."""
        now = self.clock() if now is None else float(now)
        rule = self.rule(channel)
        if facts is None and self.facts is not None and (rule.fullscreen or rule.meeting):
            try:
                facts = self.facts() or {}
            except Exception:
                facts = {}
        facts = facts or {}
        with self._lock:
            decision = self._decide(channel, rule, urgent, now, facts)
        if self.log is not None:
            try:
                self.log("attention", channel=channel, allowed=decision.allowed, reason=decision.reason,
                         urgent=urgent)
            except Exception:
                pass
        return decision

    def _decide(self, channel: str, rule: ChannelRule, urgent: bool, now: float,
                facts: dict[str, Any]) -> Decision:
        if self._snooze_until > now:
            return Decision(False, "snoozed", self._snooze_until)
        if rule.meeting and facts.get("meeting"):
            return Decision(False, "meeting", None)
        if rule.card and self._pending_card is not None:
            return Decision(False, "card_pending", None)
        if urgent:
            return Decision(True)
        if rule.quiet and self.in_quiet_hours(now):
            return Decision(False, "quiet_hours", None)
        if rule.fullscreen and facts.get("fullscreen"):
            return Decision(False, "fullscreen", None)
        if rule.card and rule.card_gap and self._last_card and now - self._last_card < self.card_gap_s:
            return Decision(False, "card_gap", self._last_card + self.card_gap_s)
        history = self._history.get(channel)
        if history:
            if rule.cooldown_s and now - history[-1] < rule.cooldown_s:
                return Decision(False, "cooldown", history[-1] + rule.cooldown_s)
            if rule.budget:
                recent = [t for t in history if now - t < rule.window_s]
                if len(recent) >= rule.budget:
                    return Decision(False, "budget", recent[0] + rule.window_s)
        return Decision(True)

    # -- after delivering --------------------------------------------------------------------

    def record(self, channel: str, *, now: float | None = None) -> None:
        """``channel`` delivered one output now."""
        now = self.clock() if now is None else float(now)
        rule = self.rule(channel)
        with self._lock:
            history = self._history.setdefault(channel, deque(maxlen=64))
            history.append(now)
            if rule.card:
                self._last_card = now

    def load(self, channel: str, times: "list[float] | tuple[float, ...]") -> None:
        """Replace ``channel``'s delivery history with ``times`` (epoch s), e.g. the cards of that kind the
        database holds: a restarted process keeps its budgets. Does not move the card gap."""
        with self._lock:
            self._history[channel] = deque(sorted(float(t) for t in times)[-64:], maxlen=64)

    def card_shown(self, card_id: str) -> None:
        """A card is on screen now (one at a time)."""
        with self._lock:
            self._pending_card = str(card_id)

    def card_closed(self, card_id: str | None = None) -> None:
        """The card went away (dismissed, answered, expired)."""
        with self._lock:
            if card_id is None or self._pending_card == str(card_id):
                self._pending_card = None

    @property
    def pending_card(self) -> str | None:
        return self._pending_card


_shared: Attention | None = None
_shared_lock = threading.Lock()


def shared() -> Attention:
    """The process-wide instance (the tray's cards and the voice narrator share it)."""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = Attention()
        return _shared
