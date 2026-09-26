"""One app's own volume: the Windows volume mixer's per-app level (docs/VOICE.md §5.6, v3.5).

Every app that plays sound has an audio session on the output device; its volume
(``ISimpleAudioVolume``) is what the Volume Mixer's slider for that app sets. It
is not the system volume (the device's master level, what the media volume keys
move) and not a player's own control (YouTube's slider inside the page).

Live 2026-09-26: "reduce the YouTube volume" lowered the system volume three times
through the volume keys, and then the thinker clicked YouTube's mute button. This
changes only the named app's sessions, through Core Audio in this process: no
window is focused or switched, nothing is clicked or typed and no script window
appears, so a game in front stays in front. Every change is read back (the
report says the level before and after, and that the system volume was untouched).
"""

from __future__ import annotations

import ctypes
import time
from ctypes import POINTER, byref, c_float, c_void_p
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from yuki.actions import ActionResult


def _com() -> tuple[Any, Any]:
    """The COM pieces (imported on first use: importing this module loads no COM)."""
    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, IUnknown

    global _ISimpleAudioVolume
    if _ISimpleAudioVolume is None:
        class ISimpleAudioVolume(IUnknown):
            _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
            _methods_ = [
                COMMETHOD([], HRESULT, "SetMasterVolume", (["in"], c_float, "level"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "level")),
                COMMETHOD([], HRESULT, "SetMute", (["in"], wintypes.BOOL, "mute"), (["in"], POINTER(GUID), "ctx")),
                COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(wintypes.BOOL), "mute")),
            ]

        _ISimpleAudioVolume = ISimpleAudioVolume
    return comtypes, _ISimpleAudioVolume


_ISimpleAudioVolume: Any = None
_NAMES: dict[int, str] = {}


def _process_name(pid: int) -> str:
    if pid in _NAMES:
        return _NAMES[pid]
    try:
        import psutil

        name = psutil.Process(pid).name()
    except Exception:  # noqa: BLE001 - a process that ended, or one this user cannot open
        name = f"pid {pid}"
    if len(_NAMES) > 256:
        _NAMES.clear()
    _NAMES[pid] = name
    return name


@dataclass
class AppSession:
    """One app's audio session on one output device (``volume`` is its live COM interface)."""

    app: str
    pid: int
    level: float
    muted: bool
    state: str
    device: str
    volume: Any = None


def sessions(pids: set[int] | None = None) -> list[AppSession]:
    """Every app audio session on the active output devices (system sounds and expired ones left out)."""
    from yuki.perception import audio as core

    comtypes, simple = _com()
    enum = core._enumerator()
    devices = enum.EnumAudioEndpoints(core.E_RENDER, core._DEVICE_STATE_ACTIVE)
    out: list[AppSession] = []
    for i in range(devices.GetCount()):
        device = devices.Item(i)
        try:
            name = core._describe(device, capture=False)["name"]
        except Exception:  # noqa: BLE001
            name = ""
        iid = core.IAudioSessionManager2._iid_
        raw = device.Activate(byref(iid), comtypes.CLSCTX_ALL, None)
        manager = ctypes.cast(c_void_p(raw), POINTER(core.IAudioSessionManager2))
        found = manager.GetSessionEnumerator()
        for j in range(found.GetCount()):
            control = found.GetSession(j)
            state = core._SESSION_STATES.get(control.GetState(), "?")
            if state == "expired":
                continue
            try:
                pid = int(control.QueryInterface(core.IAudioSessionControl2).GetProcessId())
            except comtypes.COMError:
                continue
            if pid == 0 or (pids is not None and pid not in pids):
                continue  # pid 0: Windows' system sounds, not an app
            try:
                volume = control.QueryInterface(simple)
                level, muted = float(volume.GetMasterVolume()), bool(volume.GetMute())
            except comtypes.COMError:
                continue
            out.append(AppSession(app=_process_name(pid), pid=pid, level=round(level * 100.0, 1), muted=muted,
                                  state=state, device=name, volume=volume))
    return out


