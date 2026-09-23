"""Launching apps, focusing windows and opening URLs.

App resolution goes through ``Get-StartApps`` - the same list the Start menu
shows - and matches case-insensitively on the display name: exact first, then
substring.  Nothing is guessed: when the query is ambiguous the candidates are
handed back so the model can pick.  There is no fuzzy matching and no
app-name allowlist anywhere in this module.

**Starting an app with arguments** (a URL, a file, a folder for it to open) cannot
go through ``shell:AppsFolder\\<AppID>``, which drops them.  So the app is
resolved as usual and then started another way, chosen from the *kind* of Start
menu entry it is - facts about the entry, never about which app it is:

* a packaged app (its AppID is an AppUserModelID, ``<PackageFamilyName>!<App>``)
  is activated through ``IApplicationActivationManager::ActivateApplication``,
  which hands the arguments over and keeps the app's package identity.  Running
  the executable inside its package folder directly would start it *without*
  that identity, and so with a different data folder (a fresh browser profile);
* a desktop app is started with ``Start-Process -FilePath <exe> -ArgumentList
  ...``, the executable being the target of its Start menu shortcut (whose own
  arguments and working folder are kept), else the file its AppID names, else
  the image of a running process whose window carries that AppID.

An app that is already running usually hands the arguments to its existing
window instead of opening a new one; the watcher therefore also accepts a
title change of a window of the process that was started.

**After the window shows up** it is brought to the foreground exactly as
:func:`focus_window` does and waited on until it accepts input: launching an app
is asking to use it, and on 2026-09-23 a launch that returned "not in the
foreground" cost the model a whole round just to focus it.  When the app was
handed something to open (``args``, or :func:`open_url`), the call then waits -
bounded, a condition poll - until the window has exposed that content
(:func:`yuki.perception.tree.wait_for_content`), and says which: "content ready
after N ms (Document ...)" or "content still not ready after N ms (...)".  The
window is also noted as fresh, so the next tree read of it waits out a
half-built page instead of returning the frame around it.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import threading
import time
from dataclasses import asdict

import psutil
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

#: Default bound on the wait for opened content - the value of
#: :data:`yuki.perception.tree.CONTENT_WAIT_S`, repeated here so that importing
#: this module does not build the UIA wrapper (see :func:`_note_fresh`).
CONTENT_WAIT_S = 6.0

#: ``ACTIVATEOPTIONS.AO_NOERRORUI``: a failed activation reports an HRESULT to us
#: instead of putting an error dialog in front of the user.
_AO_NOERRORUI = 0x2

_CLSID_APPLICATION_ACTIVATION_MANAGER = "{45BA127D-10A8-46EA-8AB7-56EA9078943C}"
_IID_APPLICATION_ACTIVATION_MANAGER = "{2E941141-7F97-4756-BA1D-9DECDE894A3D}"

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

    The JSON goes via a temp file rather than stdout.  ``run_powershell``'s
    stdout is what the *model* reads, so it is clipped (head + tail, middle
    announced) above :data:`yuki.actions.shell.MAX_STREAM_CHARS`; this desktop's
    Start menu serialises to ~18 kB today and would cross that line with a
    handful more apps, at which point the JSON arrives with a human-readable
    marker spliced into the middle of it and ``json.loads`` fails.  A machine
    reader must not depend on a channel shaped for a reader, so PowerShell writes
    the file and prints nothing.
    """
    global _start_apps_cache
    with _start_apps_lock:
        if _start_apps_cache is not None and not refresh:
            return _start_apps_cache
        parsed = _powershell_json(
            "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress "
            "| Set-Content -LiteralPath {path} -Encoding UTF8",
            what="Get-StartApps",
        )
        apps = [
            {"name": str(item.get("Name") or ""), "appid": str(item.get("AppID") or "")}
            for item in parsed
            if item.get("Name") and item.get("AppID")
        ]
        _start_apps_cache = apps
        return apps


