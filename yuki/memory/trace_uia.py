"""The trace worker's UI Automation client (report 78 §4.2 step 2): what element an action was on.

Own thread and COM apartment (the trace worker's), its own client, cached properties only, Windows' own call
timeouts (``IUIAutomation2``: :data:`TIMEOUT_MS`), so a hung app cannot hold the worker for longer than that. Every
answer is an :class:`Element`: the control type, name, AutomationId, RuntimeId, bounds, ``IsPassword``, toggle and
selection state, the parent's role and up to :data:`ANCESTORS` ancestors' roles and AutomationIds, the process. A
live reference is kept on the object for one follow-up read (the state after the action), never logged, never
stored.

* :meth:`TraceUia.at_point`: ``ElementFromPointBuildCache`` at the point taken at button-down (path ii); with
  ``refine`` (capC1c), inside the same lookup, a renderer's whole pane gets the descent and any other unnamed
  container (:data:`CONTAINER_ROLES`) the walk to the real control (:meth:`TraceUia.refine`: with no actionable
  pattern (:data:`ACTION_PATTERNS`), the nearest actionable or named ancestor still under the point, else the smallest
  named descendant under it; an actionable but unnamed wrapper, a named element under the point if there is one);
* :meth:`TraceUia.from_event`: the element an ``INVOKED``, ``SELECTION`` or ``FOCUS`` WinEvent named (path i), and
  the elements an action's outcome events changed. capQ2a: a window's own object is UI Automation's element for that
  handle (``ElementFromHandleBuildCache``); any other object's place comes from MSAA (``AccessibleObjectFromEvent``
  and ``accLocation``) and the element is UI Automation's own answer there (``ElementFromPointBuildCache``, walked up
  to the element whose bounds are that object's). ``ElementFromIAccessibleBuildCache`` is no longer called: every
  one of the 868 crash-log entries of 8 Oct (``0x8001010d``, a handled COM error that Python's crash handler writes
  down anyway) came from it. COM errors are counted (:attr:`TraceUia.counts`), never logged;
* :meth:`TraceUia.descend`: from a renderer's whole pane (Chromium, CEF answer ``ElementFromPoint`` with the page),
  the smallest element under the point, one level of children at a time within the deadline (path iii);
* :meth:`TraceUia.focused`: the focused element (the field typing goes into; ``IsPassword`` decides);
* :meth:`TraceUia.reread`: the clicked element's toggle or selection state after the action, or a field's value
  *length* (UI Automation has no length-only read: the value passes through this one call and only its length is
  kept);
* :meth:`TraceUia.field_state` (capQ2a): whether an element is a text field, by UI Automation's own facts (an
  editable Value, a TextPattern whose ``IsReadOnly`` reads false, or a keyboard-focusable Edit or Document that is
  not read-only), and its *visible length* (the Value's, else the TextPattern document range's, at most
  :data:`FIELD_TEXT_CHARS`): what a submit check needs in Chromium and Electron editors, which are often Groups with
  a TextPattern and no Value. A password field (or one whose ``IsPassword`` cannot be read) is never read at all.

Nothing here clicks, focuses or moves anything.
"""

from __future__ import annotations

import ctypes
import time
import unicodedata
from dataclasses import dataclass, field
from typing import Any

#: UI Automation's own connection and transaction timeouts for this client (ms): a lookup's budget.
TIMEOUT_MS = 150
#: Ancestors whose role and AutomationId are kept (the element key's ancestry).
ANCESTORS = 4
#: Children levels a renderer descent goes down at most.
DESCENT_LEVELS = 14

_P_RUNTIME_ID, _P_BOUNDS, _P_PID, _P_TYPE, _P_NAME, _P_AID, _P_PASSWORD, _P_HWND = (30000, 30001, 30002, 30003, 30005,
                                                                                   30011, 30019, 30020)
_P_TOGGLE, _P_SELECTED, _P_VALUE = 30086, 30079, 30045
_P_HAS_TOGGLE, _P_HAS_SELECTION_ITEM = 30041, 30036     # Is...PatternAvailable: a state only where the pattern is
_P_HAS_INVOKE, _P_HAS_EXPAND, _P_HAS_VALUE = 30031, 30028, 30043
#: ``ValueIsReadOnly`` (second verification V2-1): a Value pattern counts as actionable only when it can be set.
#: Every Chromium / Electron page Document exposes a read-only Value (the page's address): no control to act on.
_P_VALUE_READONLY = 30046
#: capC1c: the control patterns that make an element something a person acts on (UI Automation's own
#: ``Is...PatternAvailable`` properties, read with the element's other cached facts): the element's own machine facts,
#: never a word list. Stored with the row as ``facts.patterns``. ``value`` only when ``ValueIsReadOnly`` reads false.
ACTION_PATTERNS = ((_P_HAS_INVOKE, "invoke"), (_P_HAS_TOGGLE, "toggle"), (_P_HAS_SELECTION_ITEM, "select"),
                   (_P_HAS_EXPAND, "expand"), (_P_HAS_VALUE, "value"))
