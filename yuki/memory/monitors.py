"""The user's monitors, as measured: memory's monitor record (mon1; f\\mon\\PLAN.md section 2).

22 Oct 08:44: the user asked Yuki to turn on their first monitor. Windows listed only the VG271U (their second): the
AOpen 24HC1QR had left Windows (switched fully off, "missing on the bus"). Yuki knew monitors only as Windows listed
them now (``windows._seen_monitors`` was RAM in one process), so it called the VG271U their "first", could not name the
AOpen, and the Twinkle Tray route had no id to select it by. This module keeps what Yuki measured about each monitor so
a fresh process knows it, names it and finds its port when Windows no longer lists it.

**One row per monitor** (table ``monitors``, migration 49), keyed by a keyed digest of its EDID maker, product and
serial (``ident``; the serial itself is never stored): its EDID name, Plug and Play id, connector and native size; the
port it was last on (the Plug and Play instance key, which a monitor app such as Twinkle Tray lists it by, and the GPU
target id); when it was first and last seen attached, its seconds attached, as Windows' primary and per place word
(``main``, ``second``, ...: ``windows.monitor_roles``), its last place left to right; its last power (VCP 0xD6) and
brightness (0x10) as the display driver read them, an off the driver wrote, when Windows last stopped listing it, and
``left_bus`` (1: it left Windows within :data:`LEFT_BUS_S` of an off Yuki wrote or of a read that said off; 0: it
stayed attached that long after an off; ``None`` not measured). The levels it was measured at while visible: the
last brightness above 0 read (``lit_level``, ``lit_at``) and the lowest (``lit_low``), from the reads of the level it
sat at (a level they set read back, a read, a turn-on that found it visible), never Yuki's own restore and never a
read that a turn-on found dark (22 Oct: "turn on my first monitor" with the 24HC1QR on at 0%: the driver restores
``lit_level``, the middle of its range with none). ``lit_low`` is 0 only from a 0 read taken while they were using
that monitor (the window in front on it, their own input just now): 0% is then a level they sit at, never dark.

**Writers** (code only, measured facts, never a model, never the user's words):

* :class:`Recorder`, on the memory service's timeline thread (``timeline._desk_tick``): reads the monitors attached
  (``windows.list_monitors``) at most every :data:`READ_EVERY_S`, writes a sighting on each change (an arrival, a
  departure: ``removed_at`` and ``left_bus``; a move to another port or place) and the seconds at most every
  :data:`FLUSH_EVERY_S`. At its start it reads Windows' own device list once (``windows.monitor_devices``: SetupAPI,
  not-present devices included, with their registry EDID) to refresh the port and the removal time of the monitors
  the record already holds: never a sighting, so a monitor Yuki never saw attached (an old TV) is never theirs.
* :func:`note_power` / :func:`note_level`: the display driver's own reads and writes (any process; the process's
  memory store, :func:`use`).

**Readers** (:func:`known`, :func:`away`, :func:`match`, :func:`words`): the rows in a RAM table re-read when the
record's, the meanings' or the learned names' stamp changes (checked at most every :data:`STAMP_EVERY_S`). ``known``
is the user's monitors: the rows seen attached within :data:`KNOWN_DAYS` for at least :data:`KNOWN_MIN_S` (a TV
plugged in once long ago is not one of them). ``words`` are the user's own words for a monitor:
what the meanings pass learned they call it (``meanings.called_of``, kind ``monitor``: their "first") and the heard
forms the names pass learned for its name ("PG" for the VG271U).

Internal understanding only (21 Oct): read by the display driver, the desk and the you-card; never a UI list. Logs are
content-free (counts and code words). Lightweight: one small table, a read of the attached monitors every few seconds
in the service (no DDC/CI, no polling of a monitor), Windows' device list once per start.

Public::

    use(store)                                   # this process's memory store (MemoryClient binds it)
    known(store=None) -> list[Known]             # the user's monitors
    away(attached_paths, attached_ids, store=None) -> list[Known]   # theirs that Windows doesn't list now
    match(rows, device_path, pnp, label) -> Known | None
    words(row, heard=True) -> tuple[str, ...]    # their words for it (learned), then heard forms
    card_stamp(store) -> tuple                   # what the you-card's monitor line depends on
    note_power(device_path, value, *, wrote=False, pnp="", label="")
    note_level(device_path, level, *, sat=True, used=False, pnp="", label="")
    Recorder(store, log=None).tick(now)          # the timeline's sightings
"""

