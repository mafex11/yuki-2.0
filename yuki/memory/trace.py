"""The action trace (report 78 §4, phase C1, shadow): what the user does in each app, recorded once they say yes.

Nothing reads the trace to change Yuki's behaviour yet. It is the record later phases learn from: which app, which
button or field, what kind of action, what changed, the shortcut, the pointer's travel and rests; never the letters
typed, never what was copied, never pixels (decision D2 a: a window UI Automation cannot read, like Warp or FL
Studio, gives a point-only click plus the app's own data), never while a game runs.

Threads (all in the memory service, below-normal priority):

* the **Raw Input reader** (presence's, :mod:`yuki.memory.userpresence`; one per process) feeds
  :class:`yuki.memory.trace_input.TraceInput`, which classifies and queues small content-free items;
* the **hook thread** (:mod:`yuki.memory.trace_hooks`): window events, the clipboard, the Recent folder;
* the **trace worker** (:class:`TraceWorker`, its own thread, COM apartment and UI Automation client,
  :mod:`yuki.memory.trace_uia`): the gates, element identity at button-down, the outcome, the row, batched
  encrypted writes (:mod:`yuki.memory.trace_store`) at most every :data:`FLUSH_S`.

For each action, in order (report 78 §4.2):

1. **the hard game gate, then privacy** (:mod:`yuki.memory.trace_privacy`): the consent switch, the tray pause, the
   apps left out, blocked apps, private windows, blocked sites, Yuki's own windows; a password field fails closed.
   Dropped actions leave only a content-free counter.
2. **identity, taken at button-down**: (i) an ``INVOKED`` / ``SELECTION`` / ``FOCUS`` event from the same process
   between button-down and :data:`IDENTITY_S` after button-up whose element contains the point; (ii)
   ``ElementFromPoint`` asked as soon as the button went down; (iii) for a renderer's whole pane, the smallest element
   under the point; else point-only (``unresolved``). A structure change (shown, hidden, reordered, a menu closed,
   another window in front) between button-down and the answer makes it ``stale`` (decided per action as the events
   come, so a busy app's later events cannot push it out).
3. **the outcome**: the events of the outcome hooks until :data:`OUTCOME_S` after the action (or the next action),
   at most :data:`yuki.memory.trace_hooks.OUTCOME_CAP`, as a class (``changed`` / ``nothing`` / ``unverifiable``),
   a content-free summary (with up to :data:`OUTCOME_ELEMENTS` changed elements by role, parent role and position,
   from spare lookups only), and the machine-checked facts C1 can record (``toggle_state``, ``selected``,
   ``page_reached``, ``window_verified``, ``field_submitted`` (a length only), ``file_saved`` / ``file_opened``,
   ``command_ok``).
4. **the row**: time, app, page (``page_key``), what was on screen (``capture_id``), the source (``user`` /
   ``user_injected`` / ``yuki``) and whether a Yuki run was in progress, travel and gap, then batched.

Files: a new link in the Recent folder (the shell writes one for a file opened or saved through the common dialogs
or Explorer); one picked in an open/save dialog is that dialog's app's (``via=dialog``); Ctrl+S with no dialog
(FL Studio's usual save) re-checks the files opened or saved lately by their own write time (``via=save_chord``).
Nothing from a span while the trace was blocked (off, paused, an off hour, a game) becomes a row later: the controller
queues a ``reopen`` mark on each opening and the worker passes over what was written meanwhile.

Budgets (report 78 §4.3): at most :data:`LOOKUPS_PER_S` lookups a second, each within
:data:`yuki.memory.trace_uia.TIMEOUT_MS`; over budget the action is kept point-only. Hovers and scrolls are looked up
only with budget to spare (clicks first). Logs are content-free: counts, kinds, ms and reason codes; an exception
logs its type and line only.
"""

from __future__ import annotations

import dataclasses
import json
import os
import queue
import threading
import time
import traceback
from collections import Counter, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from yuki.memory import trace_store
from yuki.memory.trace_hooks import (
    EVENT_OBJECT_FOCUS, EVENT_OBJECT_HIDE, EVENT_OBJECT_INVOKED, EVENT_OBJECT_NAMECHANGE, EVENT_OBJECT_REORDER,
    EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD, EVENT_OBJECT_SHOW, EVENT_OBJECT_STATECHANGE,
    EVENT_OBJECT_VALUECHANGE, EVENT_SYSTEM_DIALOGSTART, EVENT_SYSTEM_MENUEND, EVENT_SYSTEM_MENUPOPUPEND,
    EVENT_SYSTEM_MENUPOPUPSTART, OUTCOME_S,
)
from yuki.memory.trace_keys import NAMED_KEY, RUN_KEYS
from yuki.memory.trace_privacy import COMMAND, CONTENT, NAME_CHARS, TITLE, TraceRules, gate, name_class, normalise_name

#: Lookups a second at most (a token bucket), and how many the low-priority ones (hover, scroll, outcome
#: elements, a click's event path when the point already answered) leave untouched for clicks.
LOOKUPS_PER_S = 4.0
RESERVE_FOR_CLICKS = 1.0
#: A lookup's own budget (s), on top of UI Automation's timeouts.
LOOKUP_S = 0.15
#: How long after button-up an event may still name the clicked element (s).
IDENTITY_S = 0.3
#: Writes are batched: at most this often (s).
FLUSH_S = 2.0
#: A typing run ends after this long without a character (s).
TYPE_GAP_S = 2.0
#: Repeats of the same run key or chord within this long fold into one row (s); wheel notches likewise.
FOLD_S = 1.0
#: A foreground change or a copy counts as the user's (a switch row, a copy row) only within this long of their
#: input (s); a file from the Recent folder within :data:`FILE_INPUT_S`.
INPUT_LINK_S = 1.5
FILE_INPUT_S = 30.0
#: An open hover is closed (its dwell capped) after this long (s).
HOVER_MAX_S = 60.0
#: A press whose release never came is closed after this long (s).
LOST_UP_S = 10.0
#: A privacy margin for the race between a key or click that moves the focus (Tab, Enter, a click) and the app's
#: ``FOCUS`` event (s): a typing run that starts within this long of such an input, before a ``FOCUS`` event came,
#: has its field read again this long after it started, and a run a ``FOCUS`` event lands on within this long of its
#: start is dropped (its characters may belong to the next field, a password box). Fails closed.
FOCUS_RACE_S = 0.5
#: A save chord re-checks the files that app opened or saved this long after the chord (s): their own write time.
SAVE_CHECK_S = 1.5
#: Files remembered (in memory only, any app: Explorer often opens what another app then saves) for a save chord's
#: re-check.
KNOWN_FILES = 8
#: Roles whose answer to ``ElementFromPoint`` is a whole renderer pane (try the descent, path iii) when it covers
#: most of the window.
PANE_ROLES = frozenset({"Pane", "Document", "Window", "Custom", "Group"})
#: Roles typing goes into (a click on one can name the field a typing run went into).
EDIT_ROLES = frozenset({"Edit", "Document", "ComboBox"})
#: Standard paste accelerators (Windows' own; machine identities of a shortcut).
PASTE_CHORDS = frozenset({"Ctrl+V", "Shift+Insert"})
#: The standard Save accelerator (Windows' own): re-checks the files known to be open for a save without a dialog.
SAVE_CHORDS = frozenset({"Ctrl+S"})
STRUCTURE_EVENTS = frozenset({EVENT_OBJECT_SHOW, EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER, EVENT_SYSTEM_MENUEND,
                              EVENT_SYSTEM_MENUPOPUPEND})
IDENTITY_EVENTS = (EVENT_OBJECT_INVOKED, EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD, EVENT_OBJECT_FOCUS)
#: Changed elements an outcome resolves to role and position at most (report 78 §4.2 step 3).
OUTCOME_ELEMENTS = 5

_BUTTON_KIND = {"left": "click", "right": "rclick", "middle": "click", "x1": "click", "x2": "click"}


class _Bucket:
    def __init__(self, rate: float, clock: Callable[[], float]) -> None:
        self.rate = rate
        self.tokens = rate
        self._clock = clock
        self._at = clock()

    def _fill(self) -> None:
        now = self._clock()
        self.tokens = min(self.rate, self.tokens + (now - self._at) * self.rate)
        self._at = now

    def take(self, keep: float = 0.0) -> bool:
        self._fill()
        if self.tokens - 1.0 < keep - 1e-9:
            return False
        self.tokens -= 1.0
        return True


@dataclass
class _Window:
    pid: int = 0
    hwnd: int = 0
    process: str = ""        # the process key (app_key: lower case, no .exe), as threads.process
    image: str = ""          # the image name ("chrome.exe"), as the privacy file names apps
    app: str = ""
    own: bool = False
    started: float | None = None
    title: str = ""
    page_key: str | None = None
    url: str | None = None
    withheld: str | None = None


