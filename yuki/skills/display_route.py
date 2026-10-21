"""Monitors: brightness and screen power over Windows' own DDC/CI, every change read back (morning21 lane B: B1, B4-B6).

The ``display`` capability's driver (``yuki/skills/connections/display.toml``) and the display read the brain's
``display`` probe uses (contract K2: :func:`read`). The user's decision of 21 Oct ("Direct + knows Twinkle"): Yuki
controls brightness and power itself, and knows the user's monitor app as app data, used when they say so.

**How a monitor is reached** (ctypes only, no new dependency): its ``HMONITOR`` (``windows.list_monitors``, which also
names it: the EDID model name, the device path, the Plug and Play id, the role by place) -> its physical monitor
(dxva2 ``GetPhysicalMonitorsFromHMONITOR``) -> MCCS VCP codes over DDC/CI: ``0x10`` brightness, ``0xD6`` power mode
(1 on; 2-5 standby or off; Yuki writes 4, the DPM off that keeps the monitor attached to Windows). Brightness falls
back to the high-level ``Get/SetMonitorBrightness``; a built-in panel (a laptop's) is read and set through WMI
(``WmiMonitorBrightness``). A monitor Windows no longer lists (switched fully off at its button) has no handle and no
path: that is said plainly, with its power button as the way.

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
Plug and Play id, never a place in the app's list; ``set`` a level; ``vcp`` the power code), the program is run and
waited for, and the monitor is read back over DDC/CI: the app is the hand, the monitor the judge. Not installed, or not
running when its command line needs it running: said and asked ("start it, or Windows' own control?"), never swapped
for Windows' control silently.

**Turning a screen off asks first** only when it is the only screen on, or shows a game or a call (the user's
decision): the question names the monitor and why, and the answer comes back as the ``anyway`` slot, honoured only as
the answer to that very question (the engine's own record, ``engine._answered_slots``), never as a value a caller
guessed.

Targets (the ``target`` slot, the model's value): ``all``; a place (``main``, ``second``, ... as the desk says it);
the desk's own ``Monitor <n>``; a monitor's own name or Plug and Play id, whole or its start ("VG"); a misheard
initialism one letter off a name's letters ("PG" for the VG271U), only when exactly one name is that close; its
connector word ("HDMI"). Spelling and data only, never a list of words. Left out: every monitor ("on" wakes the ones
that are off; "off" of every screen is asked first, as the only screen on).

Logs (content-free; ``yuki.log.events``' vocabulary): ``display stage=read|set|power route=ddc|wmi|app
verdict=held|stale|none monitors=<n> ok=<n> ms=<ms>`` (the plan's ``op`` and ``readback`` under the log's own enum
keys ``stage`` and ``verdict``); never a monitor's name.

Public API::

    monitors() -> list[Screen]                          # the displays now, named (no DDC)
    read(target=None, *, log=None) -> list[dict]        # K2: {name, role, brightness, power, ok, why} per monitor
    set_brightness(screens, *, level=None, change=None, app=None) -> list[Change]
    set_power(screens, state, *, app=None) -> list[Change]
    pick(value, screens) -> Picked                      # a target slot's value -> the monitors it names
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
#: The capability's app key (``[connection] app``): the app a run acted on (D18).
APP = "display"
#: The slot that carries the user's answer to the turn-off question.
ANYWAY_SLOT = "anyway"

ASK_WHAT = "Brighter, dimmer, a level, or the screen on or off?"
ASK_WHICH = "Which monitor: {names}?"
ASK_ONLY_SCREEN = "{name} is the only screen on, so you'd see nothing. Turn it off anyway?"
ASK_EVERY_SCREEN = "That turns off every screen, so you'd see nothing. Turn them off anyway?"
ASK_UNSURE_SCREEN = "I can't tell whether another screen is on. Turn {name} off anyway?"
ASK_GAME = "{name} is showing your game. Turn it off anyway?"
ASK_CALL = "{name} is showing your call. Turn it off anyway?"
ASK_DETACHED = ("{name} isn't connected to Windows right now (switched fully off), so I can't reach it. Could you "
                "press its power button?")
ASK_ONLY_ONE = ("I can see only one monitor connected to Windows, {name}; the other may be switched fully off. Could "
                "you press its power button?")
ASK_NO_APP = "I don't see a monitor app on this PC. Use Windows' own control instead?"
ASK_APP_OFF = "{app} isn't running. Start it, or use Windows' own control this time?"
ASK_APP_GONE = "I couldn't run {app}'s command line. Use Windows' own control this time?"
#: Why a monitor didn't take a change, as said after its name (the ``why`` code words of :class:`Change`).
WHY_WORDS: dict[str, str] = {
    "no_handle": "isn't connected to Windows", "no_ddc": "doesn't take commands over its cable",
    "no_power": "doesn't take power commands over its cable", "no_off": "has no off command",
    "write_failed": "didn't take the change", "no_readback": "didn't answer",
    "readback_stale": "still reads {after}", "app_failed": "wasn't changed by {app}",
}

_bus = threading.Lock()


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
    nothing matched, ``detached`` a monitor this process saw that Windows no longer lists, named in ``name``)."""

    screens: tuple[Screen, ...] = ()
    why: str = ""
    name: str = ""


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
    """The keys of ``names`` (``(key, label or pnp)``) that ``text`` names by spelling: the whole name or id; a word
    of it that starts the name (2 characters or more: "VG" of VG271U); else a short all-letter word one letter off one
    of a name's letter runs (a misheard initialism), only when exactly one name is that close."""
    squashed = _squash(text)
    words = _tokens(text)
    whole = [k for k, n in names if _squash(n) and _squash(n) == squashed]
    if whole:
        return list(dict.fromkeys(whole))
    starts = [k for k, n in names if _squash(n) and any(len(w) >= 2 and _squash(n).startswith(w) for w in words)]
    if starts:
        return list(dict.fromkeys(starts))
    near = [k for k, n in names for w in words if w.isalpha() and 2 <= len(w) <= 4
            and any(_one_off(w, run) for run in _letter_runs(n))]
    near = list(dict.fromkeys(near))
    return near if len(near) == 1 else []