from __future__ import annotations

import json
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

#: The record's RAM table is checked against its stamp at most this often (s).
STAMP_EVERY_S = 5.0
#: The recorder reads the monitors attached at most this often (s): cheap (no DDC/CI), and a departure is dated
#: within it.
READ_EVERY_S = 5.0
#: The seconds attached, as primary and per place are written at least this often (s), and on every change.
FLUSH_EVERY_S = 300.0
#: A gap between two reads longer than this (the PC slept) adds no seconds (s).
MAX_GAP_S = 120.0
#: An off written (or a read that said off) this close before Windows stopped listing a monitor means it leaves
#: Windows when off; one it stayed attached this long after means it doesn't (s).
LEFT_BUS_S = 30.0
#: The words kept per monitor (learned names and heard forms).
WORDS_N = 4
#: A monitor counts as one of the user's monitors when it was seen attached within this many days and for at least
#: :data:`KNOWN_MIN_S` in all (a TV plugged in once a month ago is no monitor of theirs now).
KNOWN_DAYS = 14.0
KNOWN_MIN_S = 60.0

LogFn = Callable[..., Any]


@dataclass(frozen=True)
class Known:
    """One monitor of the record (see the module docstring). ``called``: the user's own words for it (learned by the
    meanings pass); ``heard``: the heard forms learned for its name."""

    id: int
    ident: str
    label: str = ""
    pnp: str = ""
    connector: str = ""
    width: int = 0
    height: int = 0
    instance: str = ""
    target_id: int = 0
    first_seen: float | None = None
    last_seen: float | None = None
    attached_s: float = 0.0
    primary_s: float = 0.0
    roles: dict[str, float] = field(default_factory=dict)
    place: int | None = None
    power: int | None = None
    power_at: float | None = None
    level: float | None = None
    level_at: float | None = None
    lit_level: float | None = None
    lit_low: float | None = None
    lit_at: float | None = None
    off_at: float | None = None
    removed_at: float | None = None
    left_bus: int | None = None
    called: tuple[str, ...] = ()
    heard: tuple[str, ...] = ()

    @property
    def said(self) -> str:
        """How it is said: its own name, else its Plug and Play id."""
        return self.label or self.pnp or "a monitor"

    @property
    def theirs(self) -> bool:
        """Seen attached at least once (Windows' device list alone never makes a monitor theirs)."""
        return self.first_seen is not None

    @property
    def device_path(self) -> str:
        from yuki.perception.windows import device_path_of

        return device_path_of(self.pnp, self.instance)


# ---------------------------------------------------------------------------------------------------------------------
# The process's store
# ---------------------------------------------------------------------------------------------------------------------

_bound: Any = None
_bound_lock = threading.Lock()


def use(store: Any) -> None:
    """Bind this process's memory store (``MemoryClient`` does it when it opens): the readers and the display
    driver's notes use it when no store is given. The newest one wins."""
    global _bound
    if store is None:
        return
    with _bound_lock:
        _bound = store


def _store(store: Any = None) -> Any:
    if store is not None:
        return store
    with _bound_lock:
        return _bound


# ---------------------------------------------------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------------------------------------------------

_lock = threading.Lock()
#: ``db path -> (checked at (monotonic), stamp, rows)``
_cache: dict[str, tuple[float, tuple[Any, ...], list[Known]]] = {}


