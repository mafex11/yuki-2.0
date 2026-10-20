"""Select one tab of a window's own tab strip, and verify it is the one showing (docs/VOICE.md §5.6).

The strip is read as :func:`yuki.perception.tabs.walk_tabs` reads it. The tab is
selected through UI Automation, cursor and keyboard untouched: its
SelectionItem ``Select``, else its Invoke, else its LegacyIAccessible default
action. The effect is read back (a condition poll, bounded): the tab reports
itself selected. ``ok`` is True only then; the result names the tab, its place
and the window's title now. A tab list that changed since the caller saw it (the
name at that place is not the one it expected) is refused, never guessed at.

:func:`tab_action` is the brain's hand (the agent's ``tab`` tool): a tab found by its
identity (its UIA RuntimeId, :attr:`yuki.perception.tabs.Tab.tab_id`), selected or closed,
and the strip read again afterwards so what is said about it rests on that read.

**The keyboard rung of a close** (desk20, final-D S9): a read-only dump of Chrome's strip on 19 Oct (vertical tabs)
showed its TabItems have no child at all, Button or other, so no close control can ever be chosen there. A close
with no single close control then goes by the browser's own close-tab key, from the browser connection data
(``connections/browser_tabs.toml`` ``[tab_keys] close``), only in a browser's window (machine data,
:func:`yuki.actions.launch.web_app`): the tab is found by its RuntimeId in the strip (never inside a web page's
Document: the strip walk never enters one, and the tab's own ancestors are checked again before any key), the Act
switch and the game gate are asked first, the tab is selected and seen selected, the window is brought to the front
and seen there, the key is sent to that window only, and the strip is read again to see the tab gone. Integration T4
(safety F5: on every exit after its select or its raise, the tab closed or not): the rung undoes its own side effects:
the tab the user was on is selected again (UIA, no key) and the window that was in front gets the front back
(``place.focus_back``, gated and undoable, never to an app whose Act switch is off), only while the strip's selection
is still the one the rung or the close left and the browser is still in front (``details["keys_rung"]``:
``reselected``, ``front_back``). Recheck (safety timing 5): when the browser's own pick right after the close is not
known, no tab is switched; the front goes back only while the strip, read whole, is unchanged since the close was first
seen, else the browser is left in front, and either way the report says so (``pick``, ``front_left``:
:func:`_unknown_pick`).

desk20 review: the close key closes every selected tab (a browser's strip is multiselectable), so right before the key
the tab must be the one and only selected TabItem of the whole strip, read whole (:func:`_only_selected`; a user's
multi-selection is collapsed to it by its Select first, and no key goes otherwise), and not the window's only tab.
Safety F1: that check runs once more inside ``input.hotkey`` (``before_send``), after its own waits and right before
``SendInput`` (only the front is read after it). After any close, the tab counts as
gone only from a re-read that found the strip and could tell (:func:`_gone`): a read cut short is never "closed".

fa (final-F #1, A1: the page's "Leave site?" was answered "Leave" for the user and their unsaved text was lost): a
close that leaves the tab in the strip is checked for a window the browser window owns that was not up right before the
close and is in front now (or is a user window): the page's own question (:func:`raised_window`, Win32 facts only).
Then the close stops there: ``details["dialog_raised"]`` holds that window's id, the report says the page asks before
its tab closes and that nothing in it was answered, and the keyboard rung leaves that tab showing and the browser in
front with the question (``reselected`` / ``front_back``: ``dialog``). What it asks is the user's to answer.
fa review: the windows read before are the browser window's owned ones and its process's (:func:`windows_before`); a
window of its process new and in front counts too (a question not owned by the browser window); the dialog's exit is
the first one after the wait (``dialog_raised`` is on every exit); and a close pressed whose tab was not seen gone with
no window measured (a Firefox-family browser asks inside its own window) is ``details["close_held"]``: reported as it
is, never handed on to be finished.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from yuki.actions import ActionResult

_PATTERN_INVOKE = 10000
_PATTERN_SELECTION_ITEM = 10010
_PATTERN_LEGACY = 10018
_P_SELECTION_ITEM_IS_SELECTED = 30079

#: How long the selection is watched for.
WATCH_S = 1.0
POLL_S = 0.03
BUDGET_S = 2.5


def _select(element: Any, module: Any) -> str:
    """Ask ``element`` to become the selected tab; the way that was accepted."""
    for pattern_id, iface, call, how in (
        (_PATTERN_SELECTION_ITEM, "IUIAutomationSelectionItemPattern", "Select", "SelectionItem.Select"),
        (_PATTERN_INVOKE, "IUIAutomationInvokePattern", "Invoke", "Invoke"),
        (_PATTERN_LEGACY, "IUIAutomationLegacyIAccessiblePattern", "DoDefaultAction", "default action"),
    ):
        try:
            raw = element.GetCurrentPattern(pattern_id)
            if not raw:
                continue
            getattr(raw.QueryInterface(getattr(module, iface)), call)()
            return how
        except Exception:  # noqa: BLE001 - try the next way
            continue
    return ""


def select_tab(hwnd: int, index: int, *, expect_name: str | None = None) -> ActionResult:
    """Make tab ``index`` (from 1, left to right) of ``hwnd``'s strip the one showing, verified."""
    from yuki.actions.input import _Pending
    from yuki.perception.tabs import walk_tabs
    from yuki.perception.tree import _uia_core
    from yuki.perception.windows import window_info

    started = time.perf_counter()
    details: dict[str, Any] = {"hwnd": int(hwnd), "index": int(index), "expect_name": expect_name}

    def finish(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    def job(automation: Any, shared: dict) -> None:
        found, nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + BUDGET_S * 0.4)
        shared["count"], shared["nodes"] = len(found), nodes
        if not 1 <= int(index) <= len(found):
            return
        tab, element = found[int(index) - 1]
        shared["tab"] = {"index": tab.index, "name": tab.name, "was_selected": tab.selected}
        if expect_name is not None and tab.name != expect_name:
            shared["changed"] = True
            return
        if tab.selected:
            shared["how"], shared["selected"] = "already showing", True
            return
        shared["how"] = _select(element, _uia_core())
        deadline = time.monotonic() + WATCH_S
        while time.monotonic() < deadline:
            try:
                if bool(element.GetCurrentPropertyValue(_P_SELECTION_ITEM_IS_SELECTED)):
                    shared["selected"] = True
                    return
            except Exception:  # noqa: BLE001
                break
            time.sleep(POLL_S)
        shared["selected"] = False

    pending = _Pending(job, "yuki-tab", BUDGET_S)
    shared = pending.wait()
    details.update({k: shared.get(k) for k in ("count", "nodes", "tab", "how", "selected", "error")})
    tab = shared.get("tab") or {}
    count = shared.get("count")
    if shared.get("error"):
        return finish(False, f"could not read the tabs of window {hwnd}: {shared['error']}")
    if pending.timed_out and not tab:
        return finish(False, f"the tabs of window {hwnd} did not answer within {BUDGET_S:g} s")
    if not count:
        return finish(False, f"window {hwnd} shows no tabs")
    if not tab:
        return finish(False, f"window {hwnd} has {count} tabs; there is no tab {index}")
    if shared.get("changed"):
        return finish(False, f"the tabs changed: tab {index} is now \"{tab['name']}\", not \"{expect_name}\"; "
                             f"nothing was selected")
    info = window_info(int(hwnd))
    title = info.title if info else ""
    details["title"] = title
    details["verified"] = bool(shared.get("selected"))
    where = f"tab {index} of {count}, \"{tab['name']}\""
    if shared.get("selected"):
        already = shared.get("how") == "already showing"
        return finish(True, f"{where} is {'already' if already else 'now'} the one showing "
                            f"(window title now \"{title}\")")
    how = shared.get("how") or "no way to select it was accepted"
    return finish(False, f"asked {where} to show ({how}), but it is not the selected tab after {WATCH_S:g} s")