def pick(value: Any, screens: Sequence[Screen]) -> Picked:
    """The monitors a ``target`` value names (see the module docstring). Spelling and data only."""
    text = " ".join(str(value or "").split()).casefold()
    if not text:
        return Picked(why="none")
    listed = list(screens)
    if text == "all":
        return Picked(tuple(listed))
    words = _tokens(text)
    if len(words) == 2 and words[0] == "monitor" and words[1].isdigit():
        hit = [s for s in listed if s.index == int(words[1])]
        if hit:
            return Picked(tuple(hit))
    by_role = [s for s in listed if s.role and s.role.casefold() in words]
    if len(by_role) == 1:
        return Picked(tuple(by_role))
    names = [(s.key or str(s.index), s.label) for s in listed if s.label] + \
            [(s.key or str(s.index), s.pnp) for s in listed if s.pnp]
    keys = _named(text, names)
    hit = [s for s in listed if (s.key or str(s.index)) in keys]
    if hit:
        return Picked(tuple(hit))
    by_connector = [s for s in listed if s.connector and s.connector.casefold() in words]
    if len(by_connector) == 1:
        return Picked(tuple(by_connector))
    try:
        from yuki.perception.windows import seen_monitors

        attached = {s.key for s in listed}
        gone = [(k, label or pnp) for k, (label, pnp) in seen_monitors().items() if k not in attached and (label or pnp)]
    except Exception:  # noqa: BLE001
        gone = []
    away = _named(text, gone)
    if len(away) == 1:
        return Picked(why="detached", name=next(n for k, n in gone if k == away[0]))
    return Picked(why="unknown")


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


def _note_power(screen: Screen, value: int | None) -> None:
    try:
        from yuki.perception.windows import note_display_power

        note_display_power(screen.key, value)
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------------------------------------------------
# The user's monitor app (app data, yuki/connect/sites)
# ---------------------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class MonitorApp:
    """A monitor app the app data describes: its data file's ``key``, ``name``, the ``images`` it runs as, its
    ``programs`` (paths, expanded), its ``[cli]`` table, and as found now: ``program`` (the first that exists, ``""``
    not installed) and ``running``."""

    key: str
    name: str
    images: tuple[str, ...]
    programs: tuple[str, ...]
    cli: Mapping[str, Any]
    program: str = ""
    running: bool = False


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
        out.append(MonitorApp(key=key, name=str(app.get("name") or key), images=images, programs=programs, cli=cli,
                              program=program, running=any(i.casefold() in running for i in images)))
    return out


