"""Screen-triggered recall: what the whole memory holds for the screen in front, kept ready in RAM.

Recall used to start only from the user's words. Now the memory service, on each new capture of
the window in front (a page, a document, a chat, a terminal folder), ranks all of memory against
what that screen shows - the same measured ranking as :func:`yuki.memory.related.related`, no
model - and keeps the result in RAM: the screen (app, title, site), the thread of work its window
was last part of, and the best memories with their scores. The tray's ``related()`` asks for it over
the embedder's pipe (op ``screen_related``, a few milliseconds), so a request never embeds the
screen itself; the proactive phase will read the same cache.

How often (plumbing on time, to stay light): a burst of captures (switching windows) is taken once
it has been quiet :data:`SETTLE_S` (at most :data:`MAX_WAIT_S` after the first), at most every
:data:`MIN_GAP_S`, and not at all when the screen's text did not change since the last ranking.
While here, the worker also brings the links seen up to date (:func:`yuki.memory.links.sync`,
every :data:`LINKS_EVERY_S`) and embeds new call digests. Nothing runs while memory is paused.

The screen is the window in front: in the service, the eyes' live Now board (:mod:`yuki.memory.now`,
``board``: the watcher's latest read of each visible window, in RAM - read directly, no polling),
its foreground entry; without a board (or before the board has read the window in front), the newest
capture in the store taken with the window in front (the watcher's own read, privacy-filtered and
secret-redacted; captures of windows beside it, trigger ``visible:*``, and popups are skipped: the user
may not have looked at them). The capture listener still wakes it (a new read of any window).

Logs (content-free): ``screen_recall`` per ranking (ms, CPU ms, hits by kind, whether the text
changed), and the worker's counters in the service's stats.
"""

from __future__ import annotations

import hashlib
import threading
import time
from pathlib import Path
from typing import Any, Callable

from yuki.memory import related as _related
from yuki.memory import relevant as _rel

#: A burst of captures is ranked once it has been quiet this long ...
SETTLE_S = 2.0
#: ... or this long after its first capture, whichever comes first.
MAX_WAIT_S = 6.0
#: At most one ranking this often.
MIN_GAP_S = 4.0
#: How many memories the cache keeps for the screen.
KEEP = 12
#: The links seen are synced this often (and call digests embedded).
LINKS_EVERY_S = 60.0
#: The op the tray asks for the cache with (over :mod:`yuki.memory.embed_ipc`).
OP = "screen_related"