# ---------------------------------------------------------------------------
# The brain's tab hand: a tab by its identity (UIA RuntimeId), never its title
# ---------------------------------------------------------------------------

_P_CONTROL_TYPE = 30003
#: Control type of a Button: a TabItem's own close control is its Button child.
_BUTTON = 50000
_TREE_SCOPE_CHILDREN = 2
_TREE_SCOPE_ELEMENT = 1
#: How long a closed tab is watched for leaving the strip (strip re-reads, a condition poll).
CLOSE_WATCH_S = 1.5
#: Bound on one tab action: find, act, watch, re-read the strip.
ACTION_BUDGET_S = 4.0
TAB_ACTIONS = ("select", "close")


def _close_control(automation: Any, element: Any) -> Any:
    """The TabItem's own close control: its Button child at the tab's trailing edge. ``None`` if unsure.

    One Button child is that control. A tab playing sound has a second one, the browser's sound indicator
    (live 2026-09-27 14:18 and 14:19 both closes of a playing YouTube tab were refused as "no single close
    control"): the close control is the one that sits last along the tab, the right-most (measured pixels, never
    its localized name). With several buttons and no positions to tell them apart, nothing is chosen.

    chat19 (final test 19 Oct, all-browser R3: every Chrome tab close, Yuki's and the harness's, was "no single close
    control"; Chrome's newer tab strip, its names carrying "- Memory usage - N MB", gives a tab a Button that isn't
    drawn): a Button whose box was read and is empty or lies outside the tab's own box is not on the tab as shown and
    is left out before choosing, when it comes before the chosen one in tree order (:func:`_button_children`,
    :func:`pick_close_control`). A Button whose box couldn't be read, one UIA reports off-screen at the tab's trailing
    edge beside a drawn one, or an undrawn one after the chosen one is never passed over for the drawn one (review
    L4: it may be the close control, and the drawn one the sound indicator: nothing is chosen).
    """
    return pick_close_control(_button_children(automation, element))


_P_IS_OFFSCREEN = 30022
#: Where a tab's Button is (:func:`_button_children`): drawn on the tab, reported off-screen by UIA with a real box on
#: the tab (every Button of a minimised window), or not on the tab as shown (an empty box, or one outside the tab's
#: own box).
ON_TAB, OFFSCREEN, OFF_TAB = "tab", "offscreen", "off_tab"


def _box(value: Any) -> tuple[int, int, int, int] | None:
    """A cached BoundingRectangle as ``(left, top, right, bottom)`` (the RECT form or the 4-array form); no box given
    (an empty value or array) is the empty box; ``None`` when it can't be read as either (never taken for an empty
    box)."""
    if value is None:
        return (0, 0, 0, 0)
    try:
        return (int(value.left), int(value.top), int(value.right), int(value.bottom))
    except Exception:  # noqa: BLE001
        try:
            parts = [int(v) for v in value]
        except Exception:  # noqa: BLE001
            return None
        if not parts:
            return (0, 0, 0, 0)
        if len(parts) != 4:
            return None
        left, top, width, height = parts
        return (left, top, left + width, top + height)


def _button_children(automation: Any, element: Any) -> list[tuple[Any, tuple[int, int, int, int] | None, str]]:
    """``(element, (left, top, right, bottom) or None, where)`` of a TabItem's Button children, in tree order. The box
    is ``None`` only when it couldn't be read. ``where`` (chat19): :data:`ON_TAB` (drawn on the tab),
    :data:`OFFSCREEN` (UIA's ``IsOffscreen`` with a real box on the tab) or :data:`OFF_TAB` (an empty box, or one that
    doesn't overlap the TabItem's own when that one is known)."""
    from yuki.perception.tabs import _P_BOUNDING_RECT

    request = automation.CreateCacheRequest()
    request.AddProperty(_P_CONTROL_TYPE)
    request.AddProperty(_P_BOUNDING_RECT)
    try:
        request.AddProperty(_P_IS_OFFSCREEN)
    except Exception:  # noqa: BLE001 - an older UIA without it: the box alone decides
        pass
    request.TreeScope = _TREE_SCOPE_ELEMENT
    try:
        own = _box(element.CurrentBoundingRectangle)
    except Exception:  # noqa: BLE001 - the tab's own box unknown: the buttons' boxes alone decide
        own = None
    try:
        children = element.FindAllBuildCache(_TREE_SCOPE_CHILDREN, automation.ControlViewCondition, request)
        items = [children.GetElement(i) for i in range(children.Length)]
        out: list[tuple[Any, tuple[int, int, int, int] | None, str]] = []
        for c in items:
            if int(c.GetCachedPropertyValue(_P_CONTROL_TYPE) or 0) != _BUTTON:
                continue
            try:
                rect = _box(c.GetCachedPropertyValue(_P_BOUNDING_RECT))
            except Exception:  # noqa: BLE001 - no position: still a candidate when it is the only one
                rect = None
            try:
                offscreen = bool(c.GetCachedPropertyValue(_P_IS_OFFSCREEN))
            except Exception:  # noqa: BLE001 - not read: the box decides
                offscreen = False
            out.append((c, rect, _where(rect, own, offscreen)))
        return out
    except Exception:  # noqa: BLE001 - a tab that went away mid-read
        return []


def _real(rect: tuple[int, int, int, int] | None) -> bool:
    return bool(rect) and rect[2] > rect[0] and rect[3] > rect[1]


def _where(rect: tuple[int, int, int, int] | None, own: tuple[int, int, int, int] | None, offscreen: bool) -> str:
    """Where a read box is (:data:`ON_TAB`, :data:`OFFSCREEN`, :data:`OFF_TAB`): not empty, overlapping the tab's own
    box when that is a real one, and not reported off-screen. A box that couldn't be read is :data:`ON_TAB` here (its
    ``None`` box says so to :func:`pick_close_control`)."""
    if rect is None:
        return ON_TAB
    if not _real(rect):
        return OFF_TAB
    if _real(own) and not (rect[0] < own[2] and own[0] < rect[2] and rect[1] < own[3] and own[1] < rect[3]):
        return OFF_TAB
    return OFFSCREEN if offscreen else ON_TAB


