"""UI Automation events for the windows in play: is a read still current, and has the window settled (report 58).

Before this module every read was blind: whether the window had changed since the thinker's read was guessed ("you
acted on it since"), and the view after actions re-walked the whole window every 0.1 s until two reads matched.  Here
a window that Yuki reads or acts on is subscribed to UI Automation's structure-changed and property-changed events
(states, values, names, focus, off-screen), and the subscription only counts:

* :meth:`EventHub.generation` - a number that grows with every event from the window.  A read taken at generation
  ``g`` is current while the generation is still ``g``, so the read before an action costs nothing when nothing
  happened;
* :meth:`EventHub.wait_quiet` - wait until the window has had no event for a while (bounded), instead of re-reading
  it to find out;
* :meth:`EventHub.busy_since` - the elements that changed on their own before an action (a playback clock, a
  progress bar), so the account of what the action changed leaves them out.

Lightweight by construction: handlers do nothing but count under a lock; one thread owns the subscriptions; a window
is unsubscribed :data:`IDLE_S` after it was last read or acted on, and with no window in play the thread sleeps on a
condition (no polling, nothing while Yuki is idle, nothing during a game or call unless Yuki is acting then).  The
event source is an interface (:class:`UiaSource` on the desktop, a fake in tests); if subscribing fails, the hub
says so and callers fall back to reading.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

#: A window not read or acted on for this long is unsubscribed (s).
IDLE_S = 30.0
#: Windows subscribed at once, at most (the oldest goes first).
MAX_WINDOWS = 4
#: Changed elements remembered per window (their RuntimeIds, with when).
RECENT_CHANGES = 256
#: How long a subscription request may take before callers stop waiting for it (s).
SUBSCRIBE_WAIT_S = 0.5

#: UI Automation property ids whose changes are counted (the states and values a read shows, focus, visibility).
PROPERTIES = (
    30005,  # Name
    30045,  # Value.Value
    30047,  # RangeValue.Value
    30086,  # Toggle.ToggleState
    30070,  # ExpandCollapse.ExpandCollapseState
    30079,  # SelectionItem.IsSelected
    30008,  # HasKeyboardFocus
    30022,  # IsOffscreen
    30010,  # IsEnabled
)
#: Automation events counted on the window's subtree: a menu opening or closing (Chromium's menus are inside the
#: window's tree, Win32's are their own windows and show as new windows in the action's report).
EVENTS = (20003, 20007)  # MenuOpened, MenuClosed


class Source(Protocol):
    """Where events come from: subscribe one window, unsubscribe it."""

    def subscribe(self, hwnd: int, sink: Callable[[str, int, tuple | None], None]) -> Any: ...
    def unsubscribe(self, token: Any) -> None: ...


@dataclass
class WindowActivity:
    """What one window's events said."""

    hwnd: int
    generation: int = 0
    structure: int = 0
    properties: int = 0
    events: int = 0
    last_at: float = 0.0
    touched_at: float = 0.0
    #: ``(when, RuntimeId)`` of elements whose properties changed, newest last.
    recent: deque = field(default_factory=lambda: deque(maxlen=RECENT_CHANGES))
    token: Any = None
    ready: bool = False
    error: str = ""