@dataclass(eq=False)
class _Act:
    kind: str
    t: float
    source: str
    win: _Window
    x: int | None = None
    y: int | None = None
    button: str | None = None
    chord: str | None = None
    n: int = 1
    typed: int | None = None
    travel: int | None = None
    gap_ms: int | None = None
    during_run: bool = False
    el: Any = None
    resolved: str = "unresolved"
    lookup_ms: float | None = None
    lookup_end: float | None = None
    end_el: Any = None
    end_x: int | None = None
    end_y: int | None = None
    up_t: float | None = None
    open: bool = False
    last_t: float = 0.0
    events: list[tuple] = field(default_factory=list)
    outs: list[tuple] = field(default_factory=list)
    fg_change: tuple | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    via: str | None = None
    clip_seq: int | None = None
    clip_formats: list[str] | None = None
    drop: str | None = None
    dwell_ms: int | None = None
    file: dict[str, Any] | None = None
    command: dict[str, Any] | None = None
    after_type: dict[str, Any] | None = None
    stale: bool = False
    recheck_at: float | None = None
    save_check: bool = False


class TraceWorker:
    """The worker's logic, driven by :meth:`handle` (one queue item) and :meth:`tick` (deadlines). Tests drive it
    synchronously with fakes; :class:`Trace` runs it on its own thread.

    Args:
        store: the memory :class:`~yuki.memory.store.Store` (``cipher``, ``_enc``, ``_write``...).
        privacy_rules: ``() -> PrivacyRules`` (the watcher's gates).
        trace_rules: ``() -> TraceRules``.
        gate: ``() -> bool``: the hard game gate holds (``gamegate.gate_holds``).
        acting: ``() -> Acting | None``: a Yuki run in progress.
        page: ``(pid) -> (page_key, url, withheld)`` as the timeline sees the window in front of that process.
        process: ``(pid) -> (process key, image name, app name, own, started)``.
        window_at: ``(x, y) -> (hwnd, pid)``; ``foreground``: ``() -> (hwnd, pid)``; ``title``: ``(hwnd) -> str``;
        ``rect``: ``(hwnd) -> (l, t, r, b)``.
        uia: a :class:`~yuki.memory.trace_uia.TraceUia` (or a fake; ``None``: point-only).
        sink: the :class:`~yuki.memory.trace_input.TraceInput` (its rest anchor and ``moving``), or None.
        cursor: ``() -> (x, y)`` physical; ``clipboard``: ``() -> (seq, formats, private)``.
        recent: the Recent folder, ``resolve``: ``(lnk path) -> target path | None``, ``flp``: ``(path) -> dict``.
        log: ``log(type, **fields)``, content-free.
    """

    def __init__(self, store: Any, *, privacy_rules: Callable[[], Any], trace_rules: Callable[[], TraceRules],
                 gate: Callable[[], bool], acting: Callable[[], Any] = lambda: None,
                 page: Callable[[int], tuple[str | None, str | None, str | None]] = lambda pid: (None, None, None),
                 process: Callable[[int], tuple[str, str, str, bool, float | None]] | None = None,
                 window_at: Callable[[int, int], tuple[int, int]] | None = None,
                 foreground: Callable[[], tuple[int, int]] | None = None,
                 title: Callable[[int], str] = lambda hwnd: "",
                 rect: Callable[[int], tuple[int, int, int, int] | None] = lambda hwnd: None,
                 uia: Any = None, sink: Any = None, cursor: Callable[[], tuple[int, int]] | None = None,
                 clipboard: Callable[[], tuple[int, tuple[str, ...], bool]] | None = None,
                 recent: Path | None = None, resolve: Callable[[str], str | None] | None = None,
                 flp: Callable[[str], dict[str, Any]] | None = None, log: Callable[..., None] | None = None,
                 clock: Callable[[], float] = time.monotonic, wall: Callable[[], float] = time.time,
                 hover: tuple[int, int, float] | None = None) -> None:
        self.store = store
        self._privacy = privacy_rules
        self._rules = trace_rules
        self._gate = gate
        self._acting = acting
        self._page = page
        self._process = process or _process_facts()
        self._window_at = window_at or _window_at
        self._foreground = foreground or _foreground
        self._title = title
        self._rect = rect
        self.uia = uia
        self.sink = sink
        self._cursor = cursor
        self._clipboard = clipboard
        self._recent = recent
        self._recent_seen = wall()
        self._resolve = resolve or resolve_link
        self._flp = flp or _read_flp
        self._log_fn = log
        self._clock = clock
        self._wall = wall
        self._hover_rect = hover        # (w, h, seconds): Windows' hover settings (from the sink's metrics)
        self._bucket = _Bucket(LOOKUPS_PER_S, clock)
        self._acts: list[_Act] = []
        self._open: dict[str, _Act] = {}          # button -> press waiting for its release
        self._type: _Act | None = None
        self._hover: _Act | None = None
        self._hover_due: float | None = None
        self._hover_seq = -1
        self._last_hover_xy: tuple[int, int] | None = None
        self._last_hover_t = float("-inf")
        self._focus: Any = None                   # the focused element, until a focus event or a window change
        self._focus_valid = False
        self._last_click_edit: tuple[float, Any, int] | None = None
        self._last_focus_ev = float("-inf")
        self._focus_move_t = float("-inf")       # the last input that may move the focus (a non-run key, a press)
        self._opened_at = wall()                 # wall time the trace last went from blocked to open
        self._was_blocked = False
        self._dialog: tuple[float, int, _Window] | None = None   # an open/save dialog in front: (t, hwnd, owner)
        self._dialog_closed: tuple[float, _Window] | None = None
        self._known_files: deque[tuple[str, str]] = deque(maxlen=KNOWN_FILES)   # recent (path, digest), memory only
        self._last_action_t = float("-inf")
        self._last_input: tuple[float, str] | None = None
        self._last_key: _Act | None = None
        self._last_wheel: _Act | None = None
        self._fg = _Window()
        self._recent_structure: deque[tuple[float, int]] = deque(maxlen=64)
        self._batch: list[dict[str, Any]] = []
        self._elements: list[dict[str, Any]] = []
        self._files: list[dict[str, Any]] = []
        self._last_flush = clock()
        self._pending_dclick: _Act | None = None
        self.counters: Counter[str] = Counter()
        self.lookup_ms: deque[float] = deque(maxlen=500)
        self._started = clock()

    # -- small helpers ----------------------------------------------------------------------------------------------

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def _error(self, where: str, exc: BaseException) -> None:
        """Content-free: the exception's type and the code line, never its message, locals or the input."""
        self.counters["errors"] += 1
        tb = traceback.extract_tb(exc.__traceback__)
        line = f"{Path(tb[-1].filename).name}:{tb[-1].lineno}" if tb else ""
        self._log("error", where=f"trace.{where}", error=type(exc).__name__, line=line)

    def _at(self, t: float) -> float:
        """Epoch time of a monotonic time."""
        return self._wall() - (self._clock() - t)

    def _digest(self, *parts: str) -> str:
        return self.store.cipher.digest(*parts)

    def _enc(self, text: str | None) -> str | None:
        return None if text is None else self.store._enc(text)

    def _window(self, hwnd: int, pid: int) -> _Window:
        try:
            process, image, app, own, started = self._process(pid) if pid else ("", "", "", False, None)
        except Exception:  # noqa: BLE001
            process, image, app, own, started = "", "", "", False, None
        try:
            page_key, url, withheld = self._page(pid)
        except Exception:  # noqa: BLE001
            page_key, url, withheld = None, None, None
        return _Window(pid=pid, hwnd=hwnd, process=process, image=image, app=app, own=own, started=started,
                       title=self._title(hwnd) if hwnd else "", page_key=page_key, url=url, withheld=withheld)

    def _privacy_reason(self, win: _Window) -> str | None:
        if not win.process:
            return "unknown_app"
        try:
            rules = self._privacy()
        except Exception:  # noqa: BLE001
            return "privacy_error"
        return gate(rules, self._rules(), process=win.image or win.process, app=win.app, own=win.own,
                    title=win.title, url=win.url, withheld=win.withheld)

    def _blocked(self) -> str | None:
        """Why nothing may be recorded right now (consent off or the game gate), or None."""
        try:
            if not self._rules().record_actions:
                return "off"
        except Exception:  # noqa: BLE001
            return "off"
        try:
            if self._gate():
                return "game"
        except Exception:  # noqa: BLE001
            return "game"
        return None

    def _take(self, *, priority: bool) -> bool:
        ok = self._bucket.take(keep=0.0 if priority else RESERVE_FOR_CLICKS)
        if not ok:
            self.counters["lookup_over_budget"] += 1
        return ok

    def _lookup(self, fn: Callable[..., Any], *args: Any, priority: bool, **kw: Any) -> Any:
        """One UI Automation lookup within budget (never while the gate holds); None when over budget or failed."""
        if self.uia is None or self._blocked() is not None or not self._take(priority=priority):
            return None
        t0 = time.perf_counter()
        try:
            out = fn(*args, budget_s=LOOKUP_S, **kw)
        except Exception as exc:  # noqa: BLE001
            self._error("lookup", exc)
            out = None
        ms = (time.perf_counter() - t0) * 1000.0
        self.lookup_ms.append(ms)
        self.counters["lookups"] += 1
        return out

    def _input(self, t: float, source: str) -> None:
        self._last_input = (t, source)

    def _gap(self, t: float) -> int | None:
        last = self._last_action_t
        self._last_action_t = t
        return None if last == float("-inf") else int(round((t - last) * 1000))

    def _during_run(self) -> bool:
        try:
            return self._acting() is not None
        except Exception:  # noqa: BLE001
            return False

    def _new_act(self, kind: str, t: float, source: str, win: _Window, **kw: Any) -> _Act:
        act = _Act(kind=kind, t=t, source=source, win=win, during_run=self._during_run(), last_t=t, **kw)
        act.drop = self._privacy_reason(win)
        self._acts.append(act)
        return act

    # -- items -------------------------------------------------------------------------------------------------------

    def handle(self, item: tuple) -> None:
        """One queue item (see :mod:`yuki.memory.trace_input` and :mod:`yuki.memory.trace_hooks`)."""
        kind = item[0]
        try:
            if kind == "reopen":
                self._on_reopen(*item[1:])
                return
            blocked = self._blocked()
            if blocked is not None:
                self._was_blocked = True
                if kind != "fg":
                    self.counters["dropped_" + blocked] += 1
                    return
            elif self._was_blocked:
                self._on_reopen(self._wall())
            getattr(self, "_on_" + kind)(*item[1:])
        except Exception as exc:  # noqa: BLE001 - one bad item never stops the trace
            self._error(kind, exc)

    def _on_reopen(self, wall: float) -> None:
        """The trace went from blocked (off, paused, an off hour, a game) to open: nothing from the blocked span may
        become a row later, so the Recent folder's links written meanwhile are passed over, and half-built actions,
        rests and runs from before are dropped."""
        self._was_blocked = False
        self._opened_at = max(self._opened_at, float(wall))
        self._recent_seen = max(self._recent_seen, float(wall))
        self._dialog = self._dialog_closed = None
        self._type = self._hover = self._pending_dclick = self._last_key = self._last_wheel = None
        self._open.clear()
        self._hover_due = None
        self._focus_valid = False
        if self._acts:
            self.counters["dropped_reopen"] += len(self._acts)
            self._acts = []
        self._last_input = None

    def _on_down(self, t: float, button: str, x: int, y: int, source: str, travel: int, dbl: bool) -> None:
        self._close_type(t)
        self._close_hover(t)
        self._input(t, source)
        self._focus_valid = False                          # a press may move the focus
        self._focus_move_t = t
        if dbl and self._pending_dclick is not None and self._pending_dclick.button == button:
            prev = self._pending_dclick
            prev.kind = "dclick"
            prev.n = 2
            self._pending_dclick = None
            self._open[button] = prev                      # its release ends it again
            prev.open = True
            return
        hwnd, pid = self._window_at(x, y)
        win = self._window(hwnd, pid)
        act = self._new_act(_BUTTON_KIND.get(button, "click"), t, source, win, x=x, y=y, button=button,
                            travel=travel, gap_ms=self._gap(t), open=True)
        self._open[button] = act
        if act.drop is None:
            el = self._lookup(self.uia.at_point if self.uia else None, x, y, priority=True, index=True)
            act.lookup_end = self._clock()
            if el is not None:
                act.el, act.resolved, act.lookup_ms = el, "point", el.ms
                if el.role in EDIT_ROLES:
                    self._last_click_edit = (t, el, hwnd)   # typing next goes into it (a password field: dropped)
                if el.role in PANE_ROLES and self._covers_window(el, hwnd):
                    deeper = self._lookup(self.uia.descend, el, x, y, priority=False)
                    if deeper is not None:
                        act.el, act.resolved, act.lookup_ms = deeper, "snapshot", (act.lookup_ms or 0) + deeper.ms
                        act.lookup_end = self._clock()

    def _covers_window(self, el: Any, hwnd: int) -> bool:
        rect = self._rect(hwnd) if hwnd else None
        if not rect or not el.bounds:
            return el.role in ("Pane", "Document")
        area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
        return el.area() * 2 >= area

    def _on_up(self, t: float, button: str, x: int, y: int, source: str, drag: bool) -> None:
        act = self._open.pop(button, None)
        if act is None:
            return
        act.open = False
        act.up_t = act.last_t = t
        if act.kind in ("click", "dclick") and button == "left":
            self._pending_dclick = act if act.kind == "click" else None
        if drag:
            act.kind = "drag"
            act.end_x, act.end_y = x, y
            act.travel = (act.travel or 0) + int(round(abs(x - (act.x or x)) + abs(y - (act.y or y))))
            if act.drop is None and self.uia is not None:
                act.end_el = self._lookup(self.uia.at_point, x, y, priority=True)

    def _on_wheel(self, t: float, notches: int, horizontal: bool, x: int, y: int, source: str, travel: int) -> None:
        self._close_type(t)
        self._close_hover(t)
        self._input(t, source)
        direction = ("right" if notches > 0 else "left") if horizontal else ("up" if notches > 0 else "down")
        last = self._last_wheel
        hwnd, pid = self._window_at(x, y)
        if (last is not None and any(a is last for a in self._acts) and t - last.last_t <= FOLD_S
                and last.chord == direction
                and last.win.hwnd == hwnd and last.source == source):
            last.n += abs(notches)
            last.last_t = t
            return
        win = self._window(hwnd, pid)
        act = self._new_act("scroll", t, source, win, x=x, y=y, chord=direction, n=abs(notches), travel=travel,
                            gap_ms=self._gap(t))
        self._last_wheel = act
        if act.drop is None and self.uia is not None:
            el = self._lookup(self.uia.at_point, x, y, priority=False)
            act.lookup_end = self._clock()
            if el is not None:
                act.el, act.resolved, act.lookup_ms = el, "point", el.ms

    def _focused(self, *, priority: bool) -> Any:
        """The focused element: the cached one while no focus event, window change, press or focus-moving key came
        since, else the field just clicked (only while no focus event came after that click), else a lookup."""
        if self._focus_valid:
            return self._focus
        click = self._last_click_edit
        if (click is not None and click[1] is not None and click[0] > self._last_focus_ev
                and click[0] >= self._focus_move_t):
            self._focus, self._focus_valid = click[1], True
            return self._focus
        el = self._lookup(self.uia.focused, priority=priority) if self.uia is not None else None
        if el is not None:
            self._focus, self._focus_valid = el, True
        return el

    def _key_window(self) -> _Window:
        hwnd, pid = self._foreground()
        return self._window(hwnd, pid)

    def _on_key(self, t: float, kind: str, text: str, source: str, travel: int) -> None:
        after_type = None
        if self._type is not None:
            run = self._type
            after_type = {"typed": run.typed, "el": run.el}
        self._close_type(t)
        self._close_hover(t)
        self._input(t, source)
        last = self._last_key
        if (last is not None and any(a is last for a in self._acts) and last.chord == text
                and t - last.last_t <= FOLD_S
                and (text.split("+")[-1] in RUN_KEYS or kind != NAMED_KEY) and last.source == source):
            last.n += 1
            last.last_t = t
            return
        win = self._key_window()
        paste = text in PASTE_CHORDS
        act = self._new_act("paste" if paste else "keys", t, source, win, chord=text, travel=travel,
                            gap_ms=self._gap(t))
        act.after_type = after_type
        act.save_check = text in SAVE_CHORDS
        self._last_key = act
        if act.drop is None:
            el = self._focused(priority=True)
            if el is None:
                act.drop = "password_unknown"         # fail closed: the field it went into could not be checked
            elif el.secret():
                act.drop = el.secret()
            else:
                act.el, act.resolved, act.lookup_ms = el, "focus", el.ms
        if text.split("+")[-1] not in RUN_KEYS:
            self._focus_valid = False                 # Tab, Enter, a shortcut: the focus may move; read it again
            self._focus_move_t = t
        if paste and act.drop is None and self._clipboard is not None:
            try:
                seq, formats, private = self._clipboard()
            except Exception:  # noqa: BLE001
                seq, formats, private = None, (), False
            if private:
                act.drop = "private_clip"
            act.clip_seq, act.clip_formats = seq, list(formats)

    def _on_char(self, t: float, n: int, source: str) -> None:
        self._close_hover(t)
        self._input(t, source)
        run = self._type
        if run is not None and (t - run.last_t > TYPE_GAP_S or run.source != source):
            self._close_type(t)
            run = None
        if run is None:
            win = self._key_window()
            run = self._new_act("type", t, source, win, typed=0, gap_ms=self._gap(t), open=True)
            self._type = run
            if run.drop is None:
                el = self._focused(priority=True)
                if el is None:
                    run.drop = "password_unknown"
                elif el.secret():
                    run.drop = el.secret()
                else:
                    run.el, run.resolved, run.lookup_ms = el, "focus", el.ms
                    if t - self._focus_move_t <= FOCUS_RACE_S and self._last_focus_ev < self._focus_move_t:
                        run.recheck_at = t + FOCUS_RACE_S   # the app may not have moved the focus yet: read it again
        run.typed = (run.typed or 0) + int(n)
        run.last_t = t

    def _recheck(self, run: _Act) -> None:
        """A run that started right after a focus-moving input, its field read again once the app has had time to
        move the focus: a password (or unreadable) field now drops the whole run; another plain field takes it."""
        run.recheck_at = None
        if run.drop is not None:
            return
        el = self._lookup(self.uia.focused, priority=True) if self.uia is not None else None
        if el is None:
            run.drop = "password_unknown"
        elif el.secret():
            run.drop = el.secret()
        else:
            run.el, run.lookup_ms = el, el.ms
            self._focus, self._focus_valid = el, True

    def _close_type(self, t: float) -> None:
        run, self._type = self._type, None
        if run is not None:
            run.open = False
            if run.recheck_at is not None:
                self._recheck(run)                    # closed before its re-read: read the field now

    def _on_ev(self, t: float, event: int, hwnd: int, id_object: int, id_child: int) -> None:
        ev = (t, event, hwnd, id_object, id_child)
        if event == EVENT_OBJECT_FOCUS:
            self._focus_valid = False
            self._last_focus_ev = t
            run = self._type
            if run is not None:
                if t - run.t <= FOCUS_RACE_S and run.drop is None:
                    run.drop = "focus_race"           # the focus moved just after the run began: whose letters?
                run.recheck_at = None
                self._close_type(t)                   # the next character reads the (new) field again
        elif event == EVENT_SYSTEM_DIALOGSTART:
            self._dialog = (t, int(hwnd), self._fg)
        if event in STRUCTURE_EVENTS:
            self._structure(t, event)
        attached = False
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S:
                act.events.append(ev)
                attached = True
        if (not attached and event in (EVENT_OBJECT_INVOKED, EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD)
                and t - self._last_action_t > FOLD_S):
            if not self._during_run():
                self.counters["event_only_ignored"] += 1   # no input and no Yuki run: the app did it, not a person
                return
            win = self._key_window()
            act = self._new_act("invoke" if event == EVENT_OBJECT_INVOKED else "select", t, "yuki", win,
                                via="event_only", gap_ms=self._gap(t))
            act.events.append(ev)
            if act.drop is None and self.uia is not None:
                el = self._lookup(self.uia.from_event, hwnd, id_object, id_child, priority=False)
                if el is not None:
                    act.el, act.resolved, act.lookup_ms = el, "event", el.ms
                    if el.secret():
                        act.drop = el.secret()

    def _structure(self, t: float, event: int) -> None:
        """A structure change (shown, hidden, reordered, a menu closed, another window in front): every action whose
        lookup was still answering at that time is stale, decided now (a busy app's events cannot push it out)."""
        self._recent_structure.append((t, event))
        for act in self._acts:
            if act.lookup_end is not None and act.t <= t <= act.lookup_end:
                act.stale = True

    def _on_out(self, t: float, event: int, hwnd: int, id_object: int, id_child: int, gen: int = 0) -> None:
        if event in STRUCTURE_EVENTS:
            self._structure(t, event)
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S:
                act.outs.append((t, event, hwnd, id_object, id_child))

    def _on_fg(self, t: float, hwnd: int, pid: int) -> None:
        self._focus_valid = False
        self._structure(t, -1)
        dialog = self._dialog
        if dialog is not None and hwnd != dialog[1]:
            self._dialog = None
            self._dialog_closed = (t, dialog[2])     # an open/save dialog closed: its file belongs to this app
            if self._blocked() is None:
                self._on_recent(t)                   # the dialog's own Recent entry, if the shell wrote it already
        if self._last_click_edit is not None and self._last_click_edit[2] != hwnd:
            self._last_click_edit = None             # typing now goes into another window
        if self._blocked() is not None:
            return
        win = self._window(hwnd, pid)
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S and act.fg_change is None:
                act.fg_change = (t, win.process)
        last = self._last_input
        if last is not None and t - last[0] <= INPUT_LINK_S and win.process and hwnd != self._fg.hwnd:
            act = self._new_act("switch", t, last[1], win, gap_ms=self._gap(t))
            act.facts["window_verified"] = win.process
            act.resolved = "event"
        self._fg = win

    def _on_moving(self, t: float) -> None:
        self._close_hover(t)
        if self.sink is None or self._hover_rect is None:
            return
        self._hover_due = float(self.sink.anchor_at) + self._hover_rect[2]
        self._hover_seq = int(self.sink.anchor_seq)

    def _hover_fire(self, now: float) -> None:
        sink = self.sink
        anchor_at = float(sink.anchor_at)
        if int(sink.anchor_seq) != self._hover_seq:
            self._hover_seq = int(sink.anchor_seq)
            self._hover_due = anchor_at + self._hover_rect[2]
            if self._hover_due > now:
                return
        self._hover_due = None
        sink.moving = False
        if self._open or self._last_action_t >= anchor_at:
            return                                  # a press ended the rest: it is not a hover
        if self._cursor is None:
            return
        x, y = self._cursor()
        w, h, _s = self._hover_rect
        last = self._last_hover_xy
        if (last is not None and self._last_action_t <= self._last_hover_t
                and abs(x - last[0]) <= 2 * w and abs(y - last[1]) <= 2 * h):
            self.counters["hover_coalesced"] += 1     # the same rest, drifted: still one row
            return
        hwnd, pid = self._window_at(x, y)
        win = self._window(hwnd, pid)
        source = getattr(sink, "move_source", "user") or "user"
        act = self._new_act("hover", anchor_at, source, win, x=x, y=y, travel=self._take_sink_travel(),
                            gap_ms=self._gap(anchor_at), open=True)
        act.last_t = now
        self._last_hover_xy = (x, y)
        self._last_hover_t = anchor_at
        self._hover = act
        if act.drop is None and self.uia is not None:
            el = self._lookup(self.uia.at_point, x, y, priority=False)
            act.lookup_end = self._clock()
            if el is not None:
                act.el, act.resolved, act.lookup_ms = el, "point", el.ms
            else:
                act.drop = "skip_hover_unresolved"    # a hover is worth a row only with its element

    def _take_sink_travel(self) -> int | None:
        sink = self.sink
        try:
            travel = sink.travel
            sink.travel = 0.0
            return int(round(travel))
        except Exception:  # noqa: BLE001
            return None

    def _close_hover(self, t: float) -> None:
        act, self._hover = self._hover, None
        if act is not None:
            act.open = False
            act.dwell_ms = int(round(max(0.0, min(t, act.t + HOVER_MAX_S) - act.t) * 1000))
            act.last_t = t

    def _on_clip(self, t: float, seq: int, owner_pid: int, formats: tuple, private: bool) -> None:
        last = self._last_input
        if private:
            self.counters["dropped_private_clip"] += 1
            return
        if last is None or t - last[0] > INPUT_LINK_S:
            self.counters["clip_no_input"] += 1      # the clipboard changed without the user: not their copy
            return
        hwnd, pid = self._foreground()
        win = self._window(hwnd, owner_pid or pid)
        act = self._new_act("copy", t, last[1], win, clip_seq=int(seq), clip_formats=list(formats)[:8])
        act.resolved = "event"

    def _on_recent(self, t: float) -> None:
        folder = self._recent
        if folder is None:
            return
        cutoff = self._recent_seen
        newest = cutoff
        found: list[tuple[float, str]] = []
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    if not entry.name.lower().endswith(".lnk") or not entry.is_file():
                        continue
                    mtime = entry.stat().st_mtime
                    if mtime > cutoff:
                        found.append((mtime, entry.path))
                        newest = max(newest, mtime)
        except OSError:
            return
        self._recent_seen = newest
        last = self._last_input
        dialog = self._dialog_closed
        if dialog is not None and t - dialog[0] > FILE_INPUT_S:
            dialog = self._dialog_closed = None
        for mtime, link in sorted(found)[-8:]:
            if mtime < self._opened_at:
                self.counters["file_before_open"] += 1   # written while the trace was off, paused or gated
                continue
            target = self._resolve(link)
            if not target or not os.path.isfile(target):
                continue
            if self._rules().folder_blocked(target):
                self.counters["dropped_blocked_folder"] += 1
                continue
            if last is None or t - last[0] > FILE_INPUT_S:
                self.counters["file_no_input"] += 1
                continue
            try:
                written = os.path.getmtime(target)
            except OSError:
                written = 0.0
            saved = abs(self._wall() - written) <= 10.0
            if dialog is not None:
                # the app whose open/save dialog it came from, as it was then (its title and page decide privacy)
                win = dataclasses.replace(dialog[1])
            else:
                hwnd, pid = self._foreground()
                win = self._window(hwnd, pid)
            self._file_act("file_save" if saved else "file_open", t, last[1], win, target,
                           via="dialog" if dialog is not None else "recent")

    def _file_act(self, kind: str, t: float, source: str, win: _Window, target: str, *, via: str) -> _Act:
        act = self._new_act(kind, t, source, win, via=via)
        act.resolved = "event"
        ext = os.path.splitext(target)[1].lower()[:12]
        digest = self._digest("ui-file", os.path.normcase(os.path.normpath(target)))
        act.file = {"path": target, "ext": ext, "digest": digest}
        act.facts["file_saved" if kind == "file_save" else "file_opened"] = digest
        if act.drop is None:
            entry = (target, digest)
            if entry in self._known_files:
                self._known_files.remove(entry)
            self._known_files.append(entry)
        return act

    def _save_check(self, act: _Act) -> None:
        """A save chord with no dialog (FL Studio's usual Ctrl+S): the files this app opened or saved, by their own
        write time since the chord, become ``file_save`` rows (an .flp gets its project facts)."""
        chord_wall = self._at(act.t)
        for target, _digest in list(self._known_files):
            try:
                written = os.path.getmtime(target)
            except OSError:
                continue
            if written < chord_wall - 1.0 or self._rules().folder_blocked(target):
                continue
            save = self._file_act("file_save", act.t, act.source, act.win, target, via="save_chord")
            save.up_t = save.last_t = act.last_t
            self.counters["file_save_chord"] += 1

    def _on_cmd(self, items: list[dict[str, Any]]) -> None:
        """Warp's own history rows (already secret-filtered by its rules) as ``command`` actions."""
        from yuki.memory import secret_filter

        rules = self._rules()
        if rules.left_out("warp.exe", "Warp"):
            self.counters["dropped_left_out"] += len(items)
            return
        try:
            reason = self._privacy().check_app("warp.exe", "Warp")
        except Exception:  # noqa: BLE001
            reason = "privacy_error"
        if reason:
            self.counters["dropped_" + reason] += len(items)
            return
        for item in items:
            command = str(item.get("command") or "").strip()
            if not command:
                continue
            if item.get("agent"):
                self.counters["command_agent_skipped"] += 1    # Warp's own agent ran it, not the user
                continue
            at = float(item.get("at") or self._wall())
            exit_code = item.get("exit_code")
            facts = {"command_ok": True} if exit_code == 0 else {}
            row = {"at": at, "ended_at": at + float(item.get("seconds") or 0.0), "process": "warp",
                   "kind": "command", "source": "user", "via": "warp_history", "during_run": 0,
                   "command_ciphertext": self._enc(secret_filter.redact(command).text), "exit_code": exit_code,
                   "facts": json.dumps(facts) if facts else None, "resolved": "app_data", "outcome": "unverifiable"
                   if exit_code is None else "changed"}
            self._batch.append(row)
            self.counters["rows"] += 1
            self.counters["source_user"] += 1
            self.counters["resolved_app_data"] += 1

    # -- deadlines ---------------------------------------------------------------------------------------------------

    def next_due(self) -> float | None:
        """The next monotonic time :meth:`tick` has work (None: only a new item)."""
        dues = []
        if self._hover_due is not None:
            dues.append(self._hover_due)
        for i, act in enumerate(self._acts):
            due = self._due(act, self._acts[i + 1] if i + 1 < len(self._acts) else None)
            if due is not None:
                dues.append(due)
        if self._batch or self._elements:
            dues.append(self._last_flush + FLUSH_S)
        return min(dues) if dues else None

    def _due(self, act: _Act, nxt: _Act | None) -> float | None:
        if act.kind == "type":
            due = act.last_t + TYPE_GAP_S if act is self._type else act.last_t
            return due if act.recheck_at is None else min(due, act.recheck_at)
        if act.kind == "hover":
            return act.t + HOVER_MAX_S if act.open else act.last_t
        if act.open:
            return act.t + LOST_UP_S
        end = max(act.last_t, act.up_t or act.t) + OUTCOME_S
        if nxt is not None and act.kind not in ("dclick",):
            end = min(end, max(nxt.t, (act.up_t or act.t) + IDENTITY_S))
        if act.kind in ("click", "rclick", "dclick", "drag") and act is self._pending_dclick:
            end = max(end, (act.up_t or act.t) + self._double_s())
        if act.save_check:
            end = max(end, act.last_t + SAVE_CHECK_S)
        if act.recheck_at is not None:
            end = max(end, act.recheck_at)
        return end

    def _double_s(self) -> float:
        try:
            return float(self.sink.metrics.double_s)
        except Exception:  # noqa: BLE001
            return 0.5

    def tick(self, now: float | None = None) -> None:
        """Do what is due: a hover rest, finished actions, a batch write."""
        now = self._clock() if now is None else float(now)
        try:
            if self._hover_due is not None and now >= self._hover_due:
                if self._blocked() is None:
                    self._hover_fire(now)
                else:
                    self._hover_due = None
            for act in self._acts:
                if act.recheck_at is not None and now >= act.recheck_at:
                    self._recheck(act)
            if self._type is not None and now - self._type.last_t > TYPE_GAP_S:
                self._close_type(now)
            if self._hover is not None and now - self._hover.t >= HOVER_MAX_S:
                self._close_hover(now)
            keep: list[_Act] = []
            for i, act in enumerate(self._acts):
                nxt = self._acts[i + 1] if i + 1 < len(self._acts) else None
                due = self._due(act, nxt)
                if act.open and act.kind not in ("type", "hover") and now >= act.t + LOST_UP_S:
                    act.open = False
                    self._open = {b: a for b, a in self._open.items() if a is not act}
                    due = now
                if due is not None and now >= due and not act.open:
                    if act is self._pending_dclick:
                        self._pending_dclick = None
                    self._finish(act, now)
                else:
                    keep.append(act)
            self._acts = keep
            if (self._batch or self._elements or self._files) and now - self._last_flush >= FLUSH_S:
                self.flush(now)
        except Exception as exc:  # noqa: BLE001
            self._error("tick", exc)

    # -- finishing an action -------------------------------------------------------------------------------------------

    def _identity_from_events(self, act: _Act) -> None:
        if act.x is None or self.uia is None or act.drop is not None or act.kind in ("hover", "scroll"):
            return
        until = (act.up_t or act.t) + IDENTITY_S
        for wanted in IDENTITY_EVENTS:
            for t, event, hwnd, id_object, id_child in act.events:
                if event != wanted or not act.t <= t <= until:
                    continue
                if not self._take(priority=act.el is None or act.resolved != "point"):
                    return
                t0 = time.perf_counter()
                try:
                    el = self.uia.from_event(hwnd, id_object, id_child, budget_s=LOOKUP_S, index=True)
                except Exception as exc:  # noqa: BLE001
                    self._error("event_lookup", exc)
                    el = None
                self.lookup_ms.append((time.perf_counter() - t0) * 1000.0)
                self.counters["lookups"] += 1
                if el is not None and el.contains(act.x, act.y) and (not el.pid or not act.win.pid
                                                                    or el.pid == act.win.pid):
                    act.el, act.resolved, act.lookup_ms = el, "event", el.ms
                    return
                return

    def _stale(self, act: _Act) -> bool:
        if act.resolved not in ("point", "snapshot") or act.lookup_end is None:
            return False
        # decided as the events came (``act.stale``); the short history covers a structure event that reached the
        # queue before the press itself
        return act.stale or any(act.t <= t <= act.lookup_end for t, _e in self._recent_structure)

    def _outcome(self, act: _Act, now: float) -> tuple[str, dict[str, Any]]:
        counts: Counter[str] = Counter()
        names = {EVENT_OBJECT_STATECHANGE: "state", EVENT_OBJECT_VALUECHANGE: "value", EVENT_OBJECT_SHOW: "shown",
                 EVENT_OBJECT_HIDE: "hidden", EVENT_OBJECT_REORDER: "reordered"}
        for _t, event, *_rest in act.outs:
            counts[names.get(event, "other")] += 1
        for _t, event, *_rest in act.events:
            if event == EVENT_SYSTEM_MENUPOPUPSTART:
                counts["menu_opened"] += 1
            elif event == EVENT_SYSTEM_DIALOGSTART:
                counts["dialog"] += 1
            elif event == EVENT_OBJECT_FOCUS:
                counts["focus_moved"] += 1
            elif event == EVENT_OBJECT_NAMECHANGE:
                counts["title_changed"] += 1
            elif event in (EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD):
                counts["selected"] += 1
            elif event == EVENT_OBJECT_INVOKED:
                counts["invoked"] += 1
        if act.fg_change is not None:
            counts["window_changed"] += 1
            act.facts.setdefault("window_verified", act.fg_change[1])
        page_after = None
        if act.win.pid:
            try:
                page_after = self._page(act.win.pid)[0]
            except Exception:  # noqa: BLE001
                page_after = None
        if page_after and act.win.page_key and page_after != act.win.page_key:
            counts["page_changed"] += 1
            act.facts["page_reached"] = page_after
        summary: dict[str, Any] = dict(counts)
        changed = self._outcome_elements(act)
        if changed:
            summary["elements"] = changed
        if counts:
            return "changed", summary
        return ("nothing" if act.el is not None else "unverifiable"), summary

    def _outcome_elements(self, act: _Act) -> list[dict[str, Any]]:
        """Up to :data:`OUTCOME_ELEMENTS` of the elements that changed (state, value, shown), resolved to role, parent
        role and position in the window, with spare lookups only (clicks keep their reserve). No names, no values."""
        if self.uia is None or act.drop is not None or not act.outs:
            return []
        seen: set[tuple[int, int, int]] = set()
        out: list[dict[str, Any]] = []
        rect = self._rect(act.win.hwnd) if act.win.hwnd else None
        for _t, event, hwnd, id_object, id_child in act.outs:
            if event not in (EVENT_OBJECT_STATECHANGE, EVENT_OBJECT_VALUECHANGE, EVENT_OBJECT_SHOW):
                continue
            target = (hwnd, id_object, id_child)
            if target in seen:
                continue
            seen.add(target)
            if len(seen) > OUTCOME_ELEMENTS:
                break
            el = self._lookup(self.uia.from_event, hwnd, id_object, id_child, priority=False)
            if el is None:
                if self._bucket.tokens - 1.0 < RESERVE_FOR_CLICKS:
                    break                                   # no spare budget left: the counts stand alone
                continue
            if el.secret() or (el.pid and act.win.pid and el.pid != act.win.pid):
                continue
            entry: dict[str, Any] = {"role": el.role, "parent": el.parent_role or None}
            if el.bounds:
                ox, oy = (rect[0], rect[1]) if rect else (0, 0)
                entry["at"] = f"{(el.bounds[0] + el.bounds[2]) // 2 - ox},{(el.bounds[1] + el.bounds[3]) // 2 - oy}"
            out.append(entry)
        return out

    def _machine_facts(self, act: _Act) -> None:
        el = act.el
        if el is None or self.uia is None or act.drop is not None:
            return
        changed_state = any(e[1] == EVENT_OBJECT_STATECHANGE for e in act.outs)
        if act.kind in ("click", "dclick") and (el.toggle is not None or el.selected is not None) and (
                changed_state or any(e[1] in (EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD) for e in act.events)):
            if self._take(priority=False):
                state = self.uia.reread(el)
                if "toggle" in state:
                    act.facts["toggle_state"] = state["toggle"]
                if state.get("selected"):
                    act.facts["selected"] = True
        if act.kind == "keys" and act.chord == "Enter" and act.after_type and act.after_type.get("el") is not None:
            field_el = act.after_type["el"]
            if field_el.role in EDIT_ROLES and self._take(priority=False):
                state = self.uia.reread(field_el, value_length=True)
                if state.get("length") == 0:
                    act.facts["field_submitted"] = {"typed": int(act.after_type.get("typed") or 0)}

    def _element_cols(self, act: _Act, el: Any, prefix: str = "") -> tuple[dict[str, Any], dict[str, Any] | None]:
        """Row columns and the element registry use for one element, as its name class allows."""
        process = act.win.process
        cls = name_class(el.role, el.parent_role)
        norm = normalise_name(el.name)
        name_digest = self._digest("ui-name", process, el.role, norm) if norm else None
        ancestry = "/".join(r for r, _a in el.ancestors)
        key = self._digest("ui-el", process, el.role, ancestry, name_digest or "")
        name = None
        if cls in (COMMAND, TITLE) and el.name:
            from yuki.memory import secret_filter

            name = " ".join(secret_filter.redact(el.name).text.split())[:NAME_CHARS] or None
        if cls == TITLE and el.index is not None:
            position = f"{el.index}/{el.parent_role or ''}"
        else:
            position = el.parent_role or None
        cols: dict[str, Any] = {}
        if prefix:
            cols.update({"end_role": el.role, "end_name_ciphertext": self._enc(name), "end_name_digest": name_digest,
                         "end_element_key": key, "end_position": position})
        else:
            container = None
            if cls == CONTENT and el.ancestors:
                parent_role, parent_aid = el.ancestors[0]
                grand = el.ancestors[1][0] if len(el.ancestors) > 1 else ""
                container = self._digest("ui-container", process, parent_role, parent_aid, grand)
            cols.update({"role": el.role, "name_ciphertext": self._enc(name), "name_digest": name_digest,
                         "position": position, "aid_ciphertext": self._enc(el.aid) if el.aid else None,
                         "aid_digest": self._digest("ui-aid", process, el.aid) if el.aid else None,
                         "element_key": key, "container_digest": container})
        use = {"element_key": key, "process": process, "role": el.role, "name_class": cls, "name": name,
               "aid": el.aid or None, "at": self._at(act.t)}
        return cols, use

    def _finish(self, act: _Act, now: float) -> None:
        blocked = self._blocked()
        if blocked is not None:          # first: the game gate (or consent off) means no row and no further lookup
            self.counters["dropped_" + blocked] += 1
            return
        if act.drop is None and act.win.pid and act.kind in ("click", "rclick", "dclick", "drag", "scroll", "hover"):
            # the timeline may only now have caught up with a window that came to the front with this press
            now_win = self._window(act.win.hwnd, act.win.pid)
            act.drop = self._privacy_reason(now_win)
            if act.win.page_key is None:
                act.win.page_key = now_win.page_key
        if act.drop is None:
            self._identity_from_events(act)
        if act.drop is None and act.el is not None and act.el.secret():
            act.drop = act.el.secret()
        if act.drop is None and act.end_el is not None and act.end_el.secret():
            act.end_el = None
        if act.drop is not None:
            if act.drop.startswith("skip_"):
                self.counters[act.drop] += 1
            else:
                self.counters["dropped_privacy"] += 1
                self.counters["dropped_" + act.drop.split(":", 1)[0]] += 1
            return
        if self._stale(act):
            act.resolved = "stale"
        if act.el is not None and act.el.role in ("MenuItem",) and act.kind == "click":
            act.kind = "menu"
        if act.save_check:
            self._save_check(act)
        self._machine_facts(act)
        outcome, summary = self._outcome(act, now)
        row: dict[str, Any] = {
            "at": self._at(act.t), "ended_at": self._at(max(act.last_t, act.up_t or act.t)), "process":
            act.win.process, "page_key": act.win.page_key, "kind": act.kind, "source": act.source, "via": act.via,
            "during_run": int(act.during_run), "button": act.button, "chord": act.chord,
            "typed": act.typed, "n": act.n if act.n != 1 or act.kind in ("scroll", "keys") else None,
            "clip_seq": act.clip_seq, "clip_formats": json.dumps(act.clip_formats) if act.clip_formats else None,
            "outcome": outcome, "outcome_ciphertext": self._enc(json.dumps(summary, sort_keys=True)) if summary
            else None, "resolved": act.resolved if act.el is not None or act.resolved in ("event", "app_data")
            else "unresolved", "lookup_ms": round(act.lookup_ms, 2) if act.lookup_ms is not None else None,
            "travel_px": act.travel, "gap_ms": act.gap_ms, "dwell_ms": act.dwell_ms,
            "proc_started": act.win.started,
        }
        if act.el is not None:
            cols, use = self._element_cols(act, act.el)
            row.update(cols)
            if use is not None:
                self._elements.append(use)
        elif act.x is not None and act.kind in ("click", "rclick", "dclick", "drag", "scroll"):
            rect = self._rect(act.win.hwnd) if act.win.hwnd else None
            ox, oy = (rect[0], rect[1]) if rect else (0, 0)
            row["point"] = f"{act.x - ox},{act.y - oy}"
        if act.end_el is not None:
            cols, use = self._element_cols(act, act.end_el, prefix="end")
            row.update(cols)
            if use is not None:
                self._elements.append(use)
        if act.file is not None:
            row.update({"file_ciphertext": self._enc(act.file["path"]), "file_ext": act.file["ext"],
                        "file_digest": act.file["digest"]})
        if act.facts:
            row["facts"] = json.dumps(act.facts, sort_keys=True)
        index = len(self._batch)
        self._batch.append(row)
        if act.file is not None and act.file["ext"] == ".flp":
            self._flp_facts(act, index)
        self.counters["rows"] += 1
        self.counters["kind_" + act.kind] += 1
        self.counters["source_" + act.source] += 1
        self.counters["resolved_" + row["resolved"]] += 1
        if "page_reached" in act.facts and act.kind not in ("nav", "switch"):
            self._batch.append({"at": self._at(now), "process": act.win.process, "page_key": act.facts["page_reached"],
                                "kind": "nav", "source": act.source, "during_run": int(act.during_run),
                                "resolved": "event", "outcome": "changed",
                                "facts": json.dumps({"page_reached": act.facts["page_reached"]})})
            self.counters["rows"] += 1
            self.counters["kind_nav"] += 1

    def _flp_facts(self, act: _Act, index: int) -> None:
        t0 = time.perf_counter()
        try:
            data = self._flp(act.file["path"])
            outcome = "ok"
        except Exception as exc:  # noqa: BLE001 - fails closed: no facts, only the reason code
            data, outcome = None, getattr(exc, "code", type(exc).__name__)
        self._files.append({"row": index, "at": self._at(act.t), "kind": "flp", "outcome": outcome, "data": data,
                            "ms": round((time.perf_counter() - t0) * 1000.0, 1), "file_digest": act.file["digest"]})
        self.counters["flp_" + ("ok" if outcome == "ok" else "failed")] += 1

    def flush(self, now: float | None = None) -> list[int]:
        """Write the batch (one transaction), with what was on screen at each row's time."""
        now = self._clock() if now is None else float(now)
        self._last_flush = now
        rows, elements, files = self._batch, self._elements, self._files
        self._batch, self._elements, self._files = [], [], []
        if not rows and not elements and not files:
            return []
        try:
            wants = [(r["process"], r["at"]) for r in rows if r.get("process") and r.get("kind") != "command"]
            captures = trace_store.capture_ids(self.store, wants)
            for r in rows:
                if r.get("process") and r.get("kind") != "command":
                    r.setdefault("capture_id", captures.get((r["process"], r["at"])))
            ids = trace_store.write_batch(self.store, rows, elements, files)
            self.counters["batches"] += 1
            return ids
        except Exception as exc:  # noqa: BLE001
            self.counters["write_failed_rows"] += len(rows)
            self._error("write", exc)
            return []

    def stats(self) -> dict[str, Any]:
        lookups = sorted(self.lookup_ms)

        def pct(p: float) -> float | None:
            return round(lookups[min(len(lookups) - 1, int(p * len(lookups)))], 1) if lookups else None

        return {**dict(self.counters), "lookup_ms_p50": pct(0.5), "lookup_ms_p95": pct(0.95),
                "pending": len(self._acts), "batch": len(self._batch)}