class ScreenRecall:
    """The service's screen-triggered recall (one daemon thread)."""

    def __init__(self, store: Any, *, embedder: Callable[[], Any] | None = None, log: Callable[..., Any] | None = None,
                 board: Any = None, settle_s: float = SETTLE_S, max_wait_s: float = MAX_WAIT_S,
                 min_gap_s: float = MIN_GAP_S, links_every_s: float = LINKS_EVERY_S) -> None:
        self.store = store
        self._embedder_fn = embedder
        self._log = log
        self.board = board
        self.settle_s, self.max_wait_s, self.min_gap_s = float(settle_s), float(max_wait_s), float(min_gap_s)
        self.links_every_s = float(links_every_s)
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._first: float | None = None
        self._last_capture: float | None = None
        self._last_run = 0.0
        self._last_links = 0.0
        self._signature: str | None = None
        self._cache: dict[str, Any] | None = None
        self.index = _rel.shared_index(store)   # shared with the question finders (yuki.memory.questions)
        self.counters = {"captures": 0, "rankings": 0, "unchanged": 0, "errors": 0, "served": 0, "cpu_ms": 0.0,
                         "ms": 0.0, "links_syncs": 0}

    # -- the store's capture listener (the watcher's thread: returns at once) ---------------

    def seen(self, capture_ids: list[int]) -> None:
        now = time.monotonic()
        with self._lock:
            self.counters["captures"] += len(capture_ids)
            if self._first is None:
                self._first = now
            self._last_capture = now
        self._event.set()

    # -- the pipe ------------------------------------------------------------------------------

    def latest(self) -> dict[str, Any] | None:
        with self._lock:
            self.counters["served"] += 1
            return dict(self._cache) if self._cache is not None else None

    def serve(self, _request: dict[str, Any]) -> dict[str, Any]:
        """The ``screen_related`` op: the cache as plain JSON (``{"cache": {...} | None}``)."""
        return {"cache": self.latest()}

    # -- the work ------------------------------------------------------------------------------

    @property
    def embedder(self) -> Any:
        if self._embedder_fn is None:
            from yuki.memory.embed import get_embedder

            return get_embedder()
        return self._embedder_fn()

    def _screen(self, now: float) -> dict[str, Any] | None:
        """The window in front: the Now board's entry when there is a board, else the newest capture."""
        board = self.board
        if board is not None:
            try:
                snap = board.snapshot(now=now)
                fg = snap.get("foreground")
                front = next((w for w in snap.get("windows") or [] if w.get("hwnd") == fg and w.get("read_at")), None)
                if front is not None and not front.get("blocked"):
                    text = front.get("text") or "\n".join(f"{m.get('sender')}: {m.get('text')}"
                                                          for m in front.get("messages") or [])
                    screen = _related.screen_from_store(self.store, now=now) or {}
                    if screen and front.get("process") and str(screen.get("process") or "").lower() != str(
                            front.get("process")).lower():
                        screen = {}   # the store's newest front capture is another app's: no thread from it
                    return {**screen, "app": front.get("app") or screen.get("app"),
                            "title": front.get("live_title") or front.get("title") or screen.get("title"),
                            "url": front.get("url") or screen.get("url"), "text": text,
                            "at": float(front.get("read_at") or now), "source": "now"}
            except Exception:
                pass
        return _related.screen_from_store(self.store, now=now)

    def compute(self, now: float | None = None) -> dict[str, Any] | None:
        """Rank memory against the screen now; keep and return the cache (content: titles; kept in RAM only)."""
        now = time.time() if now is None else float(now)
        t0, c0 = time.perf_counter(), time.process_time()
        screen = self._screen(now)
        if not screen:
            return self._cache
        text = _related.screen_text(screen)
        signature = hashlib.sha1(f"{screen.get('thread_id')}|{text}".encode("utf-8", "surrogatepass")).hexdigest()
        if signature == self._signature and self._cache is not None:
            self.counters["unchanged"] += 1
            with self._lock:
                self._cache["checked_at"] = now
            return self._cache
        vec = None
        if text.strip():
            vecs = self.embedder.embed([text])
            vec = vecs[0] if vecs is not None and len(vecs) else None
        self.index.refresh()
        on_screen = _related.screen_thread(self.store, screen, now=now)
        threads = _related.current_threads(self.store, screen, now=now, limit=1,
                                           ids=[on_screen] if on_screen is not None else ())
        affinity = _related._affinity(self.store, threads, now)
        hits = [] if vec is None else self.index.search(
            vec, now=now, limit=KEEP * 3, min_cosine=_related.SCREEN_MIN_COSINE, kinds=_rel.KINDS, boost=affinity,
            boost_weight=_related.AFFINITY_WEIGHT, per_kind=6)
        skip = {("thread", t.id) for t in threads}   # what restates the screen itself is not a connection
        for kind, ids in _related._same_screen(self.store, screen, now).items():
            skip |= {(kind, i) for i in ids}
        link_ids = [int(h["id"]) for h in hits if h["kind"] == "link"]
        if link_ids:   # a page merely viewed is no dot for the screen (another page of the same site)
            try:
                skip |= {("link", link.id) for link in self.store.links(ids=link_ids, limit=len(link_ids))
                         if link.how == "viewed"}
            except Exception:
                pass
        hits = [h for h in hits if (h["kind"], int(h["id"])) not in skip
                and h["cosine"] >= _related.KIND_MIN_COSINE.get(h["kind"], 0.0)][:KEEP]
        cache = {
            "at": now, "checked_at": now,
            "screen": {k: screen.get(k) for k in ("at", "capture_id", "thread_id", "app", "title", "host", "url", "kind",
                                                   "by_yuki", "source")},
            "work_threads": [t.id for t in threads], "screen_thread": on_screen,
            "hits": [{k: h[k] for k in ("kind", "id", "score", "cosine", "importance", "at")} for h in hits],
        }
        ms = (time.perf_counter() - t0) * 1000.0
        cpu_ms = (time.process_time() - c0) * 1000.0
        with self._lock:
            self._cache, self._signature = cache, signature
            self.counters["rankings"] += 1
            self.counters["ms"] += ms
            self.counters["cpu_ms"] += cpu_ms
        kinds: dict[str, int] = {}
        for h in hits:
            kinds[h["kind"]] = kinds.get(h["kind"], 0) + 1
        self._say("screen_recall", ms=round(ms, 1), cpu_ms=round(cpu_ms, 1), hits=len(hits), kinds=kinds,
                  best=round(max((h["cosine"] for h in hits), default=0.0), 3), thread=bool(threads),
                  source=screen.get("source"))
        return cache

    def _say(self, type: str, **fields: Any) -> None:
        if self._log is not None:
            try:
                self._log(type, **fields)
            except Exception:
                pass

    def _links(self) -> None:
        from yuki.memory import links

        t0, c0 = time.perf_counter(), time.process_time()
        try:
            embedder = self.embedder
            out = links.sync(self.store, embedder=embedder, embed_rounds=1)   # a first catch-up spreads over minutes
            out["call_digests"] = links.embed_call_digests(self.store, embedder, limit=8)
        except Exception as exc:
            out = {"error": f"{type(exc).__name__}: {exc}"}
        self.counters["links_syncs"] += 1
        changed = any(out.get(k) for k in ("viewed", "threads", "shared", "embedded", "call_digests"))
        if changed or out.get("error"):
            self._say("links_sync", **{k: v for k, v in out.items() if k != "ms"},
                      ms=round((time.perf_counter() - t0) * 1000, 1),
                      cpu_ms=round((time.process_time() - c0) * 1000, 1))

    def stats(self) -> dict[str, Any]:
        with self._lock:
            c = dict(self.counters)
        c["ms"], c["cpu_ms"] = round(c["ms"], 1), round(c["cpu_ms"], 1)
        c["index_rows"] = sum(self.index.stats()["rows"].values())
        return c

    def run(self, stop: threading.Event, pause_path: Path | None = None) -> None:
        """Rank on settled captures and sync links until ``stop``; nothing while the ``paused`` flag exists."""
        self._say("screen_recall_start", settle_s=self.settle_s, min_gap_s=self.min_gap_s)
        while not stop.is_set():
            mono = time.monotonic()
            with self._lock:
                first, last = self._first, self._last_capture
            wait = 5.0
            if first is not None and last is not None:   # wake when the burst will have settled
                due = max(min(last + self.settle_s, first + self.max_wait_s), self._last_run + self.min_gap_s)
                wait = max(0.05, due - mono)
            wait = min(wait, max(0.05, self._last_links + self.links_every_s - mono))
            self._event.wait(wait)
            self._event.clear()
            if stop.is_set():
                break
            if pause_path is not None and Path(pause_path).exists():
                continue
            mono = time.monotonic()
            with self._lock:
                first, last = self._first, self._last_capture
            if first is not None and last is not None:
                settled = mono - last >= self.settle_s or mono - first >= self.max_wait_s
                if settled and mono - self._last_run >= self.min_gap_s:
                    with self._lock:
                        self._first = self._last_capture = None
                    self._last_run = mono
                    try:
                        self.compute()
                    except Exception as exc:
                        self.counters["errors"] += 1
                        self._say("error", where="screen_recall", error=f"{type(exc).__name__}: {exc}")
            if mono - self._last_links >= self.links_every_s:
                self._last_links = mono
                self._links()
        self._say("screen_recall_stop", **self.stats())