class EventHub:
    """Event subscriptions for the windows in play (see the module docstring).

    Args:
        source_factory: makes the :class:`Source` on the hub's own thread (UI Automation needs its subscriptions made
            from a thread of its own apartment); the desktop one by default.
        idle_s: unsubscribe a window this long after it was last touched.
        clock: monotonic time (tests pass a fake).
    """

    def __init__(self, source_factory: Callable[[], Source] | None = None, *, idle_s: float = IDLE_S,
                 clock: Callable[[], float] = time.monotonic, max_windows: int = MAX_WINDOWS) -> None:
        self._factory = source_factory or UiaSource
        self.idle_s = idle_s
        self.clock = clock
        self.max_windows = max_windows
        self._lock = threading.Condition()
        self._windows: dict[int, WindowActivity] = {}
        self._commands: deque = deque()
        self._thread: threading.Thread | None = None
        self._closed = False
        self.error = ""

    # -- the thread ---------------------------------------------------------
    def _start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="yuki-uia-events", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        try:
            import comtypes

            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except Exception:  # noqa: BLE001 - not on Windows (tests), or already initialised
            pass
        try:
            source = self._factory()
        except Exception as exc:  # noqa: BLE001 - no events: callers read instead
            with self._lock:
                self.error = f"{type(exc).__name__}: {exc}"
                for activity in self._windows.values():
                    activity.error = self.error
                self._lock.notify_all()
            return
        while True:
            with self._lock:
                while not self._commands and not self._closed:
                    deadline = self._next_expiry()
                    if deadline is None:
                        self._lock.wait()   # nothing in play: sleep until asked
                    else:
                        wait = deadline - self.clock()
                        if wait <= 0:
                            break
                        self._lock.wait(wait)
                commands = list(self._commands)
                self._commands.clear()
                closed = self._closed
                expired = [h for h, w in self._windows.items()
                           if self.clock() - w.touched_at >= self.idle_s] if not closed else list(self._windows)
            for kind, hwnd in commands:
                if kind == "watch":
                    self._subscribe(source, hwnd)
            for hwnd in expired:
                self._unsubscribe(source, hwnd)
            if closed:
                return

    def _next_expiry(self) -> float | None:
        if not self._windows:
            return None
        return min(w.touched_at for w in self._windows.values()) + self.idle_s

    def _subscribe(self, source: Source, hwnd: int) -> None:
        with self._lock:
            activity = self._windows.get(hwnd)
            if activity is None or activity.ready or activity.error:
                return

        def sink(kind: str, what: int, runtime_id: tuple | None) -> None:
            with self._lock:
                act = self._windows.get(hwnd)
                if act is None:
                    return
                act.generation += 1
                act.last_at = self.clock()
                if kind == "structure":
                    act.structure += 1
                elif kind == "property":
                    act.properties += 1
                    if runtime_id:
                        act.recent.append((act.last_at, tuple(runtime_id)))
                else:
                    act.events += 1
                self._lock.notify_all()

        try:
            token = source.subscribe(hwnd, sink)
        except Exception as exc:  # noqa: BLE001 - this window then goes without events
            with self._lock:
                activity.error = f"{type(exc).__name__}: {exc}"
                self._lock.notify_all()
            return
        with self._lock:
            current = self._windows.get(hwnd) is activity
            if current:
                activity.token = token
                activity.ready = True
                self._lock.notify_all()
        if not current:   # it left play while the subscription was being made: never leave one dangling
            try:
                source.unsubscribe(token)
            except Exception:  # noqa: BLE001
                pass

    def _unsubscribe(self, source: Source, hwnd: int) -> None:
        with self._lock:
            activity = self._windows.pop(hwnd, None)
        if activity is not None and activity.token is not None:
            try:
                source.unsubscribe(activity.token)
            except Exception:  # noqa: BLE001 - the window may be gone already
                pass

    # -- the callers' side --------------------------------------------------
    def watch(self, hwnd: int, *, wait_s: float = 0.0) -> bool:
        """Keep ``hwnd`` in play (subscribing it if it is not); True once its events are flowing. ``wait_s``: how
        long to wait for a new subscription to be made."""
        hwnd = int(hwnd)
        with self._lock:
            if self._closed or self.error:
                return False
            activity = self._windows.get(hwnd)
            now = self.clock()
            if activity is None:
                if len(self._windows) >= self.max_windows:
                    oldest = min(self._windows.values(), key=lambda w: w.touched_at)
                    oldest.touched_at = now - self.idle_s   # the thread drops it at its next turn
                activity = self._windows[hwnd] = WindowActivity(hwnd=hwnd, touched_at=now)
                self._commands.append(("watch", hwnd))
                self._start()
            activity.touched_at = now
            self._lock.notify_all()
            if wait_s > 0 and not activity.ready and not activity.error:
                self._lock.wait_for(lambda: activity.ready or bool(activity.error) or self._closed, timeout=wait_s)
            return activity.ready

    def generation(self, hwnd: int) -> int | None:
        """The window's event count, or ``None`` when its events are not flowing (not watched, failed, not yet)."""
        with self._lock:
            activity = self._windows.get(int(hwnd))
            return activity.generation if activity is not None and activity.ready else None

    def activity(self, hwnd: int) -> WindowActivity | None:
        with self._lock:
            return self._windows.get(int(hwnd))

    def wait_change(self, hwnd: int, since: int, timeout_s: float) -> bool:
        """Wait up to ``timeout_s`` for any event after generation ``since``; whether one came."""
        deadline = self.clock() + max(timeout_s, 0.0)
        with self._lock:
            while True:
                activity = self._windows.get(int(hwnd))
                if activity is None or not activity.ready:
                    return False
                if activity.generation > since:
                    return True
                remaining = deadline - self.clock()
                if remaining <= 0:
                    return False
                self._lock.wait(remaining)

    def wait_quiet(self, hwnd: int, *, quiet_s: float, cap_s: float) -> tuple[bool, float]:
        """Wait until the window has had no event for ``quiet_s``, at most ``cap_s``: ``(quiet, waited_s)``.

        A window still busy at the cap (an animation, a ticking clock) returns ``(False, cap_s)``: the caller reads
        it as it is then.  Nothing is read here; this only waits on the events.
        """
        started = self.clock()
        deadline = started + max(cap_s, 0.0)
        with self._lock:
            while True:
                activity = self._windows.get(int(hwnd))
                if activity is None or not activity.ready:
                    return False, self.clock() - started
                now = self.clock()
                since_last = now - max(activity.last_at, started) if activity.last_at else now - started
                if since_last >= quiet_s:
                    return True, now - started
                if now >= deadline:
                    return False, now - started
                self._lock.wait(min(quiet_s - since_last, deadline - now))

    def busy_since(self, hwnd: int, since_time: float) -> set[tuple]:
        """RuntimeIds of elements whose properties changed after ``since_time`` (monotonic)."""
        with self._lock:
            activity = self._windows.get(int(hwnd))
            if activity is None:
                return set()
            return {rid for at, rid in activity.recent if at >= since_time}

    def release(self, hwnd: int) -> None:
        """Take ``hwnd`` out of play now."""
        with self._lock:
            activity = self._windows.get(int(hwnd))
            if activity is not None:
                activity.touched_at = self.clock() - self.idle_s
                self._lock.notify_all()

    def watched(self) -> list[int]:
        with self._lock:
            return [h for h, w in self._windows.items() if w.ready]

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._lock.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(2.0)