def _powershell_json(script: str, *, what: str, timeout_s: float = 20.0) -> list:
    """Run ``script`` and parse the JSON it writes to ``{path}`` (a temp file).

    ``{path}`` in ``script`` is replaced by the quoted temp-file path.  A file,
    not stdout: stdout is shaped for the model to read and is clipped when long
    (see :func:`get_start_apps`).  A single object is returned as a one-item list
    and an empty file as an empty list.

    Raises:
        RuntimeError: the script failed.
    """
    handle, path = tempfile.mkstemp(prefix="yuki-ps-", suffix=".json")
    os.close(handle)
    try:
        result = run_powershell(script.replace("{path}", _quote(path)), timeout_s=timeout_s)
        if not result.ok:
            raise RuntimeError(f"{what} failed: {result.summary}")
        with open(path, encoding="utf-8-sig") as stream:
            raw = stream.read().strip()
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
    parsed = json.loads(raw) if raw else []
    if isinstance(parsed, dict):  # a single item comes back as an object
        parsed = [parsed]
    return parsed if isinstance(parsed, list) else []


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
def _window_snapshot() -> tuple[dict[int, int], set[int], dict[int, tuple[str, str]]]:
    """Current user windows as ``{hwnd: pid}``, the set of their pids, and
    ``{hwnd: (title, process name lowercased)}``."""
    windows = list_windows()
    return (
        {w.hwnd: w.pid for w in windows},
        {w.pid for w in windows},
        {w.hwnd: (w.title, (w.process_name or "").lower()) for w in windows},
    )


def _wait_for_new_window(
    before: dict[int, int],
    before_pids: set[int],
    before_foreground: int,
    deadline: float,
    *,
    before_titles: dict[int, tuple[str, str]] | None = None,
    target_processes: frozenset[str] = frozenset(),
) -> tuple[int | None, str, list[int]]:
    """Wait for the launched app to show itself, and return as soon as it has.

    Accepts, in order of confidence: a new window belonging to a process that
    did not exist before; a new window of an already running process (an app
    opening a second window); the foreground window changing to something that
    was not focused before (single-instance apps that just activate the window
    they already had).  With ``target_processes`` (lowercased image names of
    what was just started) a window of one of those processes that was already
    open and has changed its title also counts: an app that is handed a URL or a
    file while running typically opens it in the window it has, and the new title
    is the first visible sign that it did.

    Every window that appeared is returned alongside the chosen one.  When more
    than one did, the caller hands the whole list to the model rather than
    waiting on a timer to see whether a "better" window turns up -- guessing on
    a stopwatch is exactly what the contract forbids.

    Returns:
        ``(hwnd | None, reason, appeared)``.
    """
    while time.monotonic() < deadline:
        current, _, titles = _window_snapshot()
        fresh = [hwnd for hwnd in current if hwnd not in before]
        from_new_process = [
            hwnd for hwnd in fresh if current[hwnd] not in before_pids
        ]
        for hwnd in reversed(from_new_process):
            if is_user_window(hwnd):  # re-check: splash windows come and go
                return hwnd, "new window from a new process", fresh
        if fresh:
            return fresh[-1], "new window from an already running process", fresh
        if target_processes and before_titles:
            for hwnd, (title, process) in titles.items():
                old = before_titles.get(hwnd)
                if old is not None and process in target_processes and title != old[0]:
                    return hwnd, f"existing {process} window changed its title", fresh
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


def _is_packaged(appid: str) -> bool:
    """Whether a Start menu AppID is a packaged app's AppUserModelID.

    Packaged AUMIDs have the documented shape ``<PackageFamilyName>!<AppId>``;
    desktop entries are file paths (``{KNOWNFOLDERID}\\dir\\app.exe``) or an
    explicit AUMID the app chose, neither of which contains ``!``.  Parsing a
    structured id, not matching an app.
    """
    return "!" in appid


