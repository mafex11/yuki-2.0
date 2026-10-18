"""Place a window: another monitor, a half, maximised; give focus back; put it back (P1.4, 18 Oct free-thinker plan).

Until now no hand could move a window to the other monitor or hand the focus back to what the user had: the Discord
example (Discord opens maximised over the video that plays) had no fix but a blind Win+Shift+Arrow. These are the
generic verbs for it, all undoable:

* :func:`place_window` moves one window into a monitor's work area with ``SetWindowPos`` / ``ShowWindow``: no key,
  no click. ``monitor``: an index (``windows.list_monitors`` order, the same index the Situation says), ``"other"``
  (the other one of two; with more, the next one), ``"primary"``, or ``"with:<hwnd>"`` (the monitor that window is
  on). ``how``: ``keep`` (its own size, scaled by the monitors' DPI, at the same relative place, kept inside the work
  area; a maximised window stays maximised, a full-screen one fills the target monitor), ``maximize``, ``left`` /
  ``right`` (that half of the work area). A maximised window is restored without activation, moved, and maximised
  again (Windows maximises onto the monitor holding the window). Moving across monitors of different scaling, the app
  rescales itself on ``WM_DPICHANGED``; a second ``SetWindowPos`` then sets the size asked for. ``activate``: bring it
  to the front afterwards; when not asked and maximising activated it, the window that was in front gets the front
  back. Verified from Windows: ``MonitorFromWindow`` and the rectangle read back (``ok`` only when it is on the target
  monitor in the state asked). A monitor a game shows on (its top window, the game not in front) is never a target:
  ``"other"`` skips it, a named one is refused.
* :func:`focus_back` brings the window that was in front before Yuki's act forward again
  (:func:`yuki.actions.launch.focus_window`, which re-checks the game gate before every attempt).
* :func:`undo_last` ("put it back") reverses the newest group of the undo journal, newest entry first: a placement
  goes back to its rectangle, show state and monitor (and the front goes back when the placement took it); a focus
  goes back to the window in front before it; another act runs its own undo (:func:`journal_add`). A group is one
  act or one tidy-up: the entries given the same ``group`` (the caller's request id), and a focus given back within
  :data:`GROUP_S` of a placement joins that placement's group, so "move it and give the video the front" is put back
  whole. Refused (and kept for later: "after the round") with a game in front; refused (and kept: ``force=True`` puts
  it back anyway) when a placed window has moved since.

The game gate (D17, :mod:`yuki.actions.gate`): refused with a game in front, or when the target is a game's window,
checked before every ``SetWindowPos`` pass and every maximise. Windows of other threads are moved and shown
asynchronously (``SWP_ASYNCWINDOWPOS``, ``ShowWindowAsync``), so a hung app never blocks the hands; the read-back waits
for the change. Nothing here types, clicks or closes; nothing reads a title for meaning. The journal is RAM only
(:data:`JOURNAL_SIZE` entries, newest last); ``details`` and log facts are content-free (ids, app keys, monitors,
rectangles, states, never a title).
"""

from __future__ import annotations

import ctypes
import itertools
import threading
import time
from collections import deque
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import win32api
import win32con
import win32gui
import win32process

from yuki.actions import ActionResult
from yuki.actions import gate as _gate

_user32 = ctypes.windll.user32
#: A user32 of this module's own, so the argument types set here never change another module's calls.
_show_async = ctypes.WinDLL("user32").ShowWindowAsync
_show_async.argtypes = [wintypes.HWND, ctypes.c_int]
_show_async.restype = wintypes.BOOL

#: The ``how`` values :func:`place_window` takes.
HOWS = ("keep", "maximize", "left", "right")
#: Undo journal entries kept (RAM; newest last).
JOURNAL_SIZE = 8
#: A focus given back this soon after the newest placement (or focus) joins its group: one tidy-up, one "put it back".
GROUP_S = 10.0
#: How often a wait re-checks its condition, and the bound on a wait for a show state or a move to land.
_POLL_S = 0.01
_STATE_WAIT_S = 0.6
#: After a move landed, how long the rectangle may keep changing (the app answering ``WM_DPICHANGED``).
_SETTLE_S = 0.15
#: An edge this many px off the asked rectangle counts as "not there" (DPI rounding stays below).
_EDGE_PX = 8
#: Share of the asked rectangle the window must cover for a left/right placement to count as exact.
_EXACT_SHARE = 0.85
_SWP_FLAGS = win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_NOOWNERZORDER