#: capC1c: the control types that only hold other controls (UI Automation's fixed vocabulary). One of these with no
#: name and none of :data:`ACTION_PATTERNS` says nothing about what was clicked: the resolution walk
#: (:meth:`TraceUia.refine`) looks for the real control around the point.
CONTAINER_ROLES = frozenset({"Group", "Pane", "Document", "Custom", "Window"})
#: Levels the walk goes up (to the nearest actionable or named ancestor) and down (to the smallest named descendant
#: under the point) at most, and the share of a lookup's time budget it may use (the rest is kept for the chosen
#: element's ancestry, which its element key needs).
REFINE_UP = 6
REFINE_DOWN = 8
REFINE_SHARE = 0.75
#: capQ2a: ``IsKeyboardFocusable`` and ``IsTextPatternAvailable`` (cached with the other facts), the TextPattern's id
#: and the ``IsReadOnly`` text attribute (read only by :meth:`TraceUia.field_state`).
_P_FOCUSABLE, _P_HAS_TEXT = 30009, 30040
_TEXT_PATTERN_ID = 10014
_ATTR_READONLY = 40015
#: capQ2a: characters of a field's text read at most when it has no Value (a TextPattern document range): enough to
#: tell an emptied composer (an editor keeps a newline or a zero-width mark) from one still holding text; the text
#: itself is dropped at once, only how many visible characters it had is kept.
FIELD_TEXT_CHARS = 256
#: capQ2a: control types whose input is a press, never typed text (UI Automation's fixed vocabulary). An app may
#: expose a settable Value on one (measured read-only 9 Oct: two of Discord's Buttons do); it is still no text field.
PRESS_ROLES = frozenset({"Button", "CheckBox", "RadioButton", "SplitButton", "Hyperlink", "MenuItem", "TabItem"})
#: capQ2a: the control types typing went into before the facts above were read (a fake or an older answer).
LEGACY_FIELD_ROLES = frozenset({"Edit", "Document", "ComboBox"})
#: capQ2a: MSAA object ids of a window itself (``OBJID_WINDOW``, ``OBJID_CLIENT``): with ``CHILDID_SELF`` an event
#: about one is UI Automation's element for that handle.
_OBJID_WINDOW, _OBJID_CLIENT = 0, -4
#: capQ2a: a bounds difference (px) still counted as the same element: UI Automation's bounds are fractional for
#: Chromium and MSAA's whole pixels, so the two readings of one element may round apart by one.
BOUNDS_SLACK = 1


def visible_length(text: str | None) -> int:
    """capQ2a: the characters a person would see: whitespace, control and format characters (Unicode's own
    categories ``Cc``, ``Cf``, ``Zs``, ``Zl``, ``Zp``: an editor's trailing newline, a zero-width space or
    no-break mark it keeps in an empty line) are not counted."""
    if not text:
        return 0
    return sum(1 for ch in text if not ch.isspace() and unicodedata.category(ch) not in ("Cc", "Cf", "Zs", "Zl", "Zp"))


def _tick() -> int:
    """``GetTickCount`` (ms): the clock a WinEvent's own time (``dwmsEventTime``) is in."""
    try:
        return int(ctypes.windll.kernel32.GetTickCount()) & 0xFFFFFFFF
    except Exception:  # noqa: BLE001
        return 0
_TREE_SCOPE_ELEMENT, _TREE_SCOPE_CHILDREN = 1, 2
_CUIAUTOMATION8_CLSID = "{e22ad333-b25f-460c-83d0-0581107395c9}"
_CUIAUTOMATION_CLSID = "{ff48dba4-60ef-4201-aa87-54103eef594e}"

ROLE_NAMES = {
    50000: "Button", 50001: "Calendar", 50002: "CheckBox", 50003: "ComboBox", 50004: "Edit", 50005: "Hyperlink",
    50006: "Image", 50007: "ListItem", 50008: "List", 50009: "Menu", 50010: "MenuBar", 50011: "MenuItem",
    50012: "ProgressBar", 50013: "RadioButton", 50014: "ScrollBar", 50015: "Slider", 50016: "Spinner",
    50017: "StatusBar", 50018: "Tab", 50019: "TabItem", 50020: "Text", 50021: "ToolBar", 50022: "ToolTip",
    50023: "Tree", 50024: "TreeItem", 50025: "Custom", 50026: "Group", 50027: "Thumb", 50028: "DataGrid",
    50029: "DataItem", 50030: "Document", 50031: "SplitButton", 50032: "Window", 50033: "Pane", 50034: "Header",
    50035: "HeaderItem", 50036: "Table", 50037: "TitleBar", 50038: "Separator", 50039: "SemanticZoom",
    50040: "AppBar",
}
TOGGLE_WORDS = {0: "off", 1: "on", 2: "mixed"}


