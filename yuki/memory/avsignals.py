"""Who holds the microphone and the camera, and which of them also plays sound: measured, content-free.

What a call or a meeting *is* comes from these signals, never from a list of
app names or meeting sites (docs/MEMORY.md, "Calls: just my side" and
"Timeline: full screen and meetings"; docs/research/no-pattern-audit.md C):

* **Device holders**: the CapabilityAccessManager ConsentStore
  (:func:`yuki.perception.system._device_users`, the facts of
  ``activity_facts()["microphone"]`` / ``["camera"]``): each app using the
  microphone or the camera now, with the time it started (``since``).
  Yuki's own interpreters are left out (:func:`own_executables`).
* **Sound out**: the active render sessions of Core Audio
  (:func:`yuki.perception.audio.render_sessions`: session state only, no
  stream opened), matched to a holder by executable path or package family.

:class:`AvSignals` caches the holders and reads them again only when Windows
changed a ConsentStore key (``RegNotifyChangeKeyValue`` on the microphone and
webcam keys, checked without waiting) or after ``backstop_s``; render sessions
are read only when asked, i.e. only while someone holds the microphone, and
cached for ``render_max_age_s``. One instance is shared by the timeline and
the call worker in ``yuki-memory``; every read is safe from any thread.

Public API::

    AvSignals(exclude_paths=None, *, mic_fn=None, camera_fn=None, render_fn=None, identity_fn=None,
              watch=True, backstop_s=60.0, render_max_age_s=4.0)
        .read() -> AvSnapshot                 # .mic, .camera: tuples of Holder; .at; .read_ms
        .playing(holders) -> set[Holder]      # the ones whose app has an active output stream now
        .stats() -> dict                      # reads, ms (content-free)
    Holder(app, packaged, path, since).key / .matches(process_key=, exe=, ids=)
    own_executables() -> frozenset[str];  display_name(holder) -> str
    gaming_with_voice(holder, *, fullscreen, shows_page, camera, pids, gpu_fn=gpu_share) -> (bool, facts)
    foreground_fullscreen() -> dict | None;  gpu_share(pids) -> float | None

**Gaming with voice is not a meeting** (the 26 Sept Valorant case): a game's own voice chat holds
the microphone and plays sound, exactly like a call. What tells them apart is measured too: the
microphone holder is the **full-screen app in front**, it shows no web page, it does not hold the
camera, and it **draws like a game** - its own share of the GPU's compute (NVML, last 10 s) is at
least :data:`GAME_GPU_SHARE`. A full-screen meeting or video barely uses the GPU's compute and stays
a meeting; without a GPU reading, full screen + microphone + no camera + no page counts as gaming.
A call in another app (Discord in the background) is untouched.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from yuki.memory.store import app_key

_CONSENT_STORE = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore"


def own_executables() -> frozenset[str]:
    """Image paths of this process and its interpreter(s), casefolded: never a call or a meeting.

    A uv/venv ``python.exe`` launches the base interpreter, which is the
    process that holds the microphone, so both are listed.
    """
    paths = {sys.executable, getattr(sys, "_base_executable", "") or ""}
    try:
        import psutil

        me = psutil.Process()
        paths.add(me.exe())
        parent = me.parent()
        if parent is not None and Path(parent.exe()).name.lower().startswith("python"):
            paths.add(parent.exe())
    except Exception:
        pass
    out = set()
    for p in paths:
        if p:
            out.add(str(Path(p)).casefold())
            stem = Path(p)
            for variant in ("python.exe", "pythonw.exe"):   # the UI runs under pythonw
                out.add(str(stem.with_name(variant)).casefold())
    try:   # the installed app: Yuki.exe (tray, voice) and yuki-cli.exe share one program folder
        from yuki.core.procs import yuki_executables

        out |= yuki_executables()
    except Exception:
        pass
    return frozenset(out)


@dataclass(frozen=True)
class Holder:
    """One app using the microphone or the camera (a ConsentStore record)."""

    app: str          # image name ("Discord.exe"), or a packaged app's package family name ("MSTeams")
    packaged: bool
    path: str         # the executable (desktop apps), "" for packaged ones
    since: float      # epoch seconds the use started

    @property
    def key(self) -> str:
        """The app key the rest of memory uses (``discord``), or the package family, casefolded."""
        return (app_key(self.app) or "") if not self.packaged else self.app.casefold()

    def matches(self, *, process_key: str = "", exe: str = "", ids: Iterable[str] = (), family: str = "") -> bool:
        """Whether a process is this holder: same executable (desktop app) or same package family."""
        if self.packaged:
            fam = self.app.casefold()
            if family and family.casefold().split("_", 1)[0] == fam:
                return True
            return any(i.startswith(fam + "_") or i.startswith(fam + "!") for i in ids)
        if exe and self.path and exe.casefold() == self.path.casefold():
            return True
        return bool(process_key) and process_key == self.key


@dataclass(frozen=True)
class AvSnapshot:
    mic: tuple[Holder, ...] = ()
    camera: tuple[Holder, ...] = ()
    at: float = 0.0
    read_ms: float = 0.0


class _KeyWatch:
    """A change notification on one ConsentStore key, checked without waiting (``changed()``)."""

    def __init__(self, device: str) -> None:
        import winreg

        self.ok = False
        self._key = None
        self._evt = None
        self._k = ctypes.WinDLL("kernel32")
        self._a = ctypes.WinDLL("advapi32")
        self._k.CreateEventW.restype = ctypes.c_void_p
        self._k.CreateEventW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_wchar_p]
        self._k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        self._k.WaitForSingleObject.restype = ctypes.c_uint32
        self._k.CloseHandle.argtypes = [ctypes.c_void_p]
        self._a.RegNotifyChangeKeyValue.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32, ctypes.c_void_p,
                                                    ctypes.c_int]
        self._a.RegNotifyChangeKeyValue.restype = ctypes.c_long
        try:
            self._key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{_CONSENT_STORE}\\{device}", 0,
                                       winreg.KEY_NOTIFY | winreg.KEY_READ)
        except OSError:
            return   # nothing has ever used this device: the backstop read covers it
        self._evt = self._k.CreateEventW(None, False, False, None)
        self.ok = bool(self._evt) and self._arm()

    def _arm(self) -> bool:
        # REG_NOTIFY_CHANGE_NAME | REG_NOTIFY_CHANGE_LAST_SET over the subtree, asynchronous (sets the event),
        # REG_NOTIFY_THREAD_AGNOSTIC: the timeline and the call worker both read, from their own threads
        return self._a.RegNotifyChangeKeyValue(ctypes.c_void_p(int(self._key)), 1, 0x1 | 0x4 | 0x10000000,
                                               ctypes.c_void_p(self._evt), 1) == 0

    def changed(self) -> bool:
        if not self.ok:
            return False
        if self._k.WaitForSingleObject(ctypes.c_void_p(self._evt), 0) != 0:
            return False
        self.ok = self._arm()
        return True

    def close(self) -> None:
        try:
            if self._key is not None:
                self._key.Close()
            if self._evt:
                self._k.CloseHandle(ctypes.c_void_p(self._evt))
        except Exception:
            pass


def _consent_users(device: str, processes: list | None = None) -> list[dict[str, Any]]:
    """Apps using ``device`` ("microphone" | "webcam") now, from the ConsentStore (read-only)."""
    import psutil

    from yuki.perception import system as _system

    if processes is None:
        processes = _system._process_entries()
    boot_ft = int(psutil.boot_time() * 1e7) + _system._FILETIME_UNIX_EPOCH
    try:
        return _system._device_users(device, processes, boot_ft)
    except FileNotFoundError:
        return []


def _consent_both() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Microphone and camera users from one process list (the list is most of a read's cost)."""
    from yuki.perception import system as _system

    processes = _system._process_entries()
    return _consent_users("microphone", processes), _consent_users("webcam", processes)


