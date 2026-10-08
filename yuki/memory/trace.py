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
   A window of a process that shows pages whose page the timeline has not settled for that very window (a browser
   window behind, a tab switch still settling, a probe not answered) fails the blocked-site gate closed
   (:data:`PAGE_UNKNOWN`); everything is checked again when the action finishes. Dropped actions leave only a
   content-free counter.
2. **identity, taken at button-down**: (i) an ``INVOKED`` / ``SELECTION`` / ``FOCUS`` event from the same process
   between button-down and :data:`IDENTITY_S` after button-up whose element contains the point; (ii)
   ``ElementFromPoint`` asked as soon as the button went down; (iii) for a renderer's whole pane, the smallest element
   under the point; else point-only (``unresolved``). capC1c: an unnamed container with no actionable pattern is
   walked, inside the same lookup, to the real control (:meth:`yuki.memory.trace_uia.TraceUia.refine`). It is
   ``stale`` only when something could have changed what is under the press before the answer, by machine facts: a
   structure change (shown, hidden, reordered, a menu closed) whose own window covers the point (decided per action
   as the events come, so a busy app's later events cannot push it out), another top-level window under the point
   at the answer than at the press (taken at button-down by the sink), the element's own top-level window not the
   pressed one, or its bounds no longer holding the point. A window coming to the front after the press is the
   click's outcome, never staleness.
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
    EVENT_SYSTEM_MENUPOPUPSTART, OUTCOME_CAP, OUTCOME_S,
)
from yuki.memory.trace_keys import FOLD_S, NAMED_KEY, RUN_KEYS
from yuki.memory.trace_privacy import COMMAND, CONTENT, NAME_CHARS, TITLE, TraceRules, gate, name_class, normalise_name

#: Input and hook items waiting for the worker at most. A worker stuck in a call into a hung app would otherwise
#: let the queue grow without end; past this, new input items are dropped and counted (``dropped_queue_full``),
#: control items (stop, reopen, Warp commands) always go through.
QUEUE_MAX = 5000
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
#: Files remembered (in memory only, each with the app that opened or saved it; a file the shell opened goes to the
#: app it launched for it) for a save chord's re-check in that app.
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
#: capC1c review: the object ids (WinEvent ``idObject``, Windows' own) whose show / hide never changes what is under a
#: point: the text caret (``OBJID_CARET``, shown in a field the moment a click focuses it) and the mouse cursor
#: (``OBJID_CURSOR``). A structure event about them never makes a press stale.
NOT_CONTENT_OBJECTS = frozenset({-8, -9})
IDENTITY_EVENTS = (EVENT_OBJECT_INVOKED, EVENT_OBJECT_SELECTION, EVENT_OBJECT_SELECTIONADD, EVENT_OBJECT_FOCUS)
#: Changed elements an outcome resolves to role and position at most (report 78 §4.2 step 3).
OUTCOME_ELEMENTS = 5
#: The page answer for a window of a process that shows pages when the timeline has no settled reading of *that*
#: window (another window of the same browser, one not in front, a probe not answered yet, a probe that found
#: nothing): its site is not known, so the blocked-site gate fails closed (second verify V1).
PAGE_UNKNOWN = "page_unknown"
#: Kinds whose row names an element under the pointer (the page under it decides privacy).
POINTER_KINDS = frozenset({"click", "rclick", "dclick", "drag", "scroll", "hover"})
#: Kinds whose row names the focused element (keys and typing go into the page in front).
FOCUS_KINDS = frozenset({"keys", "paste", "type", "invoke", "select"})
#: How close in time a Warp command's own start is to the Enter that ran it (s).
COMMAND_ENTER_S = 2.0

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
    container_role: str | None = None      # point-only: the whole-window container the point fell in (V5)
    end_win: tuple[int, int] | None = None   # a drag's drop window (hwnd, pid), checked like the start (V2)
    down_root: int = 0                       # capC1c: the top-level window under the press, taken at button-down
    answer_root: int = 0                     # capC1c: the top-level window under the point when the lookup answered