@dataclass
class Element:
    """One element as the trace keeps it (plain values; ``ref`` is the live reference for one follow-up read)."""

    role: str = ""
    name: str = ""
    aid: str = ""
    rid: tuple[int, ...] | None = None
    bounds: tuple[int, int, int, int] | None = None    # left, top, right, bottom (screen px)
    password: bool = False
    password_known: bool = True                        # False: IsPassword could not be read (fails closed)
    toggle: int | None = None
    selected: bool | None = None
    pid: int = 0
    hwnd: int = 0
    parent_role: str = ""
    ancestors: list[tuple[str, str]] = field(default_factory=list)   # (role, AutomationId), nearest first
    index: int | None = None
    ms: float = 0.0
    ref: Any = field(default=None, repr=False, compare=False)
    #: capC1c: the actionable control patterns it exposes (:data:`ACTION_PATTERNS`); ``None``: not read (a fake or an
    #: older answer), treated by its control type.
    patterns: tuple[str, ...] | None = None
    #: capC1c: how the resolution walk found it from an unnamed container: ``"up"`` (an ancestor), ``"down"`` (a
    #: descendant) or ``"path"`` (on a renderer descent's way down); None: the lookup's own answer.
    refined: str | None = None
    #: capC1c: the first native window handle on it or its ancestors (its top-level window is that one's root).
    window: int = 0
    #: V2-4: the top-level window under the point right when ``ElementFromPoint`` answered (0: not read).
    root: int = 0
    #: capQ2a: ``IsKeyboardFocusable``, ``IsTextPatternAvailable`` and a Value pattern that reads read-only (None: not
    #: read): the cached facts :meth:`field_hint` decides by.
    focusable: bool | None = None
    text: bool | None = None
    value_ro: bool | None = None
    #: capQ2a: when the lookup's answer was settled (``ElementFromPoint`` and the resolution walk done, before the
    #: ancestry, which never changes which element it is): the worker's clock (``time.monotonic``) and Windows' tick
    #: count, the clock a WinEvent's own time is in. None: not stamped (the lookup's end stands for it).
    chosen_at: float | None = None
    chosen_tick: int | None = None

    def field_hint(self) -> bool:
        """capQ2a: whether typing may go into it, by the cached facts alone (no call): an editable Value (not a press
        control, :data:`PRESS_ROLES`), a TextPattern on a keyboard-focusable element (its ``IsReadOnly`` is read when it
        matters, :meth:`TraceUia.field_state`), or a keyboard-focusable Edit or Document whose Value is not read-only.
        Facts not read (a fake or an older answer): by control type, as before."""
        if self.patterns is None and self.focusable is None and self.text is None:
            return self.role in LEGACY_FIELD_ROLES
        if self.role in PRESS_ROLES:
            return False
        if self.patterns is not None and "value" in self.patterns:
            return True
        if self.text and self.focusable:
            return True
        return bool(self.focusable) and self.role in ("Edit", "Document") and not self.value_ro

    def actionable(self) -> bool:
        """Whether it exposes one of :data:`ACTION_PATTERNS` (Invoke, Toggle, SelectionItem, ExpandCollapse, Value).
        Unknown patterns (``None``) count as actionable unless it is a container type (no word list: control types)."""
        if self.patterns is None:
            return self.role not in CONTAINER_ROLES
        return bool(self.patterns)

    def low_info(self) -> bool:
        """An unnamed container with no actionable pattern: it says nothing about what the user acted on."""
        return self.role in CONTAINER_ROLES and not (self.name or "").strip() and not self.actionable()

    def secret(self) -> str | None:
        """Why nothing about this element may be kept: ``password``, ``password_unknown`` (IsPassword unreadable:
        fails closed, as the watcher's ``_PasswordProbe`` treats an unknown answer), else None."""
        if self.password:
            return "password"
        if not self.password_known:
            return "password_unknown"
        return None

    def contains(self, x: int, y: int) -> bool:
        b = self.bounds
        return bool(b) and b[0] <= x < b[2] and b[1] <= y < b[3]

    def area(self) -> int:
        b = self.bounds
        return max(0, b[2] - b[0]) * max(0, b[3] - b[1]) if b else 0


def _bounds(value: Any) -> tuple[int, int, int, int] | None:
    try:
        if value is None:
            return None
        if hasattr(value, "left"):
            l, t, r, b = int(value.left), int(value.top), int(value.right), int(value.bottom)
        else:
            l, t, w, h = (float(v) for v in value)
            l, t, r, b = int(l), int(t), int(l + w), int(t + h)
        if r <= l or b <= t:
            return None
        return l, t, r, b
    except Exception:  # noqa: BLE001
        return None