# -- defaults (Windows) ------------------------------------------------------------------------------------------------


def _process_facts() -> Callable[[int], tuple[str, str, str, bool, float | None]]:
    from yuki.memory.store import app_key
    from yuki.memory.watcher import _ProcessCache

    cache = _ProcessCache()

    def facts(pid: int) -> tuple[str, str, str, bool, float | None]:
        f = cache.get(int(pid))
        started = None
        try:
            import psutil

            started = round(float(psutil.Process(int(pid)).create_time()), 1)
        except Exception:  # noqa: BLE001
            pass
        return app_key(f.process_name) or "", f.process_name or "", f.app_name or "", bool(f.is_own), started

    return facts


def _window_at(x: int, y: int) -> tuple[int, int]:
    from yuki.memory.trace_hooks import _pids_behind, root_at

    hwnd = root_at(x, y)
    pids = _pids_behind(hwnd) if hwnd else ()
    return hwnd, pids[0] if pids else 0


def _foreground() -> tuple[int, int]:
    from yuki.memory.trace_hooks import _U, _GA_ROOT, _pids_behind

    hwnd = int(_U.GetForegroundWindow() or 0)
    hwnd = int(_U.GetAncestor(hwnd, _GA_ROOT) or hwnd) if hwnd else 0
    pids = _pids_behind(hwnd) if hwnd else ()
    return hwnd, pids[0] if pids else 0