def _cli_args(app: MonitorApp, screen: Screen, **values: Any) -> list[str] | None:
    """The app's command line for one monitor (its ``select`` filled with the monitor's Plug and Play id, lower case,
    then the ``[cli]`` templates named in ``values``: ``set=level``, ``vcp={code, value}``), or ``None`` when a
    template is missing or can't be filled."""
    from yuki.skills.connection import fill

    select = str(app.cli.get("select") or "")
    if not select or not screen.pnp:
        return None
    args = [fill(select, {"pnp": screen.pnp.casefold()}, strict=True)]
    for name, slots in values.items():
        template = str(app.cli.get(name) or "")
        if not template:
            return None
        args.append(fill(template, slots if isinstance(slots, Mapping) else {"level": slots}, strict=True))
    return None if any(a is None for a in args) else [str(a) for a in args]


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
    the re-check reads again (RAM only)."""

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
        return [_brightness_one(s, level, change, app) for s in screens]


def set_power(screens: Sequence[Screen], state: str, *, app: MonitorApp | None = None) -> list[Change]:
    """Turn each monitor's screen ``on`` or ``off`` (VCP 0xD6 through DDC/CI or the monitor ``app``), read back (a wake
    until it reads on, up to :data:`WAKE_S`). No question here: :func:`_off_question` is the route's."""
    with _bus:
        return [_power_one(s, state, app) for s in screens]


def _power_now(screen: Screen) -> int | None:
    """One monitor's 0xD6 now (kept for the desk), or ``None``."""
    with _bus, _physical(screen.handle) as handle:
        got = _vcp_get(handle, VCP_POWER) if handle else None
    if got is not None:
        _note_power(screen, got[0])
    return got[0] if got is not None else None


def read(target: str | None = None, *, log: Callable[..., Any] | None = None) -> list[dict[str, Any]]:
    """Contract K2 (the brain's ``display`` probe): each monitor ``target`` names (:func:`pick`; ``None`` or ``""``:
    every monitor) read now: ``{"name", "role", "brightness" (0-100 or None), "power" ("on" / "off" / None), "ok"
    (anything read), "why" ("" / "no_handle" / "no_ddc")}``. ``[]`` when the target names no attached monitor. Never
    raises; ``log(event, **fields)`` gets one content-free ``display`` line."""
    started = time.perf_counter()
    out: list[dict[str, Any]] = []
    try:
        screens = monitors()
        chosen = list(screens) if not str(target or "").strip() else list(pick(target, screens).screens)
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
                out.append({"name": s.said, "role": s.role, "brightness": brightness, "power": said, "ok": ok,
                            "why": "" if ok else ("no_handle" if not s.handle else "no_ddc")})
    except Exception:  # noqa: BLE001 - what was read so far
        pass
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


def _names(screens: Sequence[Screen]) -> str:
    said = [s.said for s in screens]
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


def _off_question(call: Any, chosen: Sequence[Screen], screens: Sequence[Screen]) -> str | None:
    """The question before turning ``chosen`` off (the user's decision: only when no other screen would stay on, or
    one of them shows a game or a call), or ``None`` when it may go ahead. The ``anyway`` slot is their yes only when
    it answers this capability's own question of moments ago (the engine's record), never a value a caller guessed."""
    from yuki.skills import engine

    cap = str(getattr(call.skill, "capability", "") or getattr(call.skill, "key", "") or "")
    if call.values.get(ANYWAY_SLOT) is True and ANYWAY_SLOT in engine._answered_slots(cap, call.ctx):
        return None
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
    game = call.ctx.game_now()
    if game:
        try:
            import win32gui

            front = int(win32gui.GetForegroundWindow() or 0)
        except Exception:  # noqa: BLE001
            front = 0
        if game == "unknown" or not front or _monitor_of(front) in handles:
            return ASK_GAME.format(name=_cap(names))
    try:
        from yuki.skills.chat import _current_calls

        calls = _current_calls(call.ctx.memory)
    except Exception:  # noqa: BLE001
        calls = None
    if calls is None:
        if engine._in_call(call.ctx.hands, call.ctx.memory):
            return ASK_CALL.format(name=_cap(names))   # a call is live and where it shows can't be told
        return None
    if calls:
        try:
            from yuki.memory.store import app_key
            from yuki.perception.windows import list_windows

            for w in list_windows():
                if app_key(w.process_name) in set(calls) and not w.is_minimized and _monitor_of(w.hwnd) in handles:
                    return ASK_CALL.format(name=_cap(names))
        except Exception:  # noqa: BLE001 - can't tell where the call shows: asked
            return ASK_CALL.format(name=_cap(names))
    return None