def _appid_path(appid: str) -> str | None:
    """The file a desktop AppID names, when it is a path to an existing file.

    ``Get-StartApps`` reports a shortcut with no explicit AUMID by its target,
    written as ``{KNOWNFOLDERID}\\relative\\path`` or as a plain absolute path.
    """
    path: str | None = None
    if appid.startswith("{") and "}\\" in appid:
        folder_id, rest = appid.split("\\", 1)
        try:
            import pywintypes
            from win32com.shell import shell

            base = shell.SHGetKnownFolderPath(pywintypes.IID(folder_id), 0, None)
        except Exception:
            return None
        path = os.path.join(base, rest)
    elif os.path.isabs(appid):
        path = appid
    return path if path and os.path.isfile(path) else None


def _start_menu_shortcuts(name: str) -> list[dict]:
    """Start menu ``.lnk`` files whose name is ``name``, with their targets.

    The Start menu's display name for a shortcut is the shortcut's file name, so
    this looks the chosen entry back up under the per-user and all-users
    Programs folders and reads each match with ``WScript.Shell``.

    Returns:
        ``[{"lnk", "target", "arguments", "workdir"}, ...]`` (possibly empty).
    """
    script = (
        f"$name = {_quote(name)}\n"
        "$shell = New-Object -ComObject WScript.Shell\n"
        "$roots = @([Environment]::GetFolderPath('Programs'), "
        "[Environment]::GetFolderPath('CommonPrograms'))\n"
        "$found = foreach ($root in $roots) {\n"
        "  if ($root -and (Test-Path -LiteralPath $root)) {\n"
        "    Get-ChildItem -LiteralPath $root -Recurse -Filter *.lnk -File "
        "-ErrorAction SilentlyContinue | Where-Object { $_.BaseName -eq $name } | "
        "ForEach-Object {\n"
        "      $s = $shell.CreateShortcut($_.FullName)\n"
        "      [pscustomobject]@{ lnk = $_.FullName; target = $s.TargetPath; "
        "arguments = $s.Arguments; workdir = $s.WorkingDirectory }\n"
        "    }\n"
        "  }\n"
        "}\n"
        "ConvertTo-Json -InputObject @($found) -Compress "
        "| Set-Content -LiteralPath {path} -Encoding UTF8"
    )
    items = _powershell_json(script, what="Start menu shortcut lookup")
    return [
        {
            "lnk": str(item.get("lnk") or ""),
            "target": str(item.get("target") or ""),
            "arguments": str(item.get("arguments") or ""),
            "workdir": str(item.get("workdir") or ""),
        }
        for item in items
        if isinstance(item, dict)
    ]


def _window_appid(hwnd: int) -> str:
    """The AppUserModelID a window declares for itself (``""`` when none)."""
    try:
        from win32com.propsys import propsys, pscon

        store = propsys.SHGetPropertyStoreForWindow(hwnd, propsys.IID_IPropertyStore)
        value = store.GetValue(pscon.PKEY_AppUserModel_ID).GetValue()
    except Exception:
        return ""
    return value if isinstance(value, str) else ""


def _running_image(appid: str) -> dict | None:
    """The executable of a running process whose window carries ``appid``.

    Identity by AppUserModelID - the id the taskbar groups windows by - so this
    only ever finds the app that was asked for, never a lookalike.
    """
    wanted = appid.lower()
    for window in list_windows():
        if _window_appid(window.hwnd).lower() != wanted:
            continue
        try:
            exe = psutil.Process(window.pid).exe()
        except Exception:
            continue
        if exe and os.path.isfile(exe):
            return {"exe": exe, "hwnd": window.hwnd, "pid": window.pid}
    return None


def _command_line(args: list[str]) -> str:
    """Arguments as one Windows command line (quoted where needed)."""
    return subprocess.list2cmdline([str(arg) for arg in args])


def _start_process_command(exe: str, arguments: str, workdir: str = "") -> str:
    """The ``Start-Process`` script that starts ``exe`` with ``arguments``.

    ``-ArgumentList`` gets one pre-quoted string: Windows PowerShell joins an
    array with bare spaces, which would split a path that contains one.
    ``-PassThru`` prints the new process id.
    """
    parts = [f"$p = Start-Process -FilePath {_quote(exe)}"]
    if arguments:
        parts.append(f"-ArgumentList {_quote(arguments)}")
    if workdir:
        parts.append(f"-WorkingDirectory {_quote(workdir)}")
    parts.append("-PassThru")
    return " ".join(parts) + "; if ($p) { $p.Id }"