def _rect(hwnd: int) -> tuple[int, int, int, int] | None:
    try:
        import win32gui

        return tuple(win32gui.GetWindowRect(hwnd))   # type: ignore[return-value]
    except Exception:  # noqa: BLE001
        return None


def resolve_link(path: str) -> str | None:
    """A shell link's target path (``IShellLink``), or None."""
    try:
        import pythoncom
        from win32com.shell import shell

        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_MULTITHREADED)
        except pythoncom.com_error:
            pass
        link = pythoncom.CoCreateInstance(shell.CLSID_ShellLink, None, pythoncom.CLSCTX_INPROC_SERVER,
                                          shell.IID_IShellLink)
        link.QueryInterface(pythoncom.IID_IPersistFile).Load(str(path))
        target = link.GetPath(shell.SLGP_RAWPATH)[0]
        return str(target) if target else None
    except Exception:  # noqa: BLE001
        return None


def _read_flp(path: str) -> dict[str, Any]:
    from yuki.memory import flp, secret_filter

    facts = flp.read_flp(path).as_dict()
    for key in ("project",):
        facts[key] = secret_filter.redact(facts.get(key) or "").text
    for key in ("generators", "effects", "channels", "patterns"):
        facts[key] = [secret_filter.redact(v).text for v in facts.get(key) or []]
    return facts


