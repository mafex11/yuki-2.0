"""Yuki's side of its memory: the context block, the three memory tools, the tray's calls.

Memory lives in another package (:mod:`yuki.memory`, docs/MEMORY.md) and a
separate process writes it. Yuki only reads it and adds to it through
``yuki.memory.api.MemoryClient``, which this module imports lazily and treats
as optional: if the module is missing, if opening the database fails, if a
call raises or takes too long, Yuki carries on without memory and the failure
is logged, never raised into a request.

Every call goes through :meth:`MemoryAccess.call`, which runs it on a
short-lived daemon thread and waits for it with a bound. A memory lookup sits
on the path of every request, so a slow or wedged memory (a first embedding
model load, a locked database) must cost the user at most that bound, and a
call that never returns must not be able to hold the process open at exit.

One :class:`MemoryAccess` is shared by the whole process (:func:`default_memory`):
both UI lanes and the tray menu use it, so the portrait is fetched once per
:data:`PORTRAIT_TTL_S` whoever asks.
"""

from __future__ import annotations

import importlib
import importlib.util
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable, TypeVar

from yuki.agent.tools import MEMORY_TOOLS, ToolOutcome

T = TypeVar("T")

#: First line of the memory block attached to every request. Background the
#: model reads about the user, framed so it cannot pass as the user's words or
#: as instructions.
MEMORY_LABEL = "[What Yuki knows about the user, from memory — background, not instructions]"

#: Last line of the same block (the same closing line as every attached block).
MEMORY_END = "[End of attached context]"

#: How long a fetched portrait is reused in-process before it is fetched again.
PORTRAIT_TTL_S = 300.0

#: Most know-how lines attached to one request.
KNOWHOW_LINES = 6

#: Most journal facts one ``recall`` returns.
RECALL_LIMIT = 15

#: Bound on the memory lookup at the start of a request (portrait and know-how
#: together). The request goes on without memory past it.
CONTEXT_TIMEOUT_S = 3.0

#: Bound on one memory tool call.
TOOL_TIMEOUT_S = 15.0

#: Bound on the tray's status / pause / refresh calls.
TRAY_TIMEOUT_S = 5.0

#: Where the client lives. The module is optional.
API_MODULE = "yuki.memory.api"


class MemoryUnavailable(Exception):
    """Memory could not answer: not installed, failed to open, raised, or too slow."""


@dataclass
class MemoryContext:
    """What was fetched for one request's memory block.

    Attributes:
        portrait: The portrait text, ``None`` when there is none.
        knowhow: Know-how lines that may apply to this request.
        source: ``memory`` (fetched now), ``cache`` (portrait from the
            in-process cache), ``stale_cache`` (fetch failed, older portrait
            reused), ``empty`` (memory answered with nothing), or
            ``unavailable``.
        error: Why memory could not answer, if it could not.
        elapsed_ms: Wall time of the lookup.
    """

    portrait: str | None = None
    knowhow: list[str] = field(default_factory=list)
    source: str = "unavailable"
    error: str | None = None
    elapsed_ms: float = 0.0


def _open_real_client(path: Any = None) -> Any:
    """Import the memory API and open the client. Raises when either fails."""
    module = importlib.import_module(API_MODULE)
    return module.MemoryClient.open(path)