class TraceUia:
    """The worker's UI Automation client. Create it on the worker thread (it initialises COM there)."""

    def __init__(self) -> None:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        except OSError:
            pass
        self._comtypes = comtypes
        module = comtypes.client.GetModule("UIAutomationCore.dll")
        self._module = module
        try:
            uia = comtypes.client.CreateObject(_CUIAUTOMATION8_CLSID, interface=module.IUIAutomation2)
            uia.ConnectionTimeout = TIMEOUT_MS
            uia.TransactionTimeout = TIMEOUT_MS
        except Exception:  # noqa: BLE001 - an older Windows: no timeouts, the deadline still bounds the work
            uia = comtypes.client.CreateObject(_CUIAUTOMATION_CLSID, interface=module.IUIAutomation)
        self._uia = uia
        request = uia.CreateCacheRequest()
        for prop in (_P_RUNTIME_ID, _P_BOUNDS, _P_PID, _P_TYPE, _P_NAME, _P_AID, _P_PASSWORD, _P_HWND, _P_TOGGLE,
                     _P_SELECTED, _P_HAS_TOGGLE, _P_HAS_SELECTION_ITEM, _P_HAS_INVOKE, _P_HAS_EXPAND, _P_HAS_VALUE,
                     _P_VALUE_READONLY, _P_FOCUSABLE, _P_HAS_TEXT):
            request.AddProperty(prop)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        self._request = request
        # capQ2a: a field's facts first (IsPassword decides before anything of its text is asked for)
        field_request = uia.CreateCacheRequest()
        for prop in (_P_TYPE, _P_PASSWORD, _P_HAS_VALUE, _P_VALUE_READONLY, _P_HAS_TEXT, _P_FOCUSABLE):
            field_request.AddProperty(prop)
        field_request.TreeScope = _TREE_SCOPE_ELEMENT
        self._field_request = field_request
        #: capQ2a: content-free counters (COM errors by code, event lookups that found no element there): the trace
        #: worker adds them to its health, so an app that does not answer is a count, never a crash-log entry.
        self.counts: dict[str, int] = {}
        self._walker = uia.ControlViewWalker
        self._true = uia.CreateTrueCondition()
        self._oleacc = ctypes.oledll.oleacc
        try:
            uia.GetFocusedElementBuildCache(request)   # pay the client's first-connection cost now
        except Exception:  # noqa: BLE001
            pass

    # -- facts -------------------------------------------------------------------------------------------------------

    def _facts(self, el: Any, t0: float) -> Element | None:
        if not el:
            return None
        get = el.GetCachedPropertyValue
        try:
            ctype = get(_P_TYPE)
        except Exception:  # noqa: BLE001
            return None
        rid = None
        try:
            raw = get(_P_RUNTIME_ID)
            rid = tuple(int(v) for v in raw) if raw else None
        except Exception:  # noqa: BLE001
            pass

        def text(prop: int) -> str:
            try:
                v = get(prop)
                return v if isinstance(v, str) else ""
            except Exception:  # noqa: BLE001
                return ""

        def number(prop: int) -> int | None:
            try:
                v = get(prop)
                return int(v) if isinstance(v, (int, bool)) else None
            except Exception:  # noqa: BLE001
                return None

        password = number(_P_PASSWORD)
        selected = number(_P_SELECTED) if number(_P_HAS_SELECTION_ITEM) else None
        toggle = number(_P_TOGGLE) if number(_P_HAS_TOGGLE) else None
        # V2-1: a read-only Value (or one whose ValueIsReadOnly cannot be read) is nothing a person sets
        has_value = bool(number(_P_HAS_VALUE))
        value_ro = number(_P_VALUE_READONLY)
        patterns = tuple(word for prop, word in ACTION_PATTERNS
                         if number(prop) and (prop != _P_HAS_VALUE or value_ro == 0))
        hwnd = number(_P_HWND) or 0
        focusable, has_text = number(_P_FOCUSABLE), number(_P_HAS_TEXT)
        return Element(role=ROLE_NAMES.get(int(ctype), f"ControlType{ctype}") if isinstance(ctype, int) else "",
                       name=text(_P_NAME), aid=text(_P_AID), rid=rid, bounds=_bounds(get(_P_BOUNDS)),
                       password=bool(password), password_known=password is not None, toggle=toggle,
                       selected=None if selected is None else bool(selected), pid=number(_P_PID) or 0,
                       hwnd=hwnd, ms=round((time.perf_counter() - t0) * 1000.0, 2), ref=el, patterns=patterns,
                       window=hwnd, focusable=None if focusable is None else bool(focusable),
                       text=None if has_text is None else bool(has_text),
                       value_ro=has_value and value_ro != 0)

    def _com_error(self, exc: BaseException) -> None:
        """capQ2a: a COM error from an app that did not answer, counted by its code (content-free), never logged."""
        code = getattr(exc, "hresult", None)
        if code is None and exc.args and isinstance(exc.args[0], int):
            code = exc.args[0]
        key = f"com_error_{int(code) & 0xFFFFFFFF:08x}" if isinstance(code, int) else f"com_error_{type(exc).__name__}"
        self.counts[key] = self.counts.get(key, 0) + 1

    def _ancestry(self, element: Element, deadline: float, *, index: bool) -> None:
        """Parent role, up to :data:`ANCESTORS` ancestors (role, AutomationId) and, when asked, the index among the
        parent's children (one more call), within the deadline."""
        current = element.ref
        parent_el = None
        for depth in range(ANCESTORS):
            if time.perf_counter() >= deadline:
                return
            try:
                parent = self._walker.GetParentElementBuildCache(current, self._request)
            except Exception:  # noqa: BLE001
                return
            facts = self._facts(parent, time.perf_counter())
            if facts is None:
                return
            if depth == 0:
                element.parent_role = facts.role
                parent_el = parent
            if not element.window and facts.hwnd:
                element.window = facts.hwnd          # capC1c: its top-level window is this handle's root
            element.ancestors.append((facts.role, facts.aid))
            if facts.role == "Window" and facts.hwnd:
                break
            current = parent
        if index and parent_el is not None and element.rid and time.perf_counter() < deadline:
            try:
                kids = parent_el.FindAllBuildCache(_TREE_SCOPE_CHILDREN, self._true, self._request)
                for i in range(min(int(kids.Length), 500)):
                    kid = self._facts(kids.GetElement(i), time.perf_counter())
                    if kid is not None and kid.rid == element.rid:
                        element.index = i
                        break
            except Exception:  # noqa: BLE001
                pass

    # -- lookups -----------------------------------------------------------------------------------------------------

    #: capC1c: this client has the resolution walk (the worker passes ``refine`` / ``max_area`` only to one that does).
    refines = True

    @staticmethod
    def _covers(el: Element, max_area: int) -> bool:
        """Whether it covers the window (half its area or more): a whole pane, never "the real control"."""
        return bool(max_area) and el.area() * 2 >= max_area

    @staticmethod
    def unnamed_container(el: Element) -> bool:
        """An unnamed container (:data:`CONTAINER_ROLES`, no name), actionable or not: the walk's starting point
        (second verification V2-3: Chromium and Electron wrap most named controls in unnamed Groups that expose
        Invoke)."""
        return el.role in CONTAINER_ROLES and not (el.name or "").strip()

    def refine(self, element: Element, x: int, y: int, *, deadline: float, max_area: int = 0) -> Element | None:
        """capC1c: the real control behind an unnamed container (:meth:`unnamed_container`), by UI Automation's own
        facts. One with no actionable pattern (:meth:`Element.low_info`): (1) the nearest ancestor that exposes an
        actionable pattern (:data:`ACTION_PATTERNS`) or has a name and still contains the point (never one that covers
        the window, ``max_area`` being the window's area); else (2) the smallest descendant under the point that has a
        name or an actionable pattern. One that is actionable but unnamed (a clickable wrapper, V2-3): a *named*
        element under the point, the smallest named descendant first, then the nearest named ancestor; the wrapper
        itself stands when neither is found. None when nothing is found before ``deadline``. Reads only; no ancestry
        (the caller takes it for the element it keeps)."""
        if element.actionable():
            return (self._refine_down(element, x, y, deadline=deadline, named_only=True)
                    or self._refine_up(element, x, y, deadline=deadline, max_area=max_area, named_only=True))
        return (self._refine_up(element, x, y, deadline=deadline, max_area=max_area, named_only=False)
                or self._refine_down(element, x, y, deadline=deadline, named_only=False))

    def _refine_up(self, element: Element, x: int, y: int, *, deadline: float, max_area: int,
                   named_only: bool) -> Element | None:
        current = element.ref
        for _level in range(REFINE_UP):
            if current is None or time.perf_counter() >= deadline:
                break
            try:
                parent = self._walker.GetParentElementBuildCache(current, self._request)
            except Exception:  # noqa: BLE001
                break
            facts = self._facts(parent, time.perf_counter())
            if facts is None or not facts.contains(x, y) or self._covers(facts, max_area):
                break
            if (facts.name or "").strip() or (not named_only and facts.actionable()):
                facts.refined = "up"
                return facts
            if facts.role == "Window" and facts.hwnd:
                break                                # a window of its own: nothing above it is the control
            current = parent
        return None

    def _refine_down(self, element: Element, x: int, y: int, *, deadline: float, named_only: bool) -> Element | None:
        best: Element | None = None
        current = element.ref
        for _level in range(REFINE_DOWN):
            if current is None or time.perf_counter() >= deadline:
                break
            try:
                kids = current.FindAllBuildCache(_TREE_SCOPE_CHILDREN, self._true, self._request)
                n = min(int(kids.Length), 400)
            except Exception:  # noqa: BLE001
                break
            hit: Element | None = None
            for i in range(n):
                kid = self._facts(kids.GetElement(i), time.perf_counter())
                if kid is not None and kid.contains(x, y) and (hit is None or kid.area() < hit.area()):
                    hit = kid
            if hit is None:
                break
            if (hit.name or "").strip() or (not named_only and hit.actionable()):
                best = hit                           # deeper is smaller: the last one found is the smallest
            current = hit.ref
        if best is not None:
            best.refined = "down"
        return best

    def at_point(self, x: int, y: int, *, budget_s: float, index: bool = False, refine: bool = False,
                 max_area: int = 0) -> Element | None:
        """The element at a screen point (path ii), with its ancestry. ``refine`` (capC1c), all within this one lookup
        (one token of the budget, one hung-app check, :data:`REFINE_SHARE` of its time, the rest kept for the
        ancestry): a container covering the window (``max_area``: the window's area; a renderer's whole pane) gets
        the renderer descent at once (``refined`` ``"descent"`` / ``"path"``), any other unnamed container the walk
        (:meth:`refine`, ``"up"`` / ``"down"``). A whole pane nothing deeper answers for comes back as it is.
        ``Element.root`` is the top-level window under the point right when ``ElementFromPoint`` answered (V2-4),
        before the walk and the ancestry: a window the click itself brings up later is never read as the answer's."""
        from ctypes import wintypes

        t0 = time.perf_counter()
        try:
            el = self._uia.ElementFromPointBuildCache(wintypes.POINT(int(x), int(y)), self._request)
        except Exception:  # noqa: BLE001
            return None
        root = _root_at(x, y)
        facts = self._facts(el, t0)
        if facts is not None and refine:
            deadline = t0 + budget_s * REFINE_SHARE
            if facts.role in CONTAINER_ROLES and self._covers(facts, max_area):
                deeper = self._descend(facts, x, y, deadline=deadline, refine=True, max_area=max_area)
                if deeper is not None:
                    facts = deeper
            elif self.unnamed_container(facts):
                better = self.refine(facts, x, y, deadline=deadline, max_area=max_area)
                if better is not None:
                    facts = better
        if facts is not None:
            facts.root = root
            # capQ2a: which element it is is settled now; the ancestry below only describes it
            facts.chosen_at, facts.chosen_tick = time.monotonic(), _tick()
            self._ancestry(facts, t0 + budget_s, index=index)
            facts.ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return facts

    def _count(self, key: str) -> None:
        self.counts[key] = self.counts.get(key, 0) + 1

    #: capQ2a: :meth:`from_event` takes ``near`` (the worker passes the click's point only to a client that does).
    event_near = True

    def from_event(self, hwnd: int, id_object: int, id_child: int, *, budget_s: float, index: bool = False,
                   near: tuple[int, int] | None = None) -> Element | None:
        """The element a WinEvent named (path i), with its ancestry. capQ2a: a window's own object
        (``OBJID_WINDOW`` / ``OBJID_CLIENT`` with ``CHILDID_SELF``) is UI Automation's element for that handle; any
        other object is placed by MSAA (``AccessibleObjectFromEvent``, ``accLocation``) and the element is UI
        Automation's answer at ``near`` (a point inside it: the click's) or its centre, walked up to the element whose
        bounds are the object's own (:data:`BOUNDS_SLACK`). None when nothing there has those bounds (covered, gone,
        not in the control view: ``event_unmatched``), and on a COM error (counted, :meth:`_com_error`)."""
        t0 = time.perf_counter()
        deadline = t0 + budget_s * REFINE_SHARE
        try:
            if int(id_child) == 0 and int(id_object) in (_OBJID_WINDOW, _OBJID_CLIENT):
                el = self._uia.ElementFromHandleBuildCache(ctypes.c_void_p(int(hwnd)), self._request)
                facts = self._facts(el, t0)
            else:
                rect = self._event_rect(hwnd, id_object, id_child)
                if rect is None:
                    self._count("event_no_place")
                    return None
                facts = self._element_with_bounds(rect, near, deadline=deadline)
                if facts is None:
                    self._count("event_unmatched")
                    return None
        except Exception as exc:  # noqa: BLE001 - an app that does not answer: a count, never a log line
            self._com_error(exc)
            return None
        if facts is not None:
            facts.chosen_at, facts.chosen_tick = time.monotonic(), _tick()
            self._ancestry(facts, t0 + budget_s, index=index)
            facts.ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return facts

    def _event_rect(self, hwnd: int, id_object: int, id_child: int) -> tuple[int, int, int, int] | None:
        """An event object's place on screen by MSAA alone (``AccessibleObjectFromEvent`` and ``accLocation``), as
        ``(left, top, right, bottom)``; None when it has none (gone, hidden, empty)."""
        from comtypes.automation import VARIANT

        acc_type = self._module.IAccessible if hasattr(self._module, "IAccessible") else None
        if acc_type is None:
            from comtypes.gen.Accessibility import IAccessible as acc_type   # noqa: N813
        acc = ctypes.POINTER(acc_type)()
        child = VARIANT()
        self._oleacc.AccessibleObjectFromEvent(ctypes.c_void_p(int(hwnd)), ctypes.c_long(int(id_object)),
                                               ctypes.c_long(int(id_child)), ctypes.byref(acc), ctypes.byref(child))
        if not acc:
            return None
        left, top, width, height = (int(v) for v in acc.accLocation(child))
        if width <= 0 or height <= 0:
            return None
        return left, top, left + width, top + height

    @staticmethod
    def _same_bounds(bounds: tuple[int, int, int, int] | None, rect: tuple[int, int, int, int]) -> bool:
        return bool(bounds) and all(abs(a - b) <= BOUNDS_SLACK for a, b in zip(bounds, rect))

    def _element_with_bounds(self, rect: tuple[int, int, int, int], near: tuple[int, int] | None, *,
                             deadline: float) -> Element | None:
        """UI Automation's element whose bounds are ``rect``: asked at ``near`` when inside it, else at its centre,
        then up the control view (at most :data:`REFINE_UP` levels, never past a window of its own)."""
        from ctypes import wintypes

        left, top, right, bottom = rect
        if near is not None and left <= near[0] < right and top <= near[1] < bottom:
            x, y = int(near[0]), int(near[1])
        else:
            x, y = (left + right) // 2, (top + bottom) // 2
        current = self._uia.ElementFromPointBuildCache(wintypes.POINT(x, y), self._request)
        facts = self._facts(current, time.perf_counter())
        for _level in range(REFINE_UP + 1):
            if facts is None:
                return None
            if self._same_bounds(facts.bounds, rect):
                return facts
            if time.perf_counter() >= deadline or (facts.role == "Window" and facts.hwnd):
                return None
            current = self._walker.GetParentElementBuildCache(current, self._request)
            facts = self._facts(current, time.perf_counter())
        return None

    def descend(self, pane: Element, x: int, y: int, *, budget_s: float, refine: bool = False,
                max_area: int = 0) -> Element | None:
        """From a renderer's pane, the smallest element under the point, a level of children at a time. ``refine``
        (capC1c): when that smallest one is an unnamed container, the smallest element on the way down that has a name
        or an actionable pattern (its nearest such ancestor under the point) is kept instead (``refined="path"``)."""
        t0 = time.perf_counter()
        deadline = t0 + budget_s
        best = self._descend(pane, x, y, deadline=deadline, refine=refine, max_area=max_area)
        if best is not None:
            best.chosen_at, best.chosen_tick = time.monotonic(), _tick()
            self._ancestry(best, deadline, index=False)
            best.ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return best

    def _descend(self, pane: Element, x: int, y: int, *, deadline: float, refine: bool,
                 max_area: int) -> Element | None:
        best: Element | None = None
        on_path: Element | None = None
        on_path_named: Element | None = None
        current = pane.ref
        for _level in range(DESCENT_LEVELS):
            if current is None or time.perf_counter() >= deadline:
                break
            try:
                kids = current.FindAllBuildCache(_TREE_SCOPE_CHILDREN, self._true, self._request)
                n = min(int(kids.Length), 400)
            except Exception:  # noqa: BLE001
                break
            hit: Element | None = None
            for i in range(n):
                kid = self._facts(kids.GetElement(i), time.perf_counter())
                if kid is not None and kid.contains(x, y) and (hit is None or kid.area() < hit.area()):
                    hit = kid
            if hit is None:
                break
            best, current = hit, hit.ref
            if not self._covers(hit, max_area) and ((hit.name or "").strip() or hit.actionable()):
                on_path = hit
                if (hit.name or "").strip():
                    on_path_named = hit
        # the smallest element under the point is an unnamed container: the nearest named element on the way down
        # (V2-3), else, for one with no actionable pattern, the nearest actionable one
        instead = None
        if refine and best is not None and self.unnamed_container(best):
            instead = on_path_named or (on_path if best.low_info() else None)
        if instead is not None and instead is not best:
            best = instead
            best.refined = "path"
        elif best is not None and refine:
            best.refined = "descent"
        return best

    def focused(self, *, budget_s: float) -> Element | None:
        """The focused element (no ancestry: the field typing goes into)."""
        t0 = time.perf_counter()
        try:
            el = self._uia.GetFocusedElementBuildCache(self._request)
        except Exception:  # noqa: BLE001
            return None
        facts = self._facts(el, t0)
        if facts is not None and budget_s > 0:
            self._ancestry(facts, t0 + budget_s, index=False)
        return facts

    def reread(self, element: Element, *, value_length: bool = False) -> dict[str, Any]:
        """The element's state after the action: ``{"toggle": "on"|"off"|"mixed", "selected": bool}`` or, with
        ``value_length``, ``{"length": n}`` (the value itself is dropped at once). ``{}`` when it cannot answer."""
        if element.ref is None:
            return {}
        if value_length:
            state = self.field_state(element)
            return {"length": state["length"]} if state.get("length") is not None else {}
        try:
            el = element.ref.BuildUpdatedCache(self._request)
            out: dict[str, Any] = {}
            if el.GetCachedPropertyValue(_P_HAS_TOGGLE):
                toggle = el.GetCachedPropertyValue(_P_TOGGLE)
                if isinstance(toggle, int) and toggle in TOGGLE_WORDS:
                    out["toggle"] = TOGGLE_WORDS[toggle]
            if el.GetCachedPropertyValue(_P_HAS_SELECTION_ITEM):
                selected = el.GetCachedPropertyValue(_P_SELECTED)
                if isinstance(selected, (int, bool)):
                    out["selected"] = bool(selected)
            return out
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)
            return {}

    def field_state(self, element: Element) -> dict[str, Any]:
        """capQ2a: whether the element is a text field now, by UI Automation's own facts, and its visible length.

        ``{"field": bool, "length": int | None, "how": "value" | "text" | "focus"}``; ``{"secret": True}`` for a
        password field or one whose ``IsPassword`` cannot be read (nothing of it is read, not even a length); ``{}``
        when it cannot answer. A text field: a Value pattern that can be set (not on a press control,
        :data:`PRESS_ROLES`), else a TextPattern whose document range reads ``IsReadOnly`` false, else a keyboard-
        focusable Edit or Document that reads read-only nowhere. The length is the Value's when it has one, else the
        TextPattern document range's (at most :data:`FIELD_TEXT_CHARS` characters; a range that long with nothing
        visible gives no length), counted by :func:`visible_length`; the text is dropped at once."""
        if element.ref is None:
            return {}
        try:
            el = element.ref.BuildUpdatedCache(self._field_request)
            get = el.GetCachedPropertyValue

            def number(prop: int) -> int | None:
                try:
                    v = get(prop)
                    return int(v) if isinstance(v, (int, bool)) else None
                except Exception:  # noqa: BLE001
                    return None

            password = number(_P_PASSWORD)
            if password is None or password:
                return {"secret": True}                   # fails closed: never read, not even a length
            ctype = number(_P_TYPE)
            role = ROLE_NAMES.get(ctype, "") if ctype is not None else ""
            has_value, value_ro = bool(number(_P_HAS_VALUE)), number(_P_VALUE_READONLY)
            has_text, focusable = bool(number(_P_HAS_TEXT)), bool(number(_P_FOCUSABLE))
            settable = has_value and value_ro == 0 and role not in PRESS_ROLES
            text_ro: bool | None = None
            doc_range = None
            if has_text:
                text_pattern = el.GetCurrentPattern(_TEXT_PATTERN_ID).QueryInterface(
                    self._module.IUIAutomationTextPattern)
                doc_range = text_pattern.DocumentRange
                ro = doc_range.GetAttributeValue(_ATTR_READONLY)
                text_ro = ro if isinstance(ro, bool) else None   # mixed or not supported: not known editable
            if settable:
                how = "value"
            elif text_ro is False:
                how = "text"
            elif focusable and role in ("Edit", "Document") and not (has_value and value_ro != 0) and not text_ro:
                how = "focus"
            else:
                return {"field": False}
            length: int | None = None
            if has_value and value_ro == 0:
                value = el.GetCurrentPropertyValue(_P_VALUE)
                length = visible_length(value) if isinstance(value, str) else None
                del value
            elif doc_range is not None:
                text = doc_range.GetText(FIELD_TEXT_CHARS)
                if isinstance(text, str):
                    seen = visible_length(text)
                    length = None if seen == 0 and len(text) >= FIELD_TEXT_CHARS else seen
                del text
            return {"field": True, "length": length, "how": how}
        except Exception as exc:  # noqa: BLE001
            self._com_error(exc)
            return {}