@dataclass
class _Mon:
    """A monitor as placement needs it (index, rectangles, scaling): ``windows.MonitorInfo`` or a fake."""

    index: int
    rect: tuple[int, int, int, int]
    work: tuple[int, int, int, int]
    primary: bool = False
    dpi_scale: float = 1.0


@dataclass
class JournalEntry:
    """One undoable act: what to put back. ``kind`` ``place`` / ``focus`` (built in) or any other (``undo`` given)."""

    kind: str
    hwnd: int
    app: str = ""
    #: The window's own rectangle before (``GetWindowRect``), restored size when it was maximised.
    rect: tuple[int, int, int, int] | None = None
    state: str = ""
    monitor: int | None = None
    #: The window in front before the act, and whether the act took the front from it.
    foreground: int = 0
    took_front: bool = False
    at: float = 0.0
    undo: Callable[[], ActionResult] | None = None
    words: str = ""
    #: The act or tidy-up it belongs to (:func:`undo_last` puts a whole group back).
    group: Any = None
    #: Where the placement left it (``GetWindowRect``, monitor, state): when it is no longer there, the user moved it.
    after_rect: tuple[int, int, int, int] | None = None
    after_monitor: int | None = None
    after_state: str = ""
    #: A custom undo that neither sends input nor takes the front (a volume): allowed with a game in front.
    front_ok: bool = False

    def facts(self) -> dict[str, Any]:
        """Content-free: kind, ids, app key, monitor, state, age."""
        return {"kind": self.kind, "hwnd": self.hwnd, "app": self.app, "monitor": self.monitor, "state": self.state,
                "age_s": round(time.monotonic() - self.at, 1)}


_journal: deque[JournalEntry] = deque(maxlen=JOURNAL_SIZE)
_journal_lock = threading.Lock()
_auto_group = itertools.count(1)


def _push(entry: JournalEntry, group: Any = None, *, join_recent: bool = False) -> None:
    """Append ``entry`` in its group: the caller's ``group``; else, with ``join_recent``, the newest placement's or
    focus's group when that is younger than :data:`GROUP_S`; else a group of its own."""
    with _journal_lock:
        if group is not None:
            entry.group = group
        else:
            last = _journal[-1] if _journal else None
            if join_recent and last is not None and last.kind in ("place", "focus") and (
                    time.monotonic() - last.at) < GROUP_S:
                entry.group = last.group
            else:
                entry.group = ("auto", next(_auto_group))
        _journal.append(entry)


def journal_add(kind: str, *, hwnd: int, undo: Callable[[], ActionResult], words: str = "", app: str = "",
                group: Any = None, front_ok: bool = False) -> None:
    """Put another undoable act on the journal (a minimise, a volume, a tab shown): ``undo`` puts it back and returns
    an :class:`ActionResult`. :func:`undo_last` calls it (newest first, with the rest of its ``group``) after the game
    gate allowed it: with a game in front only when ``front_ok`` (the undo neither sends input nor takes the front);
    never when ``hwnd`` is a game's window."""
    _push(JournalEntry(kind=str(kind), hwnd=int(hwnd or 0), app=app, at=time.monotonic(), undo=undo, words=words,
                       front_ok=bool(front_ok)), group)


def journal() -> list[dict[str, Any]]:
    """The journal, newest last, content-free (:meth:`JournalEntry.facts`)."""
    with _journal_lock:
        return [e.facts() for e in _journal]


# ---------------------------------------------------------------------------
# Pure geometry (no OS): usable on fake monitors
# ---------------------------------------------------------------------------

def _as_mon(m: Any) -> _Mon:
    if isinstance(m, _Mon):
        return m
    get = (lambda k, d=None: m.get(k, d)) if hasattr(m, "get") else (lambda k, d=None: getattr(m, k, d))
    rect = get("rect") or get("bounds")
    work = get("work") or get("work_area") or rect
    return _Mon(index=int(get("index", 0)), rect=tuple(int(v) for v in rect), work=tuple(int(v) for v in work),
                primary=bool(get("primary", get("is_primary", False))), dpi_scale=float(get("dpi_scale", 1.0) or 1.0))