def audio_apps(limit: int = 6) -> list[dict[str, Any]]:
    """The apps with sound on the output now, one entry per app: its mixer level, muted, playing.

    Playing ones first. For the desktop summary (the mouth's hands) and reports. Never raises.
    """
    try:
        found = sessions()
    except Exception:  # noqa: BLE001 - a fact for a summary, never a failure
        return []
    apps: dict[str, dict[str, Any]] = {}
    for s in found:
        entry = apps.setdefault(s.app.casefold(), {"app": s.app, "level": s.level, "muted": s.muted,
                                                   "playing": False, "sessions": 0})
        entry["sessions"] += 1
        entry["playing"] = entry["playing"] or s.state == "active"
        if s.state == "active":
            entry["level"], entry["muted"] = s.level, s.muted
    ordered = sorted(apps.values(), key=lambda a: (not a["playing"], a["app"].casefold()))
    return ordered[:limit]


def audio_line(apps: list[dict[str, Any]]) -> str:
    """The apps with sound as one line of facts ("" when none)."""
    if not apps:
        return ""
    parts = [f"{a['app']} {a['level']:g}%{' muted' if a['muted'] else ''}{' playing' if a['playing'] else ''}"
             for a in apps]
    return "app volumes (Windows volume mixer, each app's own level): " + "; ".join(parts)


def _key(name: str) -> str:
    name = (name or "").casefold().strip()
    if name.endswith(".exe"):
        name = name[:-4]
    return "".join(ch for ch in name if ch.isalnum())


def matching(found: list[AppSession], app: str) -> list[AppSession]:
    """The sessions of the app named ``app`` (its process name, with or without ``.exe``; a name that
    contains it or is contained in it, e.g. "Google Chrome" for chrome.exe). Plumbing: which process
    was named, as launch_app matches a window."""
    want = _key(app)
    if not want:
        return []
    exact = [s for s in found if _key(s.app) == want]
    if exact:
        return exact
    return [s for s in found if len(_key(s.app)) >= 3 and (_key(s.app) in want or want in _key(s.app))]


def set_app_volume(app: str, *, level: float | None = None, change: float | None = None,
                   mute: bool | None = None, _sessions: Any = None, _system: Any = None) -> ActionResult:
    """Set one app's own volume (and/or mute) in the Windows volume mixer, verified by reading it back.

    Args:
        app: The app's process name as the mixer lists it (``chrome.exe``, ``Spotify.exe``), or its name.
        level: The new level, 0-100.
        change: Or a change in points, e.g. ``-20`` (clamped to 0-100).
        mute: Mute (True) or unmute (False) the app; ``None`` leaves it.

    Nothing is focused, clicked, typed or run: the game or window in front stays there. ``ok`` is the
    effect read back from every session of the app; the system volume is reported, never changed.
    """
    started = time.perf_counter()
    details: dict[str, Any] = {"app": app, "verified": False}

    def done(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=round((time.perf_counter() - started) * 1000.0, 1))

    if level is None and change is None and mute is None:
        return done(False, "give a level (0-100), a change in points, or mute")
    try:
        found = _sessions() if _sessions is not None else sessions()
    except Exception as exc:  # noqa: BLE001
        return done(False, f"could not read the volume mixer: {type(exc).__name__}: {exc}")
    targets = matching(found, app)
    if not targets:
        names = sorted({s.app for s in found})
        return done(False, f"no app called {app!r} has sound on the output now; the mixer has: "
                           f"{', '.join(names) or 'no apps'}. Nothing was changed")
    before = max(s.level for s in targets)
    was_muted = all(s.muted for s in targets)
    new = before
    if level is not None:
        new = float(level)
    elif change is not None:
        new = before + float(change)
    new = max(0.0, min(100.0, round(new, 1)))
    errors = []
    for s in targets:
        try:
            if level is not None or change is not None:
                s.volume.SetMasterVolume(new / 100.0, None)
            if mute is not None:
                s.volume.SetMute(bool(mute), None)
        except Exception as exc:  # noqa: BLE001 - reported per session
            errors.append(f"{type(exc).__name__}: {exc}")
    after_levels, after_muted = [], []
    for s in targets:
        try:
            after_levels.append(round(float(s.volume.GetMasterVolume()) * 100.0, 1))
            after_muted.append(bool(s.volume.GetMute()))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"read back: {type(exc).__name__}: {exc}")
    level_ok = level is None and change is None or (bool(after_levels) and all(abs(x - new) <= 0.6
                                                                                for x in after_levels))
    mute_ok = mute is None or (bool(after_muted) and all(m == bool(mute) for m in after_muted))
    verified = bool(level_ok and mute_ok and not errors)
    after = max(after_levels) if after_levels else None
    details.update(verified=verified, before=before, after=after, sessions=len(targets),
                   process=targets[0].app, muted_before=was_muted, muted_after=all(after_muted) if after_muted else None)
    try:
        if _system is not None:
            system = _system()
        else:
            from yuki.perception.audio import master_volume

            system = master_volume()
    except Exception:  # noqa: BLE001
        system = None
    details["system_volume"] = system.get("level") if system else None
    what = f"{targets[0].app}'s own volume (Windows volume mixer, {len(targets)} session{'s' * (len(targets) > 1)})"
    moved = f": {before:g}% -> {after:g}%" if after is not None and (level is not None or change is not None) else ""
    muted = ("" if mute is None else ("; muted" if all(after_muted) else "; unmuted") if after_muted else "")
    system_note = (f" The system volume was not touched (still {system['level']:g}%)." if system else "")
    if verified:
        return done(True, f"{what}{moved}{muted}, read back{'' if moved else ' as set'}.{system_note} Nothing was "
                          f"focused or clicked")
    return done(False, f"{what}: asked for {new:g}%{'' if mute is None else (' muted' if mute else ' unmuted')} but "
                       f"read back {after_levels or '?'}{(' (' + '; '.join(errors)[:200] + ')') if errors else ''}."
                       f"{system_note}")