def timeline_page(timeline_now: Callable[[], Any], store: Any) -> Callable[[int], tuple[str | None, str | None,
                                                                                        str | None]]:
    """``(pid) -> (page_key, url, withheld)`` from the timeline's open stretch, when it is that process's window."""
    cache: dict[tuple, str] = {}

    def page(pid: int) -> tuple[str | None, str | None, str | None]:
        tl = timeline_now()
        st = getattr(tl, "_current", None) if tl is not None else None
        if st is None or not pid or int(getattr(st, "pid", 0) or 0) != int(pid):
            return None, None, None
        sig = (st.process, st.host, st.path, st.title, st.withheld, bool(st.fullscreen), st.meeting)
        key = cache.get(sig)
        if key is None:
            key = store.timeline_key(st.process, st.host, st.path, st.title, st.withheld, fullscreen=st.fullscreen,
                                     meeting=st.meeting)
            if len(cache) > 256:
                cache.clear()
            cache[sig] = key
        host = getattr(st, "seen_host", None) or st.host
        path = getattr(st, "seen_path", None) or st.path or ""
        url = f"https://{host}{path if str(path).startswith('/') else '/' + str(path)}" if host else None
        return key, url, st.withheld

    return page


# -- the controller ---------------------------------------------------------------------------------------------------


