"""Monitors: brightness and screen power over Windows' own DDC/CI, every change read back (morning21 lane B: B1, B4-B6).

The ``display`` capability's driver (``yuki/skills/connections/display.toml``) and the display read the brain's
``display`` probe uses (contract K2: :func:`read`). The user's decision of 21 Oct ("Direct + knows Twinkle"): Yuki
controls brightness and power itself, and knows the user's monitor app as app data, used when they say so.

**How a monitor is reached** (ctypes only, no new dependency): its ``HMONITOR`` (``windows.list_monitors``, which also
names it: the EDID model name, the device path, the Plug and Play id, the role by place) -> its physical monitor
(dxva2 ``GetPhysicalMonitorsFromHMONITOR``) -> MCCS VCP codes over DDC/CI: ``0x10`` brightness, ``0xD6`` power mode
(1 on; 2-5 standby or off; Yuki writes 4, the DPM off that keeps the HDMI VG271U attached to Windows; whether a monitor
stays attached when off is measured per monitor, memory's ``left_bus``). Brightness falls back to the high-level
``Get/SetMonitorBrightness``; a built-in panel (a laptop's) is read and set through WMI (``WmiMonitorBrightness``).

**A monitor Windows no longer lists** (mon1, 22 Oct: the AOpen "missing on the bus" after an off) has no handle, but
memory's monitor record (``yuki.memory.monitors``) still knows it: its name, its port (the Plug and Play instance key
a monitor app lists it by, the GPU target id) and when Windows dropped it. So it is named, never mistaken for the one
attached, and never asked about as "the other one". By measured state:

* attached and on (0xD6 1): nothing is written; the line names its power and brightness ("24HC1QR is on, at 0%
  brightness": on but dark is told, never "already on" alone);
* attached in standby (2-5): 0xD6 = 1 over DDC/CI, read until it reads 1;
* dropped (in the record, not attached, its port not available): the monitor app, only when its own list (read only,
  e.g. Twinkle Tray's pipe ``list``) still holds the monitor's instance key, and only counted woken when Windows lists
  it again within :data:`RETURN_S` and it reads 0xD6 1; else a plain stop line (:data:`LINE_DROPPED`), never a
  question that hands the user's own request back to them ("Could you press its power button?" is gone);
* connected but not in use (its port available, no desktop on it): handed on (the planner's way: Windows' display
  settings), never an ask.

**Every change is read back** from the monitor itself, per monitor (:class:`Change`): its level or power before, the
value asked, what it reads after (a stale reply is read again; a wake is read until 0xD6 reads 1, up to
:data:`WAKE_S`), ok or why not. The done line names each monitor ("VG271U at 80%, 24HC1QR at 80%"); one that didn't
take is named ("24HC1QR at 100%; the VG271U didn't answer"). A route ends on that read-back (``read_back``), and the
re-check (:func:`probe_still`) reads the monitors again 3 s later (some snap back).

**Bounded and quiet**: DDC/CI is a slow bus (a call takes tens of ms) and some monitors misbehave under heavy traffic,
so nothing here polls: a monitor is read only when a run, the brain's probe or the re-check asks, a read is tried at
most :data:`READ_TRIES` times, and one lock keeps two runs off the bus at once. The power state a read finds is kept
for the desk (``windows.note_display_power``: "screen off", for :data:`yuki.perception.windows.POWER_FRESH_S`).

**The user's monitor app** (means ``app``): any app data file ``yuki/connect/sites/<app>.toml`` whose ``[app]
controls = "monitors"`` (Twinkle Tray today) is a monitor app: found installed (one of its ``programs`` exists) and
running (one of its ``images`` runs). Its ``[cli]`` templates are filled per monitor (``select`` by the monitor's own
instance key, ``{instance}``, the key the app's own list holds it by, never a place in the app's list; ``set`` a
level; ``vcp`` the power code), the program is run and waited for, and the monitor is read back over DDC/CI: the app
is the hand, the monitor the judge. mon1: when the app data says how to read its list (``[pipe]``), it is never run for
a monitor that list doesn't hold, and the line names it. Not installed, or not running when its command line needs it
running: said and asked ("start it, or Windows' own control?"), never swapped for Windows' control silently. With no
means chosen, a running monitor app is the next way for a dropped monitor only (Windows' own control can't reach one),
and the line says it was used.

**Turning a screen off asks first** only when it is the only screen on, or shows a game or a call (the user's
decision; any window of a game on it, not only the one in front), or when the target named several monitors by its
parts: the question names the monitors and why, and the answer comes back as the ``anyway`` slot, honoured only as
the answer to that very question (the engine's own record, ``engine._answered_slots``) and only for the very
monitors it named (kept in RAM for :data:`ASKED_OFF_S`), never as a value a caller guessed.

Targets (the ``target`` slot, the model's value): ``all``; first (mon1) the user's own words for a monitor, learned
(memory's monitor record: what the meanings pass learned they call it, their "first", and the heard forms the names
pass learned for its name, "PG"), whole words in the value, a form two monitors share naming neither; then a place
(``main``, ``second``, ... as Windows' place gives it: a fallback); a monitor's own name or Plug and Play id, whole or
its start ("VG"); its connector word ("HDMI"), when only one monitor has it; every monitor any of these names (never
one of them alone); else a misheard initialism one letter off a name's letters ("PG" for the VG271U), only when the
value is that one short word or writes it in capitals ("PG monitor"; never a lower-case "or" inside a phrase) and
exactly one name is that close. A monitor of theirs Windows doesn't list now is named the same ways (by its words,
name, id or a misheard initialism). Never the desk's ``Monitor <n>`` (the hands' id). Spelling and data only, never a
list of words. A value that names no monitor is asked about ("Which monitor: ..., or both?", ``all`` among the options,
the dropped ones named too) with the forms the slot takes beside it (``facts_ram["form"]``), except a power-on with
one monitor attached while exactly one of theirs is dropped: that one is meant. Left out: every monitor ("on" wakes the
ones that are off, the dropped ones included; "off" of every screen is asked first, as the only screen on). An off
named by place alone asks which when the record shows that place held another monitor before (Windows' primary
moves when a monitor drops).

Logs (content-free; ``yuki.log.events``' vocabulary): ``display stage=read|set|power route=ddc|wmi|app|record
verdict=held|stale|none monitors=<n> ok=<n> ms=<ms>`` (the plan's ``op`` and ``readback`` under the log's own enum
keys ``stage`` and ``verdict``; ``record``: only dropped monitors, nothing reached); never a monitor's name.

Public API::

    monitors() -> list[Screen]                          # the displays now, named (no DDC)
    away_monitors(screens) -> list[Known]               # mon1: theirs that Windows doesn't list now (memory's record)
    read(target=None, *, log=None) -> list[dict]        # K2: {name, role, brightness, power, ok, why} per monitor
    set_brightness(screens, *, level=None, change=None, app=None) -> list[Change]
    set_power(screens, state, *, app=None) -> list[Change]
    pick(value, screens, away=None) -> Picked           # a target slot's value -> the monitors it names
    target_form(screens, away=()) -> str                # the forms the target slot takes now
    monitor_apps() -> list[MonitorApp]                  # the monitor apps the app data describes, as found now
    ROUTES = {"route_windows", "route_app"}; PROBES = {"still"}
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import subprocess
import threading
import time
from ctypes import wintypes
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Sequence

#: MCCS VCP codes: brightness (luminance) and the display's power mode.
VCP_BRIGHTNESS = 0x10
VCP_POWER = 0xD6
#: 0xD6 values: 1 on; 2 standby, 3 suspend, 4 off (DPM), 5 off (as its power button). Off is written as 4, the value
#: that keeps a monitor attached to Windows (at 5 some drop off, and then nothing but their button wakes them).
POWER_ON = 1
POWER_OFF = 4
#: A DDC read that failed is tried again, at most this many times in all, this far apart (s).
READ_TRIES = 3
READ_GAP_S = 0.06
#: After a write, its read-back waits this long first (s), and a read-back that still shows another value is read
#: again up to :data:`READ_TRIES` times (a stale reply), then written once more.
SETTLE_S = 0.12
#: A wake is read back until 0xD6 reads 1, every :data:`WAKE_POLL_S`, for at most this long (s).
WAKE_S = 3.0
WAKE_POLL_S = 0.4
#: A level read back within this many points of the one asked is that level; through a monitor app (which may map
#: levels onto its own range) within :data:`APP_TOLERANCE`.
TOLERANCE = 1.0
APP_TOLERANCE = 5.0
#: The re-check's own tolerance (a monitor that drifts a point is no snap back).
RECHECK_TOLERANCE = 2.0
#: A monitor app's command line must exit within this (s), and its change reaches the monitor within this (s).
APP_WAIT_S = 4.0
APP_SETTLE_S = 0.5
#: WMI (a built-in panel) answers within this (s).
WMI_WAIT_S = 2.0
#: mon1: a dropped monitor a monitor app was told to wake counts as back only when Windows lists it again within this
#: (s), read every :data:`RETURN_POLL_S` (and then it must read 0xD6 1).
RETURN_S = 15.0
RETURN_POLL_S = 0.5
#: mon1: a monitor app's own list (its pipe, read only) answers within this (s), at most this many bytes.
LIST_WAIT_S = 2.0
LIST_MAX_BYTES = 1 << 20
#: The capability's app key (``[connection] app``): the app a run acted on (D18).
APP = "display"
#: The slot that carries the user's answer to the turn-off question.
ANYWAY_SLOT = "anyway"

ASK_WHAT = "Brighter, dimmer, a level, or the screen on or off?"
ASK_WHICH = "Which monitor: {names}?"
#: The option of ASK_WHICH that is every monitor (the slot's own form; said "both" or "all of them").
ALL = "all"
ASK_ONLY_SCREEN = "{name} is the only screen on, so you'd see nothing. Turn it off anyway?"
ASK_EVERY_SCREEN = "That turns off every screen, so you'd see nothing. Turn them off anyway?"
ASK_UNSURE_SCREEN = "I can't tell whether another screen is on. Turn {name} off anyway?"
ASK_GAME = "{name} is showing your game. Turn it off anyway?"
ASK_CALL = "{name} is showing your call. Turn it off anyway?"
ASK_UNSURE_SHOWN = "I can't tell what {name} is showing. Turn it off anyway?"
ASK_SEVERAL = "That turns off {name}. Turn them off?"
#: mon1: memory's monitor record says this monitor left Windows the last time it was turned off (``left_bus``).
ASK_LEAVES_BUS = ("{name} dropped off Windows the last time it was turned off, and once it does I can't turn it back "
                  "on. Turn it off anyway?")
#: A turn-off question's monitors are kept this long (s, RAM only): an ``anyway`` yes counts for those monitors only.
ASKED_OFF_S = 120.0
ASK_NO_APP = "I don't see a monitor app on this PC. Use Windows' own control instead?"
ASK_APP_OFF = "{app} isn't running. Start it, or use Windows' own control this time?"
ASK_APP_GONE = "I couldn't run {app}'s command line. Use Windows' own control this time?"
#: mon1: the told app's own list doesn't hold the monitor(s): it can't reach them (Yuki's own control can).
ASK_APP_UNLISTED = "{app} doesn't list {name}, so it can't reach it. Use Windows' own control this time?"
#: mon1: the plain stop line for a monitor Windows dropped that nothing could reach (``{when}``: " at 08:45", " on 21
#: Oct at 08:45" or ""; ``{reach}``: :data:`REACH_NONE` or :data:`REACH_APP`). Said, never asked: no way is left.
LINE_DROPPED = ("Your {name} is switched fully off: Windows dropped it{when}, so {reach}. Only its own power button "
                "brings it back.")
LINE_DROPPED_SEVERAL = ("Your {name} are switched fully off: Windows dropped them, so {reach}. Only their own power "
                        "buttons bring them back.")
LINE_DROPPED_LEVEL = "Your {name} is switched fully off (Windows dropped it{when}), so it has no brightness to set."
REACH_NONE = "I can't reach {it}"
REACH_APP = "neither I nor {app} can reach {it}"
REACH_APP_TRIED = "I can't reach {it}, and {app} couldn't bring {it} back"
#: Why a monitor didn't take a change, as said after its name (the ``why`` code words of :class:`Change`).
WHY_WORDS: dict[str, str] = {
    "no_handle": "isn't connected to Windows", "no_ddc": "doesn't take commands over its cable",
    "no_power": "doesn't take power commands over its cable", "no_off": "has no off command",
    "write_failed": "didn't take the change", "no_readback": "didn't answer",
    "readback_stale": "still reads {after}", "app_failed": "wasn't changed by {app}",
    # mon1
    "dropped": "is switched fully off (Windows dropped it{when}); only its own power button brings it back",
    "app_unlisted": "isn't in {app}'s list, so {app} can't reach it",
    "inactive": "is connected, but Windows isn't showing anything on it",
}

_bus = threading.Lock()
#: fix L5: ``{capability: (the monitors the last turn-off question named, by key; monotonic time asked)}``. RAM only.
_asked_off: dict[str, tuple[frozenset[str], float]] = {}
_asked_lock = threading.Lock()


# ---------------------------------------------------------------------------------------------------------------------
# The monitors
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Screen:
    """One monitor as Windows lists it now: ``key`` its device path (identity), ``label`` its own model name,
    ``role`` its place, ``pnp`` its Plug and Play id, ``connector``, ``internal`` (a built-in panel), ``handle`` its
    ``HMONITOR``, ``index`` the desk's id, ``bounds``."""

    key: str
    label: str
    role: str
    pnp: str
    connector: str
    internal: bool
    handle: int
    index: int
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)

    @property
    def said(self) -> str:
        """How it is said: its own name, else its place ("the second monitor")."""
        if self.label:
            return self.label
        return f"the {self.role} monitor" if self.role else "the monitor"