def _line(changes: Sequence[Change], app: MonitorApp | None) -> tuple[str, str]:
    """``(done line, line when not all took)``: each monitor named with its read-back."""
    def took(c: Change) -> str:
        if c.what == "power":
            return f"{c.name} is {'on' if c.after == POWER_ON or (c.after is None and c.want == POWER_ON) else 'off'}"
        level = c.after if c.after is not None else c.want
        return f"{c.name} at {level:g}%"

    def missed(c: Change) -> str:
        after = f"{c.after:g}%" if c.what == "brightness" and c.after is not None else "the same"
        words = WHY_WORDS.get(c.why, "didn't answer").format(after=after,
                                                             app=app.name if app is not None else "the app")
        if c.what == "power" and c.want == POWER_OFF and c.why == "no_readback":
            words = "stopped answering, so it's most likely off"   # many monitors go quiet in standby
        return f"{c.name if c.name.startswith('the ') else 'the ' + c.name} {words}"

    good = [c for c in changes if c.ok is True]
    bad = [c for c in changes if c.ok is not True]
    done = _cap(", ".join(took(c) for c in good)) + "." if good else ""
    if not bad:
        return done, ""
    head = ", ".join(took(c) for c in good)
    tail = "; ".join(missed(c) for c in bad)
    return done, _cap(f"{head}; {tail}." if head else f"{tail}.")


def _finish(call: Any, changes: list[Change], *, op: str, via: str, app: MonitorApp | None, started: float) -> Any:
    """The route's result from the per-monitor changes: done (each read back), already, partly done, or nothing
    reached (the next rung, or the hand-off). Logs one content-free ``display`` line."""
    from yuki.skills.engine import _emit

    acted = [c for c in changes if c.why not in ("no_handle", "no_ddc", "no_power", "no_off", "already")]
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
        line = _cap(done.replace(" at ", " is already at ").replace(" is on", " is already on")
                    .replace(" is off", " is already off"))
        return _rr(ran=False, already=True, verified=True, outcome="already", say=line, facts=facts, facts_ram=ram,
                   value=first)
    if not acted:
        return _rr(ran=False, outcome=next((c.why for c in changes), "") or "not_reached", facts=facts)
    if held:
        return _rr(ran=True, verified=True, outcome="set", say=done, facts=facts, facts_ram=ram, value=first)
    if not any(c.ok is True for c in changes) and all(c.ok is False for c in acted):
        # every monitor it wrote to still reads what it did before: contradicted (the rung's own data stops there)
        return _rr(ran=True, verified=False, outcome="not_taken", unverified_line=partly, facts=facts, value=first)
    return _rr(ran=True, verified=None, outcome="partly", unverified_line=partly, facts=facts, facts_ram=ram,
               value=first)


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
    if not screens:
        return _rr(ran=False, outcome="no_monitors")
    picked = pick(values.get("target"), screens)
    if picked.why == "detached":
        return _rr(ran=False, ask=ASK_DETACHED.format(name=picked.name), outcome="detached",
                   facts={"slot": "target"})
    if picked.why == "unknown":
        if power == "on" and len(screens) == 1:
            return _rr(ran=False, ask=ASK_ONLY_ONE.format(name=screens[0].said), outcome="only_one",
                       facts={"slot": "target"})
        return _rr(ran=False, ask=ASK_WHICH.format(names=_names(screens)), options=[s.said for s in screens],
                   outcome="ask_which", facts={"slot": "target"})
    # left out: every monitor (an off of every screen is asked first, below)
    chosen = list(picked.screens) if picked.why != "none" else list(screens)
    app: MonitorApp | None = None
    if via == "app":
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
    if power == "off":
        question = _off_question(call, chosen, screens)
        if question is not None:
            return _rr(ran=False, ask=question, outcome="ask_off", facts={"slot": ANYWAY_SLOT})
    changes: list[Change] = []
    if power:
        changes += set_power(chosen, power, app=app)
    if level is not None or change is not None:
        lit = [s for s, c in zip(chosen, changes) if c.ok is not False] if power == "on" else chosen
        if power != "off":
            changes += set_brightness(lit, level=level, change=None if level is not None else change, app=app)
    op = "power" if power else "set"
    if app is not None and changes and all(c.why == "app_failed" and c.after is None and c.before is None
                                           for c in changes):
        return _rr(ran=False, ask=ASK_APP_GONE.format(app=app.name), options=["windows"], outcome="app_failed",
                   facts={"slot": "means"})
    result = _finish(call, changes, op=op, via="app" if app is not None else _via_word(changes), app=app,
                     started=started)
    if not result.ran and not result.already and any(c.why == "no_handle" for c in changes):
        gone = next(c for c in changes if c.why == "no_handle")
        return _rr(ran=False, ask=ASK_DETACHED.format(name=_cap(gone.name)), outcome="detached",
                   facts={"slot": "target"})
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
           "Screen", "VCP_BRIGHTNESS", "VCP_POWER", "monitor_apps", "monitors", "pick", "probe_still", "read",
           "route_app", "route_windows", "set_brightness", "set_power"]