def _render_sessions() -> list[dict[str, Any]]:
    from yuki.perception import audio

    return audio.render_sessions()


def _pid_identity(pid: int) -> tuple[str, str]:
    """``(executable path, package family)`` of a process; ``""`` for what cannot be read."""
    exe = family = ""
    try:
        import psutil

        exe = psutil.Process(pid).exe()
    except Exception:
        pass
    try:
        from yuki.perception import windows

        _, family = windows.process_package_ids(pid)
    except Exception:
        pass
    return exe, family


def _holders(rows: Iterable[dict[str, Any]], exclude: frozenset[str]) -> tuple[Holder, ...]:
    out = []
    for u in rows:
        path = str(u.get("path") or "")
        if path and path.casefold() in exclude:
            continue
        app = str(u.get("app") or "")
        if app:
            out.append(Holder(app=app, packaged=bool(u.get("packaged")), path=path, since=float(u.get("since") or 0.0)))
    return tuple(out)


class AvSignals:
    """The microphone and camera holders, cached until Windows says they changed (see the module docstring).

    Every outside read is a replaceable function, so tests run on stubbed
    ConsentStore readings and audio sessions (``watch=False`` then: each
    :meth:`read` older than ``backstop_s`` reads the stubs again; ``backstop_s=0``
    reads them every time).
    """

    def __init__(
        self,
        exclude_paths: frozenset[str] | None = None,
        *,
        mic_fn: Callable[[], list[dict]] | None = None,
        camera_fn: Callable[[], list[dict]] | None = None,
        render_fn: Callable[[], list[dict]] | None = None,
        identity_fn: Callable[[int], tuple[str, str]] | None = None,
        watch: bool = True,
        backstop_s: float = 60.0,
        render_max_age_s: float = 4.0,
    ) -> None:
        self.exclude = own_executables() if exclude_paths is None else frozenset(exclude_paths)
        #: Both devices from one process list, unless a test stubs them.
        self._both_fn = _consent_both if mic_fn is None and camera_fn is None else None
        self._mic_fn = mic_fn or (lambda: _consent_users("microphone"))
        self._camera_fn = camera_fn or (lambda: _consent_users("webcam"))
        self._render_fn = render_fn or _render_sessions
        self._identity_fn = identity_fn or _pid_identity
        self.backstop_s = float(backstop_s)
        self.render_max_age_s = float(render_max_age_s)
        self._watch = watch
        self._watches: list[_KeyWatch] | None = None
        self._lock = threading.Lock()
        self._snap: AvSnapshot | None = None
        self._snap_mono = 0.0
        self._render: list[tuple[str, str]] = []   # (exe casefolded, family casefolded) of active outputs
        self._render_mono = -1e9
        self._identities: dict[int, tuple[float, tuple[str, str]]] = {}
        self.counters = {"holder_reads": 0, "render_reads": 0, "errors": 0}
        self._holder_ms: list[float] = []
        self._render_ms: list[float] = []

    # -- holders -------------------------------------------------------------

    def _changed(self) -> bool:
        if not self._watch:
            return False
        if self._watches is None:
            try:
                self._watches = [_KeyWatch("microphone"), _KeyWatch("webcam")]
            except Exception:
                self._watches = []
            return True
        changed = False
        for w in self._watches:
            changed = w.changed() or changed   # every event is consumed and re-armed
        return changed

    def read(self) -> AvSnapshot:
        """The holders now: the cached read unless Windows changed them or the backstop is due."""
        with self._lock:
            mono = time.monotonic()
            changed = self._changed()
            if self._snap is not None and not changed and mono - self._snap_mono < self.backstop_s:
                return self._snap
            t0 = time.perf_counter()
            mic_rows = camera_rows = None
            if self._both_fn is not None:
                try:
                    mic_rows, camera_rows = self._both_fn()
                except Exception:
                    self.counters["errors"] += 1
            try:
                mic = _holders(mic_rows if mic_rows is not None else self._mic_fn(), self.exclude)
            except Exception:
                self.counters["errors"] += 1
                mic = self._snap.mic if self._snap is not None else ()
            try:
                camera = _holders(camera_rows if camera_rows is not None else self._camera_fn(), self.exclude)
            except Exception:
                self.counters["errors"] += 1
                camera = self._snap.camera if self._snap is not None else ()
            ms = (time.perf_counter() - t0) * 1000.0
            self.counters["holder_reads"] += 1
            self._keep(self._holder_ms, ms)
            self._snap = AvSnapshot(mic=mic, camera=camera, at=time.time(), read_ms=round(ms, 1))
            self._snap_mono = mono
            return self._snap

    # -- sound out -------------------------------------------------------------

    def _identity(self, pid: int) -> tuple[str, str]:
        mono = time.monotonic()
        hit = self._identities.get(pid)
        if hit is not None and mono - hit[0] < 600.0:
            return hit[1]
        exe, family = self._identity_fn(pid)
        ident = (exe.casefold(), family.casefold())
        if len(self._identities) > 256:
            self._identities.clear()
        self._identities[pid] = (mono, ident)
        return ident

    def playing(self, holders: Iterable[Holder]) -> set[Holder]:
        """The holders whose app has an active output stream now (Core Audio render sessions)."""
        holders = list(holders)
        if not holders:
            return set()
        with self._lock:
            mono = time.monotonic()
            if mono - self._render_mono >= self.render_max_age_s:
                t0 = time.perf_counter()
                try:
                    sessions = self._render_fn()
                    pids = sorted({int(s.get("pid") or 0) for s in sessions if s.get("state") == "active"} - {0})
                    self._render = [self._identity(pid) for pid in pids]
                except Exception:
                    self.counters["errors"] += 1
                    self._render = []
                self._render_mono = mono
                self.counters["render_reads"] += 1
                self._keep(self._render_ms, (time.perf_counter() - t0) * 1000.0)
            render = list(self._render)
        return {h for h in holders if any(h.matches(exe=exe, family=fam) for exe, fam in render)}

    # -- bookkeeping -------------------------------------------------------------

    @staticmethod
    def _keep(values: list[float], ms: float) -> None:
        values.append(ms)
        if len(values) > 500:
            del values[:250]

    def stats(self) -> dict[str, Any]:
        def p50(values: list[float]) -> float | None:
            return round(sorted(values)[len(values) // 2], 1) if values else None

        return {**self.counters, "holder_ms_p50": p50(self._holder_ms), "render_ms_p50": p50(self._render_ms),
                "watching": bool(self._watches) and all(w.ok for w in self._watches or [])}

    def close(self) -> None:
        for w in self._watches or []:
            w.close()


_names: dict[str, str] = {}


def display_name(holder: Holder) -> str:
    """The app's own name for a card or a timeline row: its FileDescription ("Google Chrome"), a packaged
    app's Start menu name, else its image name's stem / package name (cached)."""
    key = holder.path.casefold() if holder.path else holder.app.casefold()
    if key in _names:
        return _names[key]
    name = ""
    if holder.path:
        try:
            import win32api

            for lang, codepage in win32api.GetFileVersionInfo(holder.path, "\\VarFileInfo\\Translation") or []:
                value = win32api.GetFileVersionInfo(holder.path,
                                                    f"\\StringFileInfo\\{lang:04x}{codepage:04x}\\FileDescription")
                if value and str(value).strip():
                    name = str(value).strip()
                    break
        except Exception:
            pass
        name = name or Path(holder.path).stem
    elif holder.packaged:
        index = None
        try:
            from yuki.perception import windows

            index = windows.start_menu_index()
            fam = holder.app.casefold()
            if index is not None:
                # an AUMID is "<family>_<publisher id>!<app>"
                entry = next((e for appid, e in index.by_appid.items()
                              if appid.split("!", 1)[0].split("_", 1)[0] == fam), None)
                name = entry.name if entry is not None else ""
        except Exception:
            pass
        if index is None:   # the Start menu is still being read: do not keep the fallback
            return holder.app.rsplit(".", 1)[-1] or holder.app
        # a package family name is "Publisher.Name": the part after the publisher's prefix
        name = name or holder.app.rsplit(".", 1)[-1]
    else:
        name = Path(holder.app).stem
    _names[key] = name or holder.app
    return _names[key]


# ---------------------------------------------------------------------------
# Gaming with voice chat: the full-screen app in front, drawing like a game
# ---------------------------------------------------------------------------

#: A full-screen app in front whose own GPU compute share (percent, NVML, mean of the last 10 s) is
#: at least this is drawing like a game. Measured signal, not an app list.
GAME_GPU_SHARE = 15.0

_gpu: Any = None
_gpu_lock = threading.Lock()


def gpu_share(pids: Iterable[int]) -> float | None:
    """The summed GPU compute share (%) of these processes over the last 10 s; ``None`` when unknown."""
    global _gpu
    wanted = {int(p) for p in pids if p}
    if not wanted:
        return None
    try:
        with _gpu_lock:
            if _gpu is None:
                from yuki.memory.calls import Gpu   # NVML, silent when there is no NVIDIA driver

                _gpu = Gpu()
            shares = _gpu.process_utilization(10.0)
    except Exception:
        return None
    if shares is None:
        return None
    return float(sum(v for pid, v in shares.items() if pid in wanted))


def gaming_with_voice(holder: Holder, *, fullscreen: bool, shows_page: bool, camera: Iterable[Holder] = (),
                      pids: Iterable[int] = (), gpu_fn: Callable[[Iterable[int]], float | None] = gpu_share,
                      ) -> tuple[bool, dict[str, Any]]:
    """Whether the microphone ``holder`` is a full-screen game in front with voice chat (not a meeting).

    Args:
        holder: The microphone holder that is the app in front.
        fullscreen: The window in front is full screen (``watcher.is_fullscreen_front``).
        shows_page: The window in front shows a web page (a meeting site in a full-screen browser).
        camera: The camera holders now (a camera in use means a meeting or a video call).
        pids: The processes of the window in front (its GPU share is read for these).
        gpu_fn: Reads the GPU share (tests pass a stub).

    Returns:
        ``(gaming, facts)``; ``facts`` says why (content-free), for the log.
    """
    facts: dict[str, Any] = {"fullscreen": bool(fullscreen), "page": bool(shows_page)}
    if not fullscreen or shows_page:
        return False, facts
    if any(c.key == holder.key for c in camera):
        facts["camera"] = True
        return False, facts
    share = None
    try:
        share = gpu_fn(list(pids))
    except Exception:
        share = None
    facts["gpu_share"] = None if share is None else round(share, 1)
    if share is None:
        return True, facts
    return share >= GAME_GPU_SHARE, facts


def foreground_fullscreen() -> dict[str, Any] | None:
    """The window in front when it is full screen: ``{"hwnd", "pid", "exe"}``; ``None`` otherwise."""
    try:
        user32 = ctypes.WinDLL("user32")
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        hwnd = int(user32.GetForegroundWindow() or 0)
        if not hwnd:
            return None
        from yuki.memory.watcher import is_fullscreen_front

        if not is_fullscreen_front(hwnd):
            return None
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(ctypes.c_void_p(hwnd), ctypes.byref(pid))
        exe, _family = _pid_identity(int(pid.value)) if pid.value else ("", "")
        return {"hwnd": hwnd, "pid": int(pid.value), "exe": exe}
    except Exception:
        return None


__all__ = ["AvSignals", "AvSnapshot", "Holder", "own_executables", "display_name", "GAME_GPU_SHARE",
           "gpu_share", "gaming_with_voice", "foreground_fullscreen"]