def monitors() -> list[Screen]:
    """The displays attached now, named (``windows.list_monitors``: no DDC/CI). ``[]`` when none can be read."""
    try:
        from yuki.perception.windows import list_monitors

        listed = list_monitors()
    except Exception:  # noqa: BLE001
        return []
    return [Screen(key=str(m.device_path or ""), label=str(m.label or ""), role=str(m.role or ""),
                   pnp=str(m.pnp or ""), connector=str(m.connector or ""), internal=m.connector == "internal",
                   handle=int(m.handle or 0), index=int(m.index), bounds=tuple(m.bounds)) for m in listed]


@dataclass(frozen=True)
class Picked:
    """What a target names: the monitors (``screens``), or why none (``why``: ``none`` nothing given, ``unknown``
    nothing matched, ``detached`` only monitors of theirs Windows no longer lists, named in ``name``).
    ``several``: the value named more than one monitor by its parts (never ``all``): each is taken, and turning them
    off asks first (fix M1: a role and a name that disagree are never settled by one of them alone). mon1: ``away``,
    the monitors of theirs it named that Windows doesn't list now (memory's monitor record rows); ``place_only``: the
    attached ones were named by Windows' place alone (``main``, ``second``)."""

    screens: tuple[Screen, ...] = ()
    why: str = ""
    name: str = ""
    several: bool = False
    away: tuple[Any, ...] = ()
    place_only: bool = False


def _record_rows() -> list[Any]:
    """Memory's monitor record (every row; ``[]`` without memory). Never raises."""
    try:
        from yuki.memory import monitors as record

        return record.rows()
    except Exception:  # noqa: BLE001
        return []


def _row_of(s: Screen, rows: Sequence[Any]) -> Any:
    """The record's row of an attached monitor (by its port, else its id and name), or ``None``."""
    try:
        from yuki.memory import monitors as record

        return record.match(rows, s.key, s.pnp, s.label)
    except Exception:  # noqa: BLE001
        return None


def away_monitors(screens: Sequence[Screen]) -> list[Any]:
    """mon1: the monitors of theirs Windows doesn't list now (memory's monitor record, ``yuki.memory.monitors.Known``
    rows), and, without memory, the ones this process saw attached that it no longer lists (rows with no port facts).
    Never raises."""
    out: list[Any] = []
    try:
        from yuki.memory import monitors as record

        out = record.away([s.key for s in screens if s.key], [(s.pnp, s.label) for s in screens])
    except Exception:  # noqa: BLE001
        out = []
    try:
        from yuki.memory.monitors import Known
        from yuki.perception.windows import instance_of, seen_monitors

        attached = {s.key.casefold() for s in screens if s.key} | {f"{s.pnp}|{s.label}".casefold() for s in screens}
        have = {r.device_path.casefold() for r in out} | {f"{r.pnp}|{r.label}".casefold() for r in out}
        for n, (path, (label, pnp)) in enumerate(seen_monitors().items()):
            if not (label or pnp) or path.casefold() in attached | have or f"{pnp}|{label}".casefold() in attached:
                continue
            have.add(path.casefold())
            out.append(Known(id=-(n + 1), ident="", label=str(label or ""), pnp=str(pnp or ""),
                             instance=instance_of(path), first_seen=0.0))
    except Exception:  # noqa: BLE001
        pass
    return out


def _squash(text: Any) -> str:
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _tokens(text: Any) -> list[str]:
    return [t for t in ("".join(ch if ch.isalnum() else " " for ch in str(text or "").casefold())).split() if t]


def _letter_runs(label: str) -> list[str]:
    """A name's runs of letters (``VG271U`` -> ``["vg", "u"]``): what speech recognition writes an initialism as."""
    runs, cur = [], ""
    for ch in str(label or "").casefold():
        if ch.isalpha():
            cur += ch
        else:
            if cur:
                runs.append(cur)
            cur = ""
    if cur:
        runs.append(cur)
    return runs


def _one_off(a: str, b: str) -> bool:
    """Same length and exactly one letter different (a misheard letter: "pg" / "vg")."""
    return len(a) == len(b) and sum(1 for x, y in zip(a, b) if x != y) == 1


def _named(text: str, names: Sequence[tuple[str, str]]) -> list[str]:
    """The keys of ``names`` (``(key, label or pnp)``) that ``text`` names by spelling: the whole name or id, else a
    word of it that starts a name (2 characters or more: "VG" of VG271U)."""
    squashed = _squash(text)
    words = _tokens(text)
    whole = [k for k, n in names if _squash(n) and _squash(n) == squashed]
    if whole:
        return list(dict.fromkeys(whole))
    starts = [k for k, n in names if _squash(n) and any(len(w) >= 2 and _squash(n).startswith(w) for w in words)]
    return list(dict.fromkeys(starts))


def _near(value: Any, names: Sequence[tuple[str, str]]) -> list[str]:
    """A misheard initialism: the key of the one name with a letter run one letter off a short all-letter word of
    ``value`` ("PG" for the VG271U), only when exactly one name is that close. Fix L2: the word is the value on its
    own, or written in capitals as an initialism is ("PG monitor"); never a lower-case short word inside a phrase
    ("main or second": "or" against the "QR" of 24HC1QR). Spelling only. ``[]`` otherwise."""
    words = [t for t in "".join(ch if ch.isalnum() else " " for ch in str(value or "")).split() if t]
    short = [w.casefold() for w in words if w.isalpha() and 2 <= len(w) <= 4 and (len(words) == 1 or w.isupper())]
    near = list(dict.fromkeys(k for k, n in names for w in short if any(_one_off(w, run) for run in _letter_runs(n))))
    return near if len(near) == 1 else []


