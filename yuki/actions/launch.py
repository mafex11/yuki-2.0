"""Launching apps, focusing windows and opening URLs.

App resolution goes through ``Get-StartApps`` - the same list the Start menu
shows - and matches case-insensitively on the display name: exact first, then
substring.  Nothing is guessed: when the query is ambiguous the candidates are
handed back so the model can pick.  There is no fuzzy matching and no
app-name allowlist anywhere in this module.
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time

import win32con
import win32gui

from yuki.actions import ActionResult
from yuki.actions.input import wait_for_input_ready
from yuki.actions.shell import run_powershell
from yuki.perception.windows import is_user_window, list_windows, window_info

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

#: How often the launch/focus watchers re-check their condition.
_POLL_S = 0.02

#: How long an activation call that Windows *accepted* is given to show up in
#: ``GetForegroundWindow`` before the next, stronger strategy is tried.
_ACTIVATION_S = 0.25

_MAX_CANDIDATES = 25

_start_apps_lock = threading.Lock()
_start_apps_cache: list[dict[str, str]] | None = None


# ---------------------------------------------------------------------------
# Start menu app list
# ---------------------------------------------------------------------------
def get_start_apps(*, refresh: bool = False) -> list[dict[str, str]]:
    """The Start menu app list as ``[{"name": ..., "appid": ...}, ...]``.

    ``Get-StartApps`` costs a PowerShell start-up (~0.4 s), so the list is
    cached for the lifetime of the process; pass ``refresh=True`` after
    installing something.
    """
    global _start_apps_cache
    with _start_apps_lock:
        if _start_apps_cache is not None and not refresh:
            return _start_apps_cache
        result = run_powershell(
            "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress",
            timeout_s=20.0,
        )
        if not result.ok:
            raise RuntimeError(f"Get-StartApps failed: {result.summary}")
        raw = result.details["stdout"].strip()
        parsed = json.loads(raw) if raw else []
        if isinstance(parsed, dict):  # a single app comes back as an object
            parsed = [parsed]
        apps = [
            {"name": str(item.get("Name") or ""), "appid": str(item.get("AppID") or "")}
            for item in parsed
            if item.get("Name") and item.get("AppID")
        ]
        _start_apps_cache = apps
        return apps


def _match_apps(query: str, apps: list[dict[str, str]]) -> tuple[dict | None, list[dict]]:
    """Resolve a query to one app, or to the candidates worth showing.

    Exact (case-insensitive) display-name match wins; otherwise substring
    matches are used, and only when exactly one matches.
    """
    wanted = query.strip().lower()
    exact = [app for app in apps if app["name"].lower() == wanted]
    if exact:
        return exact[0], exact
    partial = [app for app in apps if wanted and wanted in app["name"].lower()]
    if len(partial) == 1:
        return partial[0], partial
    partial.sort(key=lambda app: (len(app["name"]), app["name"].lower()))
    return None, partial[:_MAX_CANDIDATES]


# ---------------------------------------------------------------------------
# Waiting for windows
# ---------------------------------------------------------------------------
def _window_snapshot() -> tuple[dict[int, int], set[int]]:
    """Current user windows as ``{hwnd: pid}`` plus the set of their pids."""
    windows = list_windows()
    return ({w.hwnd: w.pid for w in windows}, {w.pid for w in windows})


def _wait_for_new_window(
    before: dict[int, int], before_pids: set[int], before_foreground: int, deadline: float
) -> tuple[int | None, str, list[int]]:
    """Wait for the launched app to show itself, and return as soon as it has.

    Accepts, in order of confidence: a new window belonging to a process that
    did not exist before; a new window of an already running process (an app
    opening a second window); the foreground window changing to something that
    was not focused before (single-instance apps that just activate the window
    they already had).

    Every window that appeared is returned alongside the chosen one.  When more
    than one did, the caller hands the whole list to the model rather than
    waiting on a timer to see whether a "better" window turns up -- guessing on
    a stopwatch is exactly what the contract forbids.

    Returns:
        ``(hwnd | None, reason, appeared)``.
    """
    while time.monotonic() < deadline:
        current, _ = _window_snapshot()
        fresh = [hwnd for hwnd in current if hwnd not in before]
        from_new_process = [
            hwnd for hwnd in fresh if current[hwnd] not in before_pids
        ]
        for hwnd in reversed(from_new_process):
            if is_user_window(hwnd):  # re-check: splash windows come and go
                return hwnd, "new window from a new process", fresh
        if fresh:
            return fresh[-1], "new window from an already running process", fresh
        foreground = _user32.GetForegroundWindow()
        if (
            foreground
            and foreground != before_foreground
            and is_user_window(foreground)
        ):
            return foreground, "foreground changed to an already open window", fresh
        time.sleep(_POLL_S)
    return None, "no new or newly focused window appeared", []


# ---------------------------------------------------------------------------
# Public actions
# ---------------------------------------------------------------------------
def _quote(value: str) -> str:
    """Single-quote a string for PowerShell (no expansion, '' escapes)."""
    return "'" + value.replace("'", "''") + "'"


def launch_app(query: str, *, timeout_s: float = 8.0) -> ActionResult:
    """Launch a Start menu app by display name and wait for its window.

    Args:
        query: what the user called the app; matched against Start menu display
            names (exact, then substring).
        timeout_s: how long to wait for a window to appear after the launch.

    Returns:
        ActionResult with ``details = {"launched", "hwnd", "candidates",
        "new_windows", "name", "appid", "ready", "ready_ms"}``.  ``ready_ms`` is
        how long the new window took to start accepting keyboard input, and
        ``ready`` whether it got there at all (a window that opened in the
        background never will until it is focused).  When the query matches 0 or >1 apps
        and none matches exactly, ``ok`` is False and ``candidates`` lists the
        options - the model chooses, this function never guesses.  Likewise
        ``new_windows`` lists every window that appeared, so a launch that
        produced more than one is reported rather than resolved by guesswork.
    """
    started = time.perf_counter()

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        return ActionResult(
            ok=ok,
            summary=summary,
            details=details,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    if not query or not query.strip():
        return finish(False, "no app name given", {"launched": False, "hwnd": None, "candidates": []})

    apps = get_start_apps()
    chosen, candidates = _match_apps(query, apps)
    if chosen is None:
        listed = ", ".join(app["name"] for app in candidates) or "none"
        return finish(
            False,
            f"{len(candidates)} Start menu apps match {query!r}: {listed}. "
            f"Pick one and launch it by its exact name.",
            {"launched": False, "hwnd": None, "candidates": candidates},
        )

    before, before_pids = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    # shell:AppsFolder\<AppID> is the Start menu's own launch path and works for
    # both packaged apps (AppUserModelID) and desktop shortcuts.
    result = run_powershell(
        f"Start-Process -FilePath {_quote('shell:AppsFolder\\' + chosen['appid'])}",
        timeout_s=max(2.0, min(timeout_s, 15.0)),
    )
    details: dict = {
        "launched": result.ok,
        "hwnd": None,
        "candidates": [],
        "name": chosen["name"],
        "appid": chosen["appid"],
        "launch_stderr": result.details.get("stderr", ""),
    }
    if not result.ok:
        return finish(
            False,
            f"could not start {chosen['name']!r}: {result.details.get('stderr') or result.summary}",
            details,
        )

    hwnd, reason, appeared = _wait_for_new_window(
        before, before_pids, before_foreground, time.monotonic() + timeout_s
    )
    details["hwnd"] = hwnd
    details["window_reason"] = reason
    # More than one window showed up: say so instead of silently picking.
    details["new_windows"] = [
        {
            "hwnd": other,
            "title": (info.title if (info := window_info(other)) else ""),
            "process_name": info.process_name if info else "",
        }
        for other in appeared
    ]
    if hwnd is None:
        return finish(
            True,
            f"started {chosen['name']!r} but no window appeared within {timeout_s:g}s "
            f"(it may be a background app or still loading)",
            details,
        )
    info = window_info(hwnd)
    details["title"] = info.title if info else ""
    details["process_name"] = info.process_name if info else ""
    # A window that exists is not yet a window that listens.  Wait for the app's
    # own GUI thread to report a focused control, so the caller's first keystroke
    # is not swallowed by a half-built window - and report the wait either way.
    ready, ready_ms = wait_for_input_ready(hwnd)
    details["ready"] = ready
    details["ready_ms"] = round(ready_ms, 1)
    extra = len(details["new_windows"]) - 1
    return finish(
        True,
        f"launched {chosen['name']!r}: hwnd {hwnd} "
        f"({details['process_name']}) \"{details['title']}\" - {reason}"
        + (f"; ready for input after {ready_ms:.0f} ms" if ready else
           f"; it is not in the foreground / not accepting input yet after "
           f"{ready_ms:.0f} ms, so focus it before typing")
        + (f"; {extra} other new window(s) appeared, see new_windows" if extra > 0 else ""),
        details,
    )


def _try_activate(hwnd: int) -> bool:
    """One activation attempt using the thread-input attach trick.

    ``SetForegroundWindow`` is refused when our process does not own the
    foreground window; attaching to the foreground thread's input queue for the
    duration of the call lifts that restriction.

    Returns:
        What ``SetForegroundWindow`` reported: False means Windows refused the
        call outright, so there is nothing to wait for.
    """
    foreground = _user32.GetForegroundWindow()
    our_thread = _kernel32.GetCurrentThreadId()
    target_thread = _user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground), None)
    attached = False
    if foreground and target_thread and target_thread != our_thread:
        attached = bool(_user32.AttachThreadInput(our_thread, target_thread, True))
    try:
        _user32.BringWindowToTop(ctypes.c_void_p(hwnd))
        return bool(_user32.SetForegroundWindow(ctypes.c_void_p(hwnd)))
    finally:
        if attached:
            _user32.AttachThreadInput(our_thread, target_thread, False)


def focus_window(hwnd: int, *, timeout_s: float = 2.0) -> ActionResult:
    """Bring a window to the foreground, restoring it if minimized.

    Tries progressively stronger activation paths and polls
    ``GetForegroundWindow`` after each, so it returns as soon as the window is
    actually focused - and then waits, inside the same ``timeout_s`` budget, for
    the window to be ready to *receive* input (see
    :func:`yuki.actions.input.wait_for_input_ready`).  ``details["ready_ms"]``
    reports that wait and ``details["ready"]`` whether it ever came true; a window
    can be in the foreground a good 50 ms before it is listening, and keystrokes
    sent in between are dropped without any error.
    """
    started = time.perf_counter()
    deadline = time.monotonic() + timeout_s

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        return ActionResult(
            ok=ok,
            summary=summary,
            details=details,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    if not win32gui.IsWindow(hwnd):
        return finish(False, f"hwnd {hwnd} is not a window (it may have closed)", {"hwnd": hwnd})

    info = window_info(hwnd)
    details: dict = {
        "hwnd": hwnd,
        "title": info.title if info else "",
        "process_name": info.process_name if info else "",
        "was_minimized": bool(win32gui.IsIconic(hwnd)),
        "attempts": [],
    }
    if win32gui.IsIconic(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

    def focused() -> bool:
        return _user32.GetForegroundWindow() == hwnd

    def wait(slice_deadline: float) -> bool:
        while time.monotonic() < slice_deadline:
            if focused():
                return True
            time.sleep(_POLL_S)
        return focused()

    attempts = (
        (
            "SetForegroundWindow",
            lambda: bool(_user32.SetForegroundWindow(ctypes.c_void_p(hwnd))),
        ),
        ("AttachThreadInput+SetForegroundWindow", lambda: _try_activate(hwnd)),
        (
            "SwitchToThisWindow",
            lambda: _user32.SwitchToThisWindow(ctypes.c_void_p(hwnd), 1) or True,
        ),
    )
    for name, action in attempts:
        if focused():
            break
        details["attempts"].append(name)
        try:
            accepted = action()
        except Exception as exc:  # noqa: BLE001 - try the next strategy
            details.setdefault("errors", []).append(f"{name}: {exc}")
            continue
        if not accepted:
            # Windows refused the call, which it does whenever another process
            # owns the foreground.  Escalating now instead of polling out a
            # share of the budget saves about a second per focus - and on a
            # desktop where something else keeps grabbing focus, that second is
            # the whole window in which typing is safe.
            continue
        if time.monotonic() >= deadline:
            break
        # An accepted activation lands almost immediately; poll for it briefly,
        # then escalate rather than sit out the rest of the budget.
        if wait(min(time.monotonic() + _ACTIVATION_S, deadline)):
            break

    # Nothing has taken yet: spend what is left of the budget watching, in case
    # a slow provider is still switching.
    if not focused():
        wait(deadline)

    if focused():
        # Foreground is only half of "focused": until the window's own GUI thread
        # names a focused control it is not listening, and keystrokes sent into
        # that gap vanish although SendInput reports them accepted.  Wait for the
        # condition with what is left of the budget instead of returning early and
        # letting the caller type into nothing.
        ready, ready_ms = wait_for_input_ready(
            hwnd, timeout_s=max(deadline - time.monotonic(), 0.0)
        )
        details["ready"] = ready
        details["ready_ms"] = round(ready_ms, 1)
        note = (
            f"; ready for input after {ready_ms:.0f} ms"
            if ready
            else f"; still no focused control after {ready_ms:.0f} ms, so typing "
            f"into it may lose characters"
        )
        return finish(
            True,
            f"focused hwnd {hwnd} ({details['process_name']}) \"{details['title']}\""
            + note,
            details,
        )
    details["foreground_hwnd"] = _user32.GetForegroundWindow()
    return finish(
        False,
        f"could not focus hwnd {hwnd} within {timeout_s:g}s; foreground is "
        f"{details['foreground_hwnd']}",
        details,
    )


def open_url(url: str) -> ActionResult:
    """Open a URL (or any shell target) with the user's default handler."""
    started = time.perf_counter()

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        return ActionResult(
            ok=ok,
            summary=summary,
            details=details,
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    if not url or not url.strip():
        return finish(False, "no url given", {"url": url})
    target = url.strip()
    try:
        os.startfile(target)  # noqa: S606 - opening a user-provided target is the point
        return finish(True, f"opened {target} with the default handler", {"url": target, "via": "startfile"})
    except Exception as exc:
        result = run_powershell(f"Start-Process {_quote(target)}", timeout_s=10.0)
        details = {
            "url": target,
            "via": "Start-Process",
            "startfile_error": str(exc),
            "stderr": result.details.get("stderr", ""),
        }
        if result.ok:
            return finish(True, f"opened {target} via Start-Process", details)
        return finish(False, f"could not open {target}: {result.summary}", details)
