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
  is looked up in its package manifest (``AppxManifest.xml`` in the package's
  install folder) and started, in this order, through
  1. its **App Execution Alias** (``uap3``/``uap5:ExecutionAlias``, the
     ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` link verified to point at this
     very app), with ``Start-Process``: exactly what typing the alias in a
     terminal does.  It creates a process *with* package identity, and that
     process hands its command line to an instance that is already running the
     way the app itself does it;
  2. ``IApplicationActivationManager::ActivateForProtocol``, when every argument
     is a URI whose scheme the manifest declares under ``windows.protocol``:
     protocol activation is what a running instance is built to receive;
  3. ``IApplicationActivationManager::ActivateApplication`` otherwise.  It keeps
     package identity too, but when the app is already running Windows may
     only activate that instance and drop the arguments (on 2026-09-23 a
     running Arc was "started" with a URL and showed nothing).
  Running the executable inside its package folder directly would start it
  *without* package identity, and so with a different data folder (a fresh
  browser profile), which is why none of these does that;
* a desktop app is started with ``Start-Process -FilePath <exe> -ArgumentList
  ...``, the executable being the target of its Start menu shortcut (whose own
  arguments and working folder are kept), else the file its AppID names, else
  the image of a running process whose window carries that AppID.

**Which window is the app's.**  Only a window that belongs to the app that was
asked for is ever reported (:class:`_AppIdentity`): same AppUserModelID (the
window's own ``PKEY_AppUserModel_ID``, or the packaged process's), same package
family, same program file (or one installed under its folder), or a process the
launch itself started.  On 2026-09-23 "launch WhatsApp" reported Arc's window
because the foreground happened to change to it.  An app that is already
running usually hands the arguments to the window it has, so a title change or
foreground change of *its* window counts; if nothing of it changes, its own
window is looked up - a window hidden in the tray is shown - and when it has
none the summary says so.  An app that was already running reacts to a
hand-off within :data:`_RUNNING_REACT_S` if it reacts at all, so that is how
long the call waits for it (counted from when the hand-off finished), and a
result where nothing changed says the app may have ignored the arguments.

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
from urllib.parse import urlsplit
from xml.etree import ElementTree

import psutil
import win32con
import win32gui
import win32process

from yuki.actions import ActionResult
from yuki.actions.input import wait_for_input_ready
from yuki.actions.shell import run_powershell
from yuki.perception.windows import is_user_window, list_windows, owner_of, window_info

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
_IID_ISHELLITEM = "{43826D1E-E718-42EE-BC55-A1E261C37BFE}"
_IID_ISHELLITEMARRAY = "{B63EA76D-1F85-456F-A19C-48159EFA858B}"

#: ``PKEY_AppUserModel_PackageInstallPath`` (propkey.h): the install folder of a
#: packaged Start menu entry, read from its ``shell:AppsFolder`` item.
_PKEY_PACKAGE_INSTALL_PATH = ("{9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3}", 15)

#: ``IO_REPARSE_TAG_APPEXECLINK``: the reparse point an App Execution Alias is.
_IO_REPARSE_TAG_APPEXECLINK = 0x8000001B
_FSCTL_GET_REPARSE_POINT = 0x000900A8

#: How long an app that was *already running* is given to react to a hand-off
#: (arguments, or just "come forward"), counted from when the hand-off finished
#: - the activation call returned, or the process that carried the command line
#: to the running instance exited.  A running instance that is going to open
#: something does so well inside this; the wait ends the moment it does.
_RUNNING_REACT_S = 2.0

#: Bound on the wait for a hidden (tray) window to become visible after it has
#: been shown.
_SHOW_S = 1.0

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
# Whose window is it
# ---------------------------------------------------------------------------
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_APPMODEL_BUFFER = 512

_pkg_api = ctypes.WinDLL("kernel32")  # private: its argtypes leak nowhere
_pkg_api.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
_pkg_api.OpenProcess.restype = ctypes.c_void_p
_pkg_api.CloseHandle.argtypes = [ctypes.c_void_p]
_pkg_api.CloseHandle.restype = ctypes.c_int
for _fn in ("GetApplicationUserModelId", "GetPackageFamilyName"):
    getattr(_pkg_api, _fn).argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_wchar_p,
    ]
    getattr(_pkg_api, _fn).restype = ctypes.c_long


def _process_package_ids(pid: int) -> tuple[str, str]:
    """``(AppUserModelID, package family name)`` of a packaged process.

    Both ``""`` for a process without package identity or one that cannot be
    opened.  Read from the process itself (``GetApplicationUserModelId``,
    ``GetPackageFamilyName``): the identity Windows gave it, whatever its image.
    """
    handle = _pkg_api.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, pid)
    if not handle:
        return "", ""
    try:
        values = []
        for fn in (_pkg_api.GetApplicationUserModelId, _pkg_api.GetPackageFamilyName):
            length = ctypes.c_uint32(_APPMODEL_BUFFER)
            buffer = ctypes.create_unicode_buffer(_APPMODEL_BUFFER)
            values.append(buffer.value if fn(handle, ctypes.byref(length), buffer) == 0 else "")
        return values[0], values[1]
    finally:
        _pkg_api.CloseHandle(handle)


def _is_within(path: str, folder: str) -> bool:
    """Whether normcased ``path`` lies inside normcased ``folder``."""
    folder = folder.rstrip("\\/")
    return bool(folder) and (path == folder or path.startswith(folder + os.sep))


def _window_pid(hwnd: int) -> int:
    try:
        return int(win32process.GetWindowThreadProcessId(hwnd)[1])
    except Exception:
        return 0


class _AppIdentity:
    """What makes a process or a window "the app that was asked for".

    Facts, in the order they are checked:

    * the window's own AppUserModelID (``PKEY_AppUserModel_ID``) - the id the
      taskbar groups it under - equal to the Start menu entry's AppID.  Most
      windows declare none (a packaged app's identity is its process's), so a
      window that does not match here is judged by its process;
    * for a packaged app, the process's AppUserModelID, else its package family;
    * the process's program file: one of ``exe_paths``, or a program installed
      under the folder of one of them (a launcher stub that starts the real
      program from a versioned subfolder).  A folder inside the Windows
      directory is never used that way - it holds everybody's programs;
    * the process's image name, when that is all that is known (``open_url``'s
      default handler);
    * the process, or one of its ancestors, is one the launch itself started.

    ``confirmable`` is False when none of these is known (a Start menu entry
    that is a URL, a shell folder, an unresolvable shortcut): then nothing can
    be recognised as the app's, and the caller says so.
    """

    def __init__(
        self,
        name: str,
        *,
        aumid: str = "",
        exe_paths: tuple[str, ...] | list[str] = (),
        image_names: tuple[str, ...] | list[str] | frozenset[str] = (),
    ) -> None:
        self.name = name
        self.aumid = (aumid or "").lower()
        self.packaged = _is_packaged(self.aumid)
        self.family = self.aumid.split("!", 1)[0] if self.packaged else ""
        self.exe_paths = {
            os.path.normcase(os.path.abspath(path)) for path in exe_paths if path
        }
        windows_dir = os.path.normcase(os.environ.get("SystemRoot") or r"C:\Windows")
        self.exe_dirs = {
            os.path.dirname(path)
            for path in self.exe_paths
            if not _is_within(path, windows_dir)
        }
        self.image_names = {image.lower() for image in image_names if image}
        self.launched_pids: set[int] = set()
        self._pid_cache: dict[int, str] = {}

    @property
    def confirmable(self) -> bool:
        return bool(self.packaged or self.exe_paths or self.image_names or self.launched_pids)

    def describe(self) -> dict:
        return {
            "aumid": self.aumid,
            "package_family": self.family,
            "exe_paths": sorted(self.exe_paths),
            "image_names": sorted(self.image_names),
            "launched_pids": sorted(self.launched_pids),
            "confirmable": self.confirmable,
        }

    def add_launched(self, pid: int | None) -> None:
        """Count ``pid`` (and what it starts) as the app's from now on."""
        if pid:
            self.launched_pids.add(int(pid))
            # A "no" cached before may now be a child of the launched process.
            self._pid_cache = {k: v for k, v in self._pid_cache.items() if v}

    def owns_pid(self, pid: int) -> str:
        """How ``pid`` is known to be the app's (``""``: it is not)."""
        if not pid:
            return ""
        if pid in self._pid_cache:
            return self._pid_cache[pid]
        how = self._check_pid(pid)
        self._pid_cache[pid] = how
        return how

    def _check_pid(self, pid: int) -> str:
        if self.packaged:
            process_aumid, family = _process_package_ids(pid)
            if process_aumid:
                if process_aumid.lower() == self.aumid:
                    return "same AppUserModelID"
            elif family and family.lower() == self.family:
                return "same package"
        try:
            process = psutil.Process(pid)
        except Exception:
            return ""
        if self.exe_paths or self.image_names:
            try:
                exe = os.path.normcase(process.exe() or "")
            except Exception:
                exe = ""
            if exe and exe in self.exe_paths:
                return "same program file"
            if exe and any(_is_within(exe, folder) for folder in self.exe_dirs):
                return "program installed under the app's folder"
            if self.image_names:
                try:
                    image = (process.name() or "").lower()
                except Exception:
                    image = os.path.basename(exe)
                if image in self.image_names:
                    return "same program name"
        if self.launched_pids:
            current: psutil.Process | None = process
            for _ in range(8):  # a launcher, its child, a grandchild - never a loop
                if current is None:
                    break
                if current.pid in self.launched_pids:
                    return "started by this launch"
                try:
                    current = current.parent()
                except Exception:
                    break
        return ""

    def owns_window(self, hwnd: int) -> str:
        """How ``hwnd`` is known to be the app's (``""``: it is not, or unknown)."""
        if self.aumid and _window_appid(hwnd).lower() == self.aumid:
            return "window AppUserModelID"
        return self.owns_pid(_window_pid(hwnd))

    def running_pids(self) -> set[int]:
        """Processes of the app that exist right now."""
        if not self.confirmable:
            return set()
        return {pid for pid in psutil.pids() if self.owns_pid(pid)}


def _top_level_windows(pids: set[int]) -> list[int]:
    """Every top-level window of ``pids`` (visible or not), in Z-order."""
    if not pids:
        return []
    found: list[int] = []

    def collect(hwnd: int, _: object) -> bool:
        if _window_pid(hwnd) in pids:
            found.append(hwnd)
        return True

    try:
        win32gui.EnumWindows(collect, None)
    except Exception:
        pass
    return found


#: ``WS_EX_NOACTIVATE``: a window the user can never switch to.
_WS_EX_NOACTIVATE = 0x08000000


def _could_be_main_window(hwnd: int) -> bool:
    """Whether a window that is not on screen is one a user could switch to.

    Window-style facts only: top level and unowned, not a tool or no-activate
    window, titled, with a caption *and* a system menu (the frame a user can
    move, switch to and close).  Helper windows an app keeps hidden (tray-icon
    hosts, message sinks, IME windows) lack that frame or a title.
    """
    try:
        style = win32gui.GetWindowLong(hwnd, win32con.GWL_STYLE)
        ex_style = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
        title = win32gui.GetWindowText(hwnd)
    except Exception:
        return False
    if style & win32con.WS_CHILD or owner_of(hwnd):
        return False
    if ex_style & (win32con.WS_EX_TOOLWINDOW | _WS_EX_NOACTIVATE):
        return False
    if not title.strip():
        return False
    return (style & win32con.WS_CAPTION) == win32con.WS_CAPTION and bool(
        style & win32con.WS_SYSMENU
    )


def _find_app_window(identity: _AppIdentity, pids: set[int]) -> tuple[int | None, str]:
    """The app's own window, for when launching it changed nothing.

    Returns:
        ``(hwnd, state)`` with state ``"open"`` (on screen), ``"minimized"`` or
        ``"hidden"`` (exists but not shown - an app sitting in the tray), or
        ``(None, "")`` when the app has no window a user could switch to.
        Visible ones win, topmost first; among hidden ones the largest.
    """
    windows = _top_level_windows(pids)
    shown = [h for h in windows if is_user_window(h) and identity.owns_window(h)]
    if shown:
        restored = [h for h in shown if not win32gui.IsIconic(h)]
        return (restored or shown)[0], ("open" if restored else "minimized")
    hidden = [
        h
        for h in windows
        if not is_user_window(h) and _could_be_main_window(h) and identity.owns_window(h)
    ]
    if not hidden:
        return None, ""

    def area(hwnd: int) -> int:
        try:
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        except Exception:
            return 0
        return max(right - left, 0) * max(bottom - top, 0)

    hidden.sort(key=area, reverse=True)
    return hidden[0], "hidden"


def _show_window(hwnd: int, timeout_s: float = _SHOW_S) -> bool:
    """Show a hidden window and wait (bounded) until it is on screen."""
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_user_window(hwnd):
            return True
        time.sleep(_POLL_S)
    return is_user_window(hwnd)


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


class _ReactClock:
    """The shorter deadline for an app that was already running.

    Counts :data:`_RUNNING_REACT_S` from ``handed_off`` (when the activation
    call returned), and slides along while ``carrier`` - the process that
    carries the command line over to the running instance - is still alive, so
    the clock runs from when the hand-off actually finished.  Never later than
    ``hard_deadline``.
    """

    def __init__(
        self, handed_off: float, hard_deadline: float, carrier: psutil.Process | None = None
    ) -> None:
        self.handed_off = handed_off
        self.hard_deadline = hard_deadline
        self.carrier = carrier

    def deadline(self) -> float:
        if self.carrier is not None:
            try:
                alive = self.carrier.is_running() and (
                    self.carrier.status() != psutil.STATUS_ZOMBIE
                )
            except Exception:
                alive = False
            if alive:
                return min(time.monotonic() + _RUNNING_REACT_S, self.hard_deadline)
            self.carrier = None
            self.handed_off = max(self.handed_off, time.monotonic())
        return min(self.handed_off + _RUNNING_REACT_S, self.hard_deadline)


def _wait_for_new_window(
    identity: _AppIdentity,
    before: dict[int, int],
    before_pids: set[int],
    before_foreground: int,
    deadline: float,
    *,
    before_titles: dict[int, tuple[str, str]] | None = None,
    hidden_before: set[int] | frozenset[int] = frozenset(),
    react: _ReactClock | None = None,
) -> tuple[int | None, str, list[int]]:
    """Wait for the launched app to show itself, and return as soon as it has.

    Only windows that belong to the app (``identity``) count.  Accepted, in
    order of confidence: a new window of the app from a process that did not
    exist before; a new window of an already running process of the app (a
    second window, or one that was hidden and is now shown); an already open
    window of the app that changed its title (an app handed a URL or a file
    while running typically opens it in the window it has); the foreground
    changing to an already open window of the app.  A window of any other
    process is never reported, however it got the foreground.

    When the identity is not ``confirmable`` (nothing is known to recognise the
    app's windows by) a newly appeared window is accepted with that said in the
    reason, and a foreground change to an old window is not.

    Every window that appeared is returned alongside the chosen one.  When more
    than one did, the caller hands the whole list to the model rather than
    waiting on a timer to see whether a "better" window turns up -- guessing on
    a stopwatch is exactly what the contract forbids.

    ``react`` (an app that was already running) ends the wait earlier: see
    :class:`_ReactClock`.

    Returns:
        ``(hwnd | None, reason, appeared)``.
    """

    ownership: dict[int, str] = {}

    def owns(hwnd: int) -> str:
        if hwnd not in ownership:
            ownership[hwnd] = identity.owns_window(hwnd)
        return ownership[hwnd]

    def shown_reason(hwnd: int, default: str) -> str:
        if hwnd in hidden_before:
            return "its window, hidden before (running in the background), was shown"
        return default

    while time.monotonic() < (react.deadline() if react else deadline):
        current, _, titles = _window_snapshot()
        fresh = [hwnd for hwnd in current if hwnd not in before]
        owned = [hwnd for hwnd in fresh if owns(hwnd)]
        from_new_process = [hwnd for hwnd in owned if current[hwnd] not in before_pids]
        for hwnd in reversed(from_new_process):
            if is_user_window(hwnd):  # re-check: splash windows come and go
                return hwnd, shown_reason(hwnd, "new window from a new process"), fresh
        if owned:
            return (
                owned[-1],
                shown_reason(owned[-1], "new window from an already running process"),
                fresh,
            )
        if fresh and not identity.confirmable:
            return (
                fresh[-1],
                (
                    f"new window (not confirmed as {identity.name!r}'s: nothing is known "
                    f"to recognise its windows by)"
                ),
                fresh,
            )
        if before_titles:
            for hwnd, (title, process) in titles.items():
                old = before_titles.get(hwnd)
                if old is not None and title != old[0] and owns(hwnd):
                    return hwnd, f"its already open {process} window changed its title", fresh
        foreground = _user32.GetForegroundWindow()
        if (
            foreground
            and foreground != before_foreground
            and is_user_window(foreground)
            and owns(foreground)
        ):
            return foreground, "foreground changed to its already open window", fresh
        time.sleep(_POLL_S)
    return None, "no new, changed or newly focused window of it appeared", []


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


#: ``PKEY_Link_TargetParsingPath``: what a desktop Start menu entry points at.
_PKEY_LINK_TARGET_PARSING_PATH = ("{B9B4B3FC-2B51-4A42-B5D8-324146AFCF25}", 2)


def _run_in_com_thread(fn, timeout_s: float = 3.0):
    """Run ``fn()`` on a short-lived thread with its own COM apartment.

    Shell items need COM; initialising it on the caller's thread would leave
    that thread in an apartment it did not choose.

    Returns:
        ``(value, error)``.
    """
    result: dict = {}

    def worker() -> None:
        import pythoncom

        try:
            pythoncom.CoInitializeEx(pythoncom.COINIT_APARTMENTTHREADED)
        except Exception:
            pass
        try:
            result["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            result["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    thread = threading.Thread(target=worker, name="yuki-shell-item", daemon=True)
    thread.start()
    thread.join(timeout_s)
    if "value" in result:
        return result["value"], ""
    return None, result.get("error") or f"no answer within {timeout_s:g}s"


def _apps_folder_strings(appid: str, keys: dict[str, tuple[str, int]]) -> dict[str, str]:
    """String properties of the ``shell:AppsFolder\\<appid>`` item (``""`` if absent)."""

    def read() -> dict[str, str]:
        import pywintypes
        from win32com.shell import shell

        item = shell.SHCreateItemFromParsingName(
            "shell:AppsFolder\\" + appid, None, shell.IID_IShellItem2
        )
        values: dict[str, str] = {}
        for name, (fmtid, pid) in keys.items():
            try:
                values[name] = str(item.GetString((pywintypes.IID(fmtid), pid)) or "")
            except Exception:
                values[name] = ""
        return values

    values, _ = _run_in_com_thread(read)
    return values or {name: "" for name in keys}


def _is_program_file(path: str) -> bool:
    """An existing ``.exe`` - a file a process can have as its image."""
    return bool(path) and path.lower().endswith(".exe") and os.path.isfile(path)


def _identity_for(chosen: dict[str, str], plan: dict | None) -> _AppIdentity:
    """The :class:`_AppIdentity` of a Start menu entry (and how it is started)."""
    appid = chosen["appid"]
    if _is_packaged(appid):
        return _AppIdentity(chosen["name"], aumid=appid)
    exe_paths: list[str] = []
    if plan is not None and _is_program_file(plan.get("exe") or ""):
        exe_paths.append(plan["exe"])
    path = _appid_path(appid)
    if path and _is_program_file(path):
        exe_paths.append(path)
    target = _apps_folder_strings(appid, {"target": _PKEY_LINK_TARGET_PARSING_PATH})["target"]
    if _is_program_file(target):
        exe_paths.append(target)
    return _AppIdentity(chosen["name"], aumid=appid, exe_paths=exe_paths)


def _package_install_dir(aumid: str) -> tuple[str, str]:
    """``(install folder, error)`` of the package a packaged AppID belongs to.

    Read from the Start menu item itself (``PKEY_AppUserModel_PackageInstallPath``),
    else from ``Get-AppxPackage``.
    """
    install_dir = _apps_folder_strings(aumid, {"dir": _PKEY_PACKAGE_INSTALL_PATH})["dir"]
    if install_dir and os.path.isdir(install_dir):
        return install_dir, ""
    family = aumid.split("!", 1)[0]
    # A package family name is "<Name>_<PublisherId>"; package names hold no "_".
    try:
        found = _powershell_json(
            f"Get-AppxPackage -Name {_quote(family.rsplit('_', 1)[0])} "
            f"| Where-Object {{ $_.PackageFamilyName -eq {_quote(family)} }} "
            "| Select-Object -First 1 InstallLocation | ConvertTo-Json -Compress "
            "| Set-Content -LiteralPath {path} -Encoding UTF8",
            what="Get-AppxPackage",
        )
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        return "", f"package lookup failed: {exc}"
    for item in found:
        location = str(item.get("InstallLocation") or "") if isinstance(item, dict) else ""
        if location and os.path.isdir(location):
            return location, ""
    return "", f"no installed package with family {family!r} was found"


def _package_app_facts(aumid: str) -> dict:
    """What a packaged app's own manifest declares for it.

    Reads ``AppxManifest.xml`` in the package's install folder and, under the
    ``<Application Id=...>`` the AppID names, collects the App Execution Aliases
    (``*:ExecutionAlias Alias=``) and the URI schemes of its
    ``windows.protocol`` extensions.  Namespaces vary with the manifest schema
    version (uap3, uap5, ...), so elements are matched by local name.

    Returns:
        ``{"install_dir", "manifest", "aliases", "protocols", "error"}``.
    """
    facts: dict = {"install_dir": "", "manifest": "", "aliases": [], "protocols": [], "error": ""}
    app_id = aumid.split("!", 1)[1] if "!" in aumid else ""
    install_dir, error = _package_install_dir(aumid)
    if not install_dir:
        facts["error"] = error
        return facts
    manifest = os.path.join(install_dir, "AppxManifest.xml")
    facts.update(install_dir=install_dir, manifest=manifest)
    try:
        root = ElementTree.parse(manifest).getroot()
    except Exception as exc:  # noqa: BLE001 - reported to the caller
        facts["error"] = f"could not read {manifest}: {exc}"
        return facts

    def local(tag: object) -> str:
        return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""

    application = next(
        (
            element
            for element in root.iter()
            if local(element.tag) == "Application"
            and (element.get("Id") or "").lower() == app_id.lower()
        ),
        None,
    )
    if application is None:
        facts["error"] = f"{manifest} declares no Application Id={app_id!r}"
        return facts
    aliases: list[str] = []
    protocols: list[str] = []
    for element in application.iter():
        name = local(element.tag)
        if name == "ExecutionAlias" and element.get("Alias"):
            aliases.append(element.get("Alias"))
        elif name == "Extension" and element.get("Category") == "windows.protocol":
            for inner in element.iter():
                if local(inner.tag) == "Protocol" and inner.get("Name"):
                    protocols.append(inner.get("Name").lower())
    facts["aliases"] = list(dict.fromkeys(aliases))
    facts["protocols"] = list(dict.fromkeys(protocols))
    return facts


_pkg_api.CreateFileW.argtypes = [
    ctypes.c_wchar_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_uint32,
    ctypes.c_void_p,
]
_pkg_api.CreateFileW.restype = ctypes.c_void_p
_pkg_api.DeviceIoControl.argtypes = [
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.c_void_p,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
    ctypes.c_void_p,
]
_pkg_api.DeviceIoControl.restype = ctypes.c_int
_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _read_app_exec_link(path: str) -> dict | None:
    """Read an App Execution Alias: ``{"path", "family", "aumid", "target"}``.

    ``None`` when ``path`` is not an alias at all.  The reparse data of an
    ``IO_REPARSE_TAG_APPEXECLINK`` (version 3) holds NUL-separated UTF-16
    strings: package family name, AppUserModelID, target executable.  Fields
    that cannot be read stay ``""``.  Read-only: the link is opened for its
    attributes, never followed.
    """
    try:
        if getattr(os.lstat(path), "st_reparse_tag", 0) != _IO_REPARSE_TAG_APPEXECLINK:
            return None
    except OSError:
        return None
    link = {"path": path, "family": "", "aumid": "", "target": ""}
    handle = _pkg_api.CreateFileW(
        path,
        0x80,  # FILE_READ_ATTRIBUTES
        0x7,  # FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE
        None,
        3,  # OPEN_EXISTING
        0x00200000 | 0x02000000,  # FILE_FLAG_OPEN_REPARSE_POINT | FILE_FLAG_BACKUP_SEMANTICS
        None,
    )
    if not handle or handle == _INVALID_HANDLE:
        return link
    try:
        buffer = ctypes.create_string_buffer(16 * 1024)
        returned = ctypes.c_uint32(0)
        if not _pkg_api.DeviceIoControl(
            handle, _FSCTL_GET_REPARSE_POINT, None, 0, buffer, len(buffer),
            ctypes.byref(returned), None,
        ):
            return link
    finally:
        _pkg_api.CloseHandle(handle)
    raw = buffer.raw[: returned.value]
    length = int.from_bytes(raw[4:6], "little")
    data = raw[8 : 8 + length]
    if int.from_bytes(data[:4], "little") != 3:
        return link
    parts = data[4:].decode("utf-16-le", errors="replace").split("\0")
    link.update(
        family=parts[0] if len(parts) > 0 else "",
        aumid=parts[1] if len(parts) > 1 else "",
        target=parts[2] if len(parts) > 2 else "",
    )
    return link


def _find_alias(aumid: str, aliases: list[str]) -> tuple[dict | None, list[str]]:
    """The App Execution Alias link of ``aumid``, verified, and notes on the search.

    Looks in the package's own alias folder
    (``%LOCALAPPDATA%\\Microsoft\\WindowsApps\\<PackageFamilyName>``) first, then
    in the folder on ``PATH``.  A link counts only when its reparse data names
    this very AppUserModelID (the ``PATH`` one may belong to another package
    that declared the same alias); in the package's own folder an unreadable
    one is accepted, since the folder is the package's.
    """
    notes: list[str] = []
    base = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.expanduser(r"~\AppData\Local"),
        "Microsoft",
        "WindowsApps",
    )
    family = aumid.split("!", 1)[0]
    for alias in aliases:
        own = os.path.join(base, family, alias)
        for path in (own, os.path.join(base, alias)):
            link = _read_app_exec_link(path)
            if link is None:
                continue
            if link["aumid"]:
                if link["aumid"].lower() == aumid.lower():
                    return {**link, "alias": alias}, notes
                notes.append(f"alias link {path} belongs to {link['aumid']}, not to {aumid}")
                continue
            if path == own:
                return {**link, "alias": alias}, notes
            notes.append(f"alias link {path} could not be read to confirm whose it is")
        notes.append(f"the manifest declares alias {alias!r}, but no link for it points at this app")
    return None, notes


def _uri_schemes(args: list[str]) -> list[str] | None:
    """Lowercased schemes of ``args`` when every one is a URI, else ``None``.

    A single letter before ``:`` is a drive, not a scheme.
    """
    schemes: list[str] = []
    for arg in args:
        try:
            scheme = urlsplit(arg).scheme.lower()
        except ValueError:
            return None
        if len(scheme) <= 1:
            return None
        schemes.append(scheme)
    return schemes or None


def _plan_start_with_args(chosen: dict[str, str], args: list[str]) -> tuple[dict | None, list[str]]:
    """How to start ``chosen`` with ``args``, or why it cannot be.

    For a packaged app, in order (see the module docstring): its App Execution
    Alias, protocol activation when every argument is a URI of a scheme it
    declares, ``ActivateApplication``.  For a desktop app, ``Start-Process`` on
    its program file.

    Returns:
        ``(plan, notes)``.  ``plan["via"]`` is ``"AppExecutionAlias"`` or
        ``"Start-Process"`` (both with ``"exe", "arguments", "workdir",
        "source", "command"``), ``"ActivateForProtocol"`` (with ``"aumid",
        "uris", "arguments"``) or ``"ActivateApplication"`` (with ``"aumid",
        "arguments"``); packaged plans also carry ``"manifest"``,
        ``"manifest_aliases"`` and ``"manifest_protocols"``.  ``None`` when no
        executable could be found, in which case ``notes`` says what was tried.
    """
    arguments = _command_line(args)
    appid = chosen["appid"]
    notes: list[str] = []
    if _is_packaged(appid):
        facts = _package_app_facts(appid)
        base = {
            "aumid": appid,
            "arguments": arguments,
            "manifest": facts["manifest"],
            "manifest_aliases": facts["aliases"],
            "manifest_protocols": facts["protocols"],
        }
        if facts["error"]:
            notes.append(facts["error"])
        link, alias_notes = _find_alias(appid, facts["aliases"])
        notes.extend(alias_notes)
        if link is not None:
            return {
                **base,
                "via": "AppExecutionAlias",
                "exe": link["path"],
                "workdir": "",
                "source": (
                    f"App Execution Alias {link['alias']!r} declared in {facts['manifest']} "
                    f"(-> {link['target'] or 'target unreadable'})"
                ),
                "command": _start_process_command(link["path"], arguments),
            }, notes
        if not facts["error"] and not facts["aliases"]:
            notes.append("its package manifest declares no App Execution Alias")
        schemes = _uri_schemes(args)
        if schemes and all(scheme in facts["protocols"] for scheme in schemes):
            return {**base, "via": "ActivateForProtocol", "uris": list(args)}, notes
        if facts["protocols"]:
            notes.append(
                "not every argument is a URI of a scheme its manifest declares "
                f"({', '.join(facts['protocols'])})"
            )
        elif not facts["error"]:
            notes.append("its package manifest declares no URI scheme it handles")
        notes.append("using ActivateApplication, which an already running instance may ignore")
        return {**base, "via": "ActivateApplication"}, notes

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

    All three methods are declared, in vtable order, because the one used for
    protocols is the last.  The item array is passed as a plain ``IUnknown``
    pointer: nothing here calls into it.
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
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "ActivateForFile",
                    (["in"], wintypes.LPCWSTR, "appUserModelId"),
                    (["in"], ctypes.POINTER(comtypes.IUnknown), "itemArray"),
                    (["in"], wintypes.LPCWSTR, "verb"),
                    (["out"], ctypes.POINTER(wintypes.DWORD), "processId"),
                ),
                comtypes.COMMETHOD(
                    [],
                    comtypes.HRESULT,
                    "ActivateForProtocol",
                    (["in"], wintypes.LPCWSTR, "appUserModelId"),
                    (["in"], ctypes.POINTER(comtypes.IUnknown), "itemArray"),
                    (["out"], ctypes.POINTER(wintypes.DWORD), "processId"),
                ),
            ]

        _ACTIVATION_INTERFACE = IApplicationActivationManager
    return _ACTIVATION_INTERFACE


_ACTIVATION_INTERFACE = None

_shell_api = ctypes.WinDLL("shell32")  # private: its argtypes leak nowhere


def _uri_item_array(uri: str):  # -> ctypes.POINTER(comtypes.IUnknown)
    """A one-item ``IShellItemArray`` holding ``uri`` (COM must be initialised).

    ``SHCreateItemFromParsingName`` parses a URL into a shell item without
    touching the network or any handler; the array is what
    ``ActivateForProtocol`` takes.

    Raises:
        OSError: the shell could not make an item of it.
    """
    import comtypes

    unknown_out = ctypes.POINTER(ctypes.POINTER(comtypes.IUnknown))
    _shell_api.SHCreateItemFromParsingName.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_void_p,
        ctypes.POINTER(comtypes.GUID),
        unknown_out,
    ]
    _shell_api.SHCreateItemFromParsingName.restype = ctypes.c_long
    _shell_api.SHCreateShellItemArrayFromShellItem.argtypes = [
        ctypes.POINTER(comtypes.IUnknown),
        ctypes.POINTER(comtypes.GUID),
        unknown_out,
    ]
    _shell_api.SHCreateShellItemArrayFromShellItem.restype = ctypes.c_long
    item = ctypes.POINTER(comtypes.IUnknown)()
    hr = _shell_api.SHCreateItemFromParsingName(
        uri, None, ctypes.byref(comtypes.GUID(_IID_ISHELLITEM)), ctypes.byref(item)
    )
    if hr < 0 or not item:
        raise OSError(f"SHCreateItemFromParsingName({uri!r}) failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}")
    array = ctypes.POINTER(comtypes.IUnknown)()
    hr = _shell_api.SHCreateShellItemArrayFromShellItem(
        item, ctypes.byref(comtypes.GUID(_IID_ISHELLITEMARRAY)), ctypes.byref(array)
    )
    if hr < 0 or not array:
        raise OSError(
            f"SHCreateShellItemArrayFromShellItem({uri!r}) failed: HRESULT 0x{hr & 0xFFFFFFFF:08X}"
        )
    return array


def _activate_packaged(
    aumid: str, arguments: str, timeout_s: float, *, uris: list[str] | None = None
) -> tuple[int | None, str]:
    """Activate a packaged app, as the Start menu or the shell would.

    With ``uris``, each is handed over by protocol activation
    (``ActivateForProtocol``), one call per URI; otherwise the app is activated
    with ``arguments`` as its command line (``ActivateApplication``).  Runs on
    its own thread with its own COM apartment, bounded by ``timeout_s``.

    Returns:
        ``(pid, error)``: the activated process id (the first, with several
        URIs), or ``None`` and why not.
    """
    result: dict = {}

    def worker() -> None:
        import comtypes
        import comtypes.client

        try:
            comtypes.CoInitializeEx(comtypes.COINIT_APARTMENTTHREADED)
        except Exception:
            pass
        def activate() -> None:
            # Its own frame, so every COM reference is released on return,
            # before the apartment is torn down.
            manager = comtypes.client.CreateObject(
                _CLSID_APPLICATION_ACTIVATION_MANAGER,
                clsctx=comtypes.CLSCTX_LOCAL_SERVER,
                interface=_activation_manager_interface(),
            )
            if uris:
                for uri in uris:
                    array = _uri_item_array(uri)
                    result.setdefault("pid", int(manager.ActivateForProtocol(aumid, array)))
                    del array
            else:
                result["pid"] = int(
                    manager.ActivateApplication(aumid, arguments or None, _AO_NOERRORUI)
                )

        try:
            activate()
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
        return result["pid"], result.get("error", "")
    return None, result.get("error") or f"activation did not return within {timeout_s:g}s"


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
            :data:`_FOCUS_FLOOR_S`).  When the app was already running the
            wait ends :data:`_RUNNING_REACT_S` after the hand-off finished
            (see :class:`_ReactClock`), still within ``timeout_s``.
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
        ``new_windows`` lists every window that appeared (``is_app_window``
        says whether it is the app's), so a launch that produced more than one
        is reported rather than resolved by guesswork.  ``identity`` says what
        the app's windows were recognised by, ``already_running`` /
        ``running_pids`` whether it ran before the call, ``waited_ms`` how long
        the watch lasted; when no window of it changed and its own window was
        brought up instead, ``window_state_before`` is ``open``, ``minimized``
        or ``hidden`` (``shown_hidden_window`` then says whether showing it
        worked), and ``args_effect`` is ``"nothing visible"`` when a running
        instance showed no reaction to the arguments.
        With ``args``, ``details`` also carries ``args``, ``arguments`` (the
        command line handed over), ``via`` (``AppExecutionAlias``,
        ``ActivateForProtocol``, ``ActivateApplication`` or ``Start-Process``),
        ``exe``/``exe_source``/``start_command`` when a program file or alias
        was started, ``uris`` for protocol activation, ``manifest`` /
        ``manifest_aliases`` / ``manifest_protocols`` for a packaged app,
        ``resolution_notes``, and ``pid`` when the start reported one.
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
        if "command" in plan:
            details["exe"] = plan["exe"]
            details["exe_source"] = plan["source"]
            details["start_command"] = plan["command"]
        if "uris" in plan:
            details["uris"] = plan["uris"]
        if "manifest" in plan:
            details["manifest"] = plan["manifest"]
            details["manifest_aliases"] = plan["manifest_aliases"]
            details["manifest_protocols"] = plan["manifest_protocols"]

    # Who the app is, and whether it is running already: only its windows are
    # ever reported, and a running instance gets a short wait to react.
    identity = _identity_for(chosen, plan)
    running_before = identity.running_pids()
    already_running = bool(running_before)
    details["already_running"] = already_running
    details["running_pids"] = sorted(running_before)
    hidden_before = {
        hwnd for hwnd in _top_level_windows(running_before) if not is_user_window(hwnd)
    }

    before, before_pids, before_titles = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    launched_pid: int | None = None
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
    elif "command" in plan:
        # Start-Process on a desktop program file or on an App Execution Alias.
        result = run_powershell(plan["command"], timeout_s=start_budget)
        details["launched"] = result.ok
        details["launch_stderr"] = result.details.get("stderr", "")
        stdout = str(result.details.get("stdout") or "").strip()
        last_line = stdout.splitlines()[-1].strip() if stdout else ""
        details["pid"] = int(last_line) if last_line.isdigit() else None
        launched_pid = details["pid"]
        failure = result.details.get("stderr") or result.summary
    else:
        pid, error = _activate_packaged(
            plan["aumid"], plan["arguments"], start_budget, uris=plan.get("uris")
        )
        details["launched"] = pid is not None
        details["pid"] = pid
        launched_pid = pid
        if error:
            details["launch_error"] = error
        failure = (
            f"activating the packaged app {plan['aumid']!r} with arguments "
            f"({plan['via']}) failed: {error}"
        )
    if not details["launched"]:
        return finish(False, f"could not start {chosen['name']!r}: {failure}", details)

    handed_off = time.monotonic()
    window_deadline = handed_off + timeout_s
    carrier: psutil.Process | None = None
    if launched_pid and launched_pid not in running_before:
        identity.add_launched(launched_pid)
        if already_running:
            # A new process started while the app runs is most likely carrying
            # the command line over to the running instance: the short wait
            # counts from when it is done.
            try:
                carrier = psutil.Process(launched_pid)
            except Exception:
                carrier = None
    react = _ReactClock(handed_off, window_deadline, carrier) if already_running else None
    hwnd, reason, appeared = _wait_for_new_window(
        identity,
        before,
        before_pids,
        before_foreground,
        window_deadline,
        before_titles=before_titles,
        hidden_before=hidden_before,
        react=react,
    )
    waited_s = time.monotonic() - handed_off
    details["hwnd"] = hwnd
    details["window_reason"] = reason
    details["waited_ms"] = round(waited_s * 1000.0, 1)
    details["identity"] = identity.describe()
    # More than one window showed up: say so instead of silently picking.
    details["new_windows"] = [
        {
            "hwnd": other,
            "title": (info.title if (info := window_info(other)) else ""),
            "process_name": info.process_name if info else "",
            "is_app_window": bool(identity.owns_window(other)),
        }
        for other in appeared
    ]
    if hwnd is None:
        return finish(
            True,
            _no_window_outcome(
                chosen, identity, details, arg_list, already_running, waited_s, window_deadline
            ),
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
    same_app = [w for w in details["new_windows"] if w["is_app_window"]]
    extra = len(same_app) - 1
    unrelated = len(details["new_windows"]) - len(same_app)
    with_args = ""
    if arg_list:
        shown = details["arguments"]
        with_args = f" with arguments {shown if len(shown) <= 160 else shown[:160] + '…'}"
    return finish(
        True,
        f"launched {chosen['name']!r}{with_args}: hwnd {hwnd} "
        f"({details['process_name']}) \"{details['title']}\" - {reason}; {forward}"
        + (f"; {content_note}" if content_note else "")
        + (f"; {extra} other new window(s) of it appeared, see new_windows" if extra > 0 else "")
        + (
            f"; {unrelated} window(s) of other apps also appeared meanwhile (not it)"
            if unrelated > 0 and identity.confirmable
            else ""
        ),
        details,
    )


def _no_window_outcome(
    chosen: dict[str, str],
    identity: _AppIdentity,
    details: dict,
    arg_list: list[str],
    already_running: bool,
    waited_s: float,
    window_deadline: float,
) -> str:
    """The summary (and ``details``) for a launch after which no window of the
    app appeared, changed or came forward by itself.

    The app's own window is looked up: one on screen or minimized is brought to
    the front, a hidden one (an app sitting in the tray) is shown first.  Only
    ever a window of the app - when it has none, the summary says so.
    """
    name = repr(chosen["name"])
    via = details.get("via")
    if arg_list and already_running:
        details["args_effect"] = "nothing visible"
        lead = (
            f"{name} was already running and nothing visibly changed within "
            f"{waited_s:.1f}s of handing it the arguments ({via}); it may have ignored them"
        )
    elif already_running:
        lead = (
            f"{name} was already running and showed no window of its own within "
            f"{waited_s:.1f}s of being activated"
        )
    else:
        lead = (
            f"started {name}" + (" with arguments" if arg_list else "")
            + f" but no window of it appeared within {waited_s:.1f}s (it may still be "
            f"loading, or run without a window)"
        )
    if not identity.confirmable:
        return (
            f"{lead}; nothing is known to recognise an already open window of it by "
            f"(its Start menu entry is {chosen['appid']!r}), so none is reported"
        )
    pids = identity.running_pids()
    details["running_pids_after"] = sorted(pids)
    app_hwnd, state = _find_app_window(identity, pids)
    if app_hwnd is None:
        if pids:
            return (
                f"{lead}; it is running (pid {', '.join(str(p) for p in sorted(pids)[:6])}) "
                f"but has no window a user could switch to, shown or hidden"
            )
        return f"{lead}; no process of it is running now"
    if state == "hidden":
        shown = _show_window(app_hwnd)
        details["shown_hidden_window"] = shown
        how = (
            "its window was hidden (running in the background or the tray), so it was shown"
            if shown
            else "its window is hidden (running in the background or the tray) and did "
            "not come on screen when shown"
        )
    elif state == "minimized":
        how = "its window was minimized, so it was restored"
    else:
        how = "its already open window"
    details["hwnd"] = app_hwnd
    details["window_reason"] = how
    details["window_state_before"] = state
    info = window_info(app_hwnd)
    details["title"] = info.title if info else ""
    details["process_name"] = info.process_name if info else ""
    forward = _bring_forward(app_hwnd, window_deadline, details)
    return (
        f"{lead}; {how}: hwnd {app_hwnd} ({details['process_name']}) "
        f"\"{details['title']}\"; {forward}"
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
    # Only the handler's windows count; with no handler known, only a window
    # that newly appears does (said so in its reason), never an old one that
    # merely came to the front.
    identity = _AppIdentity(handler or "the default handler", image_names=[handler] if handler else [])
    hwnd, reason, appeared = _wait_for_new_window(
        identity,
        before,
        before_pids,
        before_foreground,
        window_deadline,
        before_titles=before_titles,
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