def _has_run(words: Sequence[str], run: Sequence[str]) -> bool:
    """Whether ``run`` (a role's or a connector's own words: "second", "usb c") stands whole and in order in
    ``words``."""
    n = len(run)
    return n > 0 and any(list(words[i:i + n]) == list(run) for i in range(len(words) - n + 1))


def _ident(s: Screen) -> str:
    return s.key or str(s.index)


def _away_ident(row: Any) -> str:
    return f"away:{getattr(row, 'id', 0)}:{getattr(row, 'pnp', '')}:{getattr(row, 'label', '')}"


def _said_keys(text: str, owned: Sequence[tuple[str, Any]]) -> set[str]:
    """mon1: the keys of the monitors whose learned words (``yuki.memory.monitors.words``: what the user calls it, a
    heard form of its name) stand whole and in order in ``text``'s words; a form two monitors share names neither.
    Spelling against learned data only, never a list of words."""
    words = _tokens(text)
    forms: dict[str, set[str]] = {}
    try:
        from yuki.memory.monitors import words as learned
    except Exception:  # noqa: BLE001
        return set()
    for key, row in owned:
        try:
            mine = learned(row) if row is not None else ()
        except Exception:  # noqa: BLE001
            mine = ()
        for form in mine:
            run = _tokens(form)
            if run:
                forms.setdefault(" ".join(run), set()).add(key)
    out: set[str] = set()
    for form, keys in forms.items():
        if len(keys) == 1 and _has_run(words, form.split()):
            out |= keys
    return out


def pick(value: Any, screens: Sequence[Screen], away: Sequence[Any] | None = None) -> Picked:
    """The monitors a ``target`` value names (see the module docstring). Spelling and data only.

    Fix M1: its place, its own name or id, and its connector are read together, and every monitor any of them names
    is taken (``several`` when that is more than one): "VG and main" is both, never the place alone. A connector
    word two monitors share names neither of them. Fix L3: the desk's ``Monitor <n>`` (the hands' id, counted from
    0 where Windows' Settings counts from 1) is no form of it: such a value names nothing and is asked about.

    mon1: the user's own learned words for a monitor come first (their "first" is the AOpen whichever monitor Windows
    calls main); ``away`` (default: :func:`away_monitors`) are the monitors of theirs Windows doesn't list now, named
    by their words, name, id or a misheard initialism."""
    text = " ".join(str(value or "").split()).casefold()
    if not text:
        return Picked(why="none")
    listed = list(screens)
    if text == "all":
        return Picked(tuple(listed))
    gone = list(away) if away is not None else away_monitors(listed)
    rows = _record_rows()
    owned = [(_ident(s), _row_of(s, rows)) for s in listed] + [(_away_ident(r), r) for r in gone]
    said = _said_keys(text, owned)
    if said:
        hit = tuple(s for s in listed if _ident(s) in said)
        off = tuple(r for r in gone if _away_ident(r) in said)
        return Picked(hit, why="" if hit else "detached", name="" if hit else off[0].said, away=off,
                      several=len(hit) + len(off) > 1)
    words = _tokens(text)
    names = [(_ident(s), s.label) for s in listed if s.label] + [(_ident(s), s.pnp) for s in listed if s.pnp]
    by_connector = [s for s in listed if s.connector and _has_run(words, _tokens(s.connector))]
    by_place = {_ident(s) for s in listed if s.role and _has_run(words, _tokens(s.role))}
    by_name = set(_named(text, names)) | ({_ident(s) for s in by_connector} if len(by_connector) == 1 else set())
    keys = by_place | by_name
    if not keys:
        keys = set(_near(value, names))
    hit = tuple(s for s in listed if _ident(s) in keys)
    if hit:
        return Picked(hit, several=len(hit) > 1, place_only=bool(by_place) and not by_name)
    gone_names = ([(_away_ident(r), r.label) for r in gone if r.label]
                  + [(_away_ident(r), r.pnp) for r in gone if r.pnp])
    found = _named(text, gone_names) or _near(value, gone_names)
    off = tuple(r for r in gone if _away_ident(r) in set(found))
    if off:
        return Picked(why="detached", name=off[0].said, away=off, several=len(off) > 1)
    return Picked(why="unknown")


#: mon1: another monitor held a place word at least this long (s, memory's monitor record) for that place to be
#: ambiguous (a reconnect's blip is not a move).
ROLE_MOVED_S = 300.0


def _place_moved(chosen: Sequence[Screen], rows: Sequence[Any]) -> bool:
    """mon1: whether memory's monitor record shows another monitor of theirs held a chosen monitor's place word before
    (at least :data:`ROLE_MOVED_S` under that role): Windows' primary moved (the 22 Oct drop made the VG271U "main"
    and the AOpen "second"), so the place alone may not be the monitor meant. Measured data only."""
    for s in chosen:
        mine = _row_of(s, rows)
        for r in rows:
            if not getattr(r, "theirs", False) or (mine is not None and r.id == mine.id):
                continue
            if mine is None and r.pnp == s.pnp and r.label == s.label:
                continue
            if s.role and float((getattr(r, "roles", None) or {}).get(s.role, 0.0)) >= ROLE_MOVED_S:
                return True
    return False


def target_form(screens: Sequence[Screen], away: Sequence[Any] = ()) -> str:
    """The forms the ``target`` slot takes, from the monitors now (fix M2: handed back to a caller whose value named
    none): each own name (the dropped ones too, mon1), each place, ``all``, or left out. Data only."""
    parts = ([s.label for s in screens if s.label] + [r.label for r in away if getattr(r, "label", "")]
             + [s.role for s in screens if s.role])
    return " | ".join([*dict.fromkeys(parts), "all"]) + "; or left out (every monitor)"


# ---------------------------------------------------------------------------------------------------------------------
# DDC/CI (dxva2) and WMI
# ---------------------------------------------------------------------------------------------------------------------


class _PhysicalMonitor(ctypes.Structure):
    """``PHYSICAL_MONITOR`` (packed to 1 byte in the SDK header)."""

    _pack_ = 1
    _fields_ = [("hPhysicalMonitor", wintypes.HANDLE), ("szPhysicalMonitorDescription", wintypes.WCHAR * 128)]


_dxva2: Any = None


def _api() -> Any:
    """dxva2 with its signatures (private: its argtypes leak nowhere), or ``None`` where it can't be loaded."""
    global _dxva2
    if _dxva2 is None:
        try:
            lib = ctypes.WinDLL("dxva2")
            lib.GetNumberOfPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
            lib.GetNumberOfPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
            lib.GetPhysicalMonitorsFromHMONITOR.argtypes = [ctypes.c_void_p, wintypes.DWORD,
                                                           ctypes.POINTER(_PhysicalMonitor)]
            lib.GetPhysicalMonitorsFromHMONITOR.restype = wintypes.BOOL
            lib.DestroyPhysicalMonitors.argtypes = [wintypes.DWORD, ctypes.POINTER(_PhysicalMonitor)]
            lib.DestroyPhysicalMonitors.restype = wintypes.BOOL
            lib.GetVCPFeatureAndVCPFeatureReply.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, ctypes.c_void_p,
                                                            ctypes.POINTER(wintypes.DWORD),
                                                            ctypes.POINTER(wintypes.DWORD)]
            lib.GetVCPFeatureAndVCPFeatureReply.restype = wintypes.BOOL
            lib.SetVCPFeature.argtypes = [wintypes.HANDLE, ctypes.c_ubyte, wintypes.DWORD]
            lib.SetVCPFeature.restype = wintypes.BOOL
            lib.GetMonitorBrightness.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD),
                                                 ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(wintypes.DWORD)]
            lib.GetMonitorBrightness.restype = wintypes.BOOL
            lib.SetMonitorBrightness.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            lib.SetMonitorBrightness.restype = wintypes.BOOL
            _dxva2 = lib
        except Exception:  # noqa: BLE001
            _dxva2 = False
    return _dxva2 or None


@contextlib.contextmanager
def _physical(hmonitor: int) -> Iterator[Any]:
    """The physical monitor behind an ``HMONITOR`` for the length of the block (its handle, or ``None``), destroyed
    after. The first one: a duplicated display's other monitors are not addressed separately."""
    lib = _api()
    if lib is None or not hmonitor:
        yield None
        return
    count = wintypes.DWORD(0)
    if not lib.GetNumberOfPhysicalMonitorsFromHMONITOR(ctypes.c_void_p(hmonitor), ctypes.byref(count)) \
            or not count.value:
        yield None
        return
    found = (_PhysicalMonitor * count.value)()
    if not lib.GetPhysicalMonitorsFromHMONITOR(ctypes.c_void_p(hmonitor), count.value, found):
        yield None
        return
    try:
        yield found[0].hPhysicalMonitor
    finally:
        try:
            lib.DestroyPhysicalMonitors(count.value, found)
        except Exception:  # noqa: BLE001
            pass


def _vcp_get(handle: Any, code: int, *, tries: int = READ_TRIES) -> tuple[int, int] | None:
    """``(current, maximum)`` of a VCP code, or ``None`` (no answer after ``tries`` reads)."""
    lib = _api()
    if lib is None or not handle:
        return None
    cur, top = wintypes.DWORD(0), wintypes.DWORD(0)
    for attempt in range(max(1, tries)):
        if lib.GetVCPFeatureAndVCPFeatureReply(handle, code, None, ctypes.byref(cur), ctypes.byref(top)):
            return int(cur.value), int(top.value)
        if attempt + 1 < tries:
            time.sleep(READ_GAP_S)
    return None


