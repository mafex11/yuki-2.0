"""When the PC was off or asleep: Windows' own power history, read-only, as timeline rows.

Contract: ``docs/MEMORY.md`` -> "Timeline: PC off and asleep".

A hole in the timeline used to be only a hole: the service was not running,
or :data:`yuki.memory.timeline.MAX_TICK_GAP_S` left a sleep out. This module
reads what Windows itself recorded and writes each span as a timeline row
(:meth:`yuki.memory.store.Store.add_power_spans`, state ``off`` / ``asleep`` /
``unknown``), so every reader of the timeline can tell "the PC was off
23:10-08:05" from "the PC was on and memory was not recording".

Sources (the System event log, through ``wevtapi.dll``; reading it needs no
admin rights; nothing is written, cleared or subscribed to):

* ``Microsoft-Windows-Kernel-General`` 13 (the OS shutting down: ``StopTime``)
  and 12 (the OS started: ``StartTime``): a 13 followed by a 12 is **off**,
  from the stop to the start.
* A 12 with no 13 since the boot before it is a start after the PC stopped
  without a clean shutdown (a crash, a power cut, a hard reset): **unknown**,
  from the last moment the PC is known to have been alive - ``EventLog`` 6008's
  "previous shutdown was unexpected" time, or the end of the last stretch the
  timeline recorded, whichever is later - to the start. Nothing is recorded
  when neither is known.
* ``Microsoft-Windows-Power-Troubleshooter`` 1 (the system returned from a
  low-power state: ``SleepTime``, ``WakeTime``): **asleep** (sleep or
  hibernation). ``Kernel-Power`` 42 (entering sleep) paired with 107 (resumed)
  is the fallback when no Power-Troubleshooter event covers it, and
  ``Kernel-Power`` 506/507 (modern standby entered/left) are asleep too. A
  sleep inside an "off" span (fast startup resumes the kernel from
  hibernation) is left to the off span.

Spans shorter than :data:`MIN_SPAN_S` are dropped. A service restart while the
PC stayed on has no shutdown, start or sleep in the log, so nothing is
recorded: the gap stays a gap ("the PC was on; nothing recorded").

When it reads (``yuki-memory``, :mod:`yuki.memory.service`): once at start,
and when the service sees that it was not running for a while (its main loop
wakes every second; a jump of :data:`RESUME_JUMP_S` in wall time between two
wakes is a sleep, a hibernation or a stall), then once more
:data:`RECHECK_AFTER_S` later, because Windows writes the wake event a little
after the resume. Checkpoint ``source_checkpoints('power.events')``: the time
of the newest event read; each read goes back :data:`OVERLAP_S` before it so a
shutdown read last time still pairs with the start read now. Re-reading never
doubles a span (the rows are unique on kind and end).

Logs (the service log, content-free): ``power_sync`` - why, events read, spans
found and added by kind, ms and CPU ms.

Public API::

    read_events(since, until=None, *, timeout_ms=5000) -> list[PowerEvent]
    derive_spans(events, *, alive_before=None, now=None) -> list[PowerSpan]
    PowerHistory(store, *, log=None, reader=read_events, clock=time.time).sync(reason, now=None) -> dict
"""

from __future__ import annotations

import ctypes
import struct
import time
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Callable, Sequence
from ctypes import wintypes
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

#: Spans shorter than this are not recorded (a reboot's own seconds, a blink of standby).
MIN_SPAN_S = 60.0
#: A read goes back this far before the checkpoint, so a shutdown pairs with the start after it.
OVERLAP_S = 2 * 86400.0
#: The first read (no checkpoint) goes back at most this far.
FIRST_READ_S = 30 * 86400.0
#: The service's main loop wakes every second; a wall-clock jump of this much between two wakes
#: means the process did not run (sleep, hibernation): the history is read again.
RESUME_JUMP_S = 30.0
#: ...and once more this long after, when Windows has written the wake event.
RECHECK_AFTER_S = 120.0
#: A 6008 ("the previous shutdown was unexpected") belongs to the start it follows within this long.
UNEXPECTED_WITHIN_S = 900.0
CHECKPOINT = "power.events"

_NS = "{http://schemas.microsoft.com/win/2004/08/events/event}"
_GENERAL = "Microsoft-Windows-Kernel-General"
_POWER = "Microsoft-Windows-Kernel-Power"
_TROUBLESHOOTER = "Microsoft-Windows-Power-Troubleshooter"
_EVENTLOG = "EventLog"