# ---------------------------------------------------------------------------
# The desktop source: UI Automation through comtypes
# ---------------------------------------------------------------------------

_TREE_SCOPE_SUBTREE = 7
_TREE_SCOPE_ELEMENT = 1
_P_RUNTIME_ID = 30000


def handler_classes(module: Any) -> tuple[type, type, type]:
    """The three COM event handler classes over ``module`` (the UIAutomationCore wrapper). Each only forwards to its
    sink; built per module so a test can check the wiring without a desktop."""
    import comtypes

    def runtime_id(sender: Any) -> tuple | None:
        try:
            raw = sender.GetCachedPropertyValue(_P_RUNTIME_ID)
            return tuple(int(x) for x in raw) if raw else None
        except Exception:  # noqa: BLE001
            return None

    class StructureHandler(comtypes.COMObject):
        _com_interfaces_ = [module.IUIAutomationStructureChangedEventHandler]

        def __init__(self, sink: Callable[[str, int, tuple | None], None]) -> None:
            super().__init__()
            self.sink = sink

        def HandleStructureChangedEvent(self, sender: Any, changeType: int, runtimeId: Any) -> int:  # noqa: N802,N803
            try:
                self.sink("structure", int(changeType), None)
            except Exception:  # noqa: BLE001 - a handler must never raise into UI Automation
                pass
            return 0

    class PropertyHandler(comtypes.COMObject):
        _com_interfaces_ = [module.IUIAutomationPropertyChangedEventHandler]

        def __init__(self, sink: Callable[[str, int, tuple | None], None]) -> None:
            super().__init__()
            self.sink = sink

        def HandlePropertyChangedEvent(self, sender: Any, propertyId: int, newValue: Any) -> int:  # noqa: N802,N803
            try:
                self.sink("property", int(propertyId), runtime_id(sender))
            except Exception:  # noqa: BLE001
                pass
            return 0

    class AutomationHandler(comtypes.COMObject):
        _com_interfaces_ = [module.IUIAutomationEventHandler]

        def __init__(self, sink: Callable[[str, int, tuple | None], None]) -> None:
            super().__init__()
            self.sink = sink

        def HandleAutomationEvent(self, sender: Any, eventId: int) -> int:  # noqa: N802,N803
            try:
                self.sink("event", int(eventId), None)
            except Exception:  # noqa: BLE001
                pass
            return 0

    return StructureHandler, PropertyHandler, AutomationHandler