def pick_close_control(buttons: list[tuple[Any, ...]]) -> Any:
    """The close control among a tab's buttons: the only one, else the right-most by its on-screen box.

    ``buttons`` are ``(element, (left, top, right, bottom) or None)``, or ``(element, box, where)`` from
    :func:`_button_children`; a ``None`` box is one that couldn't be read. ``None`` when there are none, or several
    and one of them has no box to tell them apart by (it may be the close control). chat19: Buttons not on the tab as
    shown (``where`` :data:`OFF_TAB`, or an empty box) are left out first; of the rest the right-most is the close
    control when it is drawn, or when none is drawn (a minimised window: UIA reports every Button off-screen). It is
    ``None`` instead when the right-most is reported off-screen beside a drawn one, or when a Button not drawn comes
    after it in tree order (the tab's own order along it: an undrawn Button has no position but that). Either may be
    a narrow tab's hidden close control, the drawn one its sound indicator: a close never mutes the tab.
    """
    rows = [(b[0], b[1], b[2] if len(b) > 2 else ON_TAB) for b in buttons]
    if len(rows) == 1:
        return rows[0][0]
    if len(rows) < 2 or any(r is None for _b, r, _w in rows):
        return None
    kept = [(i, r, w) for i, (_b, r, w) in enumerate(rows) if w != OFF_TAB and _real(r)]
    if not kept:
        return None
    at, _r, where = max(kept, key=lambda irw: irw[1][2])
    if where != ON_TAB and not all(w == OFFSCREEN for _i, _r, w in kept):
        return None
    if any(w == OFF_TAB or not _real(r) for _b, r, w in rows[at + 1:]):
        return None
    return rows[at][0]


def _invoke(element: Any, module: Any) -> bool:
    try:
        raw = element.GetCurrentPattern(_PATTERN_INVOKE)
        if not raw:
            return False
        raw.QueryInterface(module.IUIAutomationInvokePattern).Invoke()
        return True
    except Exception:  # noqa: BLE001
        return False


def _rows(found: list[tuple[Any, Any]]) -> list[dict[str, Any]]:
    return [{"index": t.index, "group": t.group, "id": t.tab_id, "name": t.name, "selected": t.selected}
            for t, _e in found]


def _mates(before: list[dict[str, Any]], tab_id: str) -> set[str]:
    """The ids of the other tabs of ``tab_id``'s own group in the read before the close (``_rows``); empty when unknown."""
    row = next((r for r in before or () if r.get("id") == tab_id), None)
    if row is None:
        return set()
    return {str(r["id"]) for r in before if r.get("id") and r["id"] != tab_id and r.get("group") == row.get("group")}


def _gone(now: list[tuple[Any, Any]], diag: dict[str, Any], whole: bool, tab_id: str, mates: set[str]) -> bool:
    """desk20 review 2: whether one re-read of the strip shows the tab gone. Only a read that found the strip
    (``reason`` None) and lacks the id counts, and only when it was read whole, or when every other tab of the tab's own
    group is in it (a group's tabs are read in one piece, so the tab would be there too). A read cut short, one that
    found no strip, or a window that can't be read never counts as gone. Pure."""
    if diag.get("reason") is not None:
        return False
    ids = {t.tab_id for t, _e in now if t.tab_id}
    if tab_id in ids:
        return False
    return bool(whole) or (bool(mates) and mates <= ids)


def _strip(found: list[tuple[Any, Any]]) -> dict[str, list[str]]:
    """One read of a strip by identity alone (content-free): its tab ids in order and its selected ids. Pure."""
    return {"ids": [str(t.tab_id) for t, _e in found if t.tab_id],
            "selected": sorted(str(t.tab_id) for t, _e in found if t.selected and t.tab_id)}


# ---------------------------------------------------------------------------
# fa (final-F #1, A1): a window the close itself raised ("Leave site?"), measured
# ---------------------------------------------------------------------------


def _pid(hwnd: int) -> int:
    """The process id of ``hwnd`` (``GetWindowThreadProcessId``), ``0`` unknown. Read-only; never raises."""
    try:
        import win32process

        return int(win32process.GetWindowThreadProcessId(int(hwnd))[1] or 0)
    except Exception:  # noqa: BLE001
        return 0


def owned_windows(hwnd: int) -> set[int]:
    """The visible top-level windows ``hwnd`` owns now (``GetWindow(GW_OWNER)``): a page's or the app's own question
    is one of them. Read-only Win32; an empty set when they can't be read. Never raises."""
    found: set[int] = set()
    try:
        import win32gui

        from yuki.perception.windows import owner_of

        def collect(h: int, _extra: object) -> bool:
            try:
                if win32gui.IsWindowVisible(h) and owner_of(int(h)) == int(hwnd):
                    found.add(int(h))
            except Exception:  # noqa: BLE001 - a window that went away mid-read
                pass
            return True

        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001
        return set()
    return found


def windows_before(hwnd: int) -> set[int]:
    """fa review M2: what :func:`raised_window` compares with, read right before an act on ``hwnd``: the visible
    top-level windows ``hwnd`` owns, and those of its own process (another window of the same browser or app that was
    already up is never one the act raised). One window enumeration; read-only Win32; an empty set when they can't be
    read. Never raises."""
    found: set[int] = set()
    try:
        import win32gui

        from yuki.perception.windows import owner_of

        pid = _pid(hwnd)

        def collect(h: int, _extra: object) -> bool:
            try:
                if int(h) != int(hwnd) and win32gui.IsWindowVisible(h) and (
                        owner_of(int(h)) == int(hwnd) or (pid and _pid(int(h)) == pid)):
                    found.add(int(h))
            except Exception:  # noqa: BLE001 - a window that went away mid-read
                pass
            return True

        win32gui.EnumWindows(collect, None)
    except Exception:  # noqa: BLE001
        return set()
    return found


def raised_window(hwnd: int, before: set[int] | frozenset[int] = frozenset()) -> int:
    """The window an act on ``hwnd`` raised: a window ``hwnd`` owns that was not up before the act (``before``:
    :func:`windows_before` read right before it) and is in front now (its own question took the front), else one that
    is a user window (titled, or holding its owner disabled: ``windows.is_user_window``, the window list's own rule).
    fa review M2 (3): else, when no owned window is new, the window in front now when it is of ``hwnd``'s own process
    and was not up before (a question whose owner link is not ``hwnd``). ``0``: none. Window facts only (owner,
    process, visibility, the front, styles), never a title read for meaning. Read-only; never raises."""
    try:
        import win32gui

        from yuki.actions.input import _user32
        from yuki.perception.windows import is_user_window

        known = set(before or ())
        now = owned_windows(hwnd) - known
        front = int(_user32.GetForegroundWindow() or 0)
        if front in now:
            return front
        hit = next((h for h in sorted(now) if is_user_window(h)), 0)
        if hit:
            return hit
        pid = _pid(hwnd)
        if (front and front != int(hwnd) and front not in known and pid and _pid(front) == pid
                and win32gui.IsWindowVisible(front)):
            return front
        return 0
    except Exception:  # noqa: BLE001
        return 0


def note_raised(dialog: int, owner: int, act: str = "close") -> None:
    """The measured fact for the commit check (``brain.check.note_raised``: ids only): ``dialog`` came up in reply to
    Yuki's own close of something in ``owner``. Written here, by the act that measured it, so every caller (the talker's
    and ONE's hands, the thinker's tab and window tools) leaves the same fact. safety27 D2: ``act`` ``close_held``
    (``dialog`` == ``owner``): a close of Yuki's in that window was pressed and it stayed open with no window of its own
    measured (it may be asking inside it). Never raises."""
    try:
        from yuki.brain.check import note_raised as keep

        keep(int(dialog), int(owner), str(act or "close"))
    except Exception:  # noqa: BLE001 - the check then reads the step as before
        pass


