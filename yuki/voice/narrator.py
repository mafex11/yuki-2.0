"""When the brain's news is said, and how much of it (docs/VOICE.md §6.4, §6.5).

Two pieces, both plain code with no opinion on what anything means:

* :func:`condense` trims a result for the ear before the mouth sees it: the
  first one or two whole sentences (a cap in characters as a backstop), and
  the rest kept for when the user asks for it. The brain's prompt already asks
  for a short answer; this makes it so even when it writes four paragraphs
  (measured 2026-09-25: the mouth read those out).
* :class:`NarratorGate` holds everything the brain sends while a conversation
  is on (progress from its real tool calls, its ``say`` lines, questions,
  results) and releases it only when a person would speak: not while the user
  talks, not while Yuki is talking or still owes an answer, not right after
  Yuki's own speech (a cooldown), and only once things have settled, so two
  results that land a second apart become one turn. Something that waited
  while the conversation moved on is stale: a progress line is dropped, a
  result is put into the conversation without being spoken, so it can still be
  answered from if the user asks. Modelled on gradient-bang's
  ``SubagentNarrator`` (research: docs/research/voice-parallel.md §1.4).

The gate decides *when* and *whether*; the mouth still decides every word.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Characters that end a sentence (Latin, Devanagari danda, CJK).
_ENDS = ".!?…।。！？"
_CLOSERS = "\"')]”’」』"


def sentences(text: str) -> list[str]:
    """``text`` split into sentences, whitespace collapsed; list items and lines count as sentences."""
    out: list[str] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        while line[:2] in ("- ", "* ", "• "):
            line = line[2:].strip()
        if not line:
            continue
        buf = ""
        i = 0
        while i < len(line):
            ch = line[i]
            buf += ch
            if ch in _ENDS:
                while i + 1 < len(line) and (line[i + 1] in _ENDS or line[i + 1] in _CLOSERS):
                    i += 1
                    buf += line[i]
                # a sentence ends at the mark when a space (or the line's end) follows;
                # "3.5" or "e.g." inside a word does not end one
                if i + 1 >= len(line) or line[i + 1].isspace():
                    out.append(" ".join(buf.split()))
                    buf = ""
            i += 1
        if buf.strip():
            out.append(" ".join(buf.split()))
    return [s for s in out if s]


def _cut_long(sentence: str, limit: int) -> str:
    """One over-long sentence cut at the last clause or word boundary before ``limit``."""
    head = sentence[:limit]
    for mark in ("; ", ", ", " - ", " "):
        at = head.rfind(mark)
        if at > limit // 2:
            return head[:at].rstrip(" ,;-") + " …"
    return head.rstrip() + " …"


def condense(text: str, *, max_sentences: int = 2, max_chars: int = 280) -> tuple[str, str]:
    """``(spoken, rest)``: the first ``max_sentences`` whole sentences within ``max_chars``, and what is left.

    The first sentence is always kept (cut at a clause boundary only when it
    alone is longer than ``max_chars``). ``rest`` is ``""`` when nothing is left.
    """
    parts = sentences(text)
    if not parts:
        return (text or "").strip(), ""
    first = parts[0]
    if len(first) > max_chars:
        cut = _cut_long(first, max_chars)
        return cut, " ".join([first[len(cut) - 2:].strip(), *parts[1:]]).strip()
    kept = [first]
    for part in parts[1:max_sentences]:
        if len(" ".join([*kept, part])) > max_chars:
            break
        kept.append(part)
    return " ".join(kept), " ".join(parts[len(kept):])


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

#: Kinds of item, by what may replace what. A newer item of a job replaces that
#: job's waiting items of a lower rank (a result makes a pending progress line
#: pointless; a newer progress line replaces an older one).
RANKS = {"step": 0, "queued": 0, "say": 1, "ask": 2, "final": 3}
#: User turns an item may wait through before it is stale (``final`` uses the setting).
STALE_AFTER = {"step": 1, "queued": 1, "say": 2}
#: Kinds said as soon as the user may be spoken to (no settle, only a short gap after Yuki's speech).
URGENT = ("final", "ask")
#: Kinds that are progress of a running task: said only after a real pause in Yuki's speech.
PROGRESS = ("step", "queued")


@dataclass
class Item:
    """One thing the brain sent that may be spoken.

    ``deliver(run_llm, stale)`` hands it to the mouth's context (the
    ``tell_yuki`` call's result callback); ``drop()`` settles an item that is
    never delivered (progress superseded or gone stale).
    """

    job: int
    kind: str
    deliver: Callable[[bool, bool], Awaitable[None]]
    drop: Callable[[], None] | None = None
    at: float = field(default_factory=time.monotonic)
    user_turn: int = 0


class NarratorGate:
    """Holds the brain's items and releases them when a person would speak.

    Args:
        state: Returns the conversation's live state: ``user_speaking``,
            ``user_sound`` (the microphone hears speech right now, before
            speech recognition says so),
            ``bot_speaking``, ``generating``, ``owes_reply``, ``busy_tools``
            (the mouth's own quick tools in flight), ``last_bot_stop``
            (monotonic, 0 if never), ``user_turns`` (final transcripts so far) and
            ``jobs_open`` (this conversation's tasks still running).
        settle_s / settle_max_s / cooldown_s / stale_turns / final_gap_s / progress_pause_s: see
            ``Settings.voice_*`` (``final_gap_s`` ``None``: the cooldown, as before;
            ``progress_pause_s`` ``None``: progress keeps the cooldown).
        log: Content-free event log (``event(kind, **fields)``) or ``None``.
    """

    def __init__(self, state: Callable[[], dict[str, Any]], *, settle_s: float = 1.5, settle_max_s: float = 6.0,
                 cooldown_s: float = 1.5, stale_turns: int = 3, log: Any = None,
                 final_gap_s: float | None = None, progress_pause_s: float | None = None,
                 attention: Any = None) -> None:
        self._state = state
        #: The shared anti-spam gates (yuki.core.attention, channel ``voice_progress``): proactive
        #: lines (progress, the brain's say) within its budget and cooldown, held in a meeting.
        if attention is None:
            from yuki.core.attention import shared

            attention = shared()
        self.attention = attention
        self.settle_s = settle_s
        self.settle_max_s = settle_max_s
        self.cooldown_s = cooldown_s
        self.final_gap_s = cooldown_s if final_gap_s is None else final_gap_s
        self.progress_pause_s = cooldown_s if progress_pause_s is None else max(cooldown_s, progress_pause_s)
        self.stale_turns = stale_turns
        self.log = log
        self.pending: list[Item] = []
        self._poke = asyncio.Event()
        self._first_at: float | None = None
        self._last_at = 0.0
        self._task: asyncio.Task | None = None
        self._last_why: str | None = None
        #: Counters for the log and the tests.
        self.stats = {"delivered": 0, "merged": 0, "folded": 0, "dropped": 0, "superseded": 0, "turns": 0}

    # -- in ----------------------------------------------------------------------

    def enqueue(self, item: Item) -> None:
        """Add an item (on the voice loop). A newer item replaces its job's lower-ranked waiting ones."""
        item.user_turn = int(self._state().get("user_turns", 0))
        keep: list[Item] = []
        for old in self.pending:
            if old.job == item.job and RANKS.get(old.kind, 0) <= RANKS.get(item.kind, 0) and old.kind != "final" \
                    and not (old.kind == "ask" and item.kind != "final"):
                self.stats["superseded"] += 1
                if old.drop is not None:
                    old.drop()
                continue
            keep.append(old)
        self.pending = keep + [item]
        now = time.monotonic()
        self._last_at = now
        if self._first_at is None:
            self._first_at = now
        self.poke()

    def poke(self) -> None:
        """Something changed (a frame, an item): look again."""
        self._poke.set()

    @property
    def holding(self) -> int:
        return len(self.pending)

    def take(self, job: int, kinds: tuple[str, ...] = ("final",)) -> list[Item]:
        """Remove and return a job's waiting items of these kinds (the caller delivers them now)."""
        taken = [i for i in self.pending if i.job == job and i.kind in kinds]
        if taken:
            self.pending = [i for i in self.pending if i not in taken]
            if not self.pending:
                self._first_at = None
        return taken

    # -- out ---------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="yuki-voice-narrator")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._task = None

    def _blocked(self, s: dict[str, Any], now: float) -> tuple[str | None, float | None]:
        """Why nothing may be said now (``None``: free), and when to look again at the latest."""
        if s.get("user_speaking"):
            return "user_speaking", None
        if s.get("user_sound"):
            # The microphone hears speech that speech recognition has not reported yet
            # (its start of turn comes ~0.5-1 s late): do not start talking over it.
            return "user_sound", now + 0.1
        if s.get("bot_speaking") or s.get("generating"):
            return "bot_busy", None
        if s.get("owes_reply"):
            return "owes_reply", None
        if s.get("busy_tools"):
            return "mouth_tool", None
        # A result or a question is said as soon as the user is not talking and nothing is
        # owed: after Yuki's own speech only a short natural gap, and no settle unless another
        # task of this conversation is still running (its result may land a moment later and
        # be said in the same turn). Progress lines keep the full cooldown and settle, so a
        # result that follows within the settle replaces them (measured 2026-09-25: a
        # one-action result waited 4.5 s here, 1.5 s of it settle and cooldown).
        urgent = any(i.kind in URGENT for i in self.pending)
        # Progress of a running task only after a real pause: Yuki has said nothing for
        # progress_pause_s (the acknowledgement counts), so a quick job is never narrated
        # step by step (measured 2026-09-25: "Opening the Chrome link now." right after
        # "Opening YouTube on Chrome"). A result that lands meanwhile replaces it.
        only_progress = all(i.kind in PROGRESS for i in self.pending)
        cooldown = self.final_gap_s if urgent else (self.progress_pause_s if only_progress else self.cooldown_s)
        last_stop = float(s.get("last_bot_stop") or 0.0)
        if last_stop and now - last_stop < cooldown:
            return "cooldown", last_stop + cooldown
        if self._first_at is not None and not (urgent and not s.get("jobs_open")):
            settle_until = min(self._last_at + self.settle_s, self._first_at + self.settle_max_s)
            if now < settle_until:
                return "settle", settle_until
        return None, None

    def _stale(self, item: Item, user_turns: int) -> bool:
        waited = user_turns - item.user_turn
        if item.kind == "ask":
            return False
        limit = self.stale_turns if item.kind == "final" else STALE_AFTER.get(item.kind, 1)
        return waited >= limit

    async def _run(self) -> None:
        while True:
            try:
                await self._loop()
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - logged, and the gate keeps going
                self._log("narrator_error", error=f"{type(exc).__name__}: {exc}")
                await asyncio.sleep(0.05)

    async def _loop(self) -> None:
        while True:
            self._poke.clear()
            if not self.pending:
                await self._poke.wait()
                continue
            now = time.monotonic()
            s = self._state()
            # Stale items go first, whatever the gates say: they are never spoken.
            turns = int(s.get("user_turns", 0))
            stale = [i for i in self.pending if self._stale(i, turns)]
            if stale:
                self.pending = [i for i in self.pending if i not in stale]
                for item in stale:
                    await self._settle_stale(item)
                if not self.pending:
                    self._first_at = None
                    continue
            why, until = self._blocked(s, now)
            if why != self._last_why:
                self._last_why = why
                self._log("narrator", op="waiting" if why else "clear", why=why, holding=len(self.pending))
            if why is not None:
                timeout = None if until is None else max(0.02, until - now)
                try:
                    await asyncio.wait_for(self._poke.wait(), timeout=timeout)
                except asyncio.TimeoutError:
                    pass
                continue
            # Everything is clear: yield once and look again (the user may have
            # started talking in this very instant), then say it all as one turn.
            await asyncio.sleep(0)
            if self._blocked(self._state(), time.monotonic())[0] is not None:
                continue
            batch, self.pending, self._first_at = self.pending, [], None
            batch = self._within_budget(batch)
            if not batch:
                continue
            # Results last: the turn is run by the last item, and it is the one that matters most.
            batch.sort(key=lambda i: (RANKS.get(i.kind, 0), i.at))
            self.stats["turns"] += 1
            self.stats["merged"] += max(0, len(batch) - 1)
            for n, item in enumerate(batch):
                last = n == len(batch) - 1
                try:
                    await item.deliver(last, False)
                    self.stats["delivered"] += 1
                except Exception as exc:  # noqa: BLE001 - one bad item must not stop the gate
                    self._log("narrator_error", error=f"{type(exc).__name__}: {exc}")
            self._log("narrator", op="spoke", items=len(batch), kinds=[i.kind for i in batch],
                      jobs=sorted({i.job for i in batch}),
                      waited_ms=[round((time.monotonic() - i.at) * 1000) for i in batch])

    def _within_budget(self, batch: list[Item]) -> list[Item]:
        """The batch with its proactive lines (progress, say) dropped when the shared budget says no.

        One check per turn (lines said together are one delivery); results and questions always pass.
        """
        proactive = [i for i in batch if i.kind in PROGRESS or i.kind == "say"]
        if not proactive or self.attention is None:
            return batch
        try:
            decision = self.attention.check("voice_progress",
                                            facts={"meeting": bool(self._state().get("call_mode"))})
        except Exception:  # noqa: BLE001 - the gate must never stop the voice
            return batch
        if decision.allowed:
            self.attention.record("voice_progress")
            return batch
        for item in proactive:
            self.stats["dropped"] += 1
            if item.drop is not None:
                item.drop()
        self._log("narrator", op="held_by_attention", reason=decision.reason, items=len(proactive))
        return [i for i in batch if i not in proactive]

    async def _settle_stale(self, item: Item) -> None:
        if item.kind == "final":
            self.stats["folded"] += 1
            try:
                await item.deliver(False, True)
            except Exception as exc:  # noqa: BLE001
                self._log("narrator_error", error=f"{type(exc).__name__}: {exc}")
            self._log("narrator", op="folded", job=item.job, waited_ms=round((time.monotonic() - item.at) * 1000))
        else:
            self.stats["dropped"] += 1
            if item.drop is not None:
                item.drop()
            self._log("narrator", op="dropped_stale", job=item.job, item=item.kind)

    def _log(self, event_type: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(event_type, **fields)