def _vcp_set(handle: Any, code: int, value: int) -> bool:
    """Write a VCP code (tried twice)."""
    lib = _api()
    if lib is None or not handle:
        return False
    for attempt in range(2):
        if lib.SetVCPFeature(handle, code, int(value)):
            return True
        if attempt == 0:
            time.sleep(READ_GAP_S)
    return False


def _level_get(handle: Any) -> tuple[int, int, str] | None:
    """A monitor's brightness as ``(current, maximum, via)``: VCP 0x10, else the high-level read. ``None``."""
    got = _vcp_get(handle, VCP_BRIGHTNESS)
    if got is not None:
        return got[0], got[1], "vcp"
    lib = _api()
    if lib is None or not handle:
        return None
    low, cur, top = wintypes.DWORD(0), wintypes.DWORD(0), wintypes.DWORD(0)
    if lib.GetMonitorBrightness(handle, ctypes.byref(low), ctypes.byref(cur), ctypes.byref(top)):
        return int(cur.value), int(top.value), "high"
    return None


def _level_set(handle: Any, raw: int, via: str) -> bool:
    if via == "high":
        lib = _api()
        return bool(lib is not None and handle and lib.SetMonitorBrightness(handle, int(raw)))
    return _vcp_set(handle, VCP_BRIGHTNESS, raw)


def _percent(cur: int, top: int) -> float:
    return round(100.0 * cur / top, 1) if top > 0 else float(cur)


def _raw(percent: float, top: int) -> int:
    return int(round(percent * top / 100.0)) if top > 0 else int(round(percent))


def _wmi(fn: Callable[[Any], Any]) -> Any:
    """``fn(the root\\wmi service)`` on a thread of its own (COM), bounded by :data:`WMI_WAIT_S`; ``None`` when it
    failed or didn't answer in time."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            import pythoncom
            import win32com.client

            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
            try:
                service = win32com.client.GetObject(r"winmgmts:{impersonationLevel=impersonate}!\\.\root\wmi")
                box["value"] = fn(service)
            finally:
                pythoncom.CoUninitialize()
        except Exception:  # noqa: BLE001
            box.pop("value", None)

    thread = threading.Thread(target=run, name="yuki-display-wmi", daemon=True)
    thread.start()
    thread.join(WMI_WAIT_S)
    return box.get("value")


def _wmi_level() -> float | None:
    """A built-in panel's brightness (0-100) by WMI, or ``None``."""
    def get(service: Any) -> float | None:
        for row in service.ExecQuery("SELECT CurrentBrightness FROM WmiMonitorBrightness WHERE Active=TRUE"):
            return float(row.CurrentBrightness)
        return None

    got = _wmi(get)
    return float(got) if isinstance(got, (int, float)) else None


def _wmi_set(level: float) -> bool:
    """Set a built-in panel's brightness (0-100) by WMI."""
    def put(service: Any) -> bool:
        for obj in service.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods WHERE Active=TRUE"):
            params = obj.Methods_.Item("WmiSetBrightness").InParameters.SpawnInstance_()
            params.Properties_.Item("Brightness").Value = int(round(level))
            params.Properties_.Item("Timeout").Value = 0
            obj.ExecMethod_("WmiSetBrightness", params)
            return True
        return False

    return _wmi(put) is True


#: mon1: the reads and writes memory's monitor record is told of, written once the bus is free (a database write never
#: holds the DDC/CI lock): ``(device path, pnp, label, what, value, wrote)``.
_notes: list[tuple[str, str, str, str, float, bool]] = []
_notes_lock = threading.Lock()


def _note_power(screen: Screen, value: int | None, *, wrote: bool = False) -> None:
    try:
        from yuki.perception.windows import note_display_power

        note_display_power(screen.key, value)
    except Exception:  # noqa: BLE001
        pass
    if value is not None:
        with _notes_lock:
            _notes.append((screen.key, screen.pnp, screen.label, "power", float(value), wrote))


def _note_level(screen: Screen, level: float | None) -> None:
    if level is not None:
        with _notes_lock:
            _notes.append((screen.key, screen.pnp, screen.label, "level", float(level), False))


def _flush_notes() -> None:
    """Tell memory's monitor record the reads and writes queued (``yuki.memory.monitors.note_power`` /
    ``note_level``; the process's memory store). Never raises."""
    with _notes_lock:
        queued = list(_notes)
        _notes.clear()
    if not queued:
        return
    try:
        from yuki.memory import monitors as record
    except Exception:  # noqa: BLE001
        return
    for path, pnp, label, what, value, wrote in queued:
        try:
            if what == "power":
                record.note_power(path, int(value), wrote=wrote, pnp=pnp, label=label)
            else:
                record.note_level(path, value, pnp=pnp, label=label)
        except Exception:  # noqa: BLE001
            continue


# ---------------------------------------------------------------------------------------------------------------------
# The user's monitor app (app data, yuki/connect/sites)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorApp:
    """A monitor app the app data describes: its data file's ``key``, ``name``, the ``images`` it runs as, its
    ``programs`` (paths, expanded), its ``[cli]`` table, and as found now: ``program`` (the first that exists, ``""``
    not installed) and ``running``. mon1: ``pipe``, its ``[pipe]`` table (how its own monitor list is read: ``path``
    and the ``list`` request), ``{}`` when the data gives none."""

    key: str
    name: str
    images: tuple[str, ...]
    programs: tuple[str, ...]
    cli: Mapping[str, Any]
    program: str = ""
    running: bool = False
    pipe: Mapping[str, Any] = field(default_factory=dict)


def _running_images() -> set[str]:
    """The program file names running now, casefolded."""
    try:
        import psutil

        return {str(p.info.get("name") or "").casefold() for p in psutil.process_iter(["name"])}
    except Exception:  # noqa: BLE001
        return set()


def monitor_apps() -> list[MonitorApp]:
    """The monitor apps the app data describes (``[app] controls = "monitors"`` with a ``[cli]`` table), each as found
    now (installed: one of its programs exists; running). Data only. ``[]`` with none."""
    try:
        from yuki.connect.templates import site, sites
    except Exception:  # noqa: BLE001
        return []
    out: list[MonitorApp] = []
    running = None
    for key in sites():
        data = site(key)
        app = data.get("app") if isinstance(data.get("app"), Mapping) else {}
        cli = data.get("cli") if isinstance(data.get("cli"), Mapping) else {}
        if str(app.get("controls") or "") != "monitors" or not cli:
            continue
        if running is None:
            running = _running_images()
        programs = tuple(os.path.expandvars(str(p)) for p in app.get("programs") or [] if str(p).strip())
        images = tuple(str(i) for i in app.get("images") or [] if str(i).strip())
        program = next((p for p in programs if os.path.lexists(p)), "")
        pipe = data.get("pipe") if isinstance(data.get("pipe"), Mapping) else {}
        out.append(MonitorApp(key=key, name=str(app.get("name") or key), images=images, programs=programs, cli=cli,
                              program=program, running=any(i.casefold() in running for i in images),
                              pipe=dict(pipe)))
    return out


def _instance(target: Any) -> str:
    """The Plug and Play instance key of an attached :class:`Screen` (from its device path) or of a record row (its
    last port)."""
    if isinstance(target, Screen):
        try:
            from yuki.perception.windows import instance_of

            return instance_of(target.key)
        except Exception:  # noqa: BLE001
            return ""
    return str(getattr(target, "instance", "") or "")


def _cli_args(app: MonitorApp, target: Any, **values: Any) -> list[str] | None:
    """The app's command line for one monitor (an attached :class:`Screen` or a record row): its ``select`` filled
    with the monitor's own ids, lower case (``{instance}``: the key the app's own list holds it by, mon1; ``{pnp}``:
    its Plug and Play id), then the ``[cli]`` templates named in ``values``: ``set=level``, ``vcp={code, value}``), or
    ``None`` when a template is missing or can't be filled."""
    from yuki.skills.connection import fill

    select = str(app.cli.get("select") or "")
    pnp, instance = str(getattr(target, "pnp", "") or ""), _instance(target)
    if not select or not (pnp or instance):
        return None
    args = [fill(select, {"pnp": pnp.casefold(), "instance": instance.casefold()}, strict=True)]
    for name, slots in values.items():
        template = str(app.cli.get(name) or "")
        if not template:
            return None
        args.append(fill(template, slots if isinstance(slots, Mapping) else {"level": slots}, strict=True))
    return None if any(a is None for a in args) else [str(a) for a in args]


def _app_list(app: MonitorApp) -> list[str] | None:
    """mon1: the monitor keys the app's own list holds now (its ``[pipe]``: the ``list`` request sent to its named pipe,
    the reply a JSON object keyed by monitor; Twinkle Tray's keys are each monitor's instance key), or ``None`` when
    the data gives no way to read it or it didn't answer within :data:`LIST_WAIT_S`. Read only: the one request the
    data names, nothing else is sent. Never raises."""
    path, request = str(app.pipe.get("path") or ""), str(app.pipe.get("list") or "")
    if not path or not request or not app.running:
        return None
    box: dict[str, Any] = {}

    def run() -> None:
        import json

        try:
            with open(path, "r+b", buffering=0) as pipe:
                pipe.write(request.encode("utf-8"))
                data = b""
                while len(data) < LIST_MAX_BYTES:
                    chunk = pipe.read(65536)
                    if not chunk:
                        break
                    data += chunk
                    try:
                        box["value"] = json.loads(data.decode("utf-8"))
                        break
                    except ValueError:
                        continue   # a reply in several chunks
        except Exception:  # noqa: BLE001 - no pipe (an older app), or it closed
            box.pop("value", None)

    thread = threading.Thread(target=run, name="yuki-display-applist", daemon=True)
    thread.start()
    thread.join(LIST_WAIT_S)
    got = box.get("value")
    return [str(k) for k in got] if isinstance(got, Mapping) else None