class UiaSource:
    """Subscriptions through IUIAutomation, made and removed on the hub's own (MTA) thread."""

    def __init__(self) -> None:
        import comtypes.client

        from yuki.perception.tree import _CUIAUTOMATION_CLSID, _uia_core

        self.module = _uia_core()
        self.automation = comtypes.client.CreateObject(_CUIAUTOMATION_CLSID, interface=self.module.IUIAutomation)
        self.classes = handler_classes(self.module)
        request = self.automation.CreateCacheRequest()
        request.AddProperty(_P_RUNTIME_ID)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        self.request = request

    def subscribe(self, hwnd: int, sink: Callable[[str, int, tuple | None], None]) -> Any:
        import ctypes

        element = self.automation.ElementFromHandle(int(hwnd))
        structure_cls, property_cls, automation_cls = self.classes
        structure, prop, auto = structure_cls(sink), property_cls(sink), automation_cls(sink)
        added: list[tuple[str, Any]] = []
        try:
            self.automation.AddStructureChangedEventHandler(element, _TREE_SCOPE_SUBTREE, None, structure)
            added.append(("structure", structure))
            ids = (ctypes.c_int * len(PROPERTIES))(*PROPERTIES)
            self.automation.AddPropertyChangedEventHandlerNativeArray(element, _TREE_SCOPE_SUBTREE, self.request,
                                                                     prop, ids, len(PROPERTIES))
            added.append(("property", prop))
            for event in EVENTS:
                self.automation.AddAutomationEventHandler(event, element, _TREE_SCOPE_SUBTREE, None, auto)
                added.append((f"event:{event}", auto))
        except Exception:
            self.unsubscribe((element, added))
            raise
        return (element, added)

    def unsubscribe(self, token: Any) -> None:
        element, added = token
        for kind, handler in added:
            try:
                if kind == "structure":
                    self.automation.RemoveStructureChangedEventHandler(element, handler)
                elif kind == "property":
                    self.automation.RemovePropertyChangedEventHandler(element, handler)
                else:
                    self.automation.RemoveAutomationEventHandler(int(kind.split(":")[1]), element, handler)
            except Exception:  # noqa: BLE001 - the window may be gone
                continue


_SHARED: EventHub | None = None
_SHARED_LOCK = threading.Lock()


def shared_hub() -> EventHub:
    """The process's one event hub (the thinker, the voice's hands and the view after actions share it)."""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = EventHub()
        return _SHARED