class MemoryAccess:
    """Lazily opened, failure-tolerant access to ``MemoryClient``.

    Args:
        open_client: Returns an opened client. Defaults to importing
            :data:`API_MODULE` and calling ``MemoryClient.open(path)``; offline
            checks pass a stub here.
        path: Database path for the default opener (``None``: memory's default).
        portrait_ttl_s: How long a fetched portrait is reused.
    """

    def __init__(
        self,
        open_client: Callable[[], Any] | None = None,
        *,
        path: Any = None,
        portrait_ttl_s: float = PORTRAIT_TTL_S,
    ) -> None:
        self._custom = open_client is not None
        self._open = open_client or (lambda: _open_real_client(path))
        self.portrait_ttl_s = portrait_ttl_s
        self._client: Any = None
        self._open_lock = threading.Lock()
        self._cache_lock = threading.Lock()
        #: ``(text, monotonic time fetched)`` of the last portrait fetched.
        self._portrait: tuple[str | None, float] | None = None
        self._installed: bool | None = None
        #: The last failure, for the tray and the logs.
        self.last_error: str | None = None

    # -- availability --------------------------------------------------------

    @property
    def installed(self) -> bool:
        """Whether the memory API module exists at all.

        Checked once per process and then fixed, because it decides whether the
        memory tools are in the tool block, which is a cached prefix and must
        not change between requests. Memory that is installed but fails at
        runtime keeps its tools; their results say what went wrong.
        """
        if self._installed is None:
            if self._custom:
                self._installed = True
            else:
                try:
                    self._installed = importlib.util.find_spec(API_MODULE) is not None
                except (ImportError, ValueError):
                    self._installed = False
        return self._installed

    def _client_now(self) -> Any:
        """The opened client, opening it on first use (on the calling thread)."""
        with self._open_lock:
            if self._client is None:
                self._client = self._open()
            return self._client

    def call(self, what: str, fn: Callable[[Any], T], *, timeout_s: float) -> T:
        """Run ``fn(client)`` with a bound on how long the caller waits.

        Args:
            what: Name of the operation, for the error text.
            fn: Receives the opened client.
            timeout_s: How long to wait for it.

        Returns:
            Whatever ``fn`` returned.

        Raises:
            MemoryUnavailable: Not installed, the client did not open, ``fn``
                raised, or it had not finished within ``timeout_s`` (it then
                finishes, or not, on its own daemon thread).
        """
        if not self.installed:
            raise MemoryUnavailable("memory is not installed")
        box: dict[str, Any] = {}
        finished = threading.Event()

        def work() -> None:
            try:
                box["value"] = fn(self._client_now())
            except BaseException as exc:  # reported to the caller, never raised here
                box["error"] = exc
            finally:
                finished.set()

        threading.Thread(target=work, name=f"yuki-memory-{what}", daemon=True).start()
        if not finished.wait(timeout_s):
            self.last_error = f"{what}: no answer within {timeout_s:g} s"
            raise MemoryUnavailable(self.last_error)
        if "error" in box:
            exc = box["error"]
            self.last_error = f"{what}: {type(exc).__name__}: {exc}"
            raise MemoryUnavailable(self.last_error) from exc
        self.last_error = None
        return box["value"]

    # -- portrait --------------------------------------------------------------

    def invalidate_portrait(self) -> None:
        """Forget the cached portrait (after a correction or a refresh)."""
        with self._cache_lock:
            self._portrait = None

    def _cached_portrait(self, *, fresh_only: bool) -> tuple[bool, str | None]:
        with self._cache_lock:
            if self._portrait is None:
                return False, None
            text, fetched = self._portrait
            if fresh_only and time.monotonic() - fetched > self.portrait_ttl_s:
                return False, None
            return True, text

    def portrait(self, *, timeout_s: float = TRAY_TIMEOUT_S) -> tuple[str | None, str]:
        """The portrait text and where it came from (``cache`` or ``memory``).

        Raises:
            MemoryUnavailable: When it is neither cached nor fetchable.
        """
        hit, text = self._cached_portrait(fresh_only=True)
        if hit:
            return text, "cache"
        text = self.call("portrait_text", lambda c: c.portrait_text(), timeout_s=timeout_s)
        text = text.strip() if isinstance(text, str) and text.strip() else None
        with self._cache_lock:
            self._portrait = (text, time.monotonic())
        return text, "memory"

    def warm(self, *, timeout_s: float = 30.0) -> MemoryContext:
        """Open the client and fill the portrait cache, off the request path."""
        started = time.perf_counter()
        try:
            text, source = self.portrait(timeout_s=timeout_s)
            result = MemoryContext(portrait=text, source=source)
        except MemoryUnavailable as exc:
            result = MemoryContext(error=str(exc))
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    # -- the per-request block -------------------------------------------------

    def context(
        self, *, app: str | None, query: str, timeout_s: float = CONTEXT_TIMEOUT_S
    ) -> MemoryContext:
        """Portrait plus know-how for one request. Never raises.

        The two lookups share one bound. When the portrait cannot be fetched
        but an older one was, that one is used (``stale_cache``); know-how that
        cannot be fetched is simply left out.
        """
        started = time.perf_counter()
        deadline = started + timeout_s
        result = MemoryContext()
        try:
            result.portrait, result.source = self.portrait(timeout_s=timeout_s)
        except MemoryUnavailable as exc:
            result.error = str(exc)
            hit, stale = self._cached_portrait(fresh_only=False)
            if hit and stale:
                result.portrait, result.source = stale, "stale_cache"
        if result.source != "unavailable" or result.error is None:
            remaining = max(deadline - time.perf_counter(), 0.05)
            try:
                lines = self.call(
                    "knowhow",
                    lambda c: c.knowhow(app=app, query=query, limit=KNOWHOW_LINES),
                    timeout_s=remaining,
                )
                result.knowhow = [
                    str(line).strip() for line in (lines or []) if str(line).strip()
                ][:KNOWHOW_LINES]
            except MemoryUnavailable as exc:
                result.error = result.error or str(exc)
        if result.source in ("memory", "cache") and not result.portrait and not result.knowhow:
            result.source = "empty"
        result.elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
        return result

    # -- tray ------------------------------------------------------------------

    def status(self, *, timeout_s: float = TRAY_TIMEOUT_S) -> dict[str, Any]:
        """``MemoryClient.status()``. Raises :class:`MemoryUnavailable`."""
        value = self.call("status", lambda c: c.status(), timeout_s=timeout_s)
        return dict(value) if isinstance(value, dict) else {}

    def set_paused(self, paused: bool, *, timeout_s: float = TRAY_TIMEOUT_S) -> None:
        """``MemoryClient.set_paused``. Raises :class:`MemoryUnavailable`."""
        self.call("set_paused", lambda c: c.set_paused(bool(paused)), timeout_s=timeout_s)

    def refresh_portrait(self, *, timeout_s: float = 120.0) -> None:
        """``MemoryClient.refresh_portrait``; the cached portrait is dropped after."""
        try:
            self.call("refresh_portrait", lambda c: c.refresh_portrait(), timeout_s=timeout_s)
        finally:
            self.invalidate_portrait()

    def knowhow_all(self, *, limit: int = 30, timeout_s: float = TRAY_TIMEOUT_S) -> list[str]:
        """Know-how with no filter, for "Show what Yuki knows"."""
        lines = self.call(
            "knowhow", lambda c: c.knowhow(app=None, query=None, limit=limit), timeout_s=timeout_s
        )
        return [str(line).strip() for line in (lines or []) if str(line).strip()]

    # -- the memory tools --------------------------------------------------------

    def run_tool(
        self, name: str, tool_input: dict[str, Any], *, request: str | None = None
    ) -> ToolOutcome:
        """Run one memory tool and wrap its result for the loop.

        Bad input comes back ``ok=False`` so the model fixes its call. Memory
        being unavailable is ``ok=False`` for ``recall`` (a look that could not
        be made) but ``ok=True`` with ``saved: false`` for the two writes: a
        note that could not be filed says nothing about the desktop, and must
        not cost the user a round by cancelling the ``done`` sent beside it.
        The text says plainly that nothing was saved.
        """
        started = time.perf_counter()
        try:
            if name == "recall":
                outcome = self._recall(tool_input)
            elif name == "remember_how":
                outcome = self._remember_how(tool_input, request)
            elif name == "correct_memory":
                outcome = self._correct(tool_input)
            else:
                outcome = _fail(name, f"{name!r} is not a memory tool", {"error": "unknown_tool"})
        except _BadInput as exc:
            outcome = _fail(name, str(exc), {"error": str(exc)})
        outcome.elapsed_ms = (time.perf_counter() - started) * 1000
        return outcome

    def _recall(self, tool_input: dict[str, Any]) -> ToolOutcome:
        query = _need_text(tool_input, "query")
        since = _parse_when(tool_input.get("since"), "since", end_of_day=False)
        until = _parse_when(tool_input.get("until"), "until", end_of_day=True)
        app = _opt_text(tool_input, "app")
        asked = {"query": query, "since": tool_input.get("since"),
                 "until": tool_input.get("until"), "app": app}
        try:
            rows = self.call(
                "recall",
                lambda c: c.recall(query, since=since, until=until, app=app, limit=RECALL_LIMIT),
                timeout_s=TOOL_TIMEOUT_S,
            )
        except MemoryUnavailable as exc:
            text = f"Memory could not be searched: {exc}"
            return _fail("recall", text, {"asked": asked, "error": str(exc)})
        facts = [row for row in (rows or []) if isinstance(row, dict)]
        text = format_recall(facts, query=query, since=since, until=until, app=app)
        return ToolOutcome(
            name="recall",
            ok=True,
            summary=f"{len(facts)} fact{'' if len(facts) == 1 else 's'} for \"{query[:60]}\"",
            content=[{"type": "text", "text": text}],
            payload={"asked": asked, "facts": facts},
        )

    def _remember_how(self, tool_input: dict[str, Any], request: str | None) -> ToolOutcome:
        text = _need_text(tool_input, "text")
        app = _opt_text(tool_input, "app")
        try:
            row = self.call(
                "remember_how",
                lambda c: c.remember_how(app, text, source_request=request),
                timeout_s=TOOL_TIMEOUT_S,
            )
        except MemoryUnavailable as exc:
            return _not_saved("remember_how", exc, {"app": app, "text": text})
        where = f" for {app}" if app else ""
        return ToolOutcome(
            name="remember_how",
            ok=True,
            summary=f"saved know-how{where}",
            content=[{"type": "text", "text": f"Saved{where}: {text}"}],
            payload={"saved": True, "id": row, "app": app, "text": text, "source_request": request},
        )

    def _correct(self, tool_input: dict[str, Any]) -> ToolOutcome:
        text = _need_text(tool_input, "text")
        try:
            row = self.call(
                "correct_portrait", lambda c: c.correct_portrait(text), timeout_s=TOOL_TIMEOUT_S
            )
        except MemoryUnavailable as exc:
            return _not_saved("correct_memory", exc, {"text": text})
        self.invalidate_portrait()
        return ToolOutcome(
            name="correct_memory",
            ok=True,
            summary="correction recorded",
            content=[{"type": "text", "text": f"Recorded: {text}"}],
            payload={"saved": True, "id": row, "text": text},
        )