def _stamp(store: Any) -> tuple[Any, ...]:
    """What the RAM table depends on (content-free): the record, the learned names, the meanings."""
    try:
        row = store._query("SELECT (SELECT count(*) || ':' || coalesce(max(updated_at), 0) FROM monitors),"
                           " (SELECT count(*) || ':' || coalesce(max(id), 0) || ':' || coalesce(max(ended_at), 0)"
                           " FROM name_aliases)")
        got = tuple(row[0]) if row else ()
    except Exception:  # noqa: BLE001 - no table yet (an older database)
        return ()
    try:
        from yuki.memory.meanings import stamp as meanings_stamp

        return got + tuple(meanings_stamp(store) or ())
    except Exception:  # noqa: BLE001
        return got


def card_stamp(store: Any) -> tuple[Any, ...]:
    """What the you-card's monitor line depends on: the user's monitors and their stable facts (``info_at``; never
    the seconds a flush adds). Content-free; ``()`` without the table."""
    try:
        row = store._query("SELECT count(*), coalesce(max(info_at), 0) FROM monitors WHERE first_seen IS NOT NULL")
        return tuple(row[0]) if row else ()
    except Exception:  # noqa: BLE001
        return ()


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _roles(text: Any) -> dict[str, float]:
    try:
        got = json.loads(text or "{}")
    except (TypeError, ValueError):
        return {}
    return {str(k): float(v) for k, v in got.items() if isinstance(v, (int, float))} if isinstance(got, dict) else {}


def _key(text: Any) -> str:
    return " ".join("".join(ch if ch.isalnum() else " " for ch in str(text or "").casefold()).split())


def _heard_by_name(store: Any) -> dict[str, list[str]]:
    """``{name key: [heard forms, newest first]}`` of the live learned aliases (the names pass, the misheard list)."""
    out: dict[str, list[str]] = {}
    try:
        rows = store.aliases()
    except Exception:  # noqa: BLE001
        return out
    for a in reversed(rows):
        heard = " ".join(str(a.get("heard") or "").split())
        k = _key(a.get("name"))
        if heard and k and heard.casefold() not in {h.casefold() for h in out.get(k, [])}:
            out.setdefault(k, []).append(heard)
    return out


def _read(store: Any) -> list[Known]:
    try:
        rows = store._query("SELECT * FROM monitors ORDER BY id")
    except Exception:  # noqa: BLE001
        return []
    heard = _heard_by_name(store) if rows else {}
    try:
        from yuki.memory.meanings import called_of
    except Exception:  # noqa: BLE001
        called_of = None  # type: ignore[assignment]
    out: list[Known] = []
    for r in rows:
        label, pnp = str(r["label"] or ""), str(r["pnp"] or "")
        called: list[str] = []
        if called_of is not None:
            for name in dict.fromkeys(n for n in (label, pnp) if n):
                for c in called_of(store, "monitor", name):
                    if c.casefold() not in {x.casefold() for x in called}:
                        called.append(c)
        mine = [h for n in dict.fromkeys(n for n in (label, pnp) if n) for h in heard.get(_key(n), [])]
        out.append(Known(
            id=int(r["id"]), ident=str(r["ident"]), label=label, pnp=pnp, connector=str(r["connector"] or ""),
            width=int(r["width"] or 0), height=int(r["height"] or 0), instance=str(r["instance"] or ""),
            target_id=int(r["target_id"] or 0), first_seen=r["first_seen"], last_seen=r["last_seen"],
            attached_s=float(r["attached_s"] or 0.0), primary_s=float(r["primary_s"] or 0.0),
            roles=_roles(r["roles"]), place=r["place"], power=r["power"], power_at=r["power_at"],
            level=r["level"], level_at=r["level_at"], lit_level=r["lit_level"], lit_low=r["lit_low"],
            lit_at=r["lit_at"], off_at=r["off_at"], removed_at=r["removed_at"],
            left_bus=r["left_bus"], called=tuple(called[:WORDS_N]), heard=tuple(dict.fromkeys(mine))[:WORDS_N]))
    return out