class Trace:
    """The trace in the memory service: starts the hook thread and the worker, attaches the sink to presence's one
    Raw Input reader, and follows consent, the pause and the game gate from the service's one-second beat.

    Args:
        store: the memory store; ``privacy``: the service's ``PrivacyConfig``; ``gate``: the ``GameGate`` (or None:
        fails closed); ``timeline_now``: ``() -> TimelineRecorder | None``; ``acting``: ``() -> Acting | None``;
        ``attach``: attaches the sink to the Raw Input reader (``watcher.attach_input_sink``); ``log``.
    """

    def __init__(self, store: Any, *, privacy: Any, gate: Any = None, timeline_now: Callable[[], Any] = lambda: None,
                 acting: Callable[[], Any] = lambda: None, attach: Callable[[Any], bool] | None = None,
                 log: Callable[..., None] | None = None, reader_messages: Callable[[], int] | None = None) -> None:
        from yuki.memory.timeline import PrivacySection

        self.store = store
        self.privacy = privacy
        self._game_gate = gate
        self._timeline_now = timeline_now
        self._acting = acting
        self._attach = attach
        self._log_fn = log
        self._reader_messages = reader_messages
        self._section = PrivacySection(privacy, "trace", TraceRules.from_dict)
        self.queue: queue.SimpleQueue = queue.SimpleQueue()
        self.sink = None
        self.hooks = None
        self.worker: TraceWorker | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._paused = False
        self.state = "not_started"
        self._attached = False
        self._last_health = time.monotonic()
        self._last_msgs = 0
        self._ab: _AbSampler | None = None
        self.error = ""
        #: Wall-time spans while the trace was blocked (start, end or None while it still is), and when it first
        #: opened: a Warp command whose own time falls in one (or before that) is never recorded.
        self._spans: deque[list[float | None]] = deque(maxlen=256)
        self._first_open: float | None = None

    def rules(self) -> TraceRules:
        return self._section.get()

    def _gate_holds(self) -> bool:
        from yuki.memory.gamegate import gate_holds

        return gate_holds(self._game_gate)

    def _log(self, type: str, **fields: Any) -> None:
        if self._log_fn is not None:
            try:
                self._log_fn(type, **fields)
            except Exception:  # noqa: BLE001
                pass

    def start(self) -> Trace:
        from yuki.memory.trace_hooks import TraceHooks, recent_folder
        from yuki.memory.trace_input import Metrics, TraceInput

        recent = recent_folder()
        self.hooks = TraceHooks(self.queue.put, recent=recent)
        metrics = Metrics()
        self.sink = TraceInput(self.queue.put, metrics=metrics, on_action=self.hooks.action)
        rules_fn = self.privacy.rules
        self.worker = TraceWorker(
            self.store, privacy_rules=rules_fn, trace_rules=self.rules, gate=self._gate_holds, acting=self._acting,
            page=timeline_page(self._timeline_now, self.store), title=_title, rect=_rect, sink=self.sink,
            cursor=self.sink._cursor, clipboard=_clipboard_now, recent=recent, log=self._log_fn,
            hover=(metrics.hover_w, metrics.hover_h, metrics.hover_s))
        self.hooks.start()
        self._thread = threading.Thread(target=self._run, name="yuki-trace-worker", daemon=True)
        self._thread.start()
        self.state = "started"
        self._log("trace_start", metrics=metrics.facts(), hooks=bool(self.hooks.hwnd), hooks_error=self.hooks.error)
        return self

    def _run(self) -> None:
        worker = self.worker
        try:
            from yuki.memory.trace_uia import TraceUia

            worker.uia = TraceUia()
        except Exception as exc:  # noqa: BLE001 - point-only from now on
            self.error = type(exc).__name__
            self._log("error", where="trace.uia_start", error=type(exc).__name__)
        while not self._stop.is_set():
            due = worker.next_due()
            # sleeps until the next item or deadline: nothing wakes it while nothing happens (stop() sends an item)
            timeout = None if due is None else max(0.0, due - time.monotonic())
            try:
                item = self.queue.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is not None:
                if item[0] == "stop":
                    break
                worker.handle(item)
            worker.tick()
        worker.flush()

    def stop(self) -> None:
        self._stop.set()
        self.queue.put(("stop",))
        if self.sink is not None:
            self.sink.blocked = True
        if self._thread is not None:
            self._thread.join(5.0)
        if self.hooks is not None:
            self.hooks.stop()

    def set_paused(self, paused: bool) -> None:
        self._paused = bool(paused)
        self.beat()

    def warp_commands(self, items: list[dict[str, Any]]) -> None:
        """Warp's history rows (secret-filtered by its rules) for ``command`` actions (from the Warp reader). Warp is
        read every few minutes, so each command is checked against when the trace was open by its own time."""
        if not items or self.blocked_reason():
            return
        kept = [c for c in items if self.open_at(c.get("at"))]
        if self.worker is not None and len(kept) < len(items):
            self.worker.counters["dropped_command_blocked_span"] += len(items) - len(kept)
        if kept:
            self.queue.put(("cmd", kept))

    def open_at(self, at: Any) -> bool:
        """Whether the trace was open (recording, not paused, not gated) at this wall time."""
        try:
            at = float(at)
        except (TypeError, ValueError):
            return False
        if self._first_open is None or at < self._first_open:
            return False
        return not any(start <= at and (end is None or at < end) for start, end in self._spans)

    def _span(self, blocked: bool) -> None:
        now = time.time()
        if blocked:
            if not self._spans or self._spans[-1][1] is not None:
                self._spans.append([now, None])
        else:
            if self._first_open is None:
                self._first_open = now
            if self._spans and self._spans[-1][1] is None:
                self._spans[-1][1] = now
            while self._spans and self._spans[0][1] is not None and now - float(self._spans[0][1]) > 2 * 86400.0:
                self._spans.popleft()

    def blocked_reason(self) -> str | None:
        rules = self.rules()
        if not rules.record_actions:
            return "off"
        if self._paused:
            return "paused"
        if self._ab is not None and not self._ab.on_now():
            return "ab_off"
        if self._gate_holds():
            return "game"
        if self.hooks is None or not self.hooks.alive:
            return "hooks_down"
        return None

    def beat(self) -> None:
        """From the service's one-second loop: follow consent, pause, the A/B hours and the game gate. Nothing runs
        (no thread, no hook) until the user first turns recording on; then the threads stay and block instead."""
        rules = self.rules()
        if self.sink is None:
            if not rules.record_actions:
                return
            try:
                self.start()
            except Exception as exc:  # noqa: BLE001 - recording stays off; said once
                self.state = "failed"
                self._log("error", where="trace.start", error=type(exc).__name__)
                self.sink = None
                self.rules = lambda: TraceRules()   # type: ignore[method-assign]
                return
        if rules.ab_hours and self._ab is None:
            self._ab = _AbSampler(self._log)
        elif not rules.ab_hours:
            self._ab = None
        reason = self.blocked_reason()
        blocked = reason is not None
        if blocked != self.sink.blocked:
            if not blocked and self._attach is not None:
                # (again on every opening: it also ends a mouse rest presence took while the trace was blocked)
                self._attached = bool(self._attach(self.sink))
                if not self._attached:
                    self._log("trace_state", state="no_input", why="raw_input_unavailable")
            if not blocked:
                self.sink.reset()
                # nothing from the blocked span may become a row later (Recent links, half-built actions)
                self.queue.put(("reopen", time.time()))
            self._span(blocked)
            self.sink.blocked = blocked
            self.hooks.set_blocked(blocked)
        state = reason or ("no_input" if self._attach is not None and not self._attached else "on")
        if state != self.state:
            self.state = state
            self._log("trace_state", state=self.state)
            if state == "no_input":
                self.save_health()
        if self._ab is not None:
            self._ab.beat(self.state == "on")
        now = time.monotonic()
        if now - self._last_health >= 600.0:
            self._last_health = now
            self.save_health()

    def stats(self) -> dict[str, Any]:
        out: dict[str, Any] = {"state": self.state}
        if self.worker is not None:
            out["worker"] = self.worker.stats()
        if self.sink is not None:
            out["input"] = dict(self.sink.counts)
        if self.hooks is not None:
            out["hooks"] = dict(self.hooks.counts)
        if self._reader_messages is not None:
            try:
                msgs = int(self._reader_messages())
                out["wm_input"] = msgs
            except Exception:  # noqa: BLE001
                pass
        return out

    def save_health(self) -> None:
        """The trace's health family in ``nudge_state`` (``trace:health``), content-free, for the doctor."""
        try:
            stats = self.stats()
            window = max(1.0, time.monotonic() - getattr(self.worker, "_started", time.monotonic()))
            hooks = stats.get("hooks", {})
            health = {"at": time.time(), "state": self.state, "window_s": round(window),
                      "wm_input_per_s": round(stats.get("wm_input", 0) / window, 2),
                      "low_events_per_s": round(hooks.get("low", 0) / window, 3),
                      "outcome_events_per_s": round(hooks.get("outcome", 0) / window, 3),
                      "events_over_cap": hooks.get("outcome_over", 0), **stats.get("worker", {})}
            self.store.set_nudge_state("trace:health", json.dumps(health, sort_keys=True))
        except Exception as exc:  # noqa: BLE001
            self._log("error", where="trace.health", error=type(exc).__name__)