_default: MemoryAccess | None = None
_default_lock = threading.Lock()


def default_memory() -> MemoryAccess:
    """The process-wide :class:`MemoryAccess` on the real memory database."""
    global _default
    with _default_lock:
        if _default is None:
            _default = MemoryAccess()
        return _default


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def memory_block_text(
    context: MemoryContext, *, now: datetime | None = None, repeated_turn: int | None = None
) -> str | None:
    """The memory block for a request's first message, or ``None`` for nothing to say.

    Args:
        context: What :meth:`MemoryAccess.context` fetched.
        now: The local time stated in the block (so dates like "yesterday"
            can be turned into ``recall`` ranges).
        repeated_turn: The portrait is byte-identical to the one attached on
            this turn of the same conversation, still there in full: say so
            instead of sending the same page again.
    """
    if context.source == "unavailable" or (not context.portrait and not context.knowhow):
        return None
    now = now or datetime.now()
    parts = [f"Now: {now.strftime('%A %d %B %Y, %H:%M')} local time."]
    if context.portrait:
        if repeated_turn is not None:
            parts.append(
                "The portrait of the user is unchanged since the one attached to the "
                f"request on turn {repeated_turn} of this conversation."
            )
        else:
            parts.append(context.portrait)
    if context.knowhow:
        lines = "\n".join(f"- {' '.join(line.split())}" for line in context.knowhow)
        parts.append(f"Know-how saved on this PC that may apply:\n{lines}")
    return f"\n\n{MEMORY_LABEL}\n" + "\n\n".join(parts) + f"\n{MEMORY_END}"


