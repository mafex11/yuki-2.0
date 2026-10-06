"""The trace worker's UI Automation client (report 78 §4.2 step 2): what element an action was on.

Own thread and COM apartment (the trace worker's), its own client, cached properties only, Windows' own call
timeouts (``IUIAutomation2``: :data:`TIMEOUT_MS`), so a hung app cannot hold the worker for longer than that. Every
answer is an :class:`Element`: the control type, name, AutomationId, RuntimeId, bounds, ``IsPassword``, toggle and
selection state, the parent's role and up to :data:`ANCESTORS` ancestors' roles and AutomationIds, the process. A
live reference is kept on the object for one follow-up read (the state after the action), never logged, never
stored.

* :meth:`TraceUia.at_point`: ``ElementFromPointBuildCache`` at the point taken at button-down (path ii);
* :meth:`TraceUia.from_event`: the element an ``INVOKED``, ``SELECTION`` or ``FOCUS`` WinEvent named
  (``AccessibleObjectFromEvent`` then ``ElementFromIAccessibleBuildCache``; path i);
* :meth:`TraceUia.descend`: from a renderer's whole pane (Chromium, CEF answer ``ElementFromPoint`` with the page),
  the smallest element under the point, one level of children at a time within the deadline (path iii);
* :meth:`TraceUia.focused`: the focused element (the field typing goes into; ``IsPassword`` decides);
* :meth:`TraceUia.reread`: the clicked element's toggle or selection state after the action, or a field's value
  *length* (UI Automation has no length-only read: the value passes through this one call and only its length is
  kept).

Nothing here clicks, focuses or moves anything.
"""

from __future__ import annotations

import ctypes
import time
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
    toggle: int | None = None
    selected: bool | None = None
    pid: int = 0
    hwnd: int = 0
    parent_role: str = ""
    ancestors: list[tuple[str, str]] = field(default_factory=list)   # (role, AutomationId), nearest first
    index: int | None = None
    ms: float = 0.0
    ref: Any = field(default=None, repr=False, compare=False)

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
                     _P_SELECTED):
            request.AddProperty(prop)
        request.TreeScope = _TREE_SCOPE_ELEMENT
        self._request = request
        value_request = uia.CreateCacheRequest()
        value_request.AddProperty(_P_VALUE)
        value_request.AddProperty(_P_PASSWORD)
        value_request.TreeScope = _TREE_SCOPE_ELEMENT
        self._value_request = value_request
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
        selected = number(_P_SELECTED)
        return Element(role=ROLE_NAMES.get(int(ctype), f"ControlType{ctype}") if isinstance(ctype, int) else "",
                       name=text(_P_NAME), aid=text(_P_AID), rid=rid, bounds=_bounds(get(_P_BOUNDS)),
                       password=bool(password), toggle=number(_P_TOGGLE),
                       selected=None if selected is None else bool(selected), pid=number(_P_PID) or 0,
                       hwnd=number(_P_HWND) or 0, ms=round((time.perf_counter() - t0) * 1000.0, 2), ref=el)

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

    def at_point(self, x: int, y: int, *, budget_s: float, index: bool = False) -> Element | None:
        """The element at a screen point (path ii), with its ancestry."""
        from ctypes import wintypes

        t0 = time.perf_counter()
        try:
            el = self._uia.ElementFromPointBuildCache(wintypes.POINT(int(x), int(y)), self._request)
        except Exception:  # noqa: BLE001
            return None
        facts = self._facts(el, t0)
        if facts is not None:
            self._ancestry(facts, t0 + budget_s, index=index)
            facts.ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return facts

    def from_event(self, hwnd: int, id_object: int, id_child: int, *, budget_s: float,
                   index: bool = False) -> Element | None:
        """The element a WinEvent named (path i)."""
        from comtypes.automation import VARIANT

        t0 = time.perf_counter()
        try:
            acc_type = self._module.IAccessible if hasattr(self._module, "IAccessible") else None
            if acc_type is None:
                from comtypes.gen.Accessibility import IAccessible as acc_type   # noqa: N813
            acc = ctypes.POINTER(acc_type)()
            child = VARIANT()
            self._oleacc.AccessibleObjectFromEvent(ctypes.c_void_p(int(hwnd)), ctypes.c_long(int(id_object)),
                                                   ctypes.c_long(int(id_child)), ctypes.byref(acc),
                                                   ctypes.byref(child))
            child_id = int(child.value or 0) if isinstance(child.value, int) else 0
            el = self._uia.ElementFromIAccessibleBuildCache(acc, child_id, self._request)
        except Exception:  # noqa: BLE001
            return None
        facts = self._facts(el, t0)
        if facts is not None:
            self._ancestry(facts, t0 + budget_s, index=index)
            facts.ms = round((time.perf_counter() - t0) * 1000.0, 2)
        return facts

    def descend(self, pane: Element, x: int, y: int, *, budget_s: float) -> Element | None:
        """From a renderer's pane, the smallest element under the point, a level of children at a time."""
        t0 = time.perf_counter()
        deadline = t0 + budget_s
        best: Element | None = None
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
        if best is not None:
            self._ancestry(best, deadline, index=False)
            best.ms = round((time.perf_counter() - t0) * 1000.0, 2)
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
        try:
            if value_length:
                el = element.ref.BuildUpdatedCache(self._value_request)
                if el.GetCachedPropertyValue(_P_PASSWORD):
                    return {}
                value = el.GetCachedPropertyValue(_P_VALUE)
                length = len(value) if isinstance(value, str) else None
                del value
                return {} if length is None else {"length": length}
            el = element.ref.BuildUpdatedCache(self._request)
            out: dict[str, Any] = {}
            toggle = el.GetCachedPropertyValue(_P_TOGGLE)
            if isinstance(toggle, int) and toggle in TOGGLE_WORDS:
                out["toggle"] = TOGGLE_WORDS[toggle]
            selected = el.GetCachedPropertyValue(_P_SELECTED)
            if isinstance(selected, (int, bool)):
                out["selected"] = bool(selected)
            return out
        except Exception:  # noqa: BLE001
            return {}


__all__ = ["ANCESTORS", "Element", "ROLE_NAMES", "TIMEOUT_MS", "TOGGLE_WORDS", "TraceUia"]
