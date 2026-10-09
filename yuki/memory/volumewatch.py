"""Volume changes as they happen, from Core Audio's own events: no polling (PLAN.md D1 ``volume_change``, D.1, C.5).

Nothing recorded *that* a volume moved: the master level was only read back after Yuki set it, and an app's mixer
level never. The volume watch listens to Windows' own notifications on one thread and turns each finished change into
one ``volume_change`` moment:

* the default output's **master** level: ``IAudioEndpointVolumeCallback::OnNotify``, re-registered when the default
  device changes (``IMMNotificationClient::OnDefaultDeviceChanged``);
* each app's **session** (mixer) level on that device: ``IAudioSessionNotification::OnSessionCreated`` for new
  sessions, ``IAudioSessionEvents::OnSimpleVolumeChanged`` per session; ``OnSessionDisconnected`` (or an expired
  state) releases a session (Chrome makes and drops many).

Windows' callbacks only enqueue (nothing is done on Windows' threads). The watch thread blocks on
``queue.get(timeout=60)`` (no wake-up while nothing moves, one a minute at most) and **coalesces**: a level that
moved and then stayed for :data:`COALESCE_S` (1.5 s) is one change, from the level before the first event to the
last. A change whose event context is Yuki's own (:data:`yuki.core.inputtag.YUKI_AUDIO_CONTEXT`, passed by
``actions/app_volume.py`` and ``perception/audio.set_master_volume``) is ``by='yuki'``.

The moment (``emit(dict)``): kind ``volume_change``, ``app`` (``master`` or the session's app key), ``value`` /
``value_from`` (0-100), ``means`` (``master`` | ``session``), ``by``, ``source`` ``volume``, ``evidence``
(``endpoint_event`` | ``session_event``), ``facts`` (``front``: the app in front when it ended; ``muted``). The log
(``volume_event``) carries the same codes and numbers: never a title or a name beyond the app key.

A failure in the callbacks or the thread is logged (``volume_error``) and never takes the service down; the COM
registration sits behind ``register=False`` so the coalescer is tested with a synthetic event stream.
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
from collections.abc import Callable
from ctypes import POINTER, byref, c_void_p
from dataclasses import dataclass
from typing import Any

#: A level that moved and then stayed this long (s) is one finished change.
COALESCE_S = 1.5
#: The watch thread's longest sleep with nothing pending (s).
IDLE_WAIT_S = 60.0
#: A change smaller than this (points of 0-100) with no mute change is not a change.
MIN_STEP = 0.5
#: Sessions followed at most (the oldest released first).
MAX_SESSIONS = 96

MASTER = "master"


@dataclass
class _Pending:
    target: tuple[Any, ...]
    app: str
    means: str
    start: float
    last: float
    first_at: float
    last_at: float
    muted_from: bool | None
    muted: bool | None
    yuki: bool


class Coalescer:
    """Turns a stream of level events into finished changes (pure; the thread and the tests share it).

    ``push(target, app, means, level, muted, at, yuki)`` notes one event (level 0-100); ``baseline(target, level,
    muted)`` sets what a target was before its first event (read at registration); ``due(now)`` returns the changes
    quiet for ``quiet_s``: dicts with ``target, app, means, value_from, value, muted_from, muted, at, first_at, yuki``.
    """

    def __init__(self, quiet_s: float = COALESCE_S) -> None:
        self.quiet_s = float(quiet_s)
        self._known: dict[tuple[Any, ...], tuple[float, bool | None]] = {}
        self._pending: dict[tuple[Any, ...], _Pending] = {}

    def baseline(self, target: tuple[Any, ...], level: float, muted: bool | None = None) -> None:
        self._known[target] = (float(level), muted)

    def forget(self, target: tuple[Any, ...]) -> None:
        self._known.pop(target, None)

    def push(self, target: tuple[Any, ...], app: str, means: str, level: float, muted: bool | None, at: float,
             yuki: bool = False) -> None:
        level = max(0.0, min(100.0, float(level)))
        p = self._pending.get(target)
        if p is None:
            before, muted_before = self._known.get(target, (None, None))
            self._pending[target] = _Pending(target=target, app=app, means=means,
                                             start=level if before is None else before, last=level, first_at=at,
                                             last_at=at, muted_from=muted if muted_before is None else muted_before,
                                             muted=muted, yuki=bool(yuki))
            return
        p.last, p.last_at, p.muted = level, at, muted
        p.yuki = p.yuki or bool(yuki)

    def next_due(self) -> float | None:
        """When the oldest pending change becomes due (its last event + the quiet time), or None."""
        if not self._pending:
            return None
        return min(p.last_at for p in self._pending.values()) + self.quiet_s

    def due(self, now: float) -> list[dict[str, Any]]:
        out = []
        for target, p in list(self._pending.items()):
            if now - p.last_at < self.quiet_s:
                continue
            del self._pending[target]
            self._known[target] = (p.last, p.muted)
            moved = abs(p.last - p.start) >= MIN_STEP
            mute_moved = p.muted is not None and p.muted_from is not None and p.muted != p.muted_from
            if not moved and not mute_moved:
                continue
            out.append({"target": target, "app": p.app, "means": p.means, "value_from": round(p.start, 1),
                        "value": round(p.last, 1), "muted_from": p.muted_from, "muted": p.muted, "at": p.last_at,
                        "first_at": p.first_at, "yuki": p.yuki})
        return out

    def pending(self) -> int:
        return len(self._pending)


def _app_of_pid(pid: int) -> str:
    """A process's app key (its image name, casefolded, no ``.exe``), ``""`` when unreadable."""
    try:
        import psutil

        name = psutil.Process(int(pid)).name()
    except Exception:  # noqa: BLE001
        return ""
    key = name.strip().casefold()
    return key[:-4] if key.endswith(".exe") else key