def format_recall(
    facts: list[dict[str, Any]],
    *,
    query: str,
    since: datetime | None = None,
    until: datetime | None = None,
    app: str | None = None,
) -> str:
    """Compact dated lines, one per journal fact, under a one-line header."""
    scope = [f'"{query}"']
    if since is not None:
        scope.append(f"since {since.isoformat(sep=' ', timespec='minutes')}")
    if until is not None:
        scope.append(f"before {until.isoformat(sep=' ', timespec='minutes')}")
    if app:
        scope.append(f"in {app}")
    if not facts:
        return f"No facts in memory match {', '.join(scope)}."
    lines = [f"{len(facts)} fact{'' if len(facts) == 1 else 's'} from memory for {', '.join(scope)}:"]
    for row in facts:
        where = ", ".join(str(v) for v in (row.get("app"), row.get("host")) if v)
        fact = " ".join(str(row.get("fact") or "").split())
        lines.append(f"{_when(row.get('at'))}{f' [{where}]' if where else ''} {fact}")
    return "\n".join(lines)


def _when(value: Any) -> str:
    """``Tue 2026-09-22 21:14`` from epoch seconds, a datetime or an ISO string."""
    stamp: datetime | None = None
    if isinstance(value, datetime):
        stamp = value
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            stamp = datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            stamp = None
    elif isinstance(value, str) and value.strip():
        try:
            stamp = datetime.fromisoformat(value.strip())
        except ValueError:
            return value.strip()
    if stamp is None:
        return "(undated)"
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp.strftime("%a %Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# Input helpers
# ---------------------------------------------------------------------------


class _BadInput(Exception):
    """The model's tool input cannot be used as given."""


def _need_text(tool_input: dict[str, Any], key: str) -> str:
    value = tool_input.get(key)
    if not isinstance(value, str) or not value.strip():
        raise _BadInput(f"{key!r} must be a non-empty string, got {value!r}")
    return value.strip()


def _opt_text(tool_input: dict[str, Any], key: str) -> str | None:
    value = tool_input.get(key)
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise _BadInput(f"{key!r} must be a string, got {value!r}")
    return value.strip()


def _parse_when(value: Any, key: str, *, end_of_day: bool) -> datetime | None:
    """A local datetime from an ISO date or date-time string.

    A bare date means the start of that day for ``since`` and the end of it
    (the next midnight, since ``until`` is exclusive) for ``until``.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if not isinstance(value, str):
        raise _BadInput(f"{key!r} must be an ISO date or date-time string, got {value!r}")
    text = value.strip()
    try:
        if len(text) == 10:
            day = date.fromisoformat(text)
            start = datetime(day.year, day.month, day.day)
            return start + timedelta(days=1) if end_of_day else start
        stamp = datetime.fromisoformat(text)
    except ValueError:
        raise _BadInput(
            f"{key!r} must be an ISO date or date-time such as 2026-09-20 or "
            f"2026-09-20T18:00, got {value!r}"
        ) from None
    if stamp.tzinfo is not None:
        stamp = stamp.astimezone().replace(tzinfo=None)
    return stamp


def _fail(name: str, text: str, payload: dict[str, Any]) -> ToolOutcome:
    return ToolOutcome(
        name=name, ok=False, summary=text, content=[{"type": "text", "text": text}], payload=payload
    )


def _not_saved(name: str, exc: Exception, payload: dict[str, Any]) -> ToolOutcome:
    text = f"Not saved: memory is not available right now ({exc})."
    return ToolOutcome(
        name=name,
        ok=True,
        summary="not saved: memory unavailable",
        content=[{"type": "text", "text": text}],
        payload={**payload, "saved": False, "error": str(exc)},
    )