def _window_title(hwnd: int) -> str:
    """The raised window's title, for the report (text the app wrote: RAM only, never logged). ``""`` unknown."""
    try:
        import win32gui

        return " ".join(str(win32gui.GetWindowText(int(hwnd)) or "").split())[:80]
    except Exception:  # noqa: BLE001
        return ""


def _watch_gone(automation: Any, hwnd: int, tab_id: str, mates: set[str], out: dict[str, Any]) -> None:
    """desk20 review 2: watch ``hwnd``'s strip for the tab leaving it (re-reads, a condition poll of
    :data:`CLOSE_WATCH_S`), then read it once more for what is there now. ``out`` gets ``seen`` (a read that counts,
    :func:`_gone`, lacks it, and the last read does not hold it), ``read_back`` (some read told: it held the tab, or
    counts as gone), ``after``, ``after_complete`` and ``picked`` (safety WW8, timing 5: the selected tab ids of the
    first read after the key that lacked the tab, when it counted as gone: the browser's own pick; ``None`` when not
    known so: a read before it already lacked the tab, or only the read after the watch saw it gone), and
    ``closed_strip`` (recheck: the read that first counted the tab gone, by identity, :func:`_strip`, when it was read
    whole and the read after the watch, read whole too, showed it unchanged; else ``None``). Blocking; never raises
    for a window that went away."""
    from yuki.perception.tabs import walk_tabs

    def holds(now: list[tuple[Any, Any]]) -> bool:
        return any(t.tab_id == tab_id for t, _e in now)

    def chosen(now: list[tuple[Any, Any]]) -> list[str]:
        return [str(t.tab_id) for t, _e in now if t.selected and t.tab_id]

    seen = read_back = lacked = False
    picked: list[str] | None = None
    first: dict[str, list[str]] | None = None
    deadline = time.monotonic() + CLOSE_WATCH_S
    while time.monotonic() < deadline:
        diag: dict[str, Any] = {}
        now, _n, whole = walk_tabs(automation, int(hwnd), time.monotonic() + 0.3, all_groups=True, diagnostics=diag)
        if diag.get("reason") == "error":
            break   # the window can't be read (gone): not seen, said as it is
        if _gone(now, diag, whole, tab_id, mates):
            seen = read_back = True
            # safety WW8: the tab the browser showed right after the close (first read gone); safety timing 5: only
            # when no earlier read already lacked the tab (cut short), else this one may hold the user's click: unknown
            picked = None if lacked else chosen(now)
            first = _strip(now) if whole else None
            break
        read_back = read_back or holds(now)
        lacked = lacked or not holds(now)
        time.sleep(POLL_S)
    diag = {}
    later, _n, whole = walk_tabs(automation, int(hwnd), time.monotonic() + ACTION_BUDGET_S * 0.2, all_groups=True,
                                 diagnostics=diag)
    if holds(later):
        seen, read_back = False, True   # the last read holds it: it is still there, whatever an earlier read showed
    elif not seen and _gone(later, diag, whole, tab_id, mates):
        seen = read_back = True
        # safety timing 5: first seen gone only by this read, a whole watch after the key: its selection may be the
        # user's own click by now, not the browser's pick, so no pick is known (nothing is switched back)
        picked = None
        first = _strip(later) if whole else None
    elif first is not None and (not whole or _strip(later) != first):
        first = None   # recheck: the strip changed (or was not read whole) since the close was first seen
    out.update(seen=seen, read_back=read_back, after=_rows(later), after_complete=bool(whole), picked=picked,
               closed_strip=first if seen else None)


# ---------------------------------------------------------------------------
# desk20 (final-D S9): the keyboard rung of a tab close
# ---------------------------------------------------------------------------

#: The browser connection file holding the close-tab key (``[tab_keys] close``), and its table and key.
KEYS_FILE = "browser_tabs.toml"
KEYS_TABLE = "tab_keys"
_DOCUMENT = 50030
#: How many parents of the tab are walked up to look for a page's Document (the strip sits a few levels down).
_ANCESTOR_LEVELS = 24
#: Bounds of the rung's own UIA reads (select and watch, the check right before the key).
KEYS_SELECT_S = 1.5
KEYS_CHECK_S = 1.0
_keys_cache: dict[str, Any] = {}
_keys_lock = threading.Lock()
_policy: list[Any] = []


def close_keys() -> tuple[str, ...]:
    """The browser's own close-tab key from the connection data (``[tab_keys] close`` of :data:`KEYS_FILE`, cached
    until the file changes); ``()`` when the file or the entry is missing (then no keyboard rung). Never raises."""
    try:
        import tomllib

        from yuki.skills.connection import CONNECTIONS_DIR

        path = CONNECTIONS_DIR / KEYS_FILE
        st = path.stat()
        stamp = (str(path), st.st_mtime, st.st_size)
        with _keys_lock:
            if _keys_cache.get("stamp") == stamp:
                return tuple(_keys_cache.get("close") or ())
        data = tomllib.loads(path.read_text(encoding="utf-8"))
        table = data.get(KEYS_TABLE) if isinstance(data.get(KEYS_TABLE), dict) else {}
        raw = table.get("close")
        keys = tuple(str(k).strip() for k in raw if str(k).strip()) if isinstance(raw, list) else ()
        with _keys_lock:
            _keys_cache.update(stamp=stamp, close=keys)
        return keys
    except Exception:  # noqa: BLE001 - no data: no keyboard rung
        return ()


def _act_off(process: str) -> str | None:
    """The name Settings switched "Act" off for when ``process`` is that app (no click, type or key there), else
    ``None``; an unreadable policy is ``None`` (the judge's reader, as the hands read it)."""
    try:
        from yuki.agent.judge import PolicyReader

        if not _policy:
            _policy.append(PolicyReader())
        return _policy[0].app_off(process) or None
    except Exception:  # noqa: BLE001
        return None


def _inside_document(automation: Any, element: Any) -> bool:
    """Whether a page's Document holds ``element`` (one of its ancestors is a Document): a web page's own tab widget,
    never the browser's strip. Unreadable counts as inside (no key is sent on a guess)."""
    try:
        walker = automation.ControlViewWalker
        node = element
        for _ in range(_ANCESTOR_LEVELS):
            node = walker.GetParentElement(node)
            if not node:
                return False
            if int(node.CurrentControlType or 0) == _DOCUMENT:
                return True
        return True
    except Exception:  # noqa: BLE001
        return True


def _find(automation: Any, hwnd: int, tab_id: str,
          budget_s: float) -> tuple[Any, Any, list[tuple[Any, Any]], bool]:
    """``(tab, element, every tab found, read whole)`` for the tab whose RuntimeId is ``tab_id`` in ``hwnd``'s strip
    (every group; the walk never enters a Document); ``(None, None, found, whole)`` when it is not there."""
    from yuki.perception.tabs import walk_tabs

    found, _nodes, complete = walk_tabs(automation, int(hwnd), time.monotonic() + budget_s, all_groups=True)
    match = next(((t, e) for t, e in found if t.tab_id and t.tab_id == tab_id), None)
    return (match[0], match[1], found, bool(complete)) if match else (None, None, found, bool(complete))