# -- the hung-app watchdog (capC1b; left open by C1's second verification) ------------------------------------------

#: A lookup that took this long (s) marks its app as not answering (UI Automation's own timeouts are
#: :data:`TIMEOUT_MS` a call, a lookup a few calls): measured by the worker, a machine fact, never a guess.
SLOW_CALL_S = 1.0
#: While an app is marked, it is asked again at most this often (s) whether it answers.
PROBE_EVERY_S = 2.0
#: How long the re-probe waits for the window to answer a no-op message (ms). Windows answers at once for a hung
#: window (``SMTO_ABORTIFHUNG``): the worker never waits longer than this.
ANSWER_MS = 50

_WM_NULL = 0x0000
_SMTO_ABORTIFHUNG = 0x0002
_SMTO_BLOCK = 0x0001


def window_hung(hwnd: int) -> bool:
    """Windows' own verdict (``IsHungAppWindow``): the window's thread has not taken a message for 5 s. Sends nothing;
    ``False`` for a window that is gone."""
    try:
        return bool(ctypes.windll.user32.IsHungAppWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


#: This module's own ``SendMessageTimeoutW`` prototype (review F6): built once on a private ``WinDLL`` handle, so the
#: shared ``ctypes.windll.user32`` function object - which other code in the process calls with its own argument
#: types (``yuki.perception.tree`` passes a ``DWORD`` result) - is never changed.
_send_timeout: Any = None


def _send_message_timeout() -> Any:
    global _send_timeout
    if _send_timeout is None:
        from ctypes import wintypes

        proto = ctypes.WINFUNCTYPE(ctypes.c_size_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                                   wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t))
        _send_timeout = proto(("SendMessageTimeoutW", ctypes.WinDLL("user32")))
    return _send_timeout