def rows(store: Any = None) -> list[Known]:
    """Every row of the record (the user's monitors and the ones only Windows' device list named), from the RAM
    table. ``[]`` without a store or the table. Never raises."""
    store = _store(store)
    if store is None:
        return []
    try:
        path = str(getattr(store, "path", ""))
        mono = time.monotonic()
        with _lock:
            hit = _cache.get(path)
        if hit is not None and mono - hit[0] < STAMP_EVERY_S:
            return list(hit[2])
        st = _stamp(store)
        if hit is not None and hit[1] == st and st:
            with _lock:
                _cache[path] = (mono, st, hit[2])
            return list(hit[2])
        got = _read(store)
        with _lock:
            _cache[path] = (mono, st, got)
        return list(got)
    except Exception:  # noqa: BLE001
        return []


def known(store: Any = None, *, now: float | None = None) -> list[Known]:
    """The user's monitors: seen attached within :data:`KNOWN_DAYS`, for at least :data:`KNOWN_MIN_S` in all (the
    seconds a flush wrote, or the span of its sightings), most time attached first."""
    now = time.time() if now is None else float(now)
    out = []
    for r in rows(store):
        if not r.theirs or r.last_seen is None or now - float(r.last_seen) > KNOWN_DAYS * 86400.0:
            continue
        if max(r.attached_s, float(r.last_seen) - float(r.first_seen or r.last_seen)) < KNOWN_MIN_S:
            continue
        out.append(r)
    return sorted(out, key=lambda r: -r.attached_s)


def match(rows_: Sequence[Known], device_path: str, pnp: str = "", label: str = "") -> Known | None:
    """The row of an attached monitor: by its device path (its port), else the one row of its Plug and Play id and
    name (it moved to another port). ``None`` when none, or two rows fit (twin monitors on new ports)."""
    path = str(device_path or "").casefold()
    if path:
        hit = [r for r in rows_ if r.device_path.casefold() == path]
        if len(hit) == 1:
            return hit[0]
    if pnp:
        same = [r for r in rows_ if r.pnp.casefold() == pnp.casefold()
                and (not label or not r.label or r.label.casefold() == label.casefold())]
        if len(same) == 1:
            return same[0]
    return None


def away(attached_paths: Iterable[str], attached_ids: Iterable[tuple[str, str]] = (),
         store: Any = None) -> list[Known]:
    """The user's monitors that Windows does not list now: not on any attached device path, and (when its Plug and
    Play id and name are both given) no attached monitor of that id and name (one that moved ports is attached)."""
    paths = {str(p or "").casefold() for p in attached_paths if p}
    ids = {(str(p or "").casefold(), str(n or "").casefold()) for p, n in attached_ids if p}
    out = []
    for r in known(store):
        if r.device_path and r.device_path.casefold() in paths:
            continue
        if (r.pnp.casefold(), r.label.casefold()) in ids:
            continue
        out.append(r)
    return out


def words(row: Known, *, heard: bool = True) -> tuple[str, ...]:
    """The user's words for a monitor: what they call it (learned), then (``heard``) the heard forms of its name."""
    return tuple(dict.fromkeys([*row.called, *(row.heard if heard else ())]))


# ---------------------------------------------------------------------------------------------------------------------
# The display driver's notes (any process)
# ---------------------------------------------------------------------------------------------------------------------


def _note(device_path: str, pnp: str, label: str, sets: dict[str, Any], store: Any = None, *,
          low: float | None = None) -> bool:
    store = _store(store)
    if store is None or not sets:
        return False
    try:
        row = match(rows(store), device_path, pnp, label)
        if row is None:
            return False
        assign = [f"{n} = ?" for n in sets]
        values: list[Any] = list(sets.values())
        if low is not None:
            # the lowest level it sat at, kept by the database itself (two notes of one flush never race the RAM table)
            assign.append("lit_low = CASE WHEN lit_low IS NULL OR lit_low > ? THEN ? ELSE lit_low END")
            values += [low, low]
        now = time.time()
        store._write(lambda conn: conn.execute(
            f"UPDATE monitors SET {', '.join(assign)}, updated_at = ? WHERE id = ?", (*values, now, row.id)))
        clear_cache()
        return True
    except Exception:  # noqa: BLE001 - a note never breaks a run
        return False