class TraceWorker:
    """The worker's logic, driven by :meth:`handle` (one queue item) and :meth:`tick` (deadlines). Tests drive it
    synchronously with fakes; :class:`Trace` runs it on its own thread.

    Args:
        store: the memory :class:`~yuki.memory.store.Store` (``cipher``, ``_enc``, ``_write``...).
        privacy_rules: ``() -> PrivacyRules`` (the watcher's gates).
        trace_rules: ``() -> TraceRules``.
        gate: ``() -> bool``: the hard game gate holds (``gamegate.gate_holds``).
        paused: ``() -> bool``: the tray pause or an A/B off hour (actions in flight then finish into no row).
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
                 paused: Callable[[], bool] = lambda: False,
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
                 hover: tuple[int, int, float] | None = None, hung: Any = None,
                 root_of: Callable[[int], int] | None = None, pid_of: Callable[[int], int] | None = None) -> None:
        self.store = store
        #: capC1c: ``(hwnd) -> its top-level window`` (``GetAncestor(GA_ROOT)``) and ``(hwnd) -> its process``: the
        #: machine facts the staleness rule compares (:meth:`_stale`).
        self._root_of = root_of or _root_of
        self._pid_of = pid_of or _window_pid
        #: capC1b: the hung-app watchdog (:class:`yuki.memory.trace_uia.HungWatch`): no lookup into an app that is not
        #: answering, by Windows' own verdict or a lookup that took :data:`yuki.memory.trace_uia.SLOW_CALL_S`.
        if hung is None:
            from yuki.memory.trace_uia import HungWatch

            hung = HungWatch()
        self.hung = hung
        self._privacy = privacy_rules
        self._rules = trace_rules
        self._gate = gate
        self._paused = paused
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
        # recent (path, digest, app's process key, wall time), memory only: a save chord re-checks that app's (V8)
        self._known_files: deque[tuple[str, str, str, float]] = deque(maxlen=KNOWN_FILES)
        self._last_action_t = float("-inf")
        self._last_input: tuple[float, str] | None = None
        self._input_own = False                  # the last input went into one of Yuki's own windows (V4)
        self._enters: deque[tuple[float, str, bool]] = deque(maxlen=64)   # Warp Enters: (wall, source, run) (V4)
        self._last_key: _Act | None = None
        self._last_wheel: _Act | None = None
        self._fg = _Window()
        self._recent_structure: deque[tuple[float, int, int]] = deque(maxlen=64)   # (t, event, its window)
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
            page_key, url, withheld = self._page(pid, hwnd)
        except Exception:  # noqa: BLE001 - fails closed: the page is not known
            page_key, url, withheld = None, None, PAGE_UNKNOWN
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

    def _app_reason(self, win: _Window) -> str | None:
        """The gates that need no page: Yuki's own windows, the apps left out, blocked apps, private windows. For rows
        that name only the app (a switch, the app an action brought to the front), never an element of its page."""
        if not win.process:
            return "unknown_app"
        try:
            rules = self._privacy()
        except Exception:  # noqa: BLE001
            return "privacy_error"
        return gate(rules, self._rules(), process=win.image or win.process, app=win.app, own=win.own,
                    title=win.title, url=None, withheld=None)

    def _linked_source(self, t: float, within: float) -> str | None:
        """The source of a row an input caused (a switch, a copy, a file), or None when no input came within
        ``within`` s. During a Yuki run, the user's input counts only outside Yuki's own windows (a takeover); an
        Enter in Yuki's ask bar followed by the app Yuki opens is Yuki's (second verify V4)."""
        last = self._last_input
        if last is None or t - last[0] > within:
            return None
        if last[1] != "yuki" and self._input_own and self._during_run():
            return "yuki"
        return last[1]

    def _blocked(self) -> str | None:
        """Why nothing may be recorded right now (consent off, the pause or an off hour, the game gate), or None."""
        try:
            if not self._rules().record_actions:
                return "off"
        except Exception:  # noqa: BLE001
            return "off"
        try:
            if self._paused():
                return "paused"
        except Exception:  # noqa: BLE001
            return "paused"
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

    def _hung_skip(self, target: tuple[int, int] | None, *, probe: bool = False) -> bool:
        """capC1b watchdog: whether the window a lookup would go into belongs to an app that is not answering
        (:class:`yuki.memory.trace_uia.HungWatch`); counted as ``lookup_skipped_hung``. ``probe`` (second verification
        V2): the lookup is an MSAA call with no timeout of its own, so the window must also take a no-op message within
        :data:`yuki.memory.trace_uia.ANSWER_MS` first (an app frozen for less than the 5 s Windows waits before calling
        it hung would otherwise hold the worker for its whole freeze)."""
        if target is None or self.hung is None:
            return False
        try:
            if self.hung.skip(int(target[0] or 0), int(target[1] or 0), probe=probe):
                self.counters["lookup_skipped_hung"] += 1
                return True
        except Exception as exc:  # noqa: BLE001
            self._error("hung_watch", exc)
        return False

    def _hung_note(self, target: tuple[int, int] | None, seconds: float) -> None:
        if target is not None and self.hung is not None:
            try:
                self.hung.note(int(target[0] or 0), int(target[1] or 0), seconds)
            except Exception as exc:  # noqa: BLE001
                self._error("hung_watch", exc)

    def _lookup(self, fn: Callable[..., Any], *args: Any, priority: bool, target: tuple[int, int] | None = None,
                probe: bool = False, **kw: Any) -> Any:
        """One UI Automation lookup within budget (never while the gate holds, never into an app that is not
        answering: ``target`` is the window it goes into, ``(hwnd, pid)``; ``probe``: an MSAA call, the window must
        answer a no-op message first - :meth:`_hung_skip`); None when over budget, skipped or failed."""
        if self.uia is None or self._blocked() is not None or self._hung_skip(target, probe=probe) \
                or not self._take(priority=priority):
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
        self._hung_note(target, ms / 1000.0)
        return out

    def _front_target(self) -> tuple[int, int] | None:
        """The window in front, ``(hwnd, pid)``: where a focus lookup goes."""
        try:
            hwnd, pid = self._foreground()
            return int(hwnd or 0), int(pid or 0)
        except Exception:  # noqa: BLE001
            return None

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

    def _walk_kw(self, hwnd: int) -> dict[str, Any]:
        """capC1c: the resolution walk's arguments for a lookup in this window (only a client that has the walk gets
        them): an unnamed container's answer is walked to the real control; ``max_area`` is the window's area (an
        element covering half of it or more is a whole pane, never "the control")."""
        if not getattr(self.uia, "refines", False):
            return {}
        rect = self._rect(hwnd) if hwnd else None
        area = max(0, (rect[2] - rect[0]) * (rect[3] - rect[1])) if rect else 0
        return {"refine": True, "max_area": area}

    def _on_down(self, t: float, button: str, x: int, y: int, source: str, travel: int, dbl: bool,
                 root: int = 0, root_pid: int = 0) -> None:
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
            self._input_own = prev.win.own
            return
        hwnd, pid = self._window_at(x, y)
        # capC1c: the window under the press (taken at button-down by the sink) is gone or covered before the worker
        # got to it (a menu closed, a window closed, a dialog came up): a lookup now would answer for another window.
        # The press belongs to the window that was pressed: its own app's row, stale and point-only, through that
        # window's own gates. Still there (covered), its process is read now; gone (a menu or dialog closed), the
        # process the sink read at button-down (``GetWindowThreadProcessId``, no messages) stands. Only a press whose
        # app is known neither way keeps nothing.
        moved = bool(root) and bool(hwnd) and int(root) != int(hwnd)
        if moved:
            try:
                now_pid = int(self._pid_of(int(root)) or 0)
            except Exception:  # noqa: BLE001
                now_pid = 0
            root_pid = now_pid or int(root_pid or 0)
            if root_pid:
                hwnd, pid = int(root), root_pid
        win = self._window(hwnd, pid)
        self._input_own = win.own
        act = self._new_act(_BUTTON_KIND.get(button, "click"), t, source, win, x=x, y=y, button=button,
                            travel=travel, gap_ms=self._gap(t), open=True)
        act.down_root = int(root or hwnd or 0)
        self._open[button] = act
        if moved:
            if int(hwnd) != int(root):
                act.drop = act.drop or "skip_window_changed"
            elif act.drop is None:
                act.stale = True
                act.resolved = "stale"                     # no lookup: what was under the press is not there now
                act.lookup_end = t
                self.counters["stale_window_changed"] += 1
                if not now_pid:
                    self.counters["stale_window_gone"] += 1
            return
        if act.drop is None:
            walk = self._walk_kw(hwnd)
            el = self._lookup(self.uia.at_point if self.uia else None, x, y, priority=True, index=True,
                              target=(hwnd, pid), **walk)
            act.lookup_end = self._clock()
            if el is not None:
                # capC1c: a renderer descent done inside the click's own lookup is the snapshot path (iii)
                descended = getattr(el, "refined", None) in ("descent", "path")
                act.el, act.resolved, act.lookup_ms = el, "snapshot" if descended else "point", el.ms
                if el.role in EDIT_ROLES:
                    self._last_click_edit = (t, el, hwnd)   # typing next goes into it (a password field: dropped)
                if el.role in PANE_ROLES and self._covers_window(el, hwnd) and walk.get("max_area"):
                    self._point_only(act, el)               # the lookup already went down: nothing deeper answers
                elif el.role in PANE_ROLES and self._covers_window(el, hwnd):
                    deeper = self._lookup(self.uia.descend, el, x, y, priority=False, target=(hwnd, pid), **walk)
                    if deeper is not None:
                        act.el, act.resolved, act.lookup_ms = deeper, "snapshot", (act.lookup_ms or 0) + deeper.ms
                        act.lookup_end = self._clock()
                    else:
                        self._point_only(act, el)
            act.answer_root = self._answer_root(x, y, el)

    def _answer_root(self, x: int, y: int, el: Any = None) -> int:
        """capC1c: the top-level window under the point when the lookup answered: compared with the one under the
        press (:meth:`_stale`), only for an element whose own window is unknown. Second verification V2-4: the client
        reads it right when ``ElementFromPoint`` answers (``el.root``), before the walk and the ancestry, so a window
        the click itself brings over the point afterwards is never taken for the answer's; read now otherwise."""
        root = int(getattr(el, "root", 0) or 0) if el is not None else 0
        if root:
            return root
        try:
            return int(self._window_at(x, y)[0] or 0)
        except Exception:  # noqa: BLE001
            return 0

    @staticmethod
    def _point_only(act: _Act, el: Any) -> None:
        """The only answer is the whole window (a blind window like FL Studio or Warp answers ``ElementFromPoint``
        with its own pane): the action is point-only (decision D2 a), the point kept relative to the window and the
        container's role for context, never the window as if it were the element (second verify V5)."""
        act.container_role = el.role
        act.el, act.resolved = None, "unresolved"
        act.lookup_ms = el.ms if el.ms is not None else act.lookup_ms

    def _whole_window(self, el: Any, hwnd: int) -> bool:
        return el is not None and el.role in PANE_ROLES and self._covers_window(el, hwnd)

    def _covers_window(self, el: Any, hwnd: int) -> bool:
        rect = self._rect(hwnd) if hwnd else None
        if not rect or not el.bounds:
            return el.role in ("Pane", "Document")
        area = max(1, (rect[2] - rect[0]) * (rect[3] - rect[1]))
        return el.area() * 2 >= area

    def _on_up(self, t: float, button: str, x: int, y: int, source: str, drag: bool, came_back: int = 0) -> None:
        act = self._open.pop(button, None)
        if act is None:
            return
        act.open = False
        act.up_t = act.last_t = t
        if came_back and not drag:
            # capC1c review: the cursor left the drag rectangle while held and was released back inside it: kept a
            # click (the release decides), the distance kept so a live hour can see knob-style drags among clicks
            act.facts["came_back_px"] = int(came_back)
            self.counters["click_came_back"] += 1
        if act.kind in ("click", "dclick") and button == "left":
            self._pending_dclick = act if act.kind == "click" else None
        if drag:
            act.kind = "drag"
            act.end_x, act.end_y = x, y
            # capC1c: how far the cursor ended from the press (px, the larger axis): the sink's drag test, kept so a
            # live hour can check drags near Windows' drag rectangle
            act.facts["moved_px"] = int(max(abs(x - (act.x if act.x is not None else x)),
                                            abs(y - (act.y if act.y is not None else y))))
            act.travel = (act.travel or 0) + int(round(abs(x - (act.x or x)) + abs(y - (act.y or y))))
            if act.drop is not None:
                return
            # the drop window is checked like any other before anything in it is looked up (V2): a blocked app,
            # Yuki's own window, a private window, a blocked or unknown site or an app left out keeps no end element
            end_hwnd, end_pid = self._window_at(x, y)
            act.end_win = (end_hwnd, end_pid)
            end_win = act.win if (end_hwnd, end_pid) == (act.win.hwnd, act.win.pid) else self._window(end_hwnd, end_pid)
            if self._privacy_reason(end_win) is not None:
                act.end_win = None
                self.counters["drag_end_withheld"] += 1
                return
            if self.uia is not None:
                end_el = self._lookup(self.uia.at_point, x, y, priority=True, target=(end_hwnd, end_pid),
                                      **self._walk_kw(end_hwnd))
                if end_el is not None and not self._whole_window(end_el, end_hwnd) and (
                        not end_el.pid or not end_pid or end_el.pid == end_pid):
                    act.end_el = end_el

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
            self._input_own = last.win.own
            return
        win = self._window(hwnd, pid)
        self._input_own = win.own
        act = self._new_act("scroll", t, source, win, x=x, y=y, chord=direction, n=abs(notches), travel=travel,
                            gap_ms=self._gap(t))
        self._last_wheel = act
        if act.drop is None and self.uia is not None:
            act.down_root = int(hwnd or 0)
            el = self._lookup(self.uia.at_point, x, y, priority=False, target=(hwnd, pid))
            act.lookup_end = self._clock()
            act.answer_root = self._answer_root(x, y, el)
            if self._whole_window(el, hwnd):
                self._point_only(act, el)
            elif el is not None:
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
        el = self._lookup(self.uia.focused, priority=priority, target=self._front_target()) \
            if self.uia is not None else None
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
        elif text == "Enter":
            prev = self._last_key
            # an Enter right after a paste into a field, with no other action and no focus change between: the
            # pasted field is what it may submit, read like a field typed into (report 98, review F2: paste_sent)
            if (prev is not None and prev.kind == "paste" and prev.drop is None and prev.el is not None
                    and getattr(prev.el, "role", None) in EDIT_ROLES and self._last_action_t == prev.t
                    and self._last_focus_ev <= prev.t):
                after_type = {"typed": 0, "el": prev.el}
        self._close_type(t)
        self._close_hover(t)
        self._input(t, source)
        last = self._last_key
        if (last is not None and any(a is last for a in self._acts) and last.chord == text
                and t - last.last_t <= FOLD_S
                and (text.split("+")[-1] in RUN_KEYS or kind != NAMED_KEY) and last.source == source):
            last.n += 1
            last.last_t = t
            self._input_own = last.win.own
            return
        win = self._key_window()
        self._input_own = win.own
        if text == "Enter" and win.process == "warp" and self._app_reason(win) is None:
            # who pressed the Enter that ran a Warp command (its history row comes minutes later; memory only)
            self._enters.append((self._at(t), source, self._during_run()))
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
            self._input_own = win.own
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
        el = self._lookup(self.uia.focused, priority=True, target=self._front_target()) \
            if self.uia is not None else None
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
            self._structure(t, event, hwnd, id_object)
        attached = False
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S:
                attached = True
                if len(act.events) < OUTCOME_CAP:
                    act.events.append(ev)
                else:
                    self.counters["events_over_row"] += 1
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
                el = self._lookup(self.uia.from_event, hwnd, id_object, id_child, priority=False, target=(hwnd, 0),
                                  probe=True)
                if el is not None:
                    act.el, act.resolved, act.lookup_ms = el, "event", el.ms
                    if el.secret():
                        act.drop = el.secret()

    def _structure(self, t: float, event: int, hwnd: int = 0, id_object: int = 0) -> None:
        """A structure change (shown, hidden, reordered, a menu closed): an action whose lookup was still answering at
        that time is stale when the change could alter what is under its press point, decided now (a busy app's
        events cannot push it out). capC1c: only when the event's own window (``GetWindowRect`` of its handle, a
        machine fact) covers the point; a tooltip or a thumbnail shown elsewhere on screen is not. A window that is
        gone by now cannot be placed; the window under the press is compared when the action finishes instead
        (:meth:`_stale`). Report 78 §4.2: only changes that could touch the press count. The text caret or the
        cursor shown or hidden (:data:`NOT_CONTENT_OBJECTS`: a click into a field shows its caret at once, its handle
        the field's own window) changes nothing under the point and never counts."""
        if id_object in NOT_CONTENT_OBJECTS:
            self.counters["structure_not_content"] += 1
            return
        self._recent_structure.append((t, event, int(hwnd or 0)))
        rects: dict[int, Any] = {}
        for act in self._acts:
            if act.lookup_end is not None and act.t <= t <= act.lookup_end and not act.stale \
                    and self._under_point(act, hwnd, rects):
                act.stale = True
                self.counters["stale_structure"] += 1

    def _under_point(self, act: _Act, hwnd: int, rects: dict[int, Any] | None = None) -> bool:
        """capC1c: whether a window (an event's own handle) covers the action's press point now."""
        if act.x is None or not hwnd:
            return False
        rects = {} if rects is None else rects
        if hwnd not in rects:
            try:
                rects[hwnd] = self._rect(int(hwnd))
            except Exception:  # noqa: BLE001
                rects[hwnd] = None
        rect = rects[hwnd]
        return bool(rect) and rect[0] <= act.x < rect[2] and rect[1] <= act.y < rect[3]

    def _on_out(self, t: float, event: int, hwnd: int, id_object: int, id_child: int, gen: int = 0) -> None:
        if event in STRUCTURE_EVENTS:
            self._structure(t, event, hwnd, id_object)
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S:
                if len(act.outs) < OUTCOME_CAP:      # V6: one row keeps at most the cap, however many repeats fold
                    act.outs.append((t, event, hwnd, id_object, id_child))
                else:
                    self.counters["outcome_over_row"] += 1

    def _on_fg(self, t: float, hwnd: int, pid: int) -> None:
        self._focus_valid = False
        # capC1c: a window coming to the front is never staleness by itself (a taskbar click always brings one up,
        # after the press: that is the click's outcome). Whether the window under the press changed before the
        # lookup answered is checked by machine facts when the action finishes (:meth:`_stale`).
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
        # V3: the app an action brought to the front is named on its row only when that app may be recorded at all
        # (a blocked app, Yuki's own window, a private window or an app left out: "a window changed", no name)
        app_reason = self._app_reason(win)
        for act in self._acts:
            if act.t <= t <= max(act.t, act.up_t or act.last_t) + OUTCOME_S and act.fg_change is None:
                act.fg_change = (t, None if app_reason else win.process)
        source = self._linked_source(t, INPUT_LINK_S)
        if source is not None and win.process and hwnd != self._fg.hwnd:
            act = self._new_act("switch", t, source, win, gap_ms=self._gap(t))
            act.drop = app_reason          # a switch names the app only, never its page (its page may still load)
            act.facts["window_verified"] = win.process
            act.resolved = "event"
        if win.process and win.started is not None and not app_reason:
            # V8: a file the shell opened belongs to the app it launched for it (started after the file was opened)
            wall = self._at(t)
            for i, (path, digest, proc, at) in enumerate(self._known_files):
                if proc != win.process and win.started >= at - 1.0 and wall - at <= FILE_INPUT_S:
                    self._known_files[i] = (path, digest, win.process, at)
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
        if int(getattr(sink, "jump_seq", -1)) == int(sink.anchor_seq):
            self.counters["hover_after_jump"] += 1   # software placed the pointer here (V4): not the user's rest
            return
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
            act.down_root = int(hwnd or 0)
            # spare budget only (clicks keep their reserve); an unnamed container's answer is walked like a click's
            el = self._lookup(self.uia.at_point, x, y, priority=False, target=(hwnd, pid), **self._walk_kw(hwnd))
            act.lookup_end = self._clock()
            act.answer_root = self._answer_root(x, y, el)
            if el is not None and not self._whole_window(el, hwnd) and not (el.name or "").strip() \
                    and not el.actionable():
                # capC1c: still no name and no actionable pattern after the walk (an unnamed Group, List, Window):
                # a rest over it says nothing about what the user looked at
                act.drop = "skip_hover_container"
            elif el is not None and not self._whole_window(el, hwnd):
                act.el, act.resolved, act.lookup_ms = el, "point", el.ms
            else:
                # a hover is worth a row only with its element; the whole window is none (V5)
                act.drop = "skip_hover_unresolved"
        if act.drop is not None:
            act.open, act.last_t, self._hover = False, now, None   # dropped: nothing to wait for (counted at once)

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
        if private:
            self.counters["dropped_private_clip"] += 1
            return
        source = self._linked_source(t, INPUT_LINK_S)
        if source is None:
            self.counters["clip_no_input"] += 1      # the clipboard changed without the user: not their copy
            return
        hwnd, pid = self._foreground()
        win = self._window(hwnd, owner_pid or pid)
        act = self._new_act("copy", t, source, win, clip_seq=int(seq), clip_formats=list(formats)[:8])
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
        source = self._linked_source(t, FILE_INPUT_S)
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
            if source is None:
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
            self._file_act("file_save" if saved else "file_open", t, source, win, target,
                           via="dialog" if dialog is not None else "recent")

    def _file_act(self, kind: str, t: float, source: str, win: _Window, target: str, *, via: str) -> _Act:
        act = self._new_act(kind, t, source, win, via=via)
        act.resolved = "event"
        ext = os.path.splitext(target)[1].lower()[:12]
        digest = self._digest("ui-file", os.path.normcase(os.path.normpath(target)))
        act.file = {"path": target, "ext": ext, "digest": digest}
        act.facts["file_saved" if kind == "file_save" else "file_opened"] = digest
        if act.drop is None:
            for entry in [e for e in self._known_files if e[0] == target]:
                self._known_files.remove(entry)
            self._known_files.append((target, digest, win.process, self._at(t)))
        return act

    def _save_check(self, act: _Act) -> None:
        """A save chord with no dialog (FL Studio's usual Ctrl+S): the files this app opened or saved, by their own
        write time since the chord, become ``file_save`` rows (an .flp gets its project facts). Only that app's
        files (V8): another program writing its own file meanwhile is not this chord's save."""
        chord_wall = self._at(act.t)
        for target, _digest, proc, _at in list(self._known_files):
            if not proc or proc != act.win.process:
                continue
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
            source, during_run = self._command_source(at, bool(item.get("during_run")))
            row = {"at": at, "ended_at": at + float(item.get("seconds") or 0.0), "process": "warp",
                   "kind": "command", "source": source, "via": "warp_history", "during_run": int(during_run),
                   "command_ciphertext": self._enc(secret_filter.redact(command).text), "exit_code": exit_code,
                   "facts": json.dumps(facts) if facts else None, "resolved": "app_data", "outcome": "unverifiable"
                   if exit_code is None else "changed"}
            self._batch.append(row)
            self.counters["rows"] += 1
            self.counters["source_" + source] += 1
            self.counters["resolved_app_data"] += 1

    def _command_source(self, at: float, run_then: bool) -> tuple[str, bool]:
        """Who ran a Warp command and whether a Yuki run was in progress then (V4): the Enter that ran it (the
        nearest to the command's own start, within :data:`COMMAND_ENTER_S`) gives both; with none seen, a run in
        progress at that time (from the controller's run spans) makes it Yuki's, else the user's."""
        best = None
        for wall, source, during in self._enters:
            gap = abs(wall - at)
            if gap <= COMMAND_ENTER_S and (best is None or gap < best[0]):
                best = (gap, source, during)
        if best is not None:
            return best[1], bool(best[2] or run_then)
        return ("yuki" if run_then else "user"), run_then

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
                target = (hwnd, act.win.pid if hwnd == act.win.hwnd else 0)
                if self._hung_skip(target, probe=True) or not self._take(priority=act.el is None
                                                                         or act.resolved != "point"):
                    return
                t0 = time.perf_counter()
                try:
                    el = self.uia.from_event(hwnd, id_object, id_child, budget_s=LOOKUP_S, index=True)
                except Exception as exc:  # noqa: BLE001
                    self._error("event_lookup", exc)
                    el = None
                self.lookup_ms.append((time.perf_counter() - t0) * 1000.0)
                self.counters["lookups"] += 1
                self._hung_note(target, time.perf_counter() - t0)
                if el is not None and el.contains(act.x, act.y) and (not el.pid or not act.win.pid
                                                                    or el.pid == act.win.pid):
                    if (el.low_info() and act.el is not None and not act.el.low_info()
                            and act.resolved in ("point", "snapshot") and not self._stale(act, count=False)):
                        # capC1c: the event named only an unnamed container (a focused page region) while the press
                        # itself found a real control there, still current: the control is kept
                        self.counters["event_container_kept_point"] += 1
                        return
                    act.el, act.resolved, act.lookup_ms = el, "event", el.ms
                    return
                return

    def _stale(self, act: _Act, *, count: bool = True) -> bool:
        """capC1c: whether the element a lookup answered may not be what was under the press, by machine facts only:
        a structure change whose own window covers the point while the lookup answered (decided as the events came,
        ``act.stale``; the short history covers one that reached the queue before the press itself); for an element
        whose own window is unknown, another top-level window under the point when ``ElementFromPoint`` answered than
        at the press (V2-4); the element's own top-level
        window not the one under the press; or its bounds no longer holding the point. A window that came to the
        front after the press (a taskbar click's app) is the click's outcome, never staleness."""
        if act.resolved not in ("point", "snapshot") or act.lookup_end is None:
            return False
        if act.stale:
            return True
        el = act.el
        own_window = int(getattr(el, "window", 0) or 0) if el is not None else 0
        # V2-4: the window under the point at the answer decides only for an element whose own window is unknown; one
        # whose window is known is judged by that window (below), which is what was under the point when it answered
        if not own_window and act.down_root and act.answer_root and act.answer_root != act.down_root:
            self.counters["stale_root_changed"] += int(count)
            return True
        if el is not None and act.x is not None:
            if el.bounds and not el.contains(act.x, act.y):
                self.counters["stale_bounds"] += int(count)
                return True
            window = own_window
            if window and act.down_root:
                try:
                    top = int(self._root_of(window) or 0)
                except Exception:  # noqa: BLE001
                    top = 0
                if top and top != act.down_root:
                    self.counters["stale_other_window"] += int(count)
                    return True
        rects: dict[int, Any] = {}
        if any(act.t <= t <= act.lookup_end and self._under_point(act, hwnd, rects)
               for t, _e, hwnd in self._recent_structure):
            self.counters["stale_structure"] += int(count)
            return True
        return False

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
            if act.fg_change[1]:
                act.facts.setdefault("window_verified", act.fg_change[1])
        page_after = None
        if act.win.pid:
            try:
                page_after = self._page(act.win.pid, act.win.hwnd)[0]
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
            el = self._lookup(self.uia.from_event, hwnd, id_object, id_child, priority=False,
                              target=(hwnd, act.win.pid if hwnd == act.win.hwnd else 0), probe=True)
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
            target = (act.win.hwnd, act.win.pid)
            if not self._hung_skip(target) and self._take(priority=False):
                t0 = time.perf_counter()
                state = self.uia.reread(el)
                self._hung_note(target, time.perf_counter() - t0)
                if "toggle" in state:
                    act.facts["toggle_state"] = state["toggle"]
                if state.get("selected"):
                    act.facts["selected"] = True
        if act.kind == "keys" and act.chord == "Enter" and act.after_type and act.after_type.get("el") is not None:
            field_el = act.after_type["el"]
            target = (act.win.hwnd, act.win.pid)
            if field_el.role in EDIT_ROLES and not self._hung_skip(target) and self._take(priority=False):
                t0 = time.perf_counter()
                state = self.uia.reread(field_el, value_length=True)
                self._hung_note(target, time.perf_counter() - t0)
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
        if act.drop is None and act.win.pid and act.kind in POINTER_KINDS | FOCUS_KINDS:
            # the timeline may only now have caught up with the window (it came to the front with this press, or
            # its page changed under the keys: Ctrl+Tab then typing): checked again for keys and typing too (V1)
            now_win = self._window(act.win.hwnd, act.win.pid)
            reason = self._privacy_reason(now_win)
            if reason == f"withheld:{PAGE_UNKNOWN}" and act.win.withheld != PAGE_UNKNOWN:
                # the page was known and allowed when the action began (no title change pending, the probe answered
                # for that very window); the page it led to (a link, Enter in a search box) is still loading. A page
                # that settled on a blocked site by now still drops the row.
                reason = None
            act.drop = reason
            if act.win.page_key is None:
                act.win.page_key = now_win.page_key
        if act.drop is None and act.end_el is not None and act.end_win is not None:
            end_hwnd, end_pid = act.end_win
            end_now = self._window(end_hwnd, end_pid)
            end_reason = self._privacy_reason(end_now)
            if end_reason == f"withheld:{PAGE_UNKNOWN}" and (end_hwnd, end_pid) == (act.win.hwnd, act.win.pid):
                end_reason = None                    # the same window as the start, known at the press
            if end_reason is not None:
                act.end_el = None                    # the drop window is withheld (V2): the drag keeps its start only
                self.counters["drag_end_withheld"] += 1
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
        if act.el is not None:
            patterns = getattr(act.el, "patterns", None)
            if patterns is not None:
                # capC1c: the actionable patterns the element exposes (machine facts; the go-tos leave out an element
                # with no name and none of them)
                act.facts["patterns"] = list(patterns)
            if act.kind in POINTER_KINDS:
                # an answer still an unnamed container is counted as one whatever the walk tried (a descent that
                # stopped on one is not a real control), so walk_* counts only containers that became controls
                refined = getattr(act.el, "refined", None)
                if act.el.low_info():
                    self.counters["walk_container_left"] += 1
                elif refined:
                    self.counters["walk_" + refined] += 1
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
            else None, "resolved": act.resolved if act.el is not None
            or act.resolved in ("event", "app_data", "stale") else "unresolved",
            "lookup_ms": round(act.lookup_ms, 2) if act.lookup_ms is not None else None,
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
            if act.container_role:
                row["role"] = act.container_role     # point-only in a blind window: the container, for context (V5)
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

        hung = dict(getattr(self.hung, "counts", {}) or {})
        if hung:
            hung["hung_now"] = int(self.hung.marked()) if hasattr(self.hung, "marked") else 0
        return {**dict(self.counters), **hung, "lookup_ms_p50": pct(0.5), "lookup_ms_p95": pct(0.95),
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


def _root_of(hwnd: int) -> int:
    """capC1c: a window's top-level window (``GetAncestor(GA_ROOT)``); 0 when it is gone."""
    from yuki.memory.trace_hooks import _U, _GA_ROOT

    return int(_U.GetAncestor(int(hwnd), _GA_ROOT) or 0) if hwnd else 0


def _window_pid(hwnd: int) -> int:
    """The process a pressed window shows, read the way :func:`_window_at` reads it (second verification V2-2): for a
    UWP frame (``ApplicationFrameWindow``) the hosted app's (``content_pid``: enumerates child windows, sends no
    messages), so the privacy gates see the app, never its host. A frame showing only its host (the app's window
    detached or gone) is 0: the press's app is unknown and the press keeps nothing. 0 when the window is gone."""
    try:
        from yuki.memory.extract.uia import _UWP_FRAME_CLASS, content_pid

        pid = int(content_pid(int(hwnd)) or 0) if hwnd else 0
        if not pid:
            return 0
        import win32gui
        import win32process

        if win32gui.GetClassName(int(hwnd)) == _UWP_FRAME_CLASS \
                and pid == int(win32process.GetWindowThreadProcessId(int(hwnd))[1]):
            return 0
        return pid
    except Exception:  # noqa: BLE001
        return 0


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
    """``(pid, hwnd) -> (page_key, url, withheld)`` from the timeline's open stretch, when it is that very window
    and settled.

    Second verify V1: a window of a process that shows pages (the timeline's last page probe of it found one, or the
    trace saw it on a page) gets ``(None, None, PAGE_UNKNOWN)`` (the blocked-site gate fails closed) when the
    timeline has no settled reading of that window: another window of the same browser (one pid, several windows), a
    browser not in front, a title change still settling (Ctrl+Tab), a page probe not answered yet, or a probe that
    found no page. A process that never showed a page (an editor, Explorer) is no page: ``(None, None, None)``."""
    cache: dict[tuple, str] = {}
    paged: set[int] = set()

    def has_pages(tl: Any, pid: int) -> bool:
        if pid in paged:
            return True
        try:
            entry = (getattr(tl, "_no_page", None) or {}).get(pid) if tl is not None else None
        except Exception:  # noqa: BLE001
            entry = None
        return entry is not None and int(entry[0]) == 0

    def page(pid: int, hwnd: int = 0) -> tuple[str | None, str | None, str | None]:
        if not pid:
            return None, None, None
        tl = timeline_now()
        st = getattr(tl, "_current", None) if tl is not None else None
        same = (st is not None and int(getattr(st, "pid", 0) or 0) == int(pid)
                and (not hwnd or int(getattr(st, "hwnd", 0) or 0) == int(hwnd)))
        settling = getattr(tl, "_title_first", None) is not None
        if not same or not getattr(st, "settled", False) or settling:
            return (None, None, PAGE_UNKNOWN) if has_pages(tl, int(pid)) else (None, None, None)
        seen = getattr(st, "seen_host", None) or st.host
        if seen or st.path:
            if len(paged) > 512:
                paged.clear()
            paged.add(int(pid))
        elif not st.withheld and has_pages(tl, int(pid)):
            return None, None, PAGE_UNKNOWN          # settled without a page in a process that shows pages
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
        self.dropped_queue_full = 0
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
        #: Wall-time spans of Yuki's runs (start, end or None while one runs), from the beat: whether a Warp command
        #: (read minutes later) ran during one (second verify V4). Both span lists are read by the Warp reader's
        #: thread, so they change under this lock.
        self._runs: deque[list[float | None]] = deque(maxlen=256)
        self._span_lock = threading.Lock()

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
        from yuki.memory.trace_hooks import TraceHooks, recent_folder, root_at
        from yuki.memory.trace_input import Metrics, TraceInput

        recent = recent_folder()
        self.hooks = TraceHooks(self._put_input, recent=recent)
        metrics = Metrics()
        self.sink = TraceInput(self._put_input, metrics=metrics, on_action=self.hooks.action, root_at=root_at,
                               pid_of=_window_pid)
        rules_fn = self.privacy.rules
        self.worker = TraceWorker(
            self.store, privacy_rules=rules_fn, trace_rules=self.rules, gate=self._gate_holds, acting=self._acting,
            paused=lambda: self._paused or (self._ab is not None and not self._ab.on_now()),
            page=timeline_page(self._timeline_now, self.store), title=_title, rect=_rect, sink=self.sink,
            cursor=self.sink._cursor, clipboard=_clipboard_now, recent=recent, log=self._log_fn,
            hover=(metrics.hover_w, metrics.hover_h, metrics.hover_s))
        self.hooks.start()
        self._thread = threading.Thread(target=self._run, name="yuki-trace-worker", daemon=True)
        self._thread.start()
        self.state = "started"
        self._log("trace_start", metrics=metrics.facts(), hooks=bool(self.hooks.hwnd), hooks_error=self.hooks.error)
        return self

    def _put_input(self, item: Any) -> None:
        """Queues one input or hook item unless the worker is that far behind (``QUEUE_MAX``): then it is dropped
        and counted, so a stalled worker costs bounded memory."""
        if self.queue.qsize() >= QUEUE_MAX:
            self.dropped_queue_full += 1
            return
        self.queue.put(item)

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
        kept = [dict(c, during_run=self.run_at(c.get("at"))) for c in items if self.open_at(c.get("at"))]
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
        with self._span_lock:
            if self._first_open is None or at < self._first_open:
                return False
            return not any(start <= at and (end is None or at < end) for start, end in self._spans)

    def run_at(self, at: Any) -> bool:
        """Whether a Yuki run was in progress at this wall time (the beat's one-second view of the acting marker)."""
        try:
            at = float(at)
        except (TypeError, ValueError):
            return False
        with self._span_lock:
            return any(start - 1.0 <= at and (end is None or at <= end + 1.0) for start, end in self._runs)

    def _follow_runs(self) -> None:
        try:
            acting = self._acting()
        except Exception:  # noqa: BLE001
            acting = None
        now = time.time()
        with self._span_lock:
            open_run = bool(self._runs) and self._runs[-1][1] is None
            if acting is not None and not open_run:
                try:
                    start = min(now, float(getattr(acting, "started_at", now) or now))
                except (TypeError, ValueError):
                    start = now
                self._runs.append([start, None])
            elif acting is None and open_run:
                self._runs[-1][1] = now

    def _span(self, blocked: bool) -> None:
        with self._span_lock:
            self._span_locked(blocked)

    def _span_locked(self, blocked: bool) -> None:
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
        self._follow_runs()               # a memory read of the acting marker: cheap, once a second
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
                      "events_over_cap": hooks.get("outcome_over", 0), "dropped_queue_full": self.dropped_queue_full,
                      **stats.get("worker", {})}
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