def _app_holds(listed: Sequence[str] | None, instance: str) -> bool | None:
    """Whether the app's list holds the monitor of this instance key (``None``: the list couldn't be read)."""
    if listed is None:
        return None
    want = str(instance or "").casefold()
    return bool(want) and any(want in str(k).casefold() for k in listed)


def _act_off(app: MonitorApp) -> bool:
    """Whether the user switched Act off for the monitor app (Settings, App access: the brain's own policy reader, by
    its name or a program it runs as). Unreadable: not off. Never raises."""
    try:
        from yuki.brain.util import _shared_reader

        reader = _shared_reader()
        return any(bool(reader.app_off(n)) for n in (app.name, *(os.path.splitext(i)[0] for i in app.images)) if n)
    except Exception:  # noqa: BLE001
        return False


def _run_app(app: MonitorApp, args: Sequence[str]) -> bool:
    """Run the app's program with ``args`` (never through a shell, no window) and wait for it to exit."""
    try:
        done = subprocess.run([app.program, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=APP_WAIT_S,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:  # noqa: BLE001 - a program that won't start or didn't exit in time
        return False
    time.sleep(APP_SETTLE_S)
    # an app whose exit status means nothing (its data's ``exit_works = false``) is judged by the read-back alone
    return done.returncode == 0 or not bool(app.cli.get("exit_works", False))


# ---------------------------------------------------------------------------------------------------------------------
# Changes, read back
# ---------------------------------------------------------------------------------------------------------------------


@dataclass
class Change:
    """One monitor's change: its ``name`` (as said) and ``role``; ``before`` / ``want`` / ``after`` (brightness 0-100,
    or 0xD6 power values); ``ok`` (True read back, False not so, None not readable); ``why`` (a code word,
    :data:`WHY_WORDS`, or ``already``); ``via`` (``ddc`` / ``wmi`` / ``app``). ``key``/``handle``/``what`` are what
    the re-check reads again (RAM only). mon1: ``level``, the brightness read beside a power that was already on (on
    but dark is told); ``by``, the monitor app that woke a dropped monitor with no means chosen (the line names it);
    ``dropped``, a monitor of theirs Windows doesn't list (memory's record); ``when``, when Windows dropped it (epoch
    s); ``app``, the monitor app that couldn't reach it or was tried; ``tried``, that app was run for it."""

    name: str
    role: str
    what: str = "brightness"
    before: float | None = None
    want: float | None = None
    after: float | None = None
    ok: bool | None = None
    why: str = ""
    via: str = "ddc"
    key: str = ""
    handle: int = 0
    internal: bool = False
    level: float | None = None
    by: str = ""
    when: float | None = None
    app: str = ""
    tried: bool = False
    dropped: bool = False


def _brightness_one(screen: Screen, level: float | None, change: float | None, app: MonitorApp | None) -> Change:
    out = Change(name=screen.said, role=screen.role, key=screen.key, handle=screen.handle, internal=screen.internal,
                 via="app" if app is not None else "ddc")
    with _physical(screen.handle) as handle:
        got = _level_get(handle) if handle else None
        if got is None and screen.internal:
            now = _wmi_level()
            if now is not None:
                got, out.via = (int(round(now)), 100, "wmi"), ("app" if app is not None else "wmi")
        if got is None:
            out.ok, out.why = False, ("no_handle" if not screen.handle else "no_ddc")
            return out
        cur, top, how = got
        before = _percent(cur, top)
        want = max(0.0, min(100.0, float(level) if level is not None else before + float(change or 0.0)))
        out.before, out.want = before, want
        if abs(before - want) < 0.5:
            out.after, out.ok, out.why = before, True, "already"
            return out
        tolerance = APP_TOLERANCE if app is not None else TOLERANCE

        def write() -> bool:
            if app is not None:
                args = _cli_args(app, screen, set=int(round(want)))
                return bool(args) and _run_app(app, args)
            if how == "wmi":
                return _wmi_set(want)
            return _level_set(handle, _raw(want, top), how)

        def read_now() -> float | None:
            if how == "wmi":
                return _wmi_level()
            again = _level_get(handle)
            return _percent(again[0], again[1]) if again is not None else None

        for round_ in range(2 if app is None else 1):
            if not write():
                out.ok, out.why = False, ("app_failed" if app is not None else "write_failed")
                return out
            time.sleep(SETTLE_S)
            for attempt in range(READ_TRIES):
                after = read_now()
                if after is not None:
                    out.after = after
                    if abs(after - want) <= tolerance:
                        out.ok, out.why = True, ""
                        return out
                if attempt + 1 < READ_TRIES:
                    time.sleep(SETTLE_S)
        if out.after is None:
            out.ok, out.why = None, "no_readback"
        elif abs(out.after - before) < 0.5:
            out.ok, out.why = False, ("app_failed" if app is not None else "write_failed")
        else:
            out.ok, out.why = None, "readback_stale"
    return out


def _power_one(screen: Screen, state: str, app: MonitorApp | None) -> Change:
    out = Change(name=screen.said, role=screen.role, what="power", key=screen.key, handle=screen.handle,
                 internal=screen.internal, via="app" if app is not None else "ddc")
    want_on = state == "on"
    with _physical(screen.handle) as handle:
        if not handle:
            out.ok, out.why = False, "no_handle"
            return out
        got = _vcp_get(handle, VCP_POWER)
        if got is not None:
            _note_power(screen, got[0])
            out.before = float(got[0])
            if (got[0] == POWER_ON) == want_on:
                if want_on:
                    # mon1: on already: nothing is written, and its brightness is read beside it (on at 0% looks off)
                    level = _level_get(handle)
                    out.level = _percent(level[0], level[1]) if level is not None else None
                    _note_level(screen, out.level)
                out.after, out.ok, out.why = float(got[0]), True, "already"
                return out
        elif not want_on:
            # an off is never written blind: a monitor that doesn't answer may not be the one meant (a wake is: one in
            # standby may answer only once it wakes)
            out.ok, out.why = False, "no_power"
            return out
        top = got[1] if got is not None else POWER_OFF
        value = POWER_ON if want_on else (POWER_OFF if top >= POWER_OFF else top if top >= 2 else 0)
        if not value:
            out.ok, out.why = False, "no_off"
            return out
        out.want = float(value)
        if app is not None:
            args = _cli_args(app, screen, vcp={"code": f"0x{VCP_POWER:02X}", "value": value})
            wrote = bool(args) and _run_app(app, args)
        else:
            wrote = _vcp_set(handle, VCP_POWER, value)
        if not wrote:
            out.ok, out.why = False, ("app_failed" if app is not None else "write_failed")
            return out
        if not want_on:
            _note_power(screen, value, wrote=True)   # mon1: the off Yuki wrote (the record judges left_bus from it)
        # a wake is read until it reads on, up to WAKE_S; an off is read READ_TRIES times
        gap = WAKE_POLL_S if want_on else SETTLE_S
        reads = max(READ_TRIES, int(WAKE_S / WAKE_POLL_S)) if want_on else READ_TRIES
        time.sleep(SETTLE_S)
        for attempt in range(reads):
            again = _vcp_get(handle, VCP_POWER, tries=1)
            if again is not None:
                out.after = float(again[0])
                _note_power(screen, again[0])
                if (again[0] == POWER_ON) == want_on:
                    out.ok, out.why = True, ""
                    return out
            if attempt + 1 < reads:
                time.sleep(gap)
        if out.after is None:
            # an off that no longer answers is most likely off (many monitors stop talking in standby): not confirmed
            out.ok, out.why = None, "no_readback"
            if not want_on:
                _note_power(screen, POWER_OFF)
        else:
            out.ok, out.why = False, ("app_failed" if app is not None else "write_failed")
    return out


def set_brightness(screens: Sequence[Screen], *, level: float | None = None, change: float | None = None,
                   app: MonitorApp | None = None) -> list[Change]:
    """Set each monitor's brightness to ``level`` (0-100) or by ``change`` points from its own level, through DDC/CI
    (WMI for a built-in panel) or the monitor ``app``'s command line, and read each back. One at a time, on the bus
    alone."""
    with _bus:
        got = [_brightness_one(s, level, change, app) for s in screens]
    for s, c in zip(screens, got):
        if c.after is not None:
            _note_level(s, c.after)
    _flush_notes()
    return got


def set_power(screens: Sequence[Screen], state: str, *, app: MonitorApp | None = None) -> list[Change]:
    """Turn each monitor's screen ``on`` or ``off`` (VCP 0xD6 through DDC/CI or the monitor ``app``), read back (a wake
    until it reads on, up to :data:`WAKE_S`). No question here: :func:`_off_question` is the route's."""
    with _bus:
        got = [_power_one(s, state, app) for s in screens]
    _flush_notes()
    return got


def _power_now(screen: Screen) -> int | None:
    """One monitor's 0xD6 now (kept for the desk), or ``None``."""
    with _bus, _physical(screen.handle) as handle:
        got = _vcp_get(handle, VCP_POWER) if handle else None
    if got is not None:
        _note_power(screen, got[0])
        _flush_notes()
    return got[0] if got is not None else None


def read(target: str | None = None, *, log: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Contract K2 (the brain's ``display`` probe): each monitor ``target`` names (:func:`pick`; ``None`` or ``""``:
    every monitor) read now: ``{"name", "role", "brightness" (0-100 or None), "power" ("on" / "off" / None), "ok"
    (anything read), "why" ("" / "no_handle" / "no_ddc")}``. ``[]`` when the target names no attached monitor. mon1: a
    monitor of theirs Windows doesn't list now that the target names is read as Windows' own fact: ``power`` "off",
    ``ok`` True, ``why`` "not_connected" (never "on"). Never raises; ``log(event, **fields)`` gets one content-free
    ``display`` line."""
    started = time.perf_counter()
    out: list[dict[str, Any]] = []
    try:
        screens = monitors()
        picked = pick(target, screens) if str(target or "").strip() else Picked(tuple(screens))
        chosen = list(picked.screens)
        for r in picked.away:
            out.append({"name": r.said, "role": "", "brightness": None, "power": "off", "ok": True,
                        "why": "not_connected"})
        with _bus:
            for s in chosen:
                with _physical(s.handle) as handle:
                    level = _level_get(handle) if handle else None
                    power = _vcp_get(handle, VCP_POWER) if handle else None
                brightness = _percent(level[0], level[1]) if level is not None else None
                if brightness is None and s.internal:
                    brightness = _wmi_level()
                if power is not None:
                    _note_power(s, power[0])
                said = None if power is None else ("on" if power[0] == POWER_ON else "off")
                ok = brightness is not None or said is not None
                if brightness is not None and not s.internal:
                    _note_level(s, brightness)
                out.append({"name": s.said, "role": s.role, "brightness": brightness, "power": said, "ok": ok,
                            "why": "" if ok else ("no_handle" if not s.handle else "no_ddc")})
    except Exception:  # noqa: BLE001 - what was read so far
        pass
    _flush_notes()
    _log(log, stage="read", route="ddc", verdict="held" if out and all(r["ok"] for r in out) else "none",
         monitors=len(out), ok=sum(1 for r in out if r["ok"]), ms=round((time.perf_counter() - started) * 1000.0, 1))
    return out


def _log(log: Callable[..., Any] | None, **fields: Any) -> None:
    if log is None:
        return
    try:
        log("display", **fields)
    except Exception:  # noqa: BLE001 - a log never breaks a read
        pass


# ---------------------------------------------------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------------------------------------------------


def _rr(**kw: Any) -> Any:
    from yuki.skills.engine import RouteResult

    kw.setdefault("app", APP)
    return RouteResult(**kw)


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        x = float(value)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def _which(names: Sequence[Any]) -> str:
    """ASK_WHICH's choices: each monitor by name (a :class:`Screen` or its said name), then every monitor ("both" of
    two, "all of them")."""
    said = [n.said if isinstance(n, Screen) else str(n) for n in names]
    if len(said) > 1:
        said.append("both" if len(said) == 2 else "all of them")
    return said[0] if len(said) == 1 else ", ".join(said[:-1]) + " or " + said[-1]


def _cap(line: str) -> str:
    return line[:1].upper() + line[1:] if line else line


def _monitor_of(hwnd: int) -> int:
    try:
        import win32api
        import win32con

        return int(win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST) or 0)
    except Exception:  # noqa: BLE001
        return 0


def _asked_keys(cap: str) -> frozenset[str] | None:
    """The monitors this capability's last turn-off question named (by key), within :data:`ASKED_OFF_S`; else
    ``None``."""
    with _asked_lock:
        hit = _asked_off.get(cap)
    if hit is None or time.monotonic() - hit[1] > ASKED_OFF_S:
        return None
    return hit[0]


def _off_question(call: Any, chosen: Sequence[Screen], screens: Sequence[Screen], *,
                  several: bool = False) -> str | None:
    """The question before turning ``chosen`` off (the user's decision: only when no other screen would stay on, or
    one of them shows a game or a call; and when the target named several monitors by its parts), or ``None`` when it
    may go ahead. The ``anyway`` slot is their yes only when it answers this capability's own question of moments ago
    (the engine's record) about these very monitors (fix L5: the question's monitors, kept in RAM), never a value a
    caller guessed and never a yes about another monitor."""
    from yuki.skills import engine

    cap = str(getattr(call.skill, "capability", "") or getattr(call.skill, "key", "") or "")
    keys = frozenset(_ident(s) for s in chosen)
    if (call.values.get(ANYWAY_SLOT) is True and ANYWAY_SLOT in engine._answered_slots(cap, call.ctx)
            and _asked_keys(cap) == keys):
        return None
    question = _off_reason(call, chosen, screens, several=several)
    if question is not None:
        with _asked_lock:
            _asked_off[cap] = (keys, time.monotonic())
    return question


def _off_reason(call: Any, chosen: Sequence[Screen], screens: Sequence[Screen], *, several: bool) -> str | None:
    """:func:`_off_question`'s checks, in order: the only screen on; (mon1) a monitor memory's record saw leave Windows
    the last time it was turned off (``left_bus``: then only its button brings it back); a game on a chosen monitor
    (the one in front, and fix M3: any window of a game shown there, by ``gamegate.window_game``, a measured fact); a
    call shown there; several monitors named. A check that can't read asks (fails closed), never with a claim the read
    doesn't hold."""
    from yuki.skills import engine

    said = [s.said for s in chosen]
    names = said[0] if len(said) == 1 else ", ".join(said[:-1]) + " and " + said[-1]
    handles = {s.handle for s in chosen if s.handle}
    others = [s for s in screens if s.handle not in handles]
    powers = [_power_now(s) for s in others]
    if not any(p == POWER_ON for p in powers):
        if not others:
            return ASK_ONLY_SCREEN.format(name=_cap(names)) if len(chosen) == 1 else ASK_EVERY_SCREEN
        # the others read off, or can't be read: never a claim the read doesn't hold
        return ASK_ONLY_SCREEN.format(name=_cap(names)) if all(p is not None for p in powers) \
            else ASK_UNSURE_SCREEN.format(name=names)
    rows = _record_rows()
    leaves = [s for s in chosen if getattr(_row_of(s, rows), "left_bus", None) == 1]
    if leaves:
        return ASK_LEAVES_BUS.format(name=_cap(leaves[0].said))
    unsure = ASK_UNSURE_SHOWN.format(name=names)
    try:
        from yuki.perception.windows import list_windows

        # the windows shown on a chosen monitor (one whose monitor can't be told counts as there: fails closed)
        shown = [w for w in list_windows() if not w.is_minimized and _monitor_of(w.hwnd) in (handles | {0})]
    except Exception:  # noqa: BLE001 - can't tell what shows there: asked
        return unsure
    game = call.ctx.game_now()
    if game == "unknown":
        return unsure
    if game:
        try:
            import win32gui

            front = int(win32gui.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            front = 0
        if not front or _monitor_of(front) in handles:
            return ASK_GAME.format(name=_cap(names))
    try:
        from yuki.memory.gamegate import window_game

        if any(window_game(int(w.hwnd)) for w in shown):
            return ASK_GAME.format(name=_cap(names))
    except Exception:  # noqa: BLE001 - a game check that raised: asked
        return unsure
    try:
        from yuki.skills.chat import _current_calls

        calls = _current_calls(call.ctx.memory)
    except Exception:  # noqa: BLE001
        calls = None
    if calls is None:
        if engine._in_call(call.ctx.hands, call.ctx.memory):
            return ASK_CALL.format(name=_cap(names))   # a call is live and where it shows can't be told
    elif calls:
        try:
            from yuki.memory.store import app_key

            if any(app_key(w.process_name) in set(calls) for w in shown):
                return ASK_CALL.format(name=_cap(names))
        except Exception:  # noqa: BLE001 - can't tell where the call shows: asked
            return unsure
    if several and len(chosen) > 1:
        return ASK_SEVERAL.format(name=names)
    return None


def _when_words(at: float | None, *, now: float | None = None) -> str:
    """When Windows dropped a monitor, as said after "dropped it": " at 08:45" today, " on 21 Oct at 08:45" before,
    "" unknown."""
    if not at:
        return ""
    try:
        from datetime import datetime

        then = datetime.fromtimestamp(float(at))
        today = datetime.fromtimestamp(time.time() if now is None else float(now)).date()
        return f" at {then:%H:%M}" if then.date() == today else f" on {then.day} {then:%b} at {then:%H:%M}"
    except (TypeError, ValueError, OSError, OverflowError):
        return ""


def _line(changes: Sequence[Change], app: MonitorApp | None) -> tuple[str, str]:
    """``(done line, line when not all took)``: each monitor named with its read-back."""
    def took(c: Change) -> str:
        already = " already" if c.why == "already" else ""
        by = f" (through {c.by})" if c.by else ""
        if c.what == "power":
            on = c.after == POWER_ON or (c.after is None and c.want == POWER_ON)
            if on and c.why == "already" and c.level is not None:
                # mon1: on, with what it shows: "24HC1QR is on, at 0% brightness" (on but dark is told)
                return f"{c.name} is on, at {c.level:g}% brightness"
            if not on and c.why == "already" and c.dropped:
                return f"{c.name} is already off (Windows doesn't list it now)"
            return f"{c.name} is{already} {'on' if on else 'off'}{by}"
        level = c.after if c.after is not None else c.want
        return f"{c.name} {'is already ' if already else ''}at {level:g}%"

    def missed(c: Change) -> str:
        after = f"{c.after:g}%" if c.what == "brightness" and c.after is not None else "the same"
        named = c.app or (app.name if app is not None else "the app")
        words = WHY_WORDS.get(c.why, "didn't answer").format(after=after, app=named, when=_when_words(c.when))
        if c.what == "power" and c.want == POWER_OFF and c.why == "no_readback":
            words = "stopped answering, so it's most likely off"   # many monitors go quiet in standby
        if c.why == "dropped" and c.tried and c.app:
            words = WHY_WORDS["dropped"].format(when=_when_words(c.when)).replace(
                ";", f", and {c.app} couldn't bring it back;", 1)
        return f"{c.name if c.name.startswith('the ') else 'the ' + c.name} {words}"

    good = [c for c in changes if c.ok is True]
    bad = [c for c in changes if c.ok is not True]
    done = _cap(", ".join(took(c) for c in good)) + "." if good else ""
    if not bad:
        return done, ""
    head = ", ".join(took(c) for c in good)
    tail = "; ".join(missed(c) for c in bad)
    return done, _cap(f"{head}; {tail}." if head else f"{tail}.")


#: A change's ``why`` when its monitor wasn't reached at all (nothing written to it). mon1: a monitor Windows dropped
#: (``dropped``), one its monitor app's list doesn't hold (``app_unlisted``), one connected but not in use
#: (``inactive``).
UNREACHED = ("no_handle", "no_ddc", "no_power", "no_off", "dropped", "app_unlisted", "inactive")


def _finish(call: Any, changes: list[Change], *, op: str, via: str, app: MonitorApp | None, started: float) -> Any:
    """The route's result from the per-monitor changes: done (each read back), already, partly done, or nothing
    reached (the next rung, or the hand-off). Logs one content-free ``display`` line."""
    from yuki.skills.engine import _emit

    acted = [c for c in changes if c.why not in (*UNREACHED, "already")]
    held = all(c.ok is True for c in changes) and bool(changes)
    stale = any(c.why in ("readback_stale", "no_readback") for c in changes)
    verdict = "held" if held else "stale" if stale else "none"
    _emit(call.ctx, "display", stage=op, route=via, verdict=verdict, monitors=len(changes),
          ok=sum(1 for c in changes if c.ok is True), ms=round((time.perf_counter() - started) * 1000.0, 1))
    done, partly = _line(changes, app)
    facts = {"op": op, "via": via, "status": verdict, "monitors": len(changes),
             "ok": sum(1 for c in changes if c.ok is True), "acted": len(acted)}
    ram = {"display": [c for c in changes if c.ok is True and c.why != "already"]}
    first = next((c.after for c in changes if c.what == "brightness" and c.after is not None), None)
    if held and not acted:
        return _rr(ran=False, already=True, verified=True, outcome="already", say=done, facts=facts, facts_ram=ram,
                   value=first)
    if not acted and any(c.why == "already" for c in changes) and not any(c.why == "no_handle" for c in changes):
        # fix L4: one monitor already so and the rest not reachable over their cable: nothing was written, and both
        # are said ("24HC1QR is already at 80%; the VG271U doesn't take commands over its cable"). As a no-op that
        # can't be confirmed whole, the run stops here (``ran=None`` with ``already``: the engine's settled end),
        # never handed on to steps. mon1: a monitor Windows dropped is said the same way, by name ("...; the 24HC1QR
        # is switched fully off (Windows dropped it at 08:45); only its own power button brings it back")
        return _rr(ran=None, already=True, verified=None, outcome="partly_already", unverified_line=partly,
                   facts=facts, value=first)
    if not acted:
        return _rr(ran=False, outcome=next((c.why for c in changes), "") or "not_reached", facts=facts)
    if held:
        return _rr(ran=True, verified=True, outcome="set", say=done, facts=facts, facts_ram=ram, value=first)
    if not any(c.ok is True for c in changes) and all(c.ok is False for c in acted):
        # every monitor it wrote to still reads what it did before: contradicted (the rung's own data stops there)
        return _rr(ran=True, verified=False, outcome="not_taken", unverified_line=partly, facts=facts, value=first)
    return _rr(ran=True, verified=None, outcome="partly", unverified_line=partly, facts=facts, facts_ram=ram,
               value=first)


def _ask_which(screens: Sequence[Screen], gone: Sequence[Any], *, outcome: str = "ask_which") -> Any:
    """ASK_WHICH with every monitor of theirs among the options (the dropped ones named too, mon1). Fix M2: every
    monitor is one of the answers ("both" -> the slot's ``all``), and the forms the slot takes ride beside the question
    in RAM (``facts_ram["form"]``) for a caller that calls once more with one of them (engine merge note 12: a value
    that names no monitor is the caller's slip the first time, skill22 A19)."""
    said = list(dict.fromkeys([s.said for s in screens] + [r.said for r in gone]))
    options = said + ([ALL] if len(said) > 1 else [])
    return _rr(ran=False, ask=ASK_WHICH.format(names=_which(said)), options=options, outcome=outcome,
               facts={"slot": "target"}, facts_ram={"form": {"target": target_form(screens, gone)}})


def _apps_for_dropped(call: Any) -> list[MonitorApp]:
    """mon1: with no means chosen, the monitor apps that may be the next way for a dropped monitor (Windows' own
    control can't reach one): installed and running, the Act switch on, nothing holding launches, and no game in front
    (a program started never runs with a game in front: the app route's own rule). ``[]`` otherwise."""
    try:
        live = [a for a in monitor_apps() if a.program and a.running]
        if not live:
            return []
        game = call.ctx.game_now()
        if game:
            return []
        from yuki.actions import gate

        if gate.refuse("launch") is not None:
            return []
        return [a for a in live if not _act_off(a)]
    except Exception:  # noqa: BLE001 - no next way known: the plain line
        return []


def _await_return(row: Any, deadline: float | None) -> Screen | None:
    """mon1: the attached monitor a dropped one came back as (its port's device path, else its id and name), polled
    every :data:`RETURN_POLL_S` for at most :data:`RETURN_S` (and never past the route's deadline); ``None``."""
    limit = time.monotonic() + RETURN_S
    if deadline:
        limit = min(limit, float(deadline) - 1.0)
    path = str(getattr(row, "device_path", "") or "").casefold()
    while True:
        for s in monitors():
            if (path and s.key.casefold() == path) or (s.pnp and s.pnp == row.pnp and s.label == row.label):
                return s
        if time.monotonic() + RETURN_POLL_S > limit:
            return None
        time.sleep(RETURN_POLL_S)


def _read_on(screen: Screen) -> int | None:
    """A monitor that just came back: its 0xD6, read until it reads on (up to :data:`WAKE_S`), or ``None``."""
    got: int | None = None
    with _bus, _physical(screen.handle) as handle:
        if not handle:
            return None
        for attempt in range(max(1, int(WAKE_S / WAKE_POLL_S))):
            again = _vcp_get(handle, VCP_POWER, tries=1)
            if again is not None:
                got = int(again[0])
                if got == POWER_ON:
                    break
            time.sleep(WAKE_POLL_S)
    if got is not None:
        _note_power(screen, got)
        _flush_notes()
    return got


def _wake_dropped(call: Any, row: Any, app: MonitorApp | None, via: str) -> Change:
    """mon1: one monitor of theirs Windows dropped, to be turned on (the ladder of the module docstring): connected but
    not in use -> ``inactive`` (handed on); else the monitor app (the told one, or with no means a running one), only
    when its own list holds the monitor's instance key, then woken only when Windows lists it again and it reads on;
    else ``dropped`` (the plain line)."""
    out = Change(name=row.said, role="", what="power", want=float(POWER_ON), key=row.device_path, dropped=True,
                 when=getattr(row, "removed_at", None), via="app" if app is not None else "ddc")
    port = None
    try:
        from yuki.perception.windows import display_ports, gone_at

        seen_leave = gone_at(row.device_path)
        if seen_leave and (out.when is None or seen_leave > out.when):
            out.when = seen_leave
        port = display_ports().get(int(row.target_id)) if getattr(row, "target_id", 0) else None
    except Exception:  # noqa: BLE001
        port = None
    if port is not None and port.available and not port.active:
        out.ok, out.why = False, "inactive"
        return out
    out.ok, out.why = False, "dropped"
    # the told app; with no means chosen, a running monitor app (the next way); never one when they chose Windows'
    told_windows = str((getattr(call, "values", None) or {}).get("means") or "").casefold() == "windows"
    candidates = [app] if app is not None else ([] if via == "app" or told_windows else _apps_for_dropped(call))
    if not candidates:
        out.app = next((a.name for a in monitor_apps() if a.program), "")
        return out
    for a in candidates:
        out.app = a.name
        holds = _app_holds(_app_list(a), row.instance)
        if not holds:
            # never run for a monitor its own list doesn't hold (or a list that can't be read): it can't reach it
            out.why = "app_unlisted" if holds is False else "dropped"
            continue
        args = _cli_args(a, row, vcp={"code": f"0x{VCP_POWER:02X}", "value": POWER_ON})
        if not args or not _run_app(a, args):
            out.why = "dropped"
            continue
        out.tried = True
        back = _await_return(row, getattr(call, "deadline", None))
        if back is None:
            out.why = "dropped"
            continue
        out.key, out.handle, out.internal = back.key, back.handle, back.internal
        woke = _read_on(back)
        out.after = float(woke) if woke is not None else None
        if woke == POWER_ON:
            out.ok, out.why, out.by = True, "", (a.name if app is None else "")
            return out
        out.ok, out.why = (None, "no_readback") if woke is None else (False, "app_failed")
        return out
    return out


def _dropped_line(changes: Sequence[Change], *, level: bool) -> str:
    """mon1: the plain stop line when every monitor named is one Windows dropped and nothing reached it
    (:data:`LINE_DROPPED`, :data:`LINE_DROPPED_SEVERAL`, :data:`LINE_DROPPED_LEVEL`)."""
    names = [c.name for c in changes]
    one = len(names) == 1
    said = names[0] if one else ", ".join(names[:-1]) + " and " + names[-1]
    if level:
        return LINE_DROPPED_LEVEL.format(name=said, when=_when_words(changes[0].when) if one else "")
    it = "it" if one else "them"
    app = next((c.app for c in changes if c.app), "")
    tried = any(c.tried for c in changes)
    reach = (REACH_APP_TRIED if tried else REACH_APP).format(app=app, it=it) if app else REACH_NONE.format(it=it)
    if one:
        return LINE_DROPPED.format(name=said, when=_when_words(changes[0].when), reach=reach)
    return LINE_DROPPED_SEVERAL.format(name=said, reach=reach)


def _run(call: Any, via: str) -> Any:
    started = time.perf_counter()
    values = call.values or {}
    level, change = _number(values.get("level")), _number(values.get("change"))
    power = str(values.get("power") or "").casefold()
    if power not in ("on", "off"):
        power = ""
    if level is None and change is None and not power:
        return _rr(ran=False, ask=ASK_WHAT, outcome="no_amount")
    screens = monitors()
    gone = away_monitors(screens)
    if not screens and not gone:
        return _rr(ran=False, outcome="no_monitors")
    picked = pick(values.get("target"), screens, gone)
    if picked.why == "unknown":
        if power == "on" and len(screens) <= 1 and len(gone) == 1:
            # mon1 (22 Oct, "turn on my first monitor" with only the VG271U listed): one monitor attached and
            # exactly one of theirs dropped: a turn-on that names neither means that one, never "press its button?"
            picked = Picked(why="detached", name=gone[0].said, away=(gone[0],))
        else:
            return _ask_which(screens, gone)
    if power == "off" and picked.place_only and _place_moved(picked.screens, _record_rows()):
        # mon1: an off named by Windows' place alone, where another of their monitors held that place before (the
        # primary moved): which one is asked, never the place's monitor of the moment
        return _ask_which(screens, gone, outcome="ask_place")
    # left out: every monitor (an off of every screen is asked first, below); a turn-on includes the dropped ones
    chosen = list(picked.screens) if picked.why != "none" else list(screens)
    dropped = list(picked.away) if picked.why != "none" else (list(gone) if power == "on" else [])
    app: MonitorApp | None = None
    if via == "app" and not chosen:
        # only dropped monitors: the told app is tried when it runs (its list decides); nothing to ask about it
        app = next((a for a in monitor_apps() if a.program and a.running), None)
    elif via == "app":
        apps = monitor_apps()
        found = [a for a in apps if a.program]
        if not found:
            return _rr(ran=False, ask=ASK_NO_APP, options=["windows"], outcome="no_app", facts={"slot": "means"})
        live = [a for a in found if a.running] or found
        app = live[0]
        if app.cli.get("needs_running", True) and not app.running:
            return _rr(ran=False, ask=ASK_APP_OFF.format(app=app.name), options=["windows"], outcome="app_off",
                       facts={"slot": "means"})
        from yuki.actions import gate

        why = gate.refuse("launch")
        if why is not None:
            return _rr(ran=False, held=gate.kind_of(why), outcome="refused")
        if _act_off(app):
            # the Act switch (Settings, App access) is off for the user's monitor app: never run it
            from yuki.skills.chat import LINE_ACT_OFF

            return _rr(ran=False, ask=LINE_ACT_OFF.format(app=app.name), outcome="act_off")
    if power == "off" and chosen:
        question = _off_question(call, chosen, screens, several=picked.several)
        if question is not None:
            return _rr(ran=False, ask=question, outcome="ask_off", facts={"slot": ANYWAY_SLOT})
    # mon1: the told app is never run for a monitor its own list doesn't hold (read only; a list that can't be read
    # leaves it to the read-back): that monitor is named in the line, or asked about when it is the only one
    unlisted: set[str] = set()
    if app is not None and chosen:
        listed = _app_list(app)
        unlisted = {s.key for s in chosen if _app_holds(listed, _instance(s)) is False}
    reach = [s for s in chosen if s.key not in unlisted]
    if app is not None and chosen and not reach and not dropped:
        names = [s.said for s in chosen]
        return _rr(ran=False, ask=ASK_APP_UNLISTED.format(
            app=app.name, name=names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]),
            options=["windows"], outcome="app_unlisted", facts={"slot": "means"})
    changes: list[Change] = []
    if power and reach:
        changes += set_power(reach, power, app=app)
    if (level is not None or change is not None) and reach and power != "off":
        lit = [s for s, c in zip(reach, changes) if c.ok is not False] if power == "on" else reach
        changes += set_brightness(lit, level=level, change=None if level is not None else change, app=app)
    changes += [Change(name=s.said, role=s.role, what="power" if power else "brightness", key=s.key, handle=s.handle,
                       via="app", ok=False, why="app_unlisted", app=app.name if app is not None else "")
                for s in chosen if s.key in unlisted]
    for r in dropped:
        if power == "on":
            changes.append(_wake_dropped(call, r, app, via))
        elif power == "off":
            changes.append(Change(name=r.said, role="", what="power", want=float(POWER_OFF), after=float(POWER_OFF),
                                  ok=True, why="already", dropped=True, when=r.removed_at))
        else:
            changes.append(Change(name=r.said, role="", what="brightness", ok=False, why="dropped", dropped=True,
                                  when=r.removed_at))
    if not changes:
        return _rr(ran=False, outcome="no_monitors")
    op = "power" if power else "set"
    attached = [c for c in changes if not c.dropped]
    if app is not None and attached and all(c.why == "app_failed" and c.after is None and c.before is None
                                            for c in attached):
        return _rr(ran=False, ask=ASK_APP_GONE.format(app=app.name), options=["windows"], outcome="app_failed",
                   facts={"slot": "means"})
    if all(c.dropped and c.why in ("dropped", "app_unlisted") for c in changes):
        # mon1: every monitor named is one Windows dropped and nothing reached it: said plainly (which monitor, since
        # when, that neither Yuki nor their monitor app can reach it), never a question handing their request back
        from yuki.skills.engine import _emit

        _emit(call.ctx, "display", stage=op, route="record", verdict="none", monitors=len(changes), ok=0,
              ms=round((time.perf_counter() - started) * 1000.0, 1))
        return _rr(ran=False, stop=True, outcome="dropped", say=_dropped_line(changes, level=not power),
                   facts={"op": op, "via": "record", "status": "none", "monitors": len(changes), "ok": 0,
                          "acted": 0, "dropped": len(changes)})
    result = _finish(call, changes, op=op, via="app" if app is not None else _via_word(changes), app=app,
                     started=started)
    if power == "off" and result.ran is False and not result.already:
        # integ29 (S6): an off that reached no monitor (no 0xD6, DDC/CI off, a built-in panel) ends here, said plainly:
        # never handed on to steps, whose off (SC_MONITORPOWER) would turn every screen off, the one the off question
        # judged would stay on too
        _done, line = _line(changes, app)
        return _rr(ran=False, stop=True, outcome=result.outcome or "not_reached", say=line or None,
                   facts=result.facts)
    return result


def _via_word(changes: Sequence[Change]) -> str:
    vias = {c.via for c in changes}
    return "wmi" if vias == {"wmi"} else "ddc"


def route_windows(call: Any) -> Any:
    """Rung A, Windows' own control: DDC/CI (WMI for a built-in panel), every monitor read back. No focus, no input."""
    return _run(call, "windows")


def route_app(call: Any) -> Any:
    """Rung A, the user's monitor app (means ``app``): its command line per monitor, read back over DDC/CI."""
    return _run(call, "app")


def probe_still(ctx: Any, args: Mapping[str, Any]) -> tuple[bool | None, dict[str, Any]]:
    """The re-check (D19's second probe, the same source): the monitors the run changed, read again: each still at
    what it read back -> True; one moved away (snapped back) -> False; none readable -> None."""
    result = getattr(ctx, "result", None)
    ram = getattr(result, "facts_ram", None) or {}
    changed = [c for c in ram.get("display") or [] if isinstance(c, Change)]
    if not changed:
        return None, {"status": "no_value"}
    seen = held = 0
    with _bus:
        for c in changed:
            with _physical(c.handle) as handle:
                if c.what == "power":
                    got = _vcp_get(handle, VCP_POWER) if handle else None
                    if got is None:
                        continue
                    seen += 1
                    held += int((got[0] == POWER_ON) == (c.want == POWER_ON))
                else:
                    if c.via == "wmi":
                        now = _wmi_level()
                    else:
                        level = _level_get(handle) if handle else None
                        now = _percent(level[0], level[1]) if level is not None else None
                    if now is None or c.after is None:
                        continue
                    seen += 1
                    held += int(abs(now - c.after) <= RECHECK_TOLERANCE)
    if not seen:
        return None, {"status": "unread"}
    return held == seen, {"status": "read", "monitors": seen, "ok": held}


ROUTES: dict[str, Callable[[Any], Any]] = {"route_windows": route_windows, "route_app": route_app}
#: The re-check (``probe = "driver"``, ``call = "still"``).
PROBES: dict[str, Callable[[Any, Mapping[str, Any]], tuple[bool | None, dict[str, Any]]]] = {"still": probe_still}

__all__ = ["APP", "ANYWAY_SLOT", "Change", "MonitorApp", "PROBES", "Picked", "POWER_OFF", "POWER_ON", "ROUTES",
           "Screen", "VCP_BRIGHTNESS", "VCP_POWER", "away_monitors", "monitor_apps", "monitors", "pick", "probe_still",
           "read", "route_app", "route_windows", "set_brightness", "set_power", "target_form"]