def note_power(device_path: str, value: int | None, *, wrote: bool = False, pnp: str = "", label: str = "",
               store: Any = None) -> bool:
    """The display driver read a monitor's power (VCP 0xD6, 1 on, 2-5 standby or off) or wrote it (``wrote``: an
    off it wrote is kept as ``off_at``). ``False`` when the monitor has no row yet (the service's recorder writes
    it)."""
    if value is None:
        return False
    now = time.time()
    sets: dict[str, Any] = {"power": int(value), "power_at": now}
    if wrote and int(value) != 1:
        sets["off_at"] = now
    return _note(device_path, pnp, label, sets, store)


def note_level(device_path: str, level: float | None, *, sat: bool = True, used: bool = False, pnp: str = "",
               label: str = "", store: Any = None) -> bool:
    """The display driver read a monitor's brightness (0-100). ``sat``: a level it sat at (a level they set read back,
    a read, a turn-on that found it visible): above 0 it is the last level measured while visible (``lit_level``) and
    may be the lowest (``lit_low``). ``sat=False``: a read a turn-on found dark, or Yuki's own restore read back: the
    last level only (never a level they chose or sat at). ``used`` (fix round 2, 22 Oct): the read was taken while
    they were using that monitor (the window in front on it and their own keyboard or mouse input just now: the
    driver's ``_in_use``), so even 0 is a level they sat at and ``lit_low`` may become 0 (at 10:50 the VG271U read 0%
    as their primary while they worked on it). A 0 read without it never counts (the 24HC1QR read 0% overnight and at
    08:44 with nothing of theirs in front on it). ``lit_level`` stays the last level above 0."""
    if level is None:
        return False
    now = time.time()
    sets: dict[str, Any] = {"level": float(level), "level_at": now}
    lit = sat and float(level) > 0.0
    if lit:
        sets.update(lit_level=float(level), lit_at=now)
    low = float(level) if sat and (lit or used) else None
    return _note(device_path, pnp, label, sets, store, low=low)


# ---------------------------------------------------------------------------------------------------------------------
# The recorder (the memory service's timeline thread)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class _Seen:
    """One monitor attached now, as the recorder identifies it."""

    ident: str
    label: str
    pnp: str
    connector: str
    width: int
    height: int
    instance: str
    target_id: int
    primary: bool
    role: str
    #: Its place left to right among the monitors attached, ``None`` when it is attached alone (a lone monitor has no
    #: place beside the others: the record keeps the last one measured).
    place: int | None

    def info(self) -> tuple[Any, ...]:
        return (self.label, self.pnp, self.connector, self.width, self.height, self.instance, self.target_id,
                self.place)

    def differs(self, stored: Any) -> bool:
        """Whether a stable fact differs from the stored row's (a lone monitor's unknown place never does)."""
        if stored is None:
            return True
        mine = self.info()
        return (tuple(stored[i] for i in range(7)) != mine[:7]
                or (self.place is not None and stored[7] != self.place))


def ident_of(store: Any, pnp: str, serial: str, label: str = "") -> str:
    """A monitor's key: a keyed digest of its EDID maker and product and its serial (its name stands in for a serial
    it doesn't give)."""
    return store.cipher.digest("monitor", str(pnp or "").upper(), str(serial or "") or f"name:{label or ''}")