def _plan_start_with_args(chosen: dict[str, str], args: list[str]) -> tuple[dict | None, list[str]]:
    """How to start ``chosen`` with ``args``, or why it cannot be.

    Returns:
        ``(plan, notes)``.  ``plan`` is ``{"via": "ActivateApplication", "aumid",
        "arguments"}`` for a packaged app, or ``{"via": "Start-Process", "exe",
        "arguments", "workdir", "source", "command"}`` for a desktop app; ``None``
        when no executable could be found, in which case ``notes`` says what was
        tried.
    """
    arguments = _command_line(args)
    appid = chosen["appid"]
    notes: list[str] = []
    if _is_packaged(appid):
        return {"via": "ActivateApplication", "aumid": appid, "arguments": arguments}, notes

    def plan(exe: str, source: str, prefix: str = "", workdir: str = "") -> dict:
        line = " ".join(part for part in (prefix.strip(), arguments) if part)
        return {
            "via": "Start-Process",
            "exe": exe,
            "arguments": line,
            "workdir": workdir,
            "source": source,
            "command": _start_process_command(exe, line, workdir),
        }

    try:
        shortcuts = _start_menu_shortcuts(chosen["name"])
    except Exception as exc:  # noqa: BLE001 - try the other sources
        shortcuts = []
        notes.append(f"shortcut lookup failed: {exc}")
    for shortcut in shortcuts:
        target = shortcut["target"]
        if target and os.path.isfile(target):
            return plan(
                target,
                f"Start menu shortcut {shortcut['lnk']}",
                shortcut["arguments"],
                shortcut["workdir"],
            ), notes
        notes.append(
            f"shortcut {shortcut['lnk']} does not point at a program file "
            f"({target or 'no target'})"
        )
    if not shortcuts:
        notes.append(f"no Start menu shortcut named {chosen['name']!r}")
    path = _appid_path(appid)
    if path:
        return plan(path, f"AppID path {appid}"), notes
    notes.append(f"AppID {appid!r} is not a path to a program file")
    running = _running_image(appid)
    if running:
        return plan(
            running["exe"], f"running process {running['pid']} (hwnd {running['hwnd']})"
        ), notes
    notes.append("no running window carries that AppID")
    return None, notes


def _activation_manager_interface():  # -> type[comtypes.IUnknown]
    """``IApplicationActivationManager``, declared once on first use.

    Only ``ActivateApplication`` is declared: it is the first method of the
    interface, so the vtable is right without the two after it.
    """
    global _ACTIVATION_INTERFACE
    if _ACTIVATION_INTERFACE is None:
        import comtypes
        from ctypes import wintypes

        class IApplicationActivationManager(comtypes.IUnknown):
            _iid_ = comtypes.GUID(_IID_APPLICATION_ACTIVATION_MANAGER)
            _methods_ = [
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "ActivateApplication",
                    (["in"], wintypes.LPCWSTR, "appUserModelId"),
                    (["in"], wintypes.LPCWSTR, "arguments"),
                    (["in"], ctypes.c_int, "options"),
                    (["out"], ctypes.POINTER(wintypes.DWORD), "processId"),
                ),
            ]

        _ACTIVATION_INTERFACE = IApplicationActivationManager
    return _ACTIVATION_INTERFACE


_ACTIVATION_INTERFACE = None