def _only_selected(found: list[tuple[Any, Any]], tab_id: str, whole: bool) -> str:
    """desk20 review 1: ``""`` when the tab ``tab_id`` is the one and only selected TabItem of a strip read whole
    (every group), else why not: ``several_selected``, ``not_selected`` or ``not_read`` (a read cut short may miss a
    group's selected tab). The close key closes every selected tab (a browser's strip is multiselectable: a tab the user
    highlighted reports itself selected too), and with a selected tab in another group the key would close the active
    one, not this one. Pure."""
    picked = [t for t, _e in found if t.selected]
    if len(picked) > 1:
        return "several_selected"
    if not picked or picked[0].tab_id != tab_id:
        return "not_selected"
    return "" if whole else "not_read"


def _close_by_keys(hwnd: int, tab_id: str, keys: tuple[str, ...]) -> dict[str, Any]:
    """The keyboard rung (module docstring), blocking: ``{"how", "sent", "why", "refused", "focus_ms", ...}``. Every
    check comes before the key; the key goes only to ``hwnd`` in front with that tab its strip's only selected tab
    (review 1). The caller watches the strip afterwards."""
    from yuki.actions import gate as _gate
    from yuki.actions.input import _Pending, _user32, hotkey
    from yuki.actions.launch import focus_window, web_app
    from yuki.perception.tree import _uia_core
    from yuki.perception.windows import window_info

    out: dict[str, Any] = {"how": "", "sent": False, "keys": "+".join(keys)}
    info = window_info(int(hwnd))
    if info is None:
        out["why"] = "gone"
        return out
    if not web_app(info.process_name, info.pid):
        out["why"] = "not_browser"   # the close key is a browser's; another app's tabs are never closed by it
        return out
    off = _act_off(info.process_name)
    if off:
        out.update(why="act_off", act_off=off)
        return out
    if (refused := _gate.refuse("keys", hwnd=int(hwnd))) is not None:
        out.update(why="game" if _gate.kind_of(refused) == _gate.GAME else "blocked", refused=_gate.kind_of(refused),
                   said=str(refused))
        return out

    def select(automation: Any, shared: dict) -> None:
        begun = time.monotonic()
        tab, element, found, whole = _find(automation, hwnd, tab_id, KEYS_SELECT_S * 0.4)
        if tab is None:
            shared["why"] = "gone"
            return
        if _inside_document(automation, element):
            shared["why"] = "in_page"   # a page's own tab widget: never a key
            return
        # integration T4: the tab the user was on (the strip's selected tab other than this one), selected again once
        # this one is gone (:func:`_reselect`). Safety F6: none when this tab itself reads selected (IsSelected is the
        # multi-selection: the others may be highlights only, and UIA can't tell which one shows)
        shared["was_on"] = [] if tab.selected else [str(t.tab_id) for t, _e in found
                                                     if t.selected and t.tab_id and t.tab_id != tab_id]
        state = _only_selected(found, tab_id, whole)
        if state:
            # review 1: not selected, or other tabs selected with it (a multi-selection the key would close too), or
            # not read whole: its Select (UIA's SelectionItem.Select deselects the rest) makes it the only selected
            # tab, seen on re-reads of the whole strip
            shared["select"] = _select(element, _uia_core())
            deadline = min(time.monotonic() + WATCH_S, begun + KEYS_SELECT_S - 0.4)
            while shared["select"] and time.monotonic() < deadline:
                time.sleep(POLL_S)
                again, _e, now, whole = _find(automation, hwnd, tab_id, 0.3)
                state = _only_selected(now, tab_id, whole) if again is not None else "gone"
                if state in ("", "gone"):
                    break
        shared["state"] = state

    picked = _Pending(select, "yuki-tab-keys", KEYS_SELECT_S).wait()
    out["was_on"] = list(picked.get("was_on") or [])   # integration T4: a select may have run already
    if picked.get("why") or picked.get("state") != "":
        out["why"] = picked.get("why") or picked.get("state") or "not_selected"
        return out
    started = time.monotonic()
    front = int(_user32.GetForegroundWindow() or 0)
    if front != int(hwnd):
        out["front_before"] = front or None   # integration T4: given back once the tab is gone (:func:`_front_back`)
        focused = focus_window(int(hwnd))
        details = getattr(focused, "details", None) or {}
        if not focused.ok:
            out.update(why="focus", refused=_gate.carried(details), said=str(focused.summary)[:200])
            return out
    out["focus_ms"] = round((time.monotonic() - started) * 1000)

    def check(automation: Any, shared: dict) -> None:
        tab, element, found, whole = _find(automation, hwnd, tab_id, KEYS_CHECK_S * 0.6)
        if tab is None:
            shared["why"] = "gone"
            return
        if _inside_document(automation, element):
            shared["why"] = "in_page"
            return
        # review 1: the one and only selected tab of the whole strip (every group), so the key closes this tab alone;
        # safety F3: and not the window's only tab now (its close key would close the window: never by a key)
        why = _only_selected(found, tab_id, whole) or ("only_tab" if len(found) <= 1 else "")
        if not why:
            try:
                why = "" if bool(element.GetCurrentPropertyValue(_P_SELECTION_ITEM_IS_SELECTED)) else "not_selected"
            except Exception:  # noqa: BLE001
                why = "not_selected"
        shared["why"], shared["ok"] = why, not why

    # right before the key: the tab is still the only selected one, and its window is the one in front
    checked = _Pending(check, "yuki-tab-keys-check", KEYS_CHECK_S).wait()
    if not checked.get("ok"):
        out["why"] = checked.get("why") or "not_selected"
        return out
    if int(_user32.GetForegroundWindow() or 0) != int(hwnd):
        out["why"] = "not_front"
        return out
    last: dict[str, str] = {}

    def before_send() -> str:
        # safety F1: the same check once more, after hotkey's own waits (readiness, the page and focus reads) and right
        # before SendInput (hotkey reads only the front after it): a tab picked meanwhile (their click, a page's new
        # tab) gets no key, nor does a window whose other tabs went
        again = _Pending(check, "yuki-tab-keys-last", KEYS_CHECK_S).wait()
        why = "" if again.get("ok") else str(again.get("why") or "not_read")   # no answer in time: not read
        last["why"] = "moved_on" if why == "not_selected" else why
        return last["why"]

    # sent only while hwnd is in front (read again right before SendInput); the game gate again inside
    pressed = hotkey(*keys, expect_hwnd=int(hwnd), before_send=before_send)
    details = getattr(pressed, "details", None) or {}
    out["sent"] = bool(pressed.ok)
    if not pressed.ok:
        if last.get("why"):
            out["why"] = last["why"]   # safety F1: the last check stopped it
        else:
            out.update(why="key", refused=_gate.carried(details), said=str(pressed.summary)[:200])
        return out
    out["how"] = f"the browser's close-tab key ({out['keys']})"
    return out