#: The events read (provider, ids): the OS's own event identifiers, not a behaviour list.
QUERY_EVENTS: tuple[tuple[str, tuple[int, ...]], ...] = (
    (_GENERAL, (12, 13)),
    (_POWER, (42, 107, 506, 507)),
    (_TROUBLESHOOTER, (1,)),
    (_EVENTLOG, (6008,)),
)


@dataclass
class PowerEvent:
    """One event from the System log (only what the spans need)."""

    provider: str
    event_id: int
    at: float                               # TimeCreated, epoch seconds
    data: dict[str, str] = field(default_factory=dict)
    binary: bytes = b""


@dataclass
class PowerSpan:
    kind: str                               # off | asleep | unknown
    start: float
    end: float
    source: str                             # shutdown | sleep | modern_standby | unexpected_shutdown | no_record

    @property
    def seconds(self) -> float:
        return self.end - self.start


# ---------------------------------------------------------------------------
# Reading the System log (wevtapi, read-only)
# ---------------------------------------------------------------------------

_EvtQueryChannelPath = 0x1
_EvtQueryForwardDirection = 0x100
_EvtRenderEventXml = 1
_ERROR_NO_MORE_ITEMS = 259
_ERROR_INSUFFICIENT_BUFFER = 122


def _iso_utc(at: float) -> str:
    return datetime.fromtimestamp(at, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def build_query(since: float, until: float | None = None) -> str:
    """The XPath of :data:`QUERY_EVENTS` created at or after ``since`` (and before ``until``)."""
    wanted = " or ".join(
        f"(Provider[@Name='{p}'] and ({' or '.join(f'EventID={i}' for i in ids)}))" for p, ids in QUERY_EVENTS
    )
    when = f"@SystemTime>='{_iso_utc(since)}'" + (f" and @SystemTime<'{_iso_utc(until)}'" if until else "")
    return f"*[System[TimeCreated[{when}] and ({wanted})]]"


def _parse_time(text: str | None) -> float | None:
    """``2026-09-23T03:24:52.5000000Z`` (the log's UTC form) as epoch seconds."""
    if not text:
        return None
    try:
        t = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.timestamp()


def parse_event_xml(xml: str) -> PowerEvent | None:
    """One rendered event (``EvtRenderEventXml``) as a :class:`PowerEvent`; ``None`` if unreadable."""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return None
    system = root.find(f"{_NS}System")
    if system is None:
        return None
    provider = system.find(f"{_NS}Provider")
    event_id = system.find(f"{_NS}EventID")
    created = system.find(f"{_NS}TimeCreated")
    try:
        eid = int((event_id.text or "").strip()) if event_id is not None else -1
    except ValueError:
        return None
    at = _parse_time(created.get("SystemTime") if created is not None else None)
    if at is None or provider is None:
        return None
    data: dict[str, str] = {}
    binary = b""
    body = root.find(f"{_NS}EventData")
    if body is not None:
        for n, item in enumerate(body.findall(f"{_NS}Data")):
            data[item.get("Name") or str(n)] = (item.text or "").strip()
        raw = body.find(f"{_NS}Binary")
        if raw is not None and raw.text:
            try:
                binary = bytes.fromhex(raw.text.strip())
            except ValueError:
                binary = b""
    return PowerEvent(provider.get("Name") or "", eid, at, data, binary)


def read_events(since: float, until: float | None = None, *, timeout_ms: int = 5000) -> list[PowerEvent]:
    """The power events of the System log created in ``[since, until)``, oldest first. Read-only.

    Raises ``OSError`` when the log cannot be queried.
    """
    wevt = ctypes.WinDLL("wevtapi", use_last_error=True)
    wevt.EvtQuery.argtypes = [wintypes.HANDLE, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    wevt.EvtQuery.restype = wintypes.HANDLE
    wevt.EvtNext.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
                             wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    wevt.EvtNext.restype = wintypes.BOOL
    wevt.EvtRender.argtypes = [wintypes.HANDLE, wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                               ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
    wevt.EvtRender.restype = wintypes.BOOL
    wevt.EvtClose.argtypes = [wintypes.HANDLE]
    wevt.EvtClose.restype = wintypes.BOOL

    query = wevt.EvtQuery(None, "System", build_query(since, until), _EvtQueryChannelPath | _EvtQueryForwardDirection)
    if not query:
        raise OSError(ctypes.get_last_error(), "EvtQuery(System) failed")
    out: list[PowerEvent] = []
    buffer = ctypes.create_string_buffer(8192)
    try:
        batch = (wintypes.HANDLE * 32)()
        while True:
            returned = wintypes.DWORD(0)
            if not wevt.EvtNext(query, len(batch), batch, timeout_ms, 0, ctypes.byref(returned)):
                error = ctypes.get_last_error()
                if error == _ERROR_NO_MORE_ITEMS:
                    break
                raise OSError(error, "EvtNext failed")
            for i in range(returned.value):
                handle = batch[i]
                try:
                    used, count = wintypes.DWORD(0), wintypes.DWORD(0)
                    ok = wevt.EvtRender(None, handle, _EvtRenderEventXml, len(buffer), buffer,
                                        ctypes.byref(used), ctypes.byref(count))
                    if not ok and ctypes.get_last_error() == _ERROR_INSUFFICIENT_BUFFER:
                        buffer = ctypes.create_string_buffer(used.value + 2)
                        ok = wevt.EvtRender(None, handle, _EvtRenderEventXml, len(buffer), buffer,
                                            ctypes.byref(used), ctypes.byref(count))
                    if ok:
                        xml = buffer.raw[: used.value].decode("utf-16-le", errors="replace").rstrip("\x00")
                        event = parse_event_xml(xml)
                        if event is not None:
                            out.append(event)
                finally:
                    wevt.EvtClose(handle)
    finally:
        wevt.EvtClose(query)
    out.sort(key=lambda e: e.at)
    return out


# ---------------------------------------------------------------------------
# Events -> spans (pure)
# ---------------------------------------------------------------------------


def _unexpected_time(event: PowerEvent) -> float | None:
    """The time in a 6008 ("the previous system shutdown at ... was unexpected"): its second SYSTEMTIME (UTC)."""
    raw = event.binary
    if len(raw) < 32:
        return None
    try:
        year, month, _dow, day, hour, minute, second, ms = struct.unpack_from("<8H", raw, 16)
        return datetime(year, month, day, hour, minute, second, ms * 1000, tzinfo=timezone.utc).timestamp()
    except (struct.error, ValueError):
        return None


def _data_time(event: PowerEvent, name: str) -> float:
    return _parse_time(event.data.get(name)) or event.at


def derive_spans(
    events: Sequence[PowerEvent], *, alive_before: Callable[[float], float | None] | None = None,
    now: float | None = None,
) -> list[PowerSpan]:
    """Off, asleep and unknown spans from the log's events (oldest first).

    ``alive_before(t)``: the latest moment before ``t`` the timeline saw the
    PC in use (the end of its last stretch), used as the start of an
    ``unknown`` span when the log has none.
    """
    now = time.time() if now is None else float(now)
    events = sorted(events, key=lambda e: e.at)
    boots = [(_data_time(e, "StartTime"), e) for e in events if e.provider == _GENERAL and e.event_id == 12]
    stops = [_data_time(e, "StopTime") for e in events if e.provider == _GENERAL and e.event_id == 13]
    unexpected = [e for e in events if e.provider == _EVENTLOG and e.event_id == 6008]
    spans: list[PowerSpan] = []
    previous_boot: float | None = None
    for boot, _ in sorted(boots, key=lambda b: b[0]):
        before = [s for s in stops if s <= boot and (previous_boot is None or s > previous_boot)]
        if before:
            spans.append(PowerSpan("off", max(before), boot, "shutdown"))
        else:
            mark = next((m for m in unexpected if boot - 60.0 <= m.at <= boot + UNEXPECTED_WITHIN_S), None)
            starts = []
            if mark is not None and (t := _unexpected_time(mark)) is not None:
                starts.append(t)
            if alive_before is not None and (mark is not None or previous_boot is not None):
                try:
                    t = alive_before(boot)
                except Exception:
                    t = None
                if t is not None:
                    starts.append(t)
            starts = [s for s in starts if s < boot and (previous_boot is None or s > previous_boot)]
            if starts:
                spans.append(PowerSpan("unknown", max(starts), boot,
                                       "unexpected_shutdown" if mark is not None else "no_record"))
        previous_boot = boot

    sleeps: list[PowerSpan] = []
    for e in events:
        if e.provider == _TROUBLESHOOTER and e.event_id == 1:
            start, end = _parse_time(e.data.get("SleepTime")), _parse_time(e.data.get("WakeTime"))
            if start is not None and end is not None:
                sleeps.append(PowerSpan("asleep", start, end, "sleep"))
    # Kernel-Power 42 (entering sleep) + the next 107 (resumed), where no Power-Troubleshooter event covers it
    pending: float | None = None
    for e in events:
        if e.provider != _POWER:
            continue
        if e.event_id == 42:
            pending = e.at
        elif e.event_id == 107 and pending is not None:
            if not any(s.start - 120.0 <= pending <= s.end for s in sleeps):
                sleeps.append(PowerSpan("asleep", pending, e.at, "sleep"))
            pending = None
    # modern standby: 506 (entered) + the next 507 (left)
    entered: float | None = None
    for e in events:
        if e.provider != _POWER:
            continue
        if e.event_id == 506:
            entered = e.at
        elif e.event_id == 507 and entered is not None:
            sleeps.append(PowerSpan("asleep", entered, e.at, "modern_standby"))
            entered = None

    offs = [s for s in spans if s.kind == "off"]
    for s in sleeps:
        overlap = max((min(s.end, o.end) - max(s.start, o.start) for o in offs), default=0.0)
        if overlap < 0.5 * max(s.seconds, 1.0):   # fast startup resumes from hibernation inside an off span
            spans.append(s)
    # an unknown span starts at the last sign of life; a sleep recorded after that moment is known,
    # so the unknown part starts when that sleep ended
    for u in [s for s in spans if s.kind == "unknown"]:
        for s in sleeps:
            if s in spans and s.start < u.end and s.end > u.start:
                u.start = max(u.start, min(s.end, u.end))
    out = []
    for s in spans:
        s.end = min(s.end, now)
        if s.seconds >= MIN_SPAN_S:
            out.append(s)
    return sorted(out, key=lambda s: (s.start, s.end))


# ---------------------------------------------------------------------------
# Into the store
# ---------------------------------------------------------------------------


class PowerHistory:
    """Reads the power history into the timeline (the service calls :meth:`sync`).

    Args:
        store: the shared store.
        log: ``log(type, **fields)``, content-free.
        reader: ``reader(since) -> list[PowerEvent]`` (:func:`read_events`; replaced offline).
    """

    def __init__(self, store: Any, *, log: Callable[..., Any] | None = None,
                 reader: Callable[[float], list[PowerEvent]] = read_events,
                 clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._log_fn = log
        self.reader = reader
        self.clock = clock
        self.syncs = 0

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(type, **fields)
        except Exception:
            pass

    def sync(self, reason: str, now: float | None = None) -> dict[str, Any]:
        """Read the log since the checkpoint and record new spans. Never raises; returns what it did."""
        now = float(self.clock()) if now is None else float(now)
        t0, c0 = time.perf_counter(), time.process_time()
        result: dict[str, Any] = {"reason": reason, "events": 0, "spans": 0, "added": 0, "kinds": {}, "error": None}
        try:
            checkpoint = self.store.source_checkpoint(CHECKPOINT)
            if checkpoint is not None:
                since = float(checkpoint) - OVERLAP_S
            else:
                first, _ = self.store.timeline_bounds()
                since = max((first if first is not None else now) - OVERLAP_S, now - FIRST_READ_S)
            events = self.reader(since)
            spans = derive_spans(events, alive_before=self.store.timeline_last_end_before, now=now)
            result["events"], result["spans"] = len(events), len(spans)
            result["added"] = self.store.add_power_spans([(s.kind, s.start, s.end, s.source) for s in spans])
            result["kinds"] = dict(Counter(s.kind for s in spans))
            newest = max((e.at for e in events), default=None)
            if newest is not None and (checkpoint is None or newest > float(checkpoint)):
                self.store.set_source_checkpoint(CHECKPOINT, f"{newest:.3f}")
            elif checkpoint is None:
                self.store.set_source_checkpoint(CHECKPOINT, f"{since:.3f}")
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"
        result["ms"] = round((time.perf_counter() - t0) * 1000.0, 1)
        result["cpu_ms"] = round((time.process_time() - c0) * 1000.0, 1)
        self.syncs += 1
        self._log("power_sync", **result)
        return result


__all__ = [
    "PowerEvent", "PowerSpan", "PowerHistory", "read_events", "derive_spans", "parse_event_xml", "build_query",
    "MIN_SPAN_S", "RESUME_JUMP_S", "RECHECK_AFTER_S", "CHECKPOINT",
]