#: The thinker's tool for this action (``yuki.agent.tools``: add it to the tool list and ACTION_TOOL_NAMES, give the
#: backend an ``app_volume`` method calling :func:`set_app_volume`, and dispatch it with :func:`run_tool`).
TOOL: dict[str, Any] = {
    "name": "app_volume",
    "label": "Changing an app's volume",
    "description": (
        "One app's own volume in the Windows volume mixer (what the Volume Mixer's slider for that app sets), "
        "e.g. only Chrome, so only the YouTube video playing in it, or only Spotify; the system volume and "
        "every other app are left as they are. Works through Windows' audio sessions: no window is focused, "
        "clicked or switched and no script runs, so a game in front stays in front. Prefer it to a player's "
        "own volume control and to the volume keys (those change the whole PC) whenever the user means one "
        "app's sound. Without level, change or mute it lists the apps with sound and their levels. The "
        "result is read back: the level before and after, and the system volume untouched."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "app": {"type": "string", "description": "The app's process name as the mixer lists it (chrome.exe), "
                                                     "or its name (Chrome, Spotify)."},
            "change": {"type": "number", "description": "Points to move it by, e.g. -20."},
            "level": {"type": "number", "description": "Or the level to set, 0-100."},
            "mute": {"type": "boolean", "description": "Mute (true) or unmute (false) the app."},
        },
        "required": [],
    },
}


def run_tool(tool_input: dict[str, Any], set_volume: Any = None) -> ActionResult:
    """The thinker's ``app_volume`` tool: list the apps with sound, or change one (``set_volume`` is the
    backend's ``app_volume``; the real one when ``None``)."""
    app = str(tool_input.get("app") or "").strip()
    level, change = tool_input.get("level"), tool_input.get("change")
    mute = tool_input.get("mute") if isinstance(tool_input.get("mute"), bool) else None
    if not app or (level is None and change is None and mute is None):
        apps = audio_apps(limit=12)
        return ActionResult(ok=True, summary=audio_line(apps) or "no app has sound on the output now",
                            details={"apps": apps})
    act = set_volume or set_app_volume
    return act(app, level=float(level) if level is not None else None,
               change=float(change) if change is not None else None, mute=mute)


__all__ = ["AppSession", "TOOL", "audio_apps", "audio_line", "matching", "run_tool", "sessions", "set_app_volume"]