def _reselect(hwnd: int, was_on: list[str], picked: list[str] | None = None) -> str:
    """Integration T4: after the keyboard rung, the tab the user was on before its select (the strip's one other
    selected tab, by RuntimeId) is selected again through UI Automation (SelectionItem.Select, no key), so the browser
    shows what it showed before, only while the selection is still the one the rung left (``picked``: the selected tab
    ids of the gone-watch's first read without the tab, the browser's own pick right after the close, safety WW8; the
    rung's own tab alone on an exit that closed nothing, safety F5). The strip is read first either way: a selection
    other than ``picked`` (read whole) is the user's (or a page's) and is never switched away, and the caller then
    leaves the front too. A code word: ``moved_on`` (another selection now than ``picked``, or it is not known),
    ``reselected``, ``already`` (it is the selected one), ``gone`` (no longer in the strip), ``none`` (no single tab to
    go back to: the closed tab was the one showing, or several were selected), ``failed``. Blocking, bounded; never
    raises."""
    from yuki.actions.input import _Pending
    from yuki.perception.tree import _uia_core

    def job(automation: Any, shared: dict) -> None:
        tab, element, found, whole = _find(automation, hwnd, was_on[0] if len(was_on) == 1 else "",
                                           KEYS_SELECT_S * 0.4)
        now = sorted(str(t.tab_id) for t, _e in found or () if t.selected and t.tab_id)
        if picked is None or not whole or now != sorted(str(p) for p in picked):
            shared["got"] = "moved_on"
            return
        if len(was_on) != 1:
            shared["got"] = "none"
            return
        if tab is None:
            shared["got"] = "gone"
            return
        if tab.selected:
            shared["got"] = "already"
            return
        shared["got"] = "reselected" if _select(element, _uia_core()) else "failed"

    try:
        got = _Pending(job, "yuki-tab-keys-back", KEYS_SELECT_S).wait()
        return str(got.get("got") or "failed")
    except Exception:  # noqa: BLE001
        return "failed"


def _front_back(hwnd: int, front_before: Any) -> str:
    """Integration T4: the window that was in front before the keyboard rung raised ``hwnd`` gets the front back
    (``yuki.actions.place.focus_back``: gated, journaled, undoable), only while ``hwnd`` is still in front (the user
    did not move the front meanwhile), the window still exists and shows, it is none of Yuki's own, and its app's Act
    switch is on (safety F4, as the brains' own focus_back steps). A code word: ``given``, ``moved_on`` (the front is no
    longer ``hwnd``), ``gone``, ``own``, ``act_off``, ``failed``; ``none`` when the rung raised nothing. Never
    raises."""
    try:
        front_before = int(front_before or 0)
    except (TypeError, ValueError):
        return "none"
    if not front_before:
        return "none"
    try:
        import win32gui

        from yuki.actions.input import _user32
        from yuki.agent.tools import _is_own_window

        if not win32gui.IsWindow(front_before) or not win32gui.IsWindowVisible(front_before):
            return "gone"
        if _is_own_window(front_before):
            return "own"
        from yuki.perception.windows import window_info

        info = window_info(front_before)
        if info is None:
            return "gone"
        if _act_off(info.process_name):
            return "act_off"   # safety F4: Act is off for its app: its window is never raised by Yuki
        if int(_user32.GetForegroundWindow() or 0) != int(hwnd):
            return "moved_on"
        from yuki.actions.place import focus_back

        return "given" if focus_back(front_before).ok else "failed"
    except Exception:  # noqa: BLE001
        return "failed"