def resolve_monitor(monitor: int | str, current: int | None, monitors: Sequence[Any],
                    avoid: Iterable[int] = ()) -> tuple[int | None, str]:
    """The target monitor's index for a ``monitor`` value, ``(index, "")``, or ``(None, why not)``. Pure.

    An index (or its digits); ``"other"``: the other one of two, with more the next one after ``current`` (wrapping);
    ``"primary"`` / ``"main"``: the primary monitor. ``avoid``: monitors a game shows on: ``"other"`` skips them, a
    named one is refused. (``"with:<hwnd>"`` is resolved by the caller, who can read that window's monitor.)"""
    mons = sorted((_as_mon(m) for m in monitors), key=lambda m: m.index)
    if not mons:
        return None, "no monitor could be read"
    indexes = [m.index for m in mons]
    avoid = set(avoid or ())
    value = monitor.strip().casefold() if isinstance(monitor, str) else monitor
    if isinstance(value, bool):
        return None, f"monitor must be an index, 'other' or 'primary', not {monitor!r}"
    if isinstance(value, str) and value.lstrip("-").isdigit():
        value = int(value)
    if value == "other":
        if len(mons) < 2:
            return None, "there is only one monitor"
        if current not in indexes:
            order = [m.index for m in mons if not m.primary] + [m.index for m in mons if m.primary]
        else:
            at = indexes.index(current)
            order = [indexes[(at + k) % len(indexes)] for k in range(1, len(indexes))]
        free = [i for i in order if i not in avoid]
        if not free:
            return None, (f"monitor {order[0]} shows a game: no window goes over it" if len(order) == 1 else
                          "every other monitor shows a game: no window goes over one")
        return free[0], ""
    if value in ("primary", "main"):
        value = next((m.index for m in mons if m.primary), mons[0].index)
    if isinstance(value, int):
        if value not in indexes:
            return None, f"there is no monitor {value} (monitors: {', '.join(str(i) for i in indexes)})"
        if value in avoid and value != current:
            return None, f"monitor {value} shows a game: no window goes over it"
        return value, ""
    return None, f"monitor must be an index, 'other', 'primary' or 'with:<hwnd>', not {monitor!r}"