class VolumeWatch:
    """See the module docstring.

    Args:
        on_change: ``on_change(moment: dict)``, called on the watch thread for each finished change.
        log: ``log(type, **fields)``, content-free.
        front_fn: ``() -> app key in front`` (for ``facts.front``), or None.
        app_of_pid: pid → app key (default: the process's image name).
        register: register the Core Audio callbacks (False: tests feed :meth:`feed` / the queue directly).
        clock: wall clock for ``at``.
    """

    def __init__(self, on_change: Callable[[dict[str, Any]], Any] | None = None, *,
                 log: Callable[..., Any] | None = None, front_fn: Callable[[], str | None] | None = None,
                 app_of_pid: Callable[[int], str] | None = None, register: bool = True,
                 quiet_s: float = COALESCE_S, idle_wait_s: float = IDLE_WAIT_S,
                 clock: Callable[[], float] = time.time) -> None:
        self.on_change = on_change
        self._log_fn = log
        self._front_fn = front_fn
        self._app_of_pid = app_of_pid or _app_of_pid
        self._register_com = register
        self.coalescer = Coalescer(quiet_s)
        self.idle_wait_s = float(idle_wait_s)
        self._clock = clock
        self.events: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stopping = False
        self.mode = "stopped"
        # COM objects kept referenced while registered (released on the watch thread only)
        self._endpoint: Any = None
        self._endpoint_sink: Any = None
        self._device_enum: Any = None
        self._device_sink: Any = None
        self._manager: Any = None
        self._manager_sink: Any = None
        self._sessions: dict[int, tuple[Any, Any, Any, int, str]] = {}   # key -> (control, sink, sink_ptr, pid, app)
        self._next_key = 0
        self.counters = {"events": 0, "changes": 0, "yuki": 0, "sessions": 0, "released": 0, "wakeups": 0,
                         "idle_wakeups": 0, "errors": 0, "reregistered": 0}
        self.cpu_s = 0.0
        self._cpu0 = 0.0

    # -- control ---------------------------------------------------------------------------------------

    def start(self, timeout_s: float = 3.0) -> "VolumeWatch":
        if self._thread is not None:
            return self
        self._thread = threading.Thread(target=self._run, name="yuki-memory-volume", daemon=True)
        self._thread.start()
        self._ready.wait(timeout_s)
        return self

    def stop(self, timeout_s: float = 3.0) -> None:
        self._stopping = True
        self.events.put(("stop",))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout_s)

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def feed(self, kind: str, *args: Any) -> None:
        """Queue one event as a callback would (tests; also what the COM sinks call)."""
        self.events.put((kind, *args))

    # -- the thread ------------------------------------------------------------------------------------

    def _run(self) -> None:
        coinit = False
        c0 = self._cpu0 = time.thread_time()
        try:
            if self._register_com:
                try:
                    import comtypes

                    comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
                    coinit = True
                except OSError:
                    pass
                try:
                    self._register()
                    self.mode = "events"
                except Exception as exc:  # noqa: BLE001 - no events: nothing is watched (never a poll)
                    self.counters["errors"] += 1
                    self._log("volume_error", where="register", error=type(exc).__name__)
                    self._unregister()
                    self.mode = "off"
            else:
                self.mode = "events"
            self._ready.set()
            if self.mode == "events":
                self._loop()
        except Exception as exc:  # noqa: BLE001
            self.counters["errors"] += 1
            self._log("volume_error", where="run", error=type(exc).__name__)
        finally:
            self._ready.set()
            try:
                self._flush(force=True)
            except Exception:  # noqa: BLE001
                pass
            self._unregister()
            self.mode = "stopped"
            self.cpu_s = time.thread_time() - c0
            if coinit:
                try:
                    import comtypes

                    comtypes.CoUninitialize()
                except Exception:  # noqa: BLE001
                    pass

    def _loop(self) -> None:
        while True:
            due = self.coalescer.next_due()
            wait = self.idle_wait_s if due is None else max(0.0, due - time.monotonic())
            try:
                event = self.events.get(timeout=wait)
            except queue.Empty:
                event = None
            self.counters["wakeups"] += 1
            self.cpu_s = time.thread_time() - self._cpu0   # this thread's CPU so far (stats() reads it live)
            if event is None and due is None:
                self.counters["idle_wakeups"] += 1
            if event is not None:
                if event[0] == "stop":
                    return
                try:
                    self._handle(event)
                except Exception as exc:  # noqa: BLE001 - one bad event never ends the watch
                    self.counters["errors"] += 1
                    self._log("volume_error", where=str(event[0]), error=type(exc).__name__)
            self._flush()

    def _handle(self, event: tuple[Any, ...]) -> None:
        kind = event[0]
        self.counters["events"] += 1
        from yuki.core.inputtag import is_yuki_audio_context

        if kind == "endpoint":
            _k, level, muted, ctx, _at = event
            self.coalescer.push((MASTER,), MASTER, "master", level, muted, time.monotonic(),
                                is_yuki_audio_context(ctx))
        elif kind == "session":
            _k, key, level, muted, ctx, _at = event
            entry = self._sessions.get(key)
            app = entry[4] if entry else ""
            pid = entry[3] if entry else 0
            if not app:
                return   # a session already released, or of no app (Windows' own sounds)
            self.coalescer.push(("session", pid), app, "session", level, muted, time.monotonic(),
                                is_yuki_audio_context(ctx))
        elif kind == "created":
            self._watch_session(event[1])
        elif kind == "gone":
            self._release(event[1])
        elif kind == "state":
            _k, key, state = event[:3]
            if int(state) == 2:   # AudioSessionStateExpired
                self._release(key)
        elif kind == "default":
            self.counters["reregistered"] += 1
            self._unregister()
            if self._register_com:
                self._register()

    def _flush(self, force: bool = False) -> None:
        now = time.monotonic() + (self.coalescer.quiet_s if force else 0.0)
        for change in self.coalescer.due(now):
            self._publish(change)

    def _publish(self, change: dict[str, Any]) -> None:
        self.counters["changes"] += 1
        if change["yuki"]:
            self.counters["yuki"] += 1
        front = None
        if self._front_fn is not None:
            try:
                front = self._front_fn()
            except Exception:  # noqa: BLE001
                front = None
        wall = self._clock() - max(0.0, time.monotonic() - float(change["at"]))
        means = change["means"]
        moment = {"kind": "volume_change", "at": wall, "app": change["app"], "value": change["value"],
                  "value_from": change["value_from"], "means": means, "by": "yuki" if change["yuki"] else "user",
                  "source": "volume", "evidence": "endpoint_event" if means == "master" else "session_event",
                  "facts": {"front": front, "muted": change["muted"]}}
        self._log("volume_event", target=means, app=change["app"], **{"from": change["value_from"]},
                  to=change["value"], front=front, by=moment["by"], muted=change["muted"])
        if self.on_change is not None:
            try:
                self.on_change(moment)
            except Exception as exc:  # noqa: BLE001
                self.counters["errors"] += 1
                self._log("volume_error", where="on_change", error=type(exc).__name__)

    # -- Core Audio registration (the watch thread only) ------------------------------------------------

    def _register(self) -> None:
        import comtypes

        from yuki.perception import audio

        enum = comtypes.CoCreateInstance(audio._CLSID_MMDeviceEnumerator, audio.IMMDeviceEnumerator,
                                         comtypes.CLSCTX_ALL)
        self._device_enum = enum
        sink = _DeviceSink(self.events)
        ptr = sink.QueryInterface(audio.IMMNotificationClient)
        enum.RegisterEndpointNotificationCallback(_address(ptr))
        self._device_sink = (sink, ptr)
        device = enum.GetDefaultAudioEndpoint(audio.E_RENDER, audio.E_CONSOLE)
        raw = device.Activate(byref(audio.IAudioEndpointVolume._iid_), comtypes.CLSCTX_ALL, None)
        endpoint = ctypes.cast(c_void_p(raw), POINTER(audio.IAudioEndpointVolume))
        self.coalescer.baseline((MASTER,), round(float(endpoint.GetMasterVolumeLevelScalar()) * 100.0, 1),
                                bool(endpoint.GetMute()))
        esink = _EndpointSink(self.events)
        eptr = esink.QueryInterface(audio.IAudioEndpointVolumeCallback)
        endpoint.RegisterControlChangeNotify(_address(eptr))
        self._endpoint, self._endpoint_sink = endpoint, (esink, eptr)
        raw = device.Activate(byref(audio.IAudioSessionManager2._iid_), comtypes.CLSCTX_ALL, None)
        manager = ctypes.cast(c_void_p(raw), POINTER(audio.IAudioSessionManager2))
        msink = _SessionCreated(self.events)
        mptr = msink.QueryInterface(audio.IAudioSessionNotification)
        manager.RegisterSessionNotification(_address(mptr))
        self._manager, self._manager_sink = manager, (msink, mptr)
        sessions = manager.GetSessionEnumerator()   # this also starts the session notifications
        for j in range(sessions.GetCount()):
            self._watch_session(sessions.GetSession(j))

    def _watch_session(self, control: Any) -> None:
        import comtypes

        from yuki.perception import audio

        try:
            if int(control.GetState()) == 2:   # expired
                return
            pid = int(control.QueryInterface(audio.IAudioSessionControl2).GetProcessId())
        except comtypes.COMError:
            return
        if pid <= 0 or pid == os.getpid():
            return   # Windows' own sounds; Yuki's voice is the tray's, but never this process's own
        app = self._app_of_pid(pid)
        if not app:
            return
        try:
            simple = control.QueryInterface(audio.ISimpleAudioVolume)
            self.coalescer.baseline(("session", pid), round(float(simple.GetMasterVolume()) * 100.0, 1),
                                    bool(simple.GetMute()))
        except Exception:  # noqa: BLE001 - no baseline: the first event's level is its own start
            pass
        key = self._next_key
        self._next_key += 1
        sink = _SessionEvents(self.events, key)
        ptr = sink.QueryInterface(audio.IAudioSessionEvents)
        control.RegisterAudioSessionNotification(_address(ptr))
        self._sessions[key] = (control, sink, ptr, pid, app)
        self.counters["sessions"] += 1
        while len(self._sessions) > MAX_SESSIONS:
            self._release(min(self._sessions))

    def _release(self, key: int) -> None:
        entry = self._sessions.pop(key, None)
        if entry is None:
            return
        control, _sink, ptr, pid, _app = entry
        try:
            control.UnregisterAudioSessionNotification(_address(ptr))
        except Exception:  # noqa: BLE001
            pass
        if not any(e[3] == pid for e in self._sessions.values()):
            self.coalescer.forget(("session", pid))
        self.counters["released"] += 1

    def _unregister(self) -> None:
        for key in list(self._sessions):
            self._release(key)
        if self._manager is not None and self._manager_sink is not None:
            try:
                self._manager.UnregisterSessionNotification(_address(self._manager_sink[1]))
            except Exception:  # noqa: BLE001
                pass
        if self._endpoint is not None and self._endpoint_sink is not None:
            try:
                self._endpoint.UnregisterControlChangeNotify(_address(self._endpoint_sink[1]))
            except Exception:  # noqa: BLE001
                pass
        if self._device_enum is not None and self._device_sink is not None:
            try:
                self._device_enum.UnregisterEndpointNotificationCallback(_address(self._device_sink[1]))
            except Exception:  # noqa: BLE001
                pass
        self._manager = self._manager_sink = self._endpoint = self._endpoint_sink = None
        self._device_enum = self._device_sink = None

    # -- reporting -------------------------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {**self.counters, "mode": self.mode, "watched": len(self._sessions),
                "pending": self.coalescer.pending(), "cpu_ms": round(self.cpu_s * 1000.0, 1)}

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass


def _address(ptr: Any) -> int:
    return int(ctypes.cast(ptr, c_void_p).value or 0)


# -- the COM sinks: each only queues (nothing runs on Windows' threads) ------------------------------------------

def _sink_classes() -> tuple[type, type, type, type]:
    from comtypes import COMObject

    from yuki.perception import audio

    class EndpointSink(COMObject):
        _com_interfaces_ = [audio.IAudioEndpointVolumeCallback]

        def __init__(self, events: queue.Queue) -> None:
            super().__init__()
            self._events = events

        def OnNotify(self, notify: Any) -> int:   # noqa: N802 - COM names
            try:
                data = notify.contents
                self._events.put(("endpoint", round(float(data.fMasterVolume) * 100.0, 1), bool(data.bMuted),
                                  audio.guid_text(data.guidEventContext), time.time()))
            except Exception:  # noqa: BLE001
                pass
            return 0

    class DeviceSink(COMObject):
        _com_interfaces_ = [audio.IMMNotificationClient]

        def __init__(self, events: queue.Queue) -> None:
            super().__init__()
            self._events = events

        def OnDeviceStateChanged(self, device: Any, state: Any) -> int:   # noqa: N802
            return 0

        def OnDeviceAdded(self, device: Any) -> int:   # noqa: N802
            return 0

        def OnDeviceRemoved(self, device: Any) -> int:   # noqa: N802
            return 0

        def OnDefaultDeviceChanged(self, flow: int, role: int, device: Any) -> int:   # noqa: N802
            if int(flow) == audio.E_RENDER and int(role) == audio.E_CONSOLE:
                self._events.put(("default", time.time()))
            return 0

        def OnPropertyValueChanged(self, device: Any, key: Any) -> int:   # noqa: N802
            return 0

    class SessionEvents(COMObject):
        _com_interfaces_ = [audio.IAudioSessionEvents]

        def __init__(self, events: queue.Queue, key: int) -> None:
            super().__init__()
            self._events = events
            self.key = key

        def OnDisplayNameChanged(self, name: Any, ctx: Any) -> int:   # noqa: N802
            return 0

        def OnIconPathChanged(self, path: Any, ctx: Any) -> int:   # noqa: N802
            return 0

        def OnSimpleVolumeChanged(self, volume: float, mute: Any, ctx: Any) -> int:   # noqa: N802
            try:
                self._events.put(("session", self.key, round(float(volume) * 100.0, 1), bool(mute),
                                  audio.guid_text(ctx), time.time()))
            except Exception:  # noqa: BLE001
                pass
            return 0

        def OnChannelVolumeChanged(self, count: Any, volumes: Any, changed: Any, ctx: Any) -> int:   # noqa: N802
            return 0

        def OnGroupingParamChanged(self, param: Any, ctx: Any) -> int:   # noqa: N802
            return 0

        def OnStateChanged(self, state: int) -> int:   # noqa: N802
            self._events.put(("state", self.key, int(state), time.time()))
            return 0

        def OnSessionDisconnected(self, reason: int) -> int:   # noqa: N802
            self._events.put(("gone", self.key, time.time()))
            return 0

    class SessionCreated(COMObject):
        _com_interfaces_ = [audio.IAudioSessionNotification]

        def __init__(self, events: queue.Queue) -> None:
            super().__init__()
            self._events = events

        def OnSessionCreated(self, session: Any) -> int:   # noqa: N802
            # comtypes holds a reference for the wrapper while it lives (loopback.py, measured): no AddRef here
            try:
                self._events.put(("created", session, time.time()))
            except Exception:  # noqa: BLE001
                pass
            return 0

    return EndpointSink, DeviceSink, SessionEvents, SessionCreated


_SINKS: tuple[type, type, type, type] | None = None


def _sinks() -> tuple[type, type, type, type]:
    global _SINKS
    if _SINKS is None:
        _SINKS = _sink_classes()
    return _SINKS


def _EndpointSink(events: queue.Queue) -> Any:   # noqa: N802 - a class factory, named as the class
    return _sinks()[0](events)


def _DeviceSink(events: queue.Queue) -> Any:   # noqa: N802
    return _sinks()[1](events)


def _SessionEvents(events: queue.Queue, key: int) -> Any:   # noqa: N802
    return _sinks()[2](events, key)


def _SessionCreated(events: queue.Queue) -> Any:   # noqa: N802
    return _sinks()[3](events)


__all__ = ["COALESCE_S", "Coalescer", "MASTER", "VolumeWatch"]