def _activate_packaged(aumid: str, arguments: str, timeout_s: float) -> tuple[int | None, str]:
    """Activate a packaged app with a command line, as the Start menu would.

    Runs on its own thread with its own COM apartment, bounded by ``timeout_s``.

    Returns:
        ``(pid, error)``: the activated process id, or ``None`` and why not.
    """
    result: dict = {}

    def worker() -> None:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception:
            pass
        try:
            manager = comtypes.client.CreateObject(
                _CLSID_APPLICATION_ACTIVATION_MANAGER,
                clsctx=comtypes.CLSCTX_LOCAL_SERVER,
                interface=_activation_manager_interface(),
            )
            result["pid"] = int(
                manager.ActivateApplication(aumid, arguments or None, _AO_NOERRORUI)
            )
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=worker, name="yuki-activate", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if "pid" in result:
        return result["pid"], ""
    return None, result.get("error") or f"activation did not return within {timeout_s:g}s"


def _process_name(pid: int | None) -> str:
    """Lowercased image name of ``pid``, or ``""``."""
    if not pid:
        return ""
    try:
        return psutil.Process(pid).name().lower()
    except Exception:
        return ""


def launch_app(
    query: str,
    *,
    args: list[str] | None = None,
    timeout_s: float = 8.0,
    content_timeout_s: float = CONTENT_WAIT_S,
) -> ActionResult:
    """Launch a Start menu app by display name and wait for its window.

    Args:
        query: what the user called the app; matched against Start menu display
            names (exact, then substring).
        args: command-line arguments for the app (a URL, a file, a folder).  The
            app is resolved exactly as without them and then started so that it
            receives them - see the module docstring for how.  An app that
            cannot be given arguments is reported as ``ok=False`` with the
            reason, not launched without them.
        timeout_s: how long to wait for a window to appear after the launch;
            bringing it to the front then uses what is left of it (at least
            :data:`_FOCUS_FLOOR_S`).
        content_timeout_s: with ``args``, how much longer to wait for the window
            to expose what it was handed (see
            :func:`yuki.perception.tree.wait_for_content`).

    The window that appeared (or changed) is brought to the foreground exactly as
    :func:`focus_window` does and waited on until it accepts input, so the caller
    can type into it straight away.  With ``args`` the call then waits, up to
    ``content_timeout_s``, until the window's content is on screen and readable -
    its visible area covered by accessible elements, or a newly titled Document -
    and the summary says either "content ready after N ms (Document ...)" or
    "content still not ready after N ms (...)".

    Returns:
        ActionResult with ``details = {"launched", "hwnd", "candidates",
        "new_windows", "name", "appid", "foreground", "ready", "ready_ms",
        "focus", "focus_ms"}``.  ``foreground`` says whether the window was
        brought to the front, ``ready`` whether it then accepted keyboard input
        and ``ready_ms`` how long that took.  With ``args`` also
        ``content_ready``, ``content_ms`` and ``content`` (the whole
        :class:`yuki.perception.tree.ContentCheck`).  When the query matches 0 or >1 apps
        and none matches exactly, ``ok`` is False and ``candidates`` lists the
        options - the model chooses, this function never guesses.  Likewise
        ``new_windows`` lists every window that appeared, so a launch that
        produced more than one is reported rather than resolved by guesswork.
        With ``args``, ``details`` also carries ``args``, ``arguments`` (the
        command line handed over), ``via`` (``ActivateApplication`` or
        ``Start-Process``), ``exe``/``exe_source``/``start_command`` for a desktop
        app, and ``pid`` when the start reported one.
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
    if args is not None and (
        not isinstance(args, (list, tuple)) or not all(isinstance(a, str) for a in args)
    ):
        return finish(
            False,
            f"args must be a list of strings, got {args!r}",
            {"launched": False, "hwnd": None, "candidates": []},
        )
    arg_list = [a for a in (args or []) if a != ""]

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

    details: dict = {
        "launched": False,
        "hwnd": None,
        "candidates": [],
        "name": chosen["name"],
        "appid": chosen["appid"],
    }
    start_budget = max(2.0, min(timeout_s, 15.0))
    plan: dict | None = None
    if arg_list:
        details["args"] = arg_list
        plan, notes = _plan_start_with_args(chosen, arg_list)
        if notes:
            details["resolution_notes"] = notes
        if plan is None:
            return finish(
                False,
                f"cannot pass arguments to {chosen['name']!r}: its Start menu entry "
                f"(AppID {chosen['appid']!r}) is not a packaged app and no program "
                f"file was found for it ({'; '.join(notes)}). Nothing was started.",
                details,
            )
        details["via"] = plan["via"]
        details["arguments"] = plan["arguments"]
        if plan["via"] == "Start-Process":
            details["exe"] = plan["exe"]
            details["exe_source"] = plan["source"]
            details["start_command"] = plan["command"]

    before, before_pids, before_titles = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    target_processes: frozenset[str] = frozenset()
    if plan is None:
        # shell:AppsFolder\<AppID> is the Start menu's own launch path and works
        # for both packaged apps (AppUserModelID) and desktop shortcuts.
        result = run_powershell(
            f"Start-Process -FilePath {_quote('shell:AppsFolder\\' + chosen['appid'])}",
            timeout_s=start_budget,
        )
        details["launched"] = result.ok
        details["launch_stderr"] = result.details.get("stderr", "")
        failure = result.details.get("stderr") or result.summary
    elif plan["via"] == "ActivateApplication":
        pid, error = _activate_packaged(plan["aumid"], plan["arguments"], start_budget)
        details["launched"] = pid is not None
        details["pid"] = pid
        if error:
            details["launch_error"] = error
        failure = (
            f"activating the packaged app {plan['aumid']!r} with arguments failed: "
            f"{error}"
        )
        if pid is not None and (name := _process_name(pid)):
            target_processes = frozenset({name})
    else:
        result = run_powershell(plan["command"], timeout_s=start_budget)
        details["launched"] = result.ok
        details["launch_stderr"] = result.details.get("stderr", "")
        stdout = str(result.details.get("stdout") or "").strip()
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        details["pid"] = int(last_line) if last_line.isdigit() else None
        failure = result.details.get("stderr") or result.summary
        target_processes = frozenset({os.path.basename(plan["exe"]).lower()})
    if not details["launched"]:
        return finish(False, f"could not start {chosen['name']!r}: {failure}", details)

    window_deadline = time.monotonic() + timeout_s
    hwnd, reason, appeared = _wait_for_new_window(
        before,
        before_pids,
        before_foreground,
        window_deadline,
        before_titles=before_titles,
        target_processes=target_processes,
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
            f"started {chosen['name']!r}"
            + (" with arguments" if arg_list else "")
            + f" but no window appeared or changed within {timeout_s:g}s "
            f"(it may be a background app, still loading, or already showing that)",
            details,
        )
    info = window_info(hwnd)
    details["title"] = info.title if info else ""
    details["process_name"] = info.process_name if info else ""
    _note_fresh(hwnd)
    # Launching something is asking to use it: bring the window to the front and
    # wait for it to listen, within what is left of the budget, so the next step
    # does not have to spend a round on focusing it.
    forward = _bring_forward(hwnd, window_deadline, details)
    content_note = ""
    if arg_list:
        # It was handed something to open: wait (bounded) until that is on
        # screen and readable, so the next look sees it rather than the frame.
        content_note = _wait_for_opened_content(hwnd, content_timeout_s, details)
        if (fresh := window_info(hwnd)) is not None:
            details["title"] = fresh.title
    extra = len(details["new_windows"]) - 1
    with_args = ""
    if arg_list:
        shown = details["arguments"]
        with_args = f" with arguments {shown if len(shown) <= 160 else shown[:160] + '…'}"
    return finish(
        True,
        f"launched {chosen['name']!r}{with_args}: hwnd {hwnd} "
        f"({details['process_name']}) \"{details['title']}\" - {reason}; {forward}"
        + (f"; {content_note}" if content_note else "")
        + (f"; {extra} other new window(s) appeared, see new_windows" if extra > 0 else ""),
        details,
    )


#: Least time :func:`_bring_forward` gets, however little of the launch budget
#: the window's appearance left: :func:`focus_window`'s own default.
_FOCUS_FLOOR_S = 2.0


def _bring_forward(hwnd: int, deadline: float, details: dict) -> str:
    """Activate ``hwnd`` as :func:`focus_window` does and say how that went.

    Budget: what is left until ``deadline``, but at least :data:`_FOCUS_FLOOR_S`.
    Fills ``details`` with ``foreground``, ``ready``, ``ready_ms`` and ``focus``
    (the focus attempt's own details).

    Returns:
        A clause for the summary.
    """
    budget = max(deadline - time.monotonic(), _FOCUS_FLOOR_S)
    focus = focus_window(hwnd, timeout_s=budget)
    details["foreground"] = focus.ok
    details["focus"] = focus.details
    details["focus_ms"] = round(focus.elapsed_ms, 1)
    details["ready"] = bool(focus.details.get("ready"))
    details["ready_ms"] = focus.details.get("ready_ms")
    if focus.ok and details["ready"]:
        return f"in the foreground and ready for input after {focus.elapsed_ms:.0f} ms"
    if focus.ok:
        return (
            f"in the foreground, but no control in it had keyboard focus after "
            f"{focus.elapsed_ms:.0f} ms, so typing into it may lose characters"
        )
    other = focus.details.get("foreground_hwnd")
    who = ""
    if other and (front := window_info(other)) is not None:
        who = f" ({front.process_name} \"{front.title}\" is)"
    return (
        f"could not bring it to the foreground within {budget:g}s{who}, so focus "
        f"it before typing"
    )


def _note_fresh(hwnd: int) -> None:
    """Tell the tree reader this window has just been opened or handed a target.

    Imported here, as :mod:`yuki.actions.input` does, so that launching never
    depends on the UIA wrapper having been built.
    """
    try:
        from yuki.perception.tree import note_window_fresh
    except Exception:
        return
    note_window_fresh(hwnd)


def _wait_for_opened_content(
    hwnd: int, timeout_s: float, details: dict, *, require_document: bool = False
) -> str:
    """Wait (bounded) for ``hwnd`` to expose what it was just asked to open.

    See :func:`yuki.perception.tree.wait_for_content`.  Fills ``details`` with
    ``content_ready``, ``content_ms`` and ``content`` (the whole check).

    Returns:
        A clause for the summary ("content ready after N ms (...)" or
        "content still not ready after N ms (...)").
    """
    try:
        from yuki.perception.tree import wait_for_content
    except Exception as exc:  # noqa: BLE001 - UIA unavailable: say so, do not fail
        details["content_ready"] = None
        details["content_error"] = f"{type(exc).__name__}: {exc}"
        return f"content not checked (accessibility unavailable: {exc})"
    check = wait_for_content(hwnd, timeout_s=timeout_s, require_document=require_document)
    details["content_ready"] = check.ready
    details["content_ms"] = round(check.waited_ms, 1)
    details["content"] = asdict(check)
    return check.describe()


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


#: ``ASSOCSTR_EXECUTABLE``, ``ASSOCF_NOTRUNCATE`` and ``ASSOCF_IS_PROTOCOL`` for
#: ``AssocQueryStringW``.
_ASSOCSTR_EXECUTABLE = 2
_ASSOCF_NOTRUNCATE = 0x20
_ASSOCF_IS_PROTOCOL = 0x1000

#: How long :func:`open_url` watches for the default handler's window to appear,
#: come to the front or change its title.  A bound on a condition: the watch
#: ends as soon as one of those happens, which is the common case.
_HANDLER_WINDOW_S = 3.0


def _default_handler(target: str) -> str:
    """Lowercased image name of the program Windows opens ``target`` with, or ``""``.

    Read from the shell's own association (``AssocQueryStringW``): the URL's
    scheme, ``Folder`` for a directory, else the file's extension.  A registry
    fact, not a guess about which program it is.
    """
    from urllib.parse import urlsplit

    try:
        scheme = urlsplit(target).scheme
    except ValueError:
        scheme = ""
    flags = _ASSOCF_NOTRUNCATE
    if len(scheme) > 1:  # "c:" is a drive letter, not a scheme
        assoc, flags = scheme, flags | _ASSOCF_IS_PROTOCOL
    elif os.path.isdir(target):
        assoc = "Folder"
    else:
        assoc = os.path.splitext(target)[1]
    if not assoc:
        return ""
    try:
        from ctypes import wintypes

        query = ctypes.windll.shlwapi.AssocQueryStringW
        query.argtypes = [
            ctypes.c_uint,
            ctypes.c_uint,
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        query.restype = ctypes.c_long
        size = wintypes.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if query(flags, _ASSOCSTR_EXECUTABLE, assoc, "open", buffer, ctypes.byref(size)) != 0:
            return ""
    except Exception:
        return ""
    return os.path.basename(buffer.value).lower()


def open_url(
    url: str, *, app: str | None = None, content_timeout_s: float = CONTENT_WAIT_S
) -> ActionResult:
    """Open a URL (or any shell target: a file, a folder).

    Args:
        url: the target.
        app: ``None`` opens it with the user's default handler.  Otherwise the
            Start menu app to open it with, which is exactly
            ``launch_app(app, args=[url])`` - the target is handed to the app as
            an argument, so nothing is typed anywhere - including bringing the
            window to the front and waiting for the content.
        content_timeout_s: bound on the wait for the content to be on screen.

    With the default handler, the handler's window is watched for (a new window,
    the foreground changing, or a window of the handler's program changing its
    title, up to :data:`_HANDLER_WINDOW_S`; failing those, the foreground window
    if it belongs to the handler).  That window is brought to the front as
    :func:`focus_window` does, and if it shows a document (or a content area that
    is still empty) the call waits for the content as ``launch_app`` does.
    """
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
    if app is not None and app.strip():
        launched = launch_app(app, args=[target], content_timeout_s=content_timeout_s)
        launched.details["url"] = target
        return launched

    handler = _default_handler(target)
    details: dict = {"url": target, "handler": handler}
    before, before_pids, before_titles = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    try:
        os.startfile(target)  # noqa: S606 - opening a user-provided target is the point
        details["via"] = "startfile"
        opened = f"opened {target} with the default handler"
    except Exception as exc:
        result = run_powershell(f"Start-Process {_quote(target)}", timeout_s=10.0)
        details.update(
            via="Start-Process", startfile_error=str(exc), stderr=result.details.get("stderr", "")
        )
        if not result.ok:
            return finish(False, f"could not open {target}: {result.summary}", details)
        opened = f"opened {target} via Start-Process"
    if handler:
        opened += f" ({handler})"

    window_deadline = time.monotonic() + _HANDLER_WINDOW_S
    hwnd, reason, appeared = _wait_for_new_window(
        before,
        before_pids,
        before_foreground,
        window_deadline,
        before_titles=before_titles,
        target_processes=frozenset({handler}) if handler else frozenset(),
    )
    if hwnd is None and handler:
        front = _user32.GetForegroundWindow()
        info = window_info(front) if front else None
        if info is not None and (info.process_name or "").lower() == handler:
            hwnd, reason = front, f"the foreground window is {info.process_name}'s"
    details["hwnd"] = hwnd
    details["window_reason"] = reason
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
            f"{opened}; no window of it appeared, came to the front or changed within "
            f"{_HANDLER_WINDOW_S:g}s",
            details,
        )
    info = window_info(hwnd)
    details["title"] = info.title if info else ""
    details["process_name"] = info.process_name if info else ""
    _note_fresh(hwnd)
    forward = _bring_forward(hwnd, window_deadline, details)
    content_note = _wait_for_opened_content(
        hwnd, content_timeout_s, details, require_document=True
    )
    if (fresh := window_info(hwnd)) is not None:
        details["title"] = fresh.title
    return finish(
        True,
        f"{opened}: hwnd {hwnd} ({details['process_name']}) \"{details['title']}\" - "
        f"{reason}; {forward}; {content_note}",
        details,
    )