def _title(hwnd: int) -> str:
    from yuki.memory.trace_hooks import window_title

    return window_title(hwnd)


def _clipboard_now() -> tuple[int, tuple[str, ...], bool]:
    from yuki.memory.trace_hooks import _U, clipboard_formats

    formats, private = clipboard_formats()
    return int(_U.GetClipboardSequenceNumber()), formats, private


class _AbSampler:
    """Acceptance tool (a): with ``[trace] ab_hours = true`` the trace is on in even local hours and off in odd ones;
    every :data:`EVERY_S` the CPU of the app in front and of this service are sampled, and every
    :data:`REPORT_S` a ``trace_ab`` line (content-free: app process keys, CPU %, RSS) is logged per hour kind. Read
    them with ``scripts/trace_ab_report.py``."""

    EVERY_S = 10.0
    REPORT_S = 600.0

    def __init__(self, log: Callable[..., None]) -> None:
        import psutil

        self._log = log
        self._psutil = psutil
        self._me = psutil.Process()
        self._cpus = psutil.cpu_count() or 1
        self._next = time.monotonic() + self.EVERY_S
        self._report = time.monotonic() + self.REPORT_S
        self._prev: dict[int, tuple[float, float]] = {}
        self._sums: dict[tuple[str, str], list[float]] = {}

    @staticmethod
    def on_now() -> bool:
        return time.localtime().tm_hour % 2 == 0

    def _cpu(self, proc: Any) -> float | None:
        try:
            t = proc.cpu_times()
            cpu = t.user + t.system
        except Exception:  # noqa: BLE001
            return None
        now = time.monotonic()
        prev = self._prev.get(proc.pid)
        self._prev[proc.pid] = (now, cpu)
        if prev is None or now - prev[0] <= 0:
            return None
        return 100.0 * (cpu - prev[1]) / (now - prev[0]) / self._cpus

    def beat(self, on: bool) -> None:
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + self.EVERY_S
        mode = "on" if on else "off"
        try:
            hwnd, pid = _foreground()
            if pid:
                proc = self._psutil.Process(pid)
                from yuki.memory.store import app_key

                name = app_key(proc.name()) or "?"
                cpu = self._cpu(proc)
                if cpu is not None:
                    self._sums.setdefault((mode, name), []).append(cpu)
            own = self._cpu(self._me)
            if own is not None:
                self._sums.setdefault((mode, "__service__"), []).append(own)
                self._sums.setdefault((mode, "__rss_mb__"), []).append(self._me.memory_info().rss / 2**20)
        except Exception:  # noqa: BLE001
            pass
        if now >= self._report:
            self._report = now + self.REPORT_S
            out = {f"{m}:{n}": {"mean": round(sum(v) / len(v), 3), "n": len(v)} for (m, n), v in self._sums.items()
                   if v}
            self._sums.clear()
            self._log("trace_ab", hour=time.localtime().tm_hour, samples=out)


__all__ = ["FLUSH_S", "IDENTITY_S", "LOOKUPS_PER_S", "Trace", "TraceWorker", "resolve_link", "timeline_page"]

