"""The grounding lookup: what memory has near the user's words, for the mouth, on every turn (docs/VOICE.md §5.5).

While the user is still talking, each new interim transcript starts a local
memory search (``MemoryClient.recall``: the local embedder plus keyword
search, no model call) on one worker thread, newest words winning; the final
transcript starts one more if the words changed. When the mouth's call is
about to go out it waits for the final lookup for at most
``Settings.voice_grounding_wait_ms`` (measured 30-50 ms on the user's
database); if that is not done it takes the freshest finished one of this
turn, and if none is finished the call goes without and the lookup's result
is used by the next call. The mouth never waits longer than that.

What the mouth is shown: the few nearest journal facts and episodes (what the
user did, was told, likes), dated and labelled (a joke or an uncertain item
says so). Past exchanges with Yuki and session summaries are left out: they
are Yuki's own earlier words, not a record of the user's life, and are the
brain's to weigh. This is a hint, not an answer: the mouth's prompt says what
it may answer from it (one fact that plainly answers a simple question) and
that anything about the user's day or activity is still handed over.

The embedder is memory's process-wide one (``yuki.memory.embed.get_embedder``,
~600 MB loaded; the brain loads the same one for its first request). It is
loaded when a conversation starts, off the voice loop, and unloaded when the
conversation ends if it was not loaded before it (``release``).
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

#: recall() kinds that describe the user's life (what they did, were told, like).
LIFE_KINDS = ("fact", "episode", "review")
#: How many hits to ask recall for before keeping the life kinds.
RECALL_LIMIT = 12
#: Longest fact text shown (whole sentences).
FACT_CHARS = 220


@dataclass
class Lookup:
    """One finished (or running) lookup."""

    text: str
    started: float
    turn: int
    facts: list[dict[str, Any]] = field(default_factory=list)
    ms: float | None = None
    error: str | None = None
    done: threading.Event = field(default_factory=threading.Event)


def _when(iso: str | None, now: datetime) -> str:
    if not iso:
        return ""
    try:
        at = datetime.fromisoformat(iso)
    except ValueError:
        return iso
    if at.date() == now.date():
        return f"today {at:%H:%M}"
    if (now.date() - at.date()).days == 1:
        return f"yesterday {at:%H:%M}"
    return f"{at:%a %d %b %Y}"


def format_facts(facts: list[dict[str, Any]], now: datetime | None = None) -> str:
    """The facts as the mouth sees them, one line each: ``(kind, when, app) text [label]``."""
    from yuki.voice.narrator import condense

    now = now or datetime.now()
    lines = []
    for f in facts:
        text, _rest = condense(str(f.get("fact") or ""), max_sentences=2, max_chars=FACT_CHARS)
        if not text:
            continue
        where = ", ".join(x for x in (_when(f.get("at"), now), f.get("app") or "") if x)
        nature = f.get("nature")
        label = f" [{nature}: not a plain fact]" if nature and nature != "fact" and f.get("kind") == "fact" else ""
        lines.append(f"- ({f.get('kind')}{', ' + where if where else ''}) {text}{label}")
    return "\n".join(lines)


class Grounder:
    """Speculative memory lookups for one conversation.

    Args:
        memory: :class:`~yuki.agent.memory.MemoryAccess` (its client's ``recall``).
        facts: How many facts to keep.
        log: Content-free voice log, or ``None``.
    """

    def __init__(self, memory: Any, *, facts: int = 4, log: Any = None) -> None:
        self.memory = memory
        self.keep = facts
        self.log = log
        self._pool = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="yuki-voice-ground")
        self._lock = threading.Lock()
        self._turn = 0
        self._running: Lookup | None = None
        self._next: str | None = None
        self._final: str | None = None
        self._latest: Lookup | None = None      # newest finished lookup (any turn)
        self._by_text: dict[str, Lookup] = {}   # this turn's lookups
        self.embedder_was_loaded: bool | None = None
        self.ready = threading.Event()
        #: For the log and the tests: every lookup's time, and what each call got.
        self.timings: list[float] = []
        self.attached: list[str] = []

    # -- lifecycle -------------------------------------------------------------

    def warm(self) -> None:
        """Load the embedder now, off the caller's thread (a one-word recall does it)."""
        def work() -> None:
            started = time.perf_counter()
            try:
                from yuki.memory.embed import get_embedder

                self.embedder_was_loaded = bool(get_embedder().stats().get("loaded"))
                self.memory.call("voice_grounding_warm", lambda c: c.recall("hello", limit=1), timeout_s=30.0)
                self._log("grounding", op="warm", ms=round((time.perf_counter() - started) * 1000),
                          was_loaded=self.embedder_was_loaded)
            except Exception as exc:  # noqa: BLE001 - voice works without it
                self._log("grounding", op="warm", error=f"{type(exc).__name__}: {str(exc)[:160]}")
            finally:
                self.ready.set()

        self._pool.submit(work)

    def release(self, *, busy: bool) -> bool:
        """End of the conversation: unload the embedder if this conversation loaded it and nothing else is working.

        Returns whether it was unloaded. ``busy``: a brain request is running
        (it may be using the embedder; memory's own idle timer unloads it later).
        """
        self._pool.shutdown(wait=False, cancel_futures=True)
        if busy or self.embedder_was_loaded is not False:
            return False
        try:
            from yuki.memory.embed import get_embedder

            unloaded = bool(get_embedder().unload())
        except Exception:  # noqa: BLE001
            return False
        self._log("grounding", op="unloaded", unloaded=unloaded)
        return unloaded

    # -- lookups (voice loop -> worker thread) -----------------------------------

    def interim(self, text: str) -> None:
        """The user is still talking: look up what they have said so far (newest words win)."""
        self._want(text)

    def final(self, text: str) -> None:
        """The user's turn ended with ``text``: make sure a lookup of exactly that runs."""
        with self._lock:
            self._final = " ".join((text or "").split())
        self._want(text)

    def new_turn(self) -> None:
        """A new user turn starts (the previous turn's lookups no longer count as this turn's)."""
        with self._lock:
            self._turn += 1
            self._by_text = {}
            self._final = None
            self._next = None

    def _want(self, text: str) -> None:
        text = " ".join((text or "").split())
        if len(text) < 3:
            return
        with self._lock:
            if text in self._by_text:
                return
            if self._running is not None and not self._running.done.is_set():
                self._next = text
                return
            self._start_locked(text)

    def _start_locked(self, text: str) -> None:
        lookup = Lookup(text=text, started=time.perf_counter(), turn=self._turn)
        self._by_text[text] = lookup
        self._running = lookup
        self._pool.submit(self._work, lookup)

    def _work(self, lookup: Lookup) -> None:
        try:
            hits = self.memory.call("voice_grounding", lambda c: c.recall(lookup.text, limit=RECALL_LIMIT),
                                    timeout_s=2.0)
            lookup.facts = [h for h in (hits or []) if h.get("kind") in LIFE_KINDS][: self.keep]
        except Exception as exc:  # noqa: BLE001
            lookup.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        lookup.ms = (time.perf_counter() - lookup.started) * 1000
        self.timings.append(lookup.ms)
        lookup.done.set()
        with self._lock:
            if lookup.turn == self._turn:
                self._latest = lookup
            nxt, self._next = self._next, None
            if nxt and nxt not in self._by_text:
                self._start_locked(nxt)

    # -- for the mouth's call (voice loop) ---------------------------------------

    async def for_call(self, wait_ms: float) -> tuple[Lookup | None, str]:
        """The best lookup for the call about to go out, waiting at most ``wait_ms``.

        Returns ``(lookup, how)``: ``final`` (the finished words), ``partial``
        (this turn's freshest finished lookup), ``previous`` (an earlier turn's,
        when this turn has none yet) or ``none``.
        """
        import asyncio

        with self._lock:
            final = self._by_text.get(self._final) if self._final else None
        deadline = time.perf_counter() + wait_ms / 1000.0
        if final is not None:
            while not final.done.is_set() and time.perf_counter() < deadline:
                await asyncio.sleep(0.005)
            if final.done.is_set() and final.error is None:
                self._latest = final
                return final, "final"
        with self._lock:
            latest = self._latest
            this_turn = latest is not None and latest.turn == self._turn
        if latest is None:
            return None, "none"
        return latest, ("partial" if this_turn else "previous")

    def _log(self, kind: str, **fields: Any) -> None:
        if self.log is not None:
            self.log.event(kind, **fields)