def window_answers(hwnd: int, timeout_ms: int = ANSWER_MS) -> bool:
    """Whether the window takes a no-op message (``WM_NULL``) within ``timeout_ms`` (``SendMessageTimeoutW`` with
    ``SMTO_ABORTIFHUNG``: returns at once for a hung window). Changes nothing in the app."""
    try:
        result = ctypes.c_size_t(0)
        return bool(_send_message_timeout()(int(hwnd), _WM_NULL, 0, 0, _SMTO_ABORTIFHUNG | _SMTO_BLOCK,
                                            int(timeout_ms), ctypes.byref(result)))
    except Exception:  # noqa: BLE001
        return False


def _root_at(x: int, y: int) -> int:
    """The top-level window under a physical point (``WindowFromPhysicalPoint`` + ``GetAncestor(GA_ROOT)``: no
    messages sent); 0 when it cannot be read."""
    try:
        from yuki.memory.trace_hooks import root_at

        return int(root_at(int(x), int(y)) or 0)
    except Exception:  # noqa: BLE001
        return 0


def window_pid(hwnd: int) -> int:
    """The process of a window (``GetWindowThreadProcessId``); 0 when it is gone."""
    try:
        pid = ctypes.c_ulong(0)
        ctypes.windll.user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), ctypes.byref(pid))
        return int(pid.value)
    except Exception:  # noqa: BLE001
        return 0