class Recorder:
    """The monitor sightings (see the module docstring). Single-threaded: the timeline's thread. ``read``: the monitors
    attached now (default ``windows.list_monitors``); ``devices``: Windows' device list (default
    ``windows.monitor_devices``); ``edid``: one monitor's EDID facts (default ``windows.monitor_edid``). A write that
    fails (no table yet) switches it off for this run, logged once."""

    def __init__(self, store: Any, *, log: LogFn | None = None, read: Callable[[], Any] | None = None,
                 devices: Callable[[], Any] | None = None, edid: Callable[[str, str], Any] | None = None) -> None:
        self.store = store
        self._log_fn = log
        self._read_fn = read
        self._devices_fn = devices
        self._edid_fn = edid
        self._last_read = -1e18
        self._flushed = time.monotonic()
        self._at: float | None = None
        self._attached: dict[str, _Seen] = {}
        #: ``{ident: [attached s, primary s, {role: s}]}`` not yet written.
        self._acc: dict[str, list[Any]] = {}
        self._seeded = False
        self.disabled = False
        self.counters: Counter = Counter()

    # -- the reads ---------------------------------------------------------------------------------------------------

    def _windows(self) -> Any:
        from yuki.perception import windows

        return windows

    def _listed(self) -> list[Any]:
        if self._read_fn is not None:
            return list(self._read_fn() or [])
        return list(self._windows().list_monitors())

    def _edid(self, pnp: str, instance: str) -> tuple[str, str, int, int]:
        if self._edid_fn is not None:
            return tuple(self._edid_fn(pnp, instance))  # type: ignore[return-value]
        return self._windows().monitor_edid(pnp, instance)

    def _identify(self, listed: Sequence[Any]) -> dict[str, _Seen]:
        from yuki.perception.windows import instance_of

        named = [m for m in listed if str(getattr(m, "pnp", "") or "") and str(getattr(m, "device_path", "") or "")]
        order = sorted(named, key=lambda m: (tuple(m.bounds)[0], tuple(m.bounds)[1]))
        out: dict[str, _Seen] = {}
        for m in named:
            pnp, path = str(m.pnp).upper(), str(m.device_path)
            instance = instance_of(path)
            label, serial, width, height = self._edid(pnp, instance)
            label = label or str(getattr(m, "label", "") or "")
            if not width or not height:
                b = tuple(m.bounds)
                width, height = int(b[2] - b[0]), int(b[3] - b[1])
            ident = ident_of(self.store, pnp, serial, label)
            out[ident] = _Seen(ident=ident, label=label, pnp=pnp, connector=str(getattr(m, "connector", "") or ""),
                               width=int(width), height=int(height), instance=instance,
                               target_id=int(getattr(m, "target", 0) or 0), primary=bool(m.is_primary),
                               role=str(getattr(m, "role", "") or ""),
                               place=order.index(m) if len(named) > 1 else None)
        return out

    # -- the tick ----------------------------------------------------------------------------------------------------

    def tick(self, now: float, *, force: bool = False) -> None:
        """Called on the timeline's ticks and desk changes (``force``: the monitors' layout just changed). Never
        raises."""
        if self.disabled:
            return
        try:
            if not self._seeded:
                self._seeded = True
                self._seed(now)
            mono = time.monotonic()
            if not force and mono - self._last_read < READ_EVERY_S:
                return
            self._last_read = mono
            seen = self._identify(self._listed())
            self._account(now)
            before = self._attached
            arrived = [s for k, s in seen.items() if k not in before or before[k].info() != s.info()]
            left = [s for k, s in before.items() if k not in seen]
            self._attached = seen
            if arrived or left:
                self._write_changes(now, arrived, left)
                self._flush(now)
            elif mono - self._flushed >= FLUSH_EVERY_S:
                self._flush(now)
        except Exception as exc:  # noqa: BLE001 - never breaks the timeline
            self.counters["errors"] += 1
            self._log("error", where="monitors.tick", error=type(exc).__name__)

    def close(self, now: float) -> None:
        """The timeline stops: the seconds so far are written."""
        if self.disabled:
            return
        try:
            self._account(now)
            self._flush(now)
        except Exception:  # noqa: BLE001
            pass

    def _account(self, now: float) -> None:
        last, self._at = self._at, now
        if last is None or now <= last or now - last > MAX_GAP_S:
            return
        dt = now - last
        for k, s in self._attached.items():
            acc = self._acc.setdefault(k, [0.0, 0.0, {}])
            acc[0] += dt
            if s.primary:
                acc[1] += dt
            if s.role:
                acc[2][s.role] = acc[2].get(s.role, 0.0) + dt

    # -- the writes --------------------------------------------------------------------------------------------------

    def _write_changes(self, now: float, arrived: Sequence[_Seen], left: Sequence[_Seen]) -> None:
        def op(conn: Any) -> None:
            for s in arrived:
                cur = conn.execute("SELECT label, pnp, connector, width, height, instance, target_id, place, info_at"
                                   " FROM monitors WHERE ident = ?", (s.ident,)).fetchone()
                changed = s.differs(cur)
                conn.execute(
                    "INSERT INTO monitors(ident, label, pnp, connector, width, height, instance, target_id,"
                    " first_seen, last_seen, place, info_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)"
                    " ON CONFLICT(ident) DO UPDATE SET label = excluded.label, pnp = excluded.pnp,"
                    " connector = excluded.connector, width = excluded.width, height = excluded.height,"
                    " instance = excluded.instance, target_id = excluded.target_id,"
                    " first_seen = coalesce(monitors.first_seen, excluded.first_seen), last_seen = excluded.last_seen,"
                    " place = coalesce(excluded.place, monitors.place), info_at = ?, updated_at = excluded.updated_at",
                    (s.ident, s.label, s.pnp, s.connector, s.width, s.height, s.instance, s.target_id, now, now,
                     s.place, now, now, now if changed else (cur["info_at"] if cur is not None else now)))
            for s in left:
                cur = conn.execute("SELECT off_at, power, power_at, left_bus FROM monitors WHERE ident = ?",
                                   (s.ident,)).fetchone()
                if cur is None:
                    continue
                off_at, power, power_at = cur["off_at"], cur["power"], cur["power_at"]
                # it left Windows within LEFT_BUS_S of an off, or of a read that said off: it leaves the bus when off
                # (a measured fact the off question names next time). Fix round F5: an older read says nothing (a
                # wake by the mouse or its own button is never read, so a standby read of hours ago plus a later
                # button-off is no measure of it)
                leaves = (off_at is not None and 0.0 <= now - float(off_at) <= LEFT_BUS_S) or (
                    power is not None and 2 <= int(power) <= 5
                    and power_at is not None and 0.0 <= now - float(power_at) <= LEFT_BUS_S)
                conn.execute("UPDATE monitors SET removed_at = ?, last_seen = ?, left_bus = ?, updated_at = ?"
                             " WHERE ident = ?",
                             (now, now, 1 if leaves else cur["left_bus"], now, s.ident))
            self._judge_stayed(conn, now)

        if self._run(op):
            self._log("monitors", stage="sight", attached=len(self._attached), arrived=len(arrived), left=len(left))

    def _judge_stayed(self, conn: Any, now: float) -> None:
        """An attached monitor Yuki turned off at least :data:`LEFT_BUS_S` ago that is still attached, with no read
        since that says it was woken within that time: it stays attached when off (``left_bus`` 0)."""
        for s in self._attached.values():
            cur = conn.execute("SELECT off_at, power, power_at, removed_at, left_bus FROM monitors WHERE ident = ?",
                               (s.ident,)).fetchone()
            if cur is None or cur["off_at"] is None or cur["left_bus"] == 0:
                continue
            off_at = float(cur["off_at"])
            if now - off_at < LEFT_BUS_S or (cur["removed_at"] is not None and float(cur["removed_at"]) >= off_at):
                continue
            woke = (cur["power"] == 1 and cur["power_at"] is not None
                    and off_at < float(cur["power_at"]) < off_at + LEFT_BUS_S)
            if not woke:
                conn.execute("UPDATE monitors SET left_bus = 0, updated_at = ? WHERE ident = ?", (now, s.ident))

    def _flush(self, now: float) -> None:
        self._flushed = time.monotonic()
        acc, self._acc = self._acc, {}
        attached = dict(self._attached)

        def op(conn: Any) -> None:
            for k, (a, p, roles) in acc.items():
                cur = conn.execute("SELECT roles, last_seen FROM monitors WHERE ident = ?", (k,)).fetchone()
                if cur is None:
                    continue
                merged = _roles(cur["roles"])
                for role, sec in roles.items():
                    merged[role] = round(merged.get(role, 0.0) + float(sec), 1)
                # attached now: seen now; one that left keeps the time its departure wrote
                seen = now if k in attached else cur["last_seen"]
                conn.execute("UPDATE monitors SET attached_s = attached_s + ?, primary_s = primary_s + ?, roles = ?,"
                             " last_seen = ?, updated_at = ? WHERE ident = ?",
                             (round(a, 1), round(p, 1), json.dumps(merged, separators=(",", ":")), seen, now, k))
            self._judge_stayed(conn, now)

        self._run(op)

    def _seed(self, now: float) -> None:
        """Windows' own device list, once per start: for each monitor the record holds, the port it last arrived on
        (its instance key) and, when Windows doesn't list it now, when Windows last removed it. Names and ports only:
        no row is made here (never a sighting)."""
        try:
            devices = list(self._devices_fn() if self._devices_fn is not None
                           else self._windows().monitor_devices())
        except Exception:  # noqa: BLE001
            devices = []
        if not devices:
            return
        by_ident: dict[str, list[Any]] = {}
        for d in devices:
            pnp = str(getattr(d, "pnp", "") or "").upper()
            if not pnp:
                continue
            k = ident_of(self.store, pnp, str(getattr(d, "serial", "") or ""), str(getattr(d, "label", "") or ""))
            by_ident.setdefault(k, []).append(d)
        filled = 0

        def op(conn: Any) -> None:
            nonlocal filled
            for r in conn.execute("SELECT id, ident, instance, removed_at, last_seen FROM monitors").fetchall():
                mine = by_ident.get(str(r["ident"])) or []
                if not mine:
                    continue
                newest = max(mine, key=lambda d: float(getattr(d, "arrived_at", None) or 0.0))
                instance = str(getattr(newest, "instance", "") or "") or str(r["instance"] or "")
                removed = r["removed_at"]
                if getattr(newest, "present", None) is False and getattr(newest, "removed_at", None):
                    removed = max(float(newest.removed_at), float(removed or 0.0))
                if instance != (r["instance"] or "") or removed != r["removed_at"]:
                    conn.execute("UPDATE monitors SET instance = ?, removed_at = ?, updated_at = ? WHERE id = ?",
                                 (instance, removed, now, int(r["id"])))
                    filled += 1

        if self._run(op):
            self._log("monitors", stage="seed", devices=len(devices), filled=filled)

    def _run(self, op: Callable[[Any], Any]) -> bool:
        try:
            self.store._write(op)
            clear_cache()
            self.counters["writes"] += 1
            return True
        except Exception as exc:  # noqa: BLE001 - no table yet (an older database): off for this run
            self.disabled = True
            self._log("error", where="monitors.write", error=type(exc).__name__)
            return False

    def _log(self, event: str, **fields: Any) -> None:
        if self._log_fn is None:
            return
        try:
            self._log_fn(event, **fields)
        except Exception:  # noqa: BLE001
            pass


__all__ = ["FLUSH_EVERY_S", "Known", "LEFT_BUS_S", "READ_EVERY_S", "Recorder", "STAMP_EVERY_S", "away",
           "card_stamp", "clear_cache", "ident_of", "known", "match", "note_level", "note_power", "rows", "use",
           "words"]