def target_frame(frame: Sequence[int], source: Any, target: Any, how: str) -> tuple[int, int, int, int]:
    """Where the window's visible frame should go on ``target`` (a monitor), coming from ``source``. Pure.

    ``left`` / ``right``: that half of the work area. ``maximize`` and ``keep``: its own size scaled by the two
    monitors' DPI ratio and kept inside the work area, at the same relative place it had in the source's work area
    (for ``maximize`` this is the restored rectangle it is maximised from)."""
    src, tgt = _as_mon(source), _as_mon(target)
    tl, tt, tr, tb = tgt.work
    tw, th = tr - tl, tb - tt
    if how == "left":
        return (tl, tt, tl + tw // 2, tb)
    if how == "right":
        return (tl + tw // 2, tt, tr, tb)
    sl, st, sr, sb = src.work
    sw, sh = max(sr - sl, 1), max(sb - st, 1)
    fl, ft, fr, fb = (int(v) for v in frame)
    scale = (tgt.dpi_scale or 1.0) / (src.dpi_scale or 1.0)
    w = min(max(int(round((fr - fl) * scale)), 1), tw)
    h = min(max(int(round((fb - ft) * scale)), 1), th)
    rx = min(max((fl - sl) / max(sw - (fr - fl), 1), 0.0), 1.0) if sw > (fr - fl) else 0.0
    ry = min(max((ft - st) / max(sh - (fb - ft), 1), 0.0), 1.0) if sh > (fb - ft) else 0.0
    left = tl + int(round(rx * (tw - w)))
    top = tt + int(round(ry * (th - h)))
    return (left, top, left + w, top + h)


def outer_rect(frame: Sequence[int], window_rect: Sequence[int], window_frame: Sequence[int]) -> tuple[int, int, int,
                                                                                                      int]:
    """The ``SetWindowPos`` rectangle that puts the visible frame at ``frame``: the frame grown by the window's
    invisible borders (``window_rect`` minus ``window_frame``, both read now). Pure."""
    bl = int(window_frame[0]) - int(window_rect[0])
    bt = int(window_frame[1]) - int(window_rect[1])
    br = int(window_rect[2]) - int(window_frame[2])
    bb = int(window_rect[3]) - int(window_frame[3])
    return (int(frame[0]) - bl, int(frame[1]) - bt, int(frame[2]) + br, int(frame[3]) + bb)


def _share(got: Sequence[int], want: Sequence[int]) -> float:
    area = max(0, want[2] - want[0]) * max(0, want[3] - want[1])
    if not area:
        return 0.0
    inter = max(0, min(got[2], want[2]) - max(got[0], want[0])) * max(0, min(got[3], want[3]) - max(got[1], want[1]))
    return inter / area


# ---------------------------------------------------------------------------
# OS reads and steps
# ---------------------------------------------------------------------------

def _monitors() -> list[_Mon]:
    from yuki.perception.windows import list_monitors

    return [_Mon(index=m.index, rect=tuple(m.bounds), work=tuple(m.work_area), primary=m.is_primary,
                 dpi_scale=float(m.dpi_scale)) for m in list_monitors()]


def monitor_index(hwnd: int, monitors: Sequence[_Mon] | None = None) -> int | None:
    """The index of the monitor Windows says the window is on (``MonitorFromWindow``, nearest), or ``None``."""
    try:
        handle = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
        bounds = tuple(int(v) for v in win32api.GetMonitorInfo(handle)["Monitor"])
    except Exception:  # noqa: BLE001
        return None
    for m in monitors if monitors is not None else _monitors():
        if tuple(m.rect) == bounds:
            return m.index
    return None


def _game_monitors(monitors: Sequence[_Mon], skip: int) -> set[int]:
    """The monitors a game shows on: a game's window among each monitor's windows from the top down to its first
    maximised or full-screen one (that one hides the rest). Fails closed: a monitor that could not be checked counts."""
    from yuki.memory.gamegate import window_game
    from yuki.perception.windows import list_windows

    out: set[int] = set()
    done: set[int] = set()
    try:
        infos = list_windows()   # Z-order, top first
    except Exception:  # noqa: BLE001
        return {m.index for m in monitors}
    for info in infos:
        if info.hwnd == skip or info.is_minimized:
            continue
        index = monitor_index(info.hwnd, monitors)
        if index is None or index in done:
            continue
        try:
            if window_game(info.hwnd):
                out.add(index)
        except Exception:  # noqa: BLE001
            out.add(index)
        if info.is_maximized or info.is_fullscreen:
            done.add(index)
    return out


def _frame(hwnd: int) -> tuple[int, int, int, int] | None:
    from yuki.perception.visibility import frame_bounds

    return frame_bounds(hwnd)


def _state(hwnd: int) -> str | None:
    from yuki.perception.windows import window_state

    return window_state(hwnd)


def _app(hwnd: int) -> str:
    try:
        from yuki.perception.windows import window_info

        info = window_info(hwnd)
        name = info.process_name if info else ""
    except Exception:  # noqa: BLE001
        name = ""
    return name[:-4] if name.casefold().endswith(".exe") else name


def _wait(condition: Callable[[], bool], timeout_s: float = _STATE_WAIT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while not condition():
        if time.monotonic() >= deadline:
            return condition()
        time.sleep(_POLL_S)
    return True


def _zoomed(hwnd: int) -> bool:
    try:
        return bool(_user32.IsZoomed(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _rect(hwnd: int) -> tuple[int, int, int, int]:
    return tuple(int(v) for v in win32gui.GetWindowRect(hwnd))  # type: ignore[return-value]


def _near(a: Sequence[int] | None, b: Sequence[int] | None) -> bool:
    return a is not None and b is not None and all(abs(int(x) - int(y)) < _EDGE_PX for x, y in zip(a, b))


def _other_thread(hwnd: int) -> bool:
    """Whether another thread owns the window (then a move or show is posted to it, never waited on)."""
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[0]) != int(win32api.GetCurrentThreadId())
    except Exception:  # noqa: BLE001
        return True


def _show(hwnd: int, cmd: int) -> None:
    if _other_thread(hwnd):
        _show_async(hwnd, cmd)
    else:
        win32gui.ShowWindow(hwnd, cmd)


def _set_rect(hwnd: int, rect: Sequence[int]) -> None:
    """Move and size the window; for another thread's window the request is posted and its landing waited on (a hung
    app times the wait out, it never blocks), then the rectangle let settle (the app's own ``WM_DPICHANGED`` resize)."""
    left, top, right, bottom = (int(v) for v in rect)
    if not _other_thread(hwnd):
        win32gui.SetWindowPos(hwnd, 0, left, top, max(right - left, 1), max(bottom - top, 1), _SWP_FLAGS)
        return
    start = _rect(hwnd)
    want = (left, top, right, bottom)
    win32gui.SetWindowPos(hwnd, 0, left, top, max(right - left, 1), max(bottom - top, 1),
                          _SWP_FLAGS | win32con.SWP_ASYNCWINDOWPOS)
    if not _wait(lambda: _near(_rect(hwnd), want) or _rect(hwnd) != start):
        return
    last, deadline = _rect(hwnd), time.monotonic() + _SETTLE_S
    while time.monotonic() < deadline:
        time.sleep(_POLL_S)
        now = _rect(hwnd)
        if now == last:
            return
        last = now


def _frame_to(hwnd: int, frame: Sequence[int], target: _Mon, monitors: Sequence[_Mon],
              details: dict[str, Any]) -> tuple[int, int, int, int] | None:
    """Put the visible frame at ``frame``; a second pass when a DPI change made the app resize itself. The game gate
    is asked before each pass. Returns the frame read back, or ``None`` when the gate refused a pass."""
    for _ in range(2):
        refused = _gate.refuse("window_place", hwnd=hwnd)
        if refused is not None:
            details["refused"] = _gate.kind_of(refused)
            return None
        rect, now = _rect(hwnd), _frame(hwnd)
        _set_rect(hwnd, outer_rect(frame, rect, now or rect))
        got = _frame(hwnd) or _rect(hwnd)
        if monitor_index(hwnd, monitors) == target.index and _near(got, frame):
            break
    return tuple(_frame(hwnd) or _rect(hwnd))  # type: ignore[return-value]


def _give_front_back(front: int, hwnd: int, details: dict[str, Any]) -> None:
    """When showing ``hwnd`` maximised took the front from ``front``, hand it back (gated like any focus)."""
    try:
        if front and front != hwnd and int(_user32.GetForegroundWindow() or 0) == hwnd and win32gui.IsWindow(front):
            from yuki.actions.launch import focus_window

            back = focus_window(front)
            details["front_given_back"] = bool(back.ok)
    except Exception:  # noqa: BLE001
        details["front_given_back"] = False


def _maximize(hwnd: int, details: dict[str, Any]) -> bool:
    refused = _gate.refuse("window_maximize", hwnd=hwnd)
    if refused is not None:
        details["refused"] = _gate.kind_of(refused)
        return False
    _show(hwnd, win32con.SW_MAXIMIZE)
    return _wait(lambda: _zoomed(hwnd))


def _restore_quietly(hwnd: int) -> bool:
    """Un-maximise without activating (``SW_SHOWNOACTIVATE``), waited on."""
    _show(hwnd, win32con.SW_SHOWNOACTIVATE)
    return _wait(lambda: not _zoomed(hwnd))


def _result(started: float, ok: bool, summary: str, details: dict[str, Any]) -> ActionResult:
    return ActionResult(ok=ok, summary=summary, details=details, elapsed_ms=(time.perf_counter() - started) * 1000.0)


def _mon_words(index: int | None, monitors: Sequence[_Mon]) -> str:
    if index is None:
        return "no monitor"
    m = next((m for m in monitors if m.index == index), None)
    return f"monitor {index}" + (" (main)" if m is not None and m.primary and len(monitors) > 1 else "")


def _note_after(entry: JournalEntry, hwnd: int, monitors: Sequence[_Mon]) -> None:
    """Where the placement left the window: what :func:`undo_last` checks it is still at."""
    try:
        entry.after_rect, entry.after_monitor = _rect(hwnd), monitor_index(hwnd, monitors)
        entry.after_state = _state(hwnd) or ""
    except Exception:  # noqa: BLE001 - gone: nothing to put back later either
        pass


def place_window(hwnd: int, *, monitor: int | str = "other", how: str = "keep", activate: bool = False,
                 group: Any = None) -> ActionResult:
    """Move one window to a monitor (module docstring) and verify it there. ``group``: the act or tidy-up it belongs
    to (the caller's request id), so one "put it back" undoes all of it. Never raises."""
    started = time.perf_counter()
    how = str(how or "keep").strip().casefold()
    how = {"maximise": "maximize", "maximized": "maximize", "maximised": "maximize"}.get(how, how)
    details: dict[str, Any] = {"hwnd": int(hwnd or 0), "how": how, "monitor_asked": str(monitor)}
    entry: JournalEntry | None = None
    state: str | None = None
    front = 0
    monitors: list[_Mon] = []
    try:
        hwnd = int(hwnd)
        if how not in HOWS:
            return _result(started, False, f"how must be one of {', '.join(HOWS)}, not {how!r}", details)
        if not hwnd or not win32gui.IsWindow(hwnd):
            return _result(started, False, f"[{hwnd}] is not a window (it may have closed)", details)
        from yuki.perception.windows import is_user_window

        if not is_user_window(hwnd):
            return _result(started, False, f"[{hwnd}] is not a window the user can switch to", details)
        refused = _gate.refuse("window_place", hwnd=hwnd)
        if refused is not None:
            details["refused"] = _gate.kind_of(refused)
            return _result(started, False, str(refused), details)
        app = _app(hwnd)
        details["app"] = app
        name = f"[{hwnd}] {app or 'window'}"
        monitors = _monitors()
        current = monitor_index(hwnd, monitors)
        details["from_monitor"] = current
        state = _state(hwnd)
        details["state_before"] = state
        if state == "minimized":
            return _result(started, False, f"{name} is minimised: restore it first, then place it", details)
        asked = monitor
        if isinstance(monitor, str) and monitor.strip().casefold().startswith("with:"):
            other = monitor.split(":", 1)[1].strip()
            if not other.isdigit() or not win32gui.IsWindow(int(other)):
                return _result(started, False, f"{monitor!r}: no such window to place it with", details)
            asked = monitor_index(int(other), monitors)
            if asked is None:
                return _result(started, False, f"{monitor!r}: that window's monitor could not be read", details)
        games = _game_monitors(monitors, hwnd) if len(monitors) > 1 else set()
        if games:
            details["game_monitors"] = sorted(games)
        index, why = resolve_monitor(asked, current, monitors, avoid=games)
        if index is None:
            return _result(started, False, f"{name}: {why}", details)
        details["to_monitor"] = index
        target = next(m for m in monitors if m.index == index)
        source = next((m for m in monitors if m.index == current), target)
        if state == "fullscreen" and how in ("left", "right"):
            return _result(started, False, f"{name} is full screen: it can only go whole to another monitor", details)
        want_max = how == "maximize" or (how == "keep" and state == "maximized")
        if index == current and how == "keep":
            return _result(started, True, f"{name} is already on {_mon_words(index, monitors)}", details)
        if index == current and how == "maximize" and state == "maximized":
            return _result(started, True, f"{name} is already maximised on {_mon_words(index, monitors)}", details)

        front = int(_user32.GetForegroundWindow() or 0)
        entry = JournalEntry(kind="place", hwnd=hwnd, app=app, rect=_rect(hwnd), state=state or "normal",
                             monitor=current, foreground=front, at=time.monotonic())
        if state == "maximized":
            if not _restore_quietly(hwnd):
                return _result(started, False, f"{name}: could not un-maximise it to move it", details)
            entry.rect = _rect(hwnd)   # the restored rectangle: where "put it back" goes
        _push(entry, group)
        if state == "fullscreen":
            got = _frame_to(hwnd, target.rect, target, monitors, details)
        else:
            frame = _frame(hwnd) or _rect(hwnd)
            got = _frame_to(hwnd, target_frame(frame, source, target, how), target, monitors, details)
        if got is None:   # a game came to the front between two passes: nothing more is done to the desk
            _note_after(entry, hwnd, monitors)
            return _result(started, False, f"{name}: stopped, a game came to the front (it is on "
                                           f"{_mon_words(monitor_index(hwnd, monitors), monitors)}; \"put it back\" "
                                           f"after the round)", details)
        if want_max and not _maximize(hwnd, details):
            if details.get("refused"):
                _note_after(entry, hwnd, monitors)
                return _result(started, False, f"{name}: moved to {_mon_words(index, monitors)}, but not maximised "
                                                f"(a game came to the front)", details)
        if activate:
            from yuki.actions.launch import focus_window

            focused = focus_window(hwnd)
            details["activated"] = bool(focused.ok)
        elif want_max:
            _give_front_back(front, hwnd, details)
        entry.took_front = bool(front and front != hwnd and int(_user32.GetForegroundWindow() or 0) == hwnd)
        _note_after(entry, hwnd, monitors)

        # verified from Windows: the monitor it is on and the state it is in
        now_monitor = monitor_index(hwnd, monitors)
        now_state = _state(hwnd) or "gone"
        got = tuple(_frame(hwnd) or got)
        details.update(to_monitor_seen=now_monitor, state_after=now_state, rect_after=got)
        on_target = now_monitor == index
        if want_max:
            right_state = now_state == "maximized"
        elif state == "fullscreen":
            right_state = now_state == "fullscreen"
        else:
            right_state = now_state == "normal"
        exact = True
        if how in ("left", "right"):
            exact = _share(got, target_frame(got, target, target, how)) >= _EXACT_SHARE
        details["exact"] = exact
        where = _mon_words(now_monitor, monitors)
        shown = {"maximized": "maximised", "fullscreen": "full screen", "normal": ""}.get(now_state, now_state)
        if how in ("left", "right") and on_target:
            shown = f"on the {how} half" + ("" if exact else " (the app kept its own size)")
        tail = f", {shown}" if shown else ""
        if on_target and right_state:
            return _result(started, True, f"{name} is now on {where}{tail}", details)
        return _result(started, False, f"{name}: asked for {_mon_words(index, monitors)}"
                                       f"{', maximised' if want_max else ''}; it is on {where}{tail}", details)
    except Exception as exc:  # noqa: BLE001 - a window that closed mid-way, an access denied by the app
        details["error"] = type(exc).__name__
        if entry is not None:   # the OS steps had begun: a window that was maximised is maximised again
            try:
                if state == "maximized" and win32gui.IsWindow(hwnd) and not _zoomed(hwnd) and _maximize(hwnd, details):
                    _give_front_back(front, hwnd, details)
                _note_after(entry, hwnd, monitors)
            except Exception:  # noqa: BLE001
                pass
        return _result(started, False, f"[{hwnd}]: could not place it ({type(exc).__name__})", details)


def focus_back(hwnd: int, *, group: Any = None) -> ActionResult:
    """Bring ``hwnd`` (the window in front before Yuki's act) to the front again: :func:`launch.focus_window` (gated:
    never with a game in front, never into a game). Journaled in ``group``, else in the group of a placement made
    within :data:`GROUP_S`, so :func:`undo_last` puts the front back with that placement."""
    started = time.perf_counter()
    details: dict[str, Any] = {"hwnd": int(hwnd or 0), "focus_back": True}
    try:
        hwnd = int(hwnd)
        if not hwnd or not win32gui.IsWindow(hwnd):
            return _result(started, False, f"[{hwnd}] is not a window (it may have closed)", details)
        front = int(_user32.GetForegroundWindow() or 0)
        if front == hwnd:
            return _result(started, True, f"[{hwnd}] {_app(hwnd) or 'window'} is already in front", details)
        from yuki.actions.launch import focus_window

        result = focus_window(hwnd)
        details.update({k: v for k, v in (result.details or {}).items() if k in ("refused", "ready", "ready_ms")})
        if result.ok:
            _push(JournalEntry(kind="focus", hwnd=hwnd, app=_app(hwnd), foreground=front, took_front=True,
                               at=time.monotonic()), group, join_recent=True)
            return _result(started, True, f"[{hwnd}] {_app(hwnd) or 'window'} is in front again", details)
        return _result(started, False, result.summary, details)
    except Exception as exc:  # noqa: BLE001
        details["error"] = type(exc).__name__
        return _result(started, False, f"[{hwnd}]: could not bring it back to the front ({type(exc).__name__})",
                       details)


def _refusal(entry: JournalEntry) -> Any:
    """The game gate's answer for putting ``entry`` back (``None`` = allowed)."""
    if entry.undo is not None:
        return _gate.refuse("journal_undo", hwnd=entry.hwnd or None, front_ok=entry.front_ok)
    if entry.kind == "focus":
        return _gate.refuse("window_focus", hwnd=entry.foreground or None)
    return _gate.refuse("window_place", hwnd=entry.hwnd)


def _moved_since(entry: JournalEntry, monitors: Sequence[_Mon]) -> bool:
    """Whether a placed window is no longer where the placement left it (the user moved it since)."""
    if entry.kind != "place" or entry.undo is not None or entry.after_rect is None:
        return False
    if not win32gui.IsWindow(entry.hwnd):
        return False
    return (monitor_index(entry.hwnd, monitors) != entry.after_monitor
            or (_state(entry.hwnd) or "") != entry.after_state or not _near(_rect(entry.hwnd), entry.after_rect))


def _undo_one(entry: JournalEntry, details: dict[str, Any]) -> ActionResult:
    started = time.perf_counter()
    if entry.undo is not None:
        result = entry.undo()
        if not isinstance(result, ActionResult):
            return _result(started, False, f"{entry.kind}: its undo gave no result", details)
        details.update(result.details or {})
        return result
    if entry.kind == "focus":
        if not entry.foreground or not win32gui.IsWindow(entry.foreground):
            return _result(started, False, "the window that was in front before is gone", details)
        from yuki.actions.launch import focus_window

        result = focus_window(entry.foreground)
        details.update({k: v for k, v in (result.details or {}).items() if k == "refused"})
        return _result(started, result.ok, f"[{entry.foreground}] {_app(entry.foreground) or 'window'} is in "
                                           f"front again" if result.ok else result.summary, details)
    hwnd = entry.hwnd
    name = f"[{hwnd}] {entry.app or 'window'}"
    if not win32gui.IsWindow(hwnd):
        return _result(started, False, f"{name} is gone: nothing to put back", details)
    refused = _gate.refuse("window_place", hwnd=hwnd)
    if refused is not None:
        details["refused"] = _gate.kind_of(refused)
        return _result(started, False, str(refused), details)
    if _state(hwnd) == "maximized" and not _restore_quietly(hwnd):
        return _result(started, False, f"{name}: could not un-maximise it to put it back", details)
    if entry.rect is not None:
        refused = _gate.refuse("window_place", hwnd=hwnd)
        if refused is not None:
            details["refused"] = _gate.kind_of(refused)
            return _result(started, False, str(refused), details)
        _set_rect(hwnd, entry.rect)
    if entry.state == "maximized":
        _maximize(hwnd, details)
    if entry.took_front and entry.foreground and win32gui.IsWindow(entry.foreground):
        from yuki.actions.launch import focus_window

        details["front_back"] = bool(focus_window(entry.foreground).ok)
    elif entry.state == "maximized":
        _give_front_back(entry.foreground, hwnd, details)
    monitors = _monitors()
    now_monitor = monitor_index(hwnd, monitors)
    now_state = _state(hwnd) or "gone"
    details.update(to_monitor_seen=now_monitor, state_after=now_state)
    ok = now_monitor == entry.monitor and now_state == (entry.state or "normal")
    shown = {"maximized": ", maximised", "fullscreen": ", full screen"}.get(now_state, "")
    return _result(started, ok, (f"{name} is back on {_mon_words(now_monitor, monitors)}{shown}" if ok else
                                 f"{name}: put back, but it is on {_mon_words(now_monitor, monitors)}{shown}"),
                   details)


def undo_last(*, force: bool = False) -> ActionResult:
    """Put back the newest group of undoable acts ("put it back"), newest first: placements (rectangle, show state,
    monitor, and the front when a placement took it), focuses (the front before), other acts' own undos.

    Nothing is done, and the group stays on the journal, when the game gate refuses any of it (``details["refused"]``:
    "after the round" works), or when a placed window has moved since (``details["stale"]``; ``force=True`` puts it
    back anyway). Entries a refusal stops mid-way stay on the journal too."""
    started = time.perf_counter()
    with _journal_lock:
        batch: list[JournalEntry] = []
        if _journal:
            group = _journal[-1].group
            while _journal and _journal[-1].group == group:
                batch.append(_journal.pop())   # newest first

    def keep(rest: list[JournalEntry]) -> None:
        with _journal_lock:
            for e in reversed(rest):
                _journal.append(e)

    if not batch:
        return _result(started, False, "nothing to put back: no placement or focus of mine is on record", {})
    details: dict[str, Any] = {"undo": [e.kind for e in batch], "hwnd": batch[0].hwnd}
    try:
        for e in batch:
            refused = _refusal(e)
            if refused is not None:
                keep(batch)
                details["refused"] = _gate.kind_of(refused)
                return _result(started, False, str(refused), details)
        if not force:
            monitors = _monitors()
            seen: set[int] = set()
            for e in batch:   # each window's newest placement is where the group left it
                if e.kind != "place" or e.hwnd in seen:
                    continue
                seen.add(e.hwnd)
                if _moved_since(e, monitors):
                    keep(batch)
                    details["stale"] = True
                    return _result(started, False, f"[{e.hwnd}] {e.app or 'window'} has moved since I placed it: "
                                                   f"not put back (say so and I put it back anyway)", details)
        said: list[str] = []
        ok = True
        for i, e in enumerate(batch):
            result = _undo_one(e, details)
            said.append(result.summary)
            ok = ok and bool(result.ok)
            if not result.ok and details.get("refused"):
                if e.kind == "place" and e.undo is None:   # part put back: "after the round" starts from here
                    _note_after(e, e.hwnd, _monitors())
                keep(batch[i:])
                break
        return _result(started, ok, "; ".join(s for s in said if s), details)
    except Exception as exc:  # noqa: BLE001
        details["error"] = type(exc).__name__
        return _result(started, False, f"could not put it back ({type(exc).__name__})", details)


__all__ = ["GROUP_S", "HOWS", "JOURNAL_SIZE", "JournalEntry", "focus_back", "journal", "journal_add",
           "monitor_index", "outer_rect", "place_window", "resolve_monitor", "target_frame", "undo_last"]