def window_exists(hwnd: int) -> bool:
    """Whether the handle is a window at all (``IsWindow``; sends nothing): a gone window cannot hang a call."""
    try:
        return bool(ctypes.windll.user32.IsWindow(ctypes.c_void_p(int(hwnd))))
    except Exception:  # noqa: BLE001
        return False


class HungWatch:
    """Keeps the trace worker out of apps that stopped answering (a UI Automation or MSAA call into a hung app could
    hold the worker for as long as UI Automation's own timeouts allow).

    Before a lookup into a window, :meth:`skip` asks Windows (``IsHungAppWindow``, no message sent) whether it is hung;
    after one, :meth:`note` marks its process when the lookup took :data:`SLOW_CALL_S` or more (measured by the
    worker). A marked process is skipped - no lookup at all; its actions are kept point-only, as over the lookup
    budget - and asked again at most every :data:`PROBE_EVERY_S` whether it answers (not hung, and it takes a
    ``WM_NULL`` within :data:`ANSWER_MS`); the first time it does, it is lookups as usual again.

    Second verification V2: ``IsHungAppWindow`` says hung only after 5 s without messages, and an MSAA call
    (``AccessibleObjectFromEvent``) has no timeout of its own - an app that froze right after a click would hold the
    worker for its whole freeze. So before such a call (``skip(..., probe=True)``) the window is also sent a
    ``WM_NULL`` (:func:`window_answers`, at most :data:`ANSWER_MS`); one that does not take it is marked at once
    (``hung_unanswered``) and the call is skipped. Health counters: ``hung_marked``, ``hung_slow``, ``hung_unanswered``,
    ``hung_skipped``, ``hung_recovered``. Content-free; nothing in the app changes."""

    def __init__(self, *, hung: Any = window_hung, answers: Any = window_answers, pid_of: Any = window_pid,
                 clock: Any = time.monotonic, slow_s: float = SLOW_CALL_S, probe_every_s: float = PROBE_EVERY_S,
                 exists: Any = window_exists) -> None:
        self._hung = hung
        self._answers = answers
        self._exists = exists
        self._pid_of = pid_of
        self._clock = clock
        self.slow_s = float(slow_s)
        self.probe_every_s = float(probe_every_s)
        #: ``{process key (pid, or -hwnd when the pid is unknown): [hwnd, marked at, last probe]}``
        self._marked: dict[int, list[float]] = {}
        self.counts: dict[str, int] = {"hung_marked": 0, "hung_slow": 0, "hung_unanswered": 0, "hung_skipped": 0,
                                       "hung_recovered": 0}

    def _key(self, hwnd: int, pid: int) -> int:
        if not pid and hwnd:
            pid = self._pid_of(int(hwnd)) or 0
        return int(pid) if pid else -int(hwnd or 0)

    def marked(self) -> int:
        """How many apps are marked as not answering now."""
        return len(self._marked)

    def skip(self, hwnd: int, pid: int = 0, *, probe: bool = False) -> bool:
        """Whether a lookup into this window must be skipped now (its app is hung, or still marked). ``probe``: the
        call has no timeout of its own (MSAA): the window must also take a ``WM_NULL`` within :data:`ANSWER_MS`."""
        if not hwnd and not pid:
            return False
        key = self._key(hwnd, pid)
        now = self._clock()
        entry = self._marked.get(key)
        if entry is not None:
            if now - entry[2] < self.probe_every_s:
                self.counts["hung_skipped"] += 1
                return True
            entry[2] = now
            probe = int(hwnd or entry[0])
            if probe and not self._hung(probe) and self._answers(probe):
                del self._marked[key]
                self.counts["hung_recovered"] += 1
                return False
            self.counts["hung_skipped"] += 1
            return True
        if hwnd and self._hung(int(hwnd)):
            self._marked[key] = [float(hwnd), now, now]
            self.counts["hung_marked"] += 1
            self.counts["hung_skipped"] += 1
            return True
        if probe and hwnd and self._exists(int(hwnd)) and not self._answers(int(hwnd)):
            # V2: frozen less than 5 s (Windows does not call it hung yet) - an MSAA call would wait out the freeze
            self._marked[key] = [float(hwnd), now, now]
            self.counts["hung_marked"] += 1
            self.counts["hung_unanswered"] += 1
            self.counts["hung_skipped"] += 1
            return True
        return False

    def note(self, hwnd: int, pid: int, seconds: float) -> None:
        """A lookup into this window took ``seconds``: past :data:`SLOW_CALL_S` its app is marked."""
        if seconds < self.slow_s or (not hwnd and not pid):
            return
        key = self._key(hwnd, pid)
        if key not in self._marked:
            now = self._clock()
            self._marked[key] = [float(hwnd or 0), now, now]
            self.counts["hung_marked"] += 1
        self.counts["hung_slow"] += 1


__all__ = ["ACTION_PATTERNS", "ANCESTORS", "ANSWER_MS", "BOUNDS_SLACK", "CONTAINER_ROLES", "Element",
           "FIELD_TEXT_CHARS", "HungWatch", "PRESS_ROLES", "PROBE_EVERY_S", "ROLE_NAMES", "SLOW_CALL_S", "TIMEOUT_MS",
           "TOGGLE_WORDS", "TraceUia", "visible_length", "window_answers", "window_exists", "window_hung",
           "window_pid"]