def _unknown_pick(hwnd: int, was_on: list[str], front_before: Any,
                  closed: dict[str, list[str]] | None) -> dict[str, Any]:
    """Recheck (safety timing 5, the user doing nothing): the keyboard rung's close was seen, but the browser's own pick
    right after it is not known (a read cut short came first, or only the read after the watch saw the tab gone). No
    tab is switched (that would be a guess), and the strip is read once more, whole, by identity (:func:`_strip`):
    ``reselected`` is ``already`` when the tab they were on is the one selected now, ``none`` when there was no single
    one, else ``unknown`` (the report says it was not shown again). The window that was in front gets the front back
    (:func:`_front_back`: the browser still in front, that window still there and none of Yuki's own, its Act switch
    on, the game gate) only while the strip is unchanged since the read that first saw the tab gone (``closed``, from
    :func:`_watch_gone`; ``None``: not known so); else nothing is given back and ``front_back`` is ``left`` while the
    browser is still in front. ``front_left``: the browser the rung raised is left in front (the report says so).
    ``pick``: ``unknown``. Blocking, bounded; never raises."""
    from yuki.actions.input import _Pending, _user32

    def job(automation: Any, shared: dict) -> None:
        _t, _e, found, whole = _find(automation, hwnd, "", KEYS_SELECT_S * 0.4)
        shared["now"] = _strip(found) if whole and found else None

    try:
        now = _Pending(job, "yuki-tab-keys-since", KEYS_SELECT_S).wait().get("now")
    except Exception:  # noqa: BLE001 - not read: unknown
        now = None
    out: dict[str, Any] = {"pick": "unknown"}
    if len(was_on) != 1:
        out["reselected"] = "none"
    else:
        out["reselected"] = "already" if now is not None and now["selected"] == [str(was_on[0])] else "unknown"
    if not front_before:
        out["front_back"] = "none"
        return out
    same = now is not None and closed is not None and now == closed
    got = _front_back(hwnd, front_before) if same else "left"
    try:
        in_front = int(_user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:  # noqa: BLE001
        in_front = False
    out["front_back"] = "moved_on" if got == "left" and not in_front else got
    if in_front and out["front_back"] not in ("given", "moved_on"):
        out["front_left"] = True
    return out


def tab_action(hwnd: int, tab_id: str, action: str) -> ActionResult:
    """Select or close the tab of ``hwnd``'s strip whose identity is ``tab_id``, then re-read the strip.

    ``tab_id`` is a :attr:`yuki.perception.tabs.Tab.tab_id` from a read of the strip (the
    TabItem's UIA RuntimeId), from any of the strip's groups (a pinned tab's too): the tab is
    found by it, whatever its title or place now, and
    one that is no longer there is reported, never replaced by another. ``select`` asks it
    to show (as :func:`select_tab`) and watches it report itself selected; ``close`` invokes
    its own close control (its single Button child) and watches it leave the strip; with none
    or several chosen, a browser's tab goes by the keyboard rung (desk20 S9, module docstring:
    selected, its window in front, the close-tab key from the connection data, Act switch and
    game gate first). Either way the strip is read again at the end and
    ``details["tabs_after"]`` lists what is there now, so what is said about it rests on
    that read. ``ok`` only when the effect was seen. Cursor untouched; the keyboard only in the
    keyboard rung (``details["keys_rung"]``).
    """
    from yuki.actions.input import _Pending
    from yuki.perception.tabs import walk_tabs
    from yuki.perception.tree import _uia_core
    from yuki.perception.windows import window_info

    started = time.perf_counter()
    tab_id = str(tab_id or "").strip().strip("[]").strip()
    details: dict[str, Any] = {"hwnd": int(hwnd), "tab_id": tab_id, "action": action}

    def finish(ok: bool, summary: str) -> ActionResult:
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    if action not in TAB_ACTIONS:
        return finish(False, f"unknown tab action {action!r}: use one of {', '.join(TAB_ACTIONS)}")
    if not tab_id:
        return finish(False, "no tab id given: read the tabs first and pass one of their ids")

    def job(automation: Any, shared: dict) -> None:
        # a tab id from any group of the strip (PLAN.md D8: a pinned or Essentials tab sits in a smaller group)
        found, nodes, _complete = walk_tabs(automation, int(hwnd), time.monotonic() + ACTION_BUDGET_S * 0.25,
                                            all_groups=True)
        shared["count"], shared["nodes"], shared["before"] = len(found), nodes, _rows(found)
        match = next(((t, e) for t, e in found if t.tab_id and t.tab_id == tab_id), None)
        if match is None:
            shared["after"] = _rows(found)
            return
        tab, element = match
        shared["tab"] = {"index": tab.index, "id": tab.tab_id, "name": tab.name, "was_selected": tab.selected}
        module = _uia_core()
        if action == "select":
            if tab.selected:
                shared["how"], shared["seen"] = "already showing", True
            else:
                shared["how"] = _select(element, module)
                deadline = time.monotonic() + WATCH_S
                while shared["how"] and time.monotonic() < deadline:
                    try:
                        if bool(element.GetCurrentPropertyValue(_P_SELECTION_ITEM_IS_SELECTED)):
                            shared["seen"] = True
                            break
                    except Exception:  # noqa: BLE001
                        break
                    time.sleep(POLL_S)
        else:
            control = _close_control(automation, element)
            if control is None:
                shared["no_control"] = True
            elif _invoke(control, module):
                shared["how"] = "its close control"
                # review 2: gone only by a read that counts (:func:`_gone`), and the last read must not hold it
                _watch_gone(automation, int(hwnd), tab_id, _mates(shared["before"], tab_id), shared)
                return
        after, _n, complete = walk_tabs(automation, int(hwnd), time.monotonic() + ACTION_BUDGET_S * 0.2,
                                        all_groups=True)
        shared["after"], shared["after_complete"] = _rows(after), complete

    # fa (A1): the windows the browser window owns (and, review M2, its process's) right before the close, so a question
    # the close raised is told apart from one that was already up (read-only Win32, a few ms; a select raises none)
    owned_before = windows_before(int(hwnd)) if action == "close" else set()
    pending = _Pending(job, "yuki-tab-action", ACTION_BUDGET_S)
    shared = pending.wait()
    if action == "close" and shared.get("how") and not shared.get("seen"):
        # fa (A1): the close control was pressed and the tab is still there: a window of the browser's own that came up
        # in front since (the page's "Leave site?") is measured and stops the hand
        shared["dialog"] = raised_window(int(hwnd), owned_before)
    keys = close_keys() if action == "close" and shared.get("no_control") and shared.get("tab") else ()
    if keys and int(shared.get("count") or 0) <= 1:
        # the window's only tab: its close key closes the window itself, which is more than a tab (never by a key)
        keys, shared["keys_why"] = (), "only_tab"
    if keys and not pending.timed_out and not shared.get("error"):
        # desk20 (S9): no single close control was chosen (Chrome's TabItems have no child at all): the browser's own
        # close-tab key on that very tab, every check first (:func:`_close_by_keys`), then the strip watched
        rung = _close_by_keys(int(hwnd), tab_id, keys)
        details["keys_rung"] = {k: v for k, v in rung.items() if k not in ("said", "was_on", "front_before")}
        left: list[str] | None = [tab_id]   # safety F5: the selection the rung's own select left (the tab alone)
        unknown, closed = False, None   # recheck: closed, the browser's own pick not known (:func:`_unknown_pick`)
        if rung.get("refused"):
            details["refused"] = rung["refused"]
        if rung.get("sent"):
            shared["no_control"], shared["how"] = False, rung["how"]

            mates = _mates(shared.get("before") or [], tab_id)

            def watch(automation: Any, out: dict) -> None:
                # review 2: a re-read cut short (Chrome busy, e.g. with a "Leave site?" prompt up) is never "gone"
                _watch_gone(automation, int(hwnd), tab_id, mates, out)

            seen = _Pending(watch, "yuki-tab-keys-watch", CLOSE_WATCH_S + ACTION_BUDGET_S * 0.3).wait()
            shared["seen"], shared["read_back"] = bool(seen.get("seen")), bool(seen.get("read_back"))
            if "after" in seen:
                shared["after"], shared["after_complete"] = seen["after"], seen.get("after_complete", True)
            else:
                shared["after"], shared["after_complete"] = [], False   # no read after the key: never the old one
            if shared["seen"]:
                left = seen.get("picked")   # safety WW8: the browser's own pick right after the close (None: unknown)
                unknown, closed = left is None, seen.get("closed_strip")
            else:
                # fa (A1): the key was sent and the tab is still there: the page's own question the close raised
                shared["dialog"] = raised_window(int(hwnd), owned_before)
                if not (shared["read_back"] and shared["after_complete"]):
                    left = None   # safety F5: not read back whole after the key (a prompt may be up): kept
        else:
            shared["keys_why"], shared["keys_said"] = rung.get("why"), rung.get("said")
        if shared.get("dialog") and (rung.get("was_on") or rung.get("front_before")):
            # fa (A1): the page asks before its tab closes: its tab stays the one showing and the browser stays in
            # front with that question (another tab or window brought up would hide it under their answer)
            details["keys_rung"]["reselected"], details["keys_rung"]["front_back"] = "dialog", "dialog"
        elif rung.get("was_on") or rung.get("front_before"):
            # integration T4, safety F5: on every exit after its select or its raise, the rung undoes its own side
            # effects (the brains' tidy steps can't: a tab switch is no window step, and the raised window counts as
            # asked): the tab they were on shows again, and the window that was in front gets the front back, only
            # while they have not moved on (the strip's selection is still the one the rung or the close left, and
            # the browser is still in front)
            if unknown:
                # recheck (safety timing 5): closed, but the browser's own pick is not known: no tab is switched, the
                # front goes back only on measured facts, and what is left is said (never in silence)
                details["keys_rung"].update(_unknown_pick(int(hwnd), list(rung.get("was_on") or []),
                                                          rung.get("front_before"), closed))
            else:
                put = _reselect(int(hwnd), list(rung.get("was_on") or []), list(left) if left is not None else None)
                details["keys_rung"]["reselected"] = put
                details["keys_rung"]["front_back"] = ("moved_on" if put == "moved_on"
                                                      else _front_back(int(hwnd), rung.get("front_before")))
    tab = shared.get("tab") or {}
    after = list(shared.get("after") or [])
    details.update({"count": shared.get("count"), "nodes": shared.get("nodes"), "tab": tab or None,
                    "how": shared.get("how"), "verified": bool(shared.get("seen")), "tabs_after": after,
                    "tabs_after_complete": bool(shared.get("after_complete", "after" in shared)),
                    "read_back": bool(shared.get("read_back"))})
    # integration T4, safety F5: what the keyboard rung put back, on whatever exit it took; recheck: the browser it left
    # in front is always said (``front_left``), in the code's own words when it has some
    put_back = details.get("keys_rung") or {}
    dialog = int(shared.get("dialog") or 0) if action == "close" and not shared.get("seen") else 0
    details["dialog_raised"] = dialog or None
    # fa review M2 (1): the close was pressed (its control, or its key sent) and the tab was not seen gone, with no
    # window of the page's measured: the page may be asking inside its own window (a Firefox-family browser draws its
    # "leave page" question there), so this is reported as it is, never finished by another hand
    details["close_held"] = bool(action == "close" and shared.get("how") and not shared.get("seen") and not dialog)
    if details["close_held"]:
        # safety27 D2: the window itself noted for the commit check (and the planner's hold): a step in it is judged
        # knowing a close of Yuki's there was pressed and the tab stayed (it may be asking inside the window)
        note_raised(int(hwnd), int(hwnd), "close_held")
    if dialog:
        # fa (A1, final-F k_tab_close_leave_site: "Leave" was then clicked for the user and their unsaved text lost):
        # the close raised the page's own question; nothing in it was answered, and that answer is theirs. Review L5:
        # the first exit after the wait (a strip re-read that failed while the question is up never hides it)
        note_raised(dialog, int(hwnd))
        title = _window_title(dialog)
        named = f' "{title}"' if title else ""
        left = "; the browser was left in front with it" if put_back.get("front_back") == "dialog" else ""
        where = f"tab \"{tab.get('name', '')}\" [{tab_id}]" if tab else f"tab [{tab_id}]"
        return finish(False, f"asked {where} to close ({shared.get('how')}); the page raised a window of its own "
                             f"[{dialog}]{named} in front of it, asking before the tab closes, so the tab is still "
                             f"open. Nothing in that window was answered: what it asks is theirs to answer{left}")
    if shared.get("error"):
        return finish(False, f"could not read the tabs of window {hwnd}: {shared['error']}")
    if pending.timed_out and not tab:
        return finish(False, f"the tabs of window {hwnd} did not answer within {ACTION_BUDGET_S:g} s")
    if not shared.get("count"):
        return finish(False, f"window {hwnd} shows no tabs")
    if not tab:
        return finish(False, f"window {hwnd} has no tab with id {tab_id} now (closed, or the strip changed); "
                             f"nothing was done; its {len(after)} tabs now are listed below")
    where = f"tab \"{tab['name']}\" [{tab_id}]"
    front_said = _KEYS_FRONT_SAID.get(str(put_back.get("front_back") or ""), "")
    back = _KEYS_BACK_SAID.get(str(put_back.get("reselected") or ""), "") \
        + (front_said or (_KEYS_FRONT_SAID["left"] if put_back.get("front_left") else ""))
    if action == "select":
        if not shared.get("seen"):
            how = shared.get("how") or "no way to select it was accepted"
            return finish(False, f"asked {where} to show ({how}), but it is not the selected tab after {WATCH_S:g} s")
        info = window_info(int(hwnd))
        details["title"] = info.title if info else ""
        already = shared.get("how") == "already showing"
        return finish(True, f"{where} is {'already' if already else 'now'} the one showing "
                            f"(window title now \"{details['title']}\")")
    if shared.get("no_control"):
        why = shared.get("keys_why")
        if why:
            # desk20 (S9): the keyboard rung was tried and stopped before any key, or its key was not sent
            said = str(shared.get("keys_said") or "").strip()
            return finish(False, f"{where} has no single close control of its own, and its browser's close-tab key "
                                 f"was not sent: {_KEYS_WHY.get(str(why), str(why))}{'; ' + said if said else ''}; "
                                 f"nothing was closed{back}")
        return finish(False, f"{where} has no single close control of its own; nothing was closed")
    if not shared.get("how"):
        return finish(False, f"{where}: its close control did not accept the request; nothing was closed")
    whole = bool(shared.get("after_complete")) and bool(after)
    # review 2: tabs counted as gone only from a strip read whole (a read cut short lists fewer than there are)
    ids_after = {a.get("id") for a in after}
    lost = [r for r in shared.get("before") or ()
            if r.get("id") and r["id"] != tab_id and r["id"] not in ids_after] if whole else []
    if lost:
        details["closed_other"] = len(lost)
    if not shared.get("seen"):
        # fa review M2 (1): no window of the page's was measured, but the page may be asking inside its own window
        held = ("; the close was pressed and no reason was measured: the page may be asking something inside its "
                "window, and that answer is theirs") if details["close_held"] else ""
        if not shared.get("read_back"):
            return finish(False, f"asked {where} to close ({shared.get('how')}), but its strip could not be read again "
                                 f"in {CLOSE_WATCH_S:g} s to see it gone, so it is not known to be closed" + held)
        return finish(False, f"asked {where} to close ({shared.get('how')}), but it is still in the strip after "
                             f"{CLOSE_WATCH_S:g} s" + (f"; {len(lost)} other tab(s) left the strip meanwhile"
                                                       if lost else "") + held + back)
    if not whole:
        return finish(True, f"closed {where} ({shared.get('how')}); seen gone from the strip (the last read of it was "
                            f"cut short){back}")
    return finish(True, f"closed {where} ({shared.get('how')}); the strip, read again, has {len(after)} tabs"
                        + (f"; {len(lost)} other tab(s) left the strip with it" if lost else "") + back)


#: Integration T4: what the keyboard rung put back after its close, in words for the report (codes from
#: :func:`_reselect`, :func:`_front_back` and :func:`_unknown_pick`; the ones that changed nothing say nothing).
#: Recheck (safety timing 5): with the browser's own pick after the close not known, the tab they were on is not
#: shown again (``unknown``) and the browser may be left in front (``left``): both said plainly.
_KEYS_BACK_SAID: dict[str, str] = {
    "reselected": "; the tab that was showing before is showing again",
    "gone": "; the tab that was showing before is gone, so the browser shows another one",
    "failed": "; the tab that was showing before could not be shown again",
    "unknown": "; the tab that was showing before was not shown again (which tab the browser picked right after the "
               "close is not known, so no tab was switched)",
}
_KEYS_FRONT_SAID: dict[str, str] = {
    "given": "; the window that was in front before is in front again",
    "act_off": "; the window that was in front before was left behind it (Act is switched off for its app)",
    "failed": "; the window that was in front before could not be brought back to the front",
    "left": "; the browser was left in front",
}

#: Why the keyboard rung sent no key, in words for the report (codes from :func:`_close_by_keys`).
_KEYS_WHY: dict[str, str] = {
    "gone": "the tab or its window is gone",
    "only_tab": "it is the window's only tab, and its close key would close the whole window",
    "not_browser": "the window is not a browser's, and the close key is a browser's",
    "act_off": "Act is switched off for that app in Yuki's settings, so Yuki does not press keys there",
    "game": "a game is in front",
    "blocked": "something on the PC stopped it",
    "in_page": "that tab is a web page's own widget, not the browser's tab",
    "not_selected": "the tab could not be made the selected one",
    "several_selected": "more than one tab is selected, and the close key would close them all",
    "not_read": "the tab strip could not be read whole right before the key",
    "moved_on": "another tab was picked right before the key (their click, or a page's own new tab)",
    "focus": "its window could not be brought to the front",
    "not_front": "its window was not in front right before the key",
    "key": "the key was refused",
}
