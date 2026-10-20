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
  1. ``IApplicationActivationManager::ActivateForProtocol``, when every argument
     is a URI whose scheme the manifest declares under ``windows.protocol``:
     protocol activation is what a running instance is built to receive.  It
     comes before the alias (x8WebArc, 10 Oct): Arc declares both, and every
     web address its alias carried was ignored (it stayed on the page it had,
     or showed an old tab);
  2. its **App Execution Alias** (``uap3``/``uap5:ExecutionAlias``, the
     ``%LOCALAPPDATA%\\Microsoft\\WindowsApps`` link verified to point at this
     very app), with ``Start-Process``: exactly what typing the alias in a
     terminal does.  It creates a process *with* package identity, and that
     process hands its command line to an instance that is already running the
     way the app itself does it;
  3. ``IApplicationActivationManager::ActivateApplication`` otherwise.  It keeps
     package identity too, but when the app is already running Windows may
     only activate that instance and drop the arguments (on 2026-09-23 a
     running Arc was "started" with a URL and showed nothing).
  Running the executable inside its package folder directly would start it
  *without* package identity, and so with a different data folder (a fresh
  browser profile), which is why none of these does that.
  lSkills (live 17 Oct): a package may declare a scheme and still refuse the
  protocol contract - Arc, a full-trust desktop package, failed every open with
  ``0x80270254`` ("does not support the contract specified or is not
  installed"; a model read that aloud as "Arc isn't installed"). Then the same
  start goes on at once through the alias, else ``ActivateApplication``, the
  refusal is kept for the process (:data:`_NO_PROTOCOL`), and a failure is
  said as a fact (:func:`_activation_failure`), never in Windows' words;
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

**Handed a web address, success is the page, not the window.**  When an
argument is a web address (:func:`_page_address`), the launch - and
``open_url`` with the default handler - waits, within the content deadline,
until one of the app's windows shows a page at that address
(:func:`yuki.perception.tree.wait_for_page`: a page-only read of each window,
the Document element's Value being the page URL), with redirects that keep the
host and path prefix accepted (:func:`_same_page`).  The summary states the
fact: "Arc is now showing https://... (page "...")", or "handed Arc ..., but
after N s no page in Arc is at that address; the visible page is ...", which
is ``ok=False``.  A browser's window title is not evidence: on 2026-09-23 Arc
opened the URL every time while its title stayed "Arc", and the old "nothing
visibly changed" report cost the model rounds re-doing work that had worked.
The window-title and foreground signals remain for arguments that are not
pages.

**A page that was already there is not the page opened** (x8WebArc, 10 Oct).
When the app (or the default handler) was already running, the main page of
each of its windows is read right before the address is handed over
(:func:`_pages_before`), and a page a window already showed never counts
afterwards.  On 10 Oct at 20:55 Arc ignored the address while its window,
behind another app, still showed a YouTube watch page; that page was taken for
the one opened and the hand said "verified".  Now that is ``ok=False`` with
``details["reason"] = "page_unchanged"`` (also when the pages shown are of
another host), so a skill moves on to its next rung.  A window whose page
before the hand-off *was* the very page asked for, and still is, is another
matter (review r1 M1): the requested page is showing, so that is ``ok=True``
with ``details["page_state"] = "already"`` and the window brought forward
(a skill says "already", never a fresh "done").  ``deadline=`` (a
``time.monotonic()`` time) bounds the whole call - the start, the page, the
window and bringing it forward - for a caller that must answer in time (a
skill's rung; on 10 Oct one Arc launch took 26-34 s).  ``details["via_word"]``
says how the app was started as a code word a content-free log keeps.

**The browser's own address bar** (lSkills, live 17 Oct). ``open_url(url,
app=...)`` with a web address: when the browser didn't open the address handed
to it (``page_unchanged``, a start that failed, or a packaged browser's windows
showing no page at all) and it is a browser by machine data (its manifest
declares the scheme, or Windows lists it under ``StartMenuInternet``), the
address is typed into its own address bar in its own window
(:func:`_address_bar_open`): a new tab, the address typed only into a text
field of the browser's own frame (never a page's box, so never a send) and read
back, Enter, and done only when a window newly shows the page. A browser that
ignored the last hand-offs gets the address typed at once.

**An app running with no window** (closed to the background or the tray) is
brought back when launched without arguments: its own hidden main window is
shown, or, when it has none, a new window is asked for the way the Start menu
would start it (``ActivateApplication`` for a packaged app, its program run
again for a desktop app), and the summary says "Arc was running in the
background with no window; opened a new window".

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
from collections.abc import Callable, Sequence
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
from yuki.actions import gate as _gate
from yuki.memory import gamegate as _gamegate
from yuki.perception.windows import (
    cloak_state,
    could_be_main_window,
    hidden_main_window,
    is_user_window,
    list_windows,
    note_on_screen,
    process_package_ids,
    was_on_screen,
    window_info,
)

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


def start_apps_cached() -> list[dict[str, str]] | None:
    """names21: the Start menu list as this process last read it (:func:`get_start_apps`), or ``None`` when it was
    never read: never a new read (the names measure, :mod:`yuki.skills.names`, must not start PowerShell)."""
    with _start_apps_lock:
        return list(_start_apps_cache) if _start_apps_cache is not None else None


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


#: Moved to :mod:`yuki.perception.windows` (the overview needs it too).
_process_package_ids = process_package_ids


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
        via_handler: bool = False,
    ) -> None:
        self.name = name
        #: second review V3: ``exe_paths`` is the program Windows opens the entry's target with (a document's, a
        #: shell location's: Explorer, MMC, the browser), not the app's own program - it shows the entry's window,
        #: but its running processes are not "the app already running" (Explorer always runs)
        self.via_handler = bool(via_handler)
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
            **({"via_handler": True} if self.via_handler else {}),
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
        if not self.confirmable or self.via_handler:
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


#: Moved to :mod:`yuki.perception.windows` (the overview needs it too).
_could_be_main_window = could_be_main_window


def _find_app_window(identity: _AppIdentity, pids: set[int]) -> tuple[int | None, str]:
    """The app's own window, for when launching it changed nothing.

    Returns:
        ``(hwnd, state)`` with state ``"open"`` (on screen, or on another
        virtual desktop - cloaked by the shell, which activating it switches
        to), ``"minimized"`` or ``"hidden"`` (exists but not shown, or cloaked
        by the app itself - an app sitting in the tray or the background; see
        :func:`yuki.perception.windows.hidden_main_window`), or ``(None, "")``
        when the app has no window a user could switch to.  Visible ones win,
        topmost first; among hidden ones the largest.
    """
    windows = _top_level_windows(pids)
    shown = [h for h in windows if is_user_window(h) and identity.owns_window(h)]
    if shown:
        restored = [h for h in shown if not win32gui.IsIconic(h)]
        return (restored or shown)[0], ("open" if restored else "minimized")
    elsewhere = [
        h
        for h in windows
        if win32gui.IsWindowVisible(h)
        and cloak_state(h)
        and not hidden_main_window(h)
        and _could_be_main_window(h)
        and identity.owns_window(h)
    ]
    if elsewhere:
        return elsewhere[0], "open"
    # appOpen (5 Oct): only a window that was on screen before (an app closed to the tray) is brought back by
    # showing it; one never seen on screen (a tray-icon host that has a caption and a system menu, like the Riot
    # Client's "BackgroundModeTrayIconClass") is not the app's window to show: the entry is started instead
    hidden = [h for h in windows if hidden_main_window(h) and identity.owns_window(h) and was_on_screen(h)]
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
    """Show a hidden window and wait (bounded) until it is on screen. Never with a game in front, never a game's own
    window (D17: showing or restoring a window can put it over the game)."""
    if _gate.refuse("window_show", hwnd=hwnd) is not None:
        return False
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
    except Exception:
        return False
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_user_window(hwnd):
            note_on_screen([hwnd])
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
        # second review V3: a handler that only hands the target on (LaunchWinApp.exe for a PDF, to the browser) owns
        # no window: a new window is then accepted as before, unconfirmed
        if fresh and (not identity.confirmable or getattr(identity, "via_handler", False)):
            return (
                fresh[-1],
                (
                    f"new window (not confirmed as {identity.name!r}'s: it is not a window of the program "
                    f"its target opens with)"
                    if identity.confirmable
                    else f"new window (not confirmed as {identity.name!r}'s: nothing is known "
                    f"to recognise its windows by)"
                ),
                fresh,
            )
        # final check V3-explorer-title: the program that shows a shell entry is shared (every Explorer window is
        # explorer.exe's): an already open window of it counts only when its title names the entry
        shared = getattr(identity, "via_handler", False)

        def named(hwnd: int) -> bool:
            return not shared or _title_score((titles.get(hwnd) or ("", ""))[0], identity.name) > 0

        if before_titles:
            for hwnd, (title, process) in titles.items():
                old = before_titles.get(hwnd)
                if old is not None and title != old[0] and owns(hwnd) and named(hwnd):
                    return hwnd, f"its already open {process} window changed its title", fresh
        foreground = _user32.GetForegroundWindow()
        if (
            foreground
            and foreground != before_foreground
            and is_user_window(foreground)
            and owns(foreground)
            and named(foreground)
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
    if not exe_paths and plan is None:
        # second review V3: an entry that is no program (a shell location, a console snap-in, a document, a folder,
        # a web link) is shown by the program Windows opens its target with - a registry fact (the shell's own
        # association; a shell namespace location is Explorer's) - so its new window can be recognised by that
        handler = _target_handler(target or path or "")
        if handler:
            return _AppIdentity(chosen["name"], aumid=appid, exe_paths=[handler], via_handler=True)
    return _AppIdentity(chosen["name"], aumid=appid, exe_paths=exe_paths)


def _target_handler(target: str) -> str:
    """The program file Windows opens a Start entry's target with (``""``: none known): Explorer for a shell namespace
    location (``::{CLSID}``, ``shell:``), else the shell's association for the target (:func:`_handler_path`)."""
    target = (target or "").strip()
    if not target:
        return ""
    if target.startswith("::{") or target.lower().startswith("shell:"):
        handler = os.path.join(os.environ.get("SystemRoot") or r"C:\Windows", "explorer.exe")
    else:
        handler = _handler_path(target)
    return handler if _is_program_file(handler) else ""


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


def _plan_start_with_args(chosen: dict[str, str], args: list[str], *,
                          protocol: bool = True) -> tuple[dict | None, list[str]]:
    """How to start ``chosen`` with ``args``, or why it cannot be.

    For a packaged app, in order (see the module docstring): protocol activation
    when every argument is a URI of a scheme it declares, its App Execution
    Alias, ``ActivateApplication``.  For a desktop app, ``Start-Process`` on its
    program file.  ``protocol=False`` (lSkills, live 17 Oct): protocol activation
    is left out - the app refused that contract (:data:`_NO_PROTOCOL`), so the
    alias, else ``ActivateApplication``, carries the arguments instead.

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
        schemes = _uri_schemes(args)
        declared = bool(schemes) and all(scheme in facts["protocols"] for scheme in schemes)
        refused = appid.lower() in _NO_PROTOCOL
        if declared and (refused or not protocol):
            # lSkills (live 17 Oct): the manifest declares the scheme, but Windows refused protocol activation for it
            # (Arc, a full-trust desktop package: HRESULT 0x80270254, "does not support the contract"): the alias,
            # else ActivateApplication, carries the address instead; never "not installed"
            notes.append("protocol activation left out: the app refused that contract"
                         + (" earlier in this session" if refused else ""))
        elif declared:
            # x8WebArc (10 Oct): every argument is a URI of a scheme the package declares, so it is handed over
            # the way a running instance is built to receive it, before the alias: Arc ignored every web address
            # its alias carried (3, 5 and 10 Oct: it stayed on the page it had, or showed an old tab).
            if facts["aliases"]:
                notes.append("protocol activation chosen over its App Execution Alias: every argument is a URI "
                             "of a scheme its manifest declares")
            return {**base, "via": "ActivateForProtocol", "uris": list(args)}, notes
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
        if declared:
            pass   # the scheme is declared; protocol activation was refused (noted above)
        elif facts["protocols"]:
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

#: lSkills (live 17 Oct): the AppUserModelIDs (lowercased) whose protocol activation Windows refused in this process
#: (every Arc open failed with ``COMError -2144927148`` = ``0x80270254``, "This app does not support the contract
#: specified or is not installed": a full-trust desktop package declares ``windows.protocol`` but takes links through
#: its program's command line). A fact about the installed package, so the next start goes straight to its alias. RAM
#: only; a new process (an updated package) tries protocol activation again.
_NO_PROTOCOL: set[str] = set()
_no_protocol_lock = threading.Lock()
#: How long a package that refused protocol activation is watched for the page its alias carried (its react time plus a
#: second), before ``open_url``'s address bar (or the web skill's next rung) takes over: from the hand-off when it was
#: running, from its first window on screen when it started cold (review r1 M1: a cold Arc drops the address and shows
#: its new-tab view, percFix 5 Oct; the whole 14 s cold page wait and a second alias hand-off took 23 s).
_REFUSED_PAGE_S = _RUNNING_REACT_S + 1.0
#: How often the cold start's window is looked for (a window list, no page read) before that watch begins.
_WINDOW_UP_POLL_S = 0.1


def _hresult_of(error: str) -> int | None:
    """The HRESULT a ``COMError``/``OSError`` text carries (``"COMError: (-2144927148, ..."``), as an unsigned int, or
    ``None``. A machine format (comtypes' own error text), never words."""
    text = str(error or "")
    head = text.partition("(")[2].partition(",")[0].strip()
    try:
        value = int(head)
    except ValueError:
        return None
    return value & 0xFFFFFFFF


def _await_window_up(identity: "_AppIdentity", before: dict[int, int], until: float) -> float | None:
    """Review r1 M1: when (``time.monotonic()``) one of the app's own windows is first on screen after a cold start,
    looked for every :data:`_WINDOW_UP_POLL_S` by a window list alone (no page read), until ``until``; ``None`` when
    none came. Only for an app whose windows can be recognised (``identity.confirmable``); never raises."""
    windows = _app_windows(identity, before)
    while True:
        try:
            if windows():
                return time.monotonic()
        except Exception:  # noqa: BLE001 - not listed this time
            pass
        left = until - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(_WINDOW_UP_POLL_S, left))


def _activation_failure(name: str, via: str, error: str) -> str:
    """What a failed packaged activation says, as a fact the caller can repeat: never Windows' own message text, whose
    "or is not installed" a model read aloud as "Arc isn't installed" on 17 Oct (the app was installed: its Start menu
    entry was found, which is how it was resolved)."""
    code = _hresult_of(error)
    if code is not None:
        return (f"Windows would not hand {name} the arguments by {via} (HRESULT 0x{code:08X}); {name} is installed "
                f"(its Start menu entry was found)")
    if "did not return within" in str(error):
        return (f"activating {name} by {via} did not answer in time; {name} is installed "
                f"(its Start menu entry was found)")
    return (f"activating {name} by {via} failed ({str(error)[:120]}); {name} is installed "
            f"(its Start menu entry was found)")

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
    if (refused := _gate.refuse("launch")) is not None:   # D17: no app launch with a game in front
        return None, refused
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


# ---------------------------------------------------------------------------
# Verifying that a page opened: the address, not the window title
# ---------------------------------------------------------------------------
#: chat19: the URL schemes whose address a browser window's page shows as it was handed (the web's own; a fixed
#: machine format, :func:`_page_address`).
_PAGE_SCHEMES = frozenset({"http", "https", "ftp", "file"})


def _page_address(arg: str) -> str | None:
    """The web address an argument asks an app to open, or ``None``.

    Parsing the argument's shape: one token that is a URL with a host
    (``https://...``, ``ftp://...``), a ``file:`` URL, or a bare
    ``host.tld/path`` / ``www.host.tld`` / ``host:port`` - which a browser
    handed it opens as ``https://`` - and not a path to something on disk.  A
    bare ``name.ext`` is not taken for a page (it is as likely a file), nor is
    a URI of a scheme with no host (``mailto:``, ``spotify:track:...``).

    chat19 (final test 19 Oct, all-messaging RC2): only the web schemes
    (:data:`_PAGE_SCHEMES`) are pages. An app's own link of another scheme with
    an authority part (``discord://-/channels/@me/<id>``, ``slack://channel?..``)
    is no page: Discord's window then shows ``https://discord.com/...``, which
    can never equal the link, so the open waited out its whole deadline and,
    with the old page readable, said "not opened". Such a link is checked by
    its handler's window only.
    """
    text = (arg or "").strip()
    if not text or any(ch.isspace() for ch in text) or "\\" in text:
        return None
    try:
        parts = urlsplit(text)
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if len(scheme) > 1:
        if scheme in _PAGE_SCHEMES:
            return text if parts.netloc or scheme == "file" else None
        if parts.netloc or text[len(scheme) + 1:].startswith("//"):
            return None  # an app's own link (discord://, slack://): its window is the check, never a page
        # "localhost:3000/x" parses as scheme "localhost": a port, not a scheme.
        if not text[len(scheme) + 1 :].split("/", 1)[0].isdigit():
            return None
    if len(scheme) == 1 or os.path.exists(text):
        return None  # a drive letter, or a file or folder on disk
    host, slash, _ = text.partition("/")
    host = host.split("?", 1)[0].split("#", 1)[0]
    name, _, port = host.partition(":")
    if port and not port.isdigit():
        return None
    labels = name.split(".")
    # A bare "name.ext" is as likely a file name as a host, so the bare form
    # counts only with a path after the host or a "www." in front of it.
    dotted = len(labels) > 1 and all(labels)
    if (dotted or name == "localhost") and (slash or port or name.startswith("www.")):
        return "https://" + text
    return None


def _address_parts(url: str) -> tuple[str, str, int | None, str, list[tuple[str, str]]] | None:
    """``(scheme, host, port, path, query pairs)`` of an address, normalised.

    Scheme and host lowercased, ``http`` and ``https`` counted as one (a site
    upgrades one to the other), a leading ``www.`` and a default port dropped,
    the path unquoted, case-folded and without a trailing ``/``, the fragment
    ignored.  ``None`` when it cannot be parsed.
    """
    from urllib.parse import parse_qsl, unquote

    try:
        parts = urlsplit(url if "://" in url or url.lower().startswith("file:") else "https://" + url)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    if scheme in ("http", "https"):
        if port in (80, 443):
            port = None
        scheme = "http"
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = unquote(parts.path).casefold().rstrip("/")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)]
    return scheme, host, port, path, query


def _same_page(requested: str, actual: str) -> bool:
    """Whether the page at ``actual`` is the one ``requested`` asked for.

    After normalisation (:func:`_address_parts`): the same scheme and port,
    the same host or a subdomain of it (``youtube.com`` -> ``m.youtube.com``),
    a path that is the requested one or continues it
    (``/@mrbeast`` -> ``/@mrbeast/videos``), and every query parameter asked
    for still present with the same value (a redirect may add more).  So a
    redirect that keeps the host and the path prefix counts, and one to a
    login or consent page elsewhere does not.
    """
    want = _address_parts(requested)
    got = _address_parts(actual)
    if want is None or got is None:
        return False
    w_scheme, w_host, w_port, w_path, w_query = want
    g_scheme, g_host, g_port, g_path, g_query = got
    if w_scheme != g_scheme or w_port != g_port:
        return False
    if not (g_host == w_host or g_host.endswith("." + w_host)):
        return False
    if w_path and not (g_path == w_path or g_path.startswith(w_path + "/")):
        return False
    return all(pair in g_query for pair in w_query)


def _app_windows(identity: _AppIdentity, before: dict[int, int]):
    """A callable listing, topmost first, the on-screen windows to look for the
    page in: the app's own (restored) windows, or - when nothing is known to
    recognise them by - windows that appeared since ``before`` and the
    foreground window."""
    owned: dict[int, bool] = {}

    def windows() -> list[int]:
        found: list[int] = []
        for window in list_windows():
            if window.is_minimized:
                continue
            if identity.confirmable:
                if window.hwnd not in owned:
                    owned[window.hwnd] = bool(identity.owns_window(window.hwnd))
                if owned[window.hwnd]:
                    found.append(window.hwnd)
            elif window.hwnd not in before or window.is_foreground:
                found.append(window.hwnd)
        return found

    return windows


def _past_root(requested: str, actual: str) -> bool:
    """V11: ``actual`` matches ``requested`` only because a request for a site's root (no path) takes any page of that
    site: the site's home page was asked for and this is some other page of it. Address parts only."""
    want, got = _address_parts(requested), _address_parts(actual)
    return bool(want and got and not want[3] and got[3] and _same_page(requested, actual))


def _page_key(url: str):
    """An address as "the same page" is compared before and after a hand-off: its normalised parts
    (:func:`_address_parts`: scheme, host without ``www.``, port, path, the query pairs), the address itself when it
    cannot be parsed."""
    parts = _address_parts(url or "")
    if parts is None:
        return url or ""
    scheme, host, port, path, query = parts
    return (scheme, host, port, path, tuple(query))


#: x8WebArc: the read of the app's own pages right before an address is handed over (only when the app is already
#: running): at most this many of its on-screen windows, topmost first, each read for at most
#: :data:`_PAGE_BEFORE_WINDOW_S`, all of them within :data:`_PAGE_BEFORE_S`.
_PAGE_BEFORE_WINDOWS = 4
_PAGE_BEFORE_WINDOW_S = 0.35
_PAGE_BEFORE_S = 1.0
#: fSkills (live 18 Oct, youtube_channel_ndl: 5 s for nothing): when a window already showed the very page asked for
#: right before the hand-off, no change can come to wait for; the page is read once or twice in this short look (a
#: new window at the address still counts as opened) and then said as already up (:func:`_already_page`).
_ALREADY_PAGE_S = 0.8
#: With a deadline (``deadline=``), the address is handed a second time (percFix, a cold start that dropped it) only
#: when at least this much of it is left.
_AGAIN_MIN_S = 1.5
#: How an app was started with its arguments (``details["via"]``, a Windows name) as the code word a content-free
#: log keeps (``details["via_word"]``; x8WebArc: "the launch's via isn't logged").
VIA_WORDS: dict[str, str] = {
    "ActivateForProtocol": "protocol",
    "AppExecutionAlias": "alias",
    "ActivateApplication": "activate",
    "Start-Process": "start_process",
    "startfile": "startfile",
}


def _pages_before(identity: _AppIdentity, budget_s: float = _PAGE_BEFORE_S) -> dict[int, str]:
    """``{hwnd: address}``: the main page each on-screen window of the app shows right now, read right before an
    address is handed over (x8WebArc). A page-only read per window
    (:func:`yuki.perception.tree.wait_for_page`, whose first accepted page is the window's main one), windows that
    show no page left out. Only an app whose windows can be recognised (``identity.confirmable``); never raises.

    On 10 Oct at 20:55 Arc ignored the address and its window, behind another app, still showed the YouTube watch
    page it had: that page was taken for the one opened and the launch said "verified". A page its window already
    showed is never the page opened."""
    if not identity.confirmable or budget_s <= 0:
        return {}
    try:
        from yuki.perception.tree import wait_for_page

        hwnds = list(_app_windows(identity, {})())
    except Exception:
        return {}
    out: dict[int, str] = {}
    end = time.monotonic() + budget_s
    for hwnd in hwnds[:_PAGE_BEFORE_WINDOWS]:
        left = end - time.monotonic()
        if left <= 0.05:
            break
        try:
            got = wait_for_page(lambda h=hwnd: [h], lambda _url: True, timeout_s=min(_PAGE_BEFORE_WINDOW_S, left))
        except Exception:  # noqa: BLE001 - that window just isn't known before
            continue
        if got.matched is not None and got.matched.url:
            out[int(hwnd)] = str(got.matched.url)
    return out


def _await_page(identity: _AppIdentity, targets: list[str], before: dict[int, int], timeout_s: float,
                unchanged: dict[int, str] | None = None):
    """Wait (bounded) until one of the app's windows shows a page at one of
    ``targets`` (see :func:`yuki.perception.tree.wait_for_page`); ``None`` when
    the accessibility reader cannot be loaded. V11: for a site's home page, another page of that site that was already
    showing when the address was handed over is not the page asked for (a redirect from the root to a deeper page,
    which appears after the hand-off, still counts). x8WebArc: a page one of its windows already showed before the
    hand-off (``unchanged``, :func:`_pages_before`) never counts, so a browser that ignored the address is not taken
    for one that opened it."""
    try:
        from yuki.perception.tree import wait_for_page
    except Exception:
        return None
    unchanged = unchanged or {}
    old = {_page_key(url) for url in unchanged.values()}
    wait = wait_for_page(
        _app_windows(identity, before),
        lambda url: any(_same_page(target, url) for target in targets) and _page_key(url) not in old,
        timeout_s=timeout_s,
        stale=lambda url: any(_past_root(target, url) for target in targets),
    )
    if wait.matched is None and old:
        # The test above sees an address, not its window: a page another window already showed was refused even in
        # a window that showed something else before (a second window on the same page). The last read settles it
        # window by window: never a window's own old page, and only the very page asked for (a page deeper than a
        # requested site root stays V11's to judge).
        wait.matched = next(
            (page for page in wait.seen
             if _page_key(page.url) in old
             and (page.hwnd not in unchanged or _page_key(page.url) != _page_key(unchanged[page.hwnd]))
             and any(_same_page(target, page.url) and not _past_root(target, page.url) for target in targets)),
            None,
        )
    return wait


def _already_page(wait, unchanged: dict[int, str] | None, targets: list[str]):
    """Review r1 M1: the page seen after the hand-off in a window whose main page right before it was already the very
    page asked for and still is (a browser that ignored the address, or opened another tab of that same page), else
    ``None``. Only the exact page: a page deeper than a requested site root (V11, :func:`_past_root`) never counts, so
    the 20:55 old watch tab stays ``page_unchanged``."""
    if wait is None or not unchanged:
        return None
    return next(
        (page for page in wait.seen
         if page.main and page.hwnd in unchanged and _page_key(page.url) == _page_key(unchanged[page.hwnd])
         and any(_same_page(target, page.url) and not _past_root(target, page.url) for target in targets)),
        None,
    )


def _unchanged_windows(wait, unchanged: dict[int, str] | None) -> list[int]:
    """The windows whose main page after the hand-off is the very page they showed before it."""
    if wait is None or not unchanged:
        return []
    return [page.hwnd for page in wait.seen
            if page.main and page.hwnd in unchanged and _page_key(page.url) == _page_key(unchanged[page.hwnd])]


def _page_details(wait) -> dict:
    """The page check, as plain data for ``details``."""
    if wait is None:
        return {"checked": False, "reason": "accessibility unavailable"}
    return {
        "checked": True,
        "matched": asdict(wait.matched) if wait.matched else None,
        "seen": [asdict(page) for page in wait.seen],
        "windows": list(wait.windows),
        "polls": wait.polls,
        "answered": wait.answered,
        "waited_ms": round(wait.waited_ms, 1),
        "error": wait.error,
    }


def _page_phrase(title: str, url: str) -> str:
    if title and title != url:
        return f'{url} (page "{title}")'
    return f"{url} (page still loading, no title yet)"


def _page_miss(name: str, targets: list[str], wait, unchanged: Sequence[int] = ()) -> tuple[bool, str]:
    """``(ok, clause)`` for a hand-off after which no window showed the page.

    ``ok`` is False only when the app's windows do show pages and none is at
    the address - the effect provably did not happen.  When no page could be
    read at all, the clause says the address could not be checked.
    ``unchanged``: the windows still showing the very page they showed before
    the hand-off (x8WebArc): said so, since that page may well be at the
    address (an old tab of the same site) without being the one opened.
    """
    target = targets[0] + (f" (+{len(targets) - 1} more)" if len(targets) > 1 else "")
    if wait is None:
        return True, f"handed {name} {target}; whether it opened could not be checked (accessibility unavailable)"
    secs = wait.waited_ms / 1000.0
    visible = [page for page in wait.seen if page.main]
    if visible:
        shown = next((page for page in visible if page.hwnd in unchanged), visible[0])
        others = f" (+{len(visible) - 1} other window(s) with a page)" if len(visible) > 1 else ""
        if shown.hwnd in unchanged:
            return False, (
                f"handed {name} {target}, but after {secs:.1f} s {name} still shows the page it showed "
                f"before, {_page_phrase(shown.title, shown.url)}: it did not open the address{others}"
            )
        return False, (
            f"handed {name} {target}, but after {secs:.1f} s no page in {name} is at that "
            f"address; the visible page is {_page_phrase(shown.title, shown.url)}{others}"
        )
    if wait.windows:
        return True, (
            f"handed {name} {target}; after {secs:.1f} s none of its windows shows a page "
            f"whose address can be read, so whether it opened is not known"
        )
    return True, f"handed {name} {target}; after {secs:.1f} s it has no window on screen showing it"


def _page_opened(
    name: str,
    wait,
    before: dict[int, int],
    details: dict,
    content_deadline: float,
    targets: list[str],
    identity: _AppIdentity | None = None,
    *,
    focus_floor_s: float | None = None,
    already: bool = False,
) -> str:
    """Bring the window showing the requested page forward, wait out the rest of
    its content (within ``content_deadline``) and say so, fact first.
    ``focus_floor_s``: the bring-forward's least time (``None``: its default).
    ``already`` (review r1 M1): that window showed this very page before the
    hand-off and still does (:func:`_already_page`): said so, and
    ``details["page_state"] = "already"``, never "now showing"."""
    seen = wait.matched
    hwnd = seen.hwnd
    info = window_info(hwnd)
    details.update(
        hwnd=hwnd,
        window_reason=("a window of it already showed the requested page before the address was handed over"
                       if already else "a window of it is showing the requested page"),
        title=info.title if info else "",
        process_name=info.process_name if info else "",
        page_verified=True,
        page={"title": seen.title, "url": seen.url},
    )
    if already:
        details["page_state"] = "already"
    appeared = [h for h in _window_snapshot()[0] if h not in before]
    details["new_windows"] = [
        {
            "hwnd": other,
            "title": (w.title if (w := window_info(other)) else ""),
            "process_name": w.process_name if w else "",
            "is_app_window": bool(identity.owns_window(other)) if identity else None,
        }
        for other in appeared
    ]
    _note_fresh(hwnd)
    forward = _bring_forward(hwnd, time.monotonic(), details,
                             **({} if focus_floor_s is None else {"floor_s": focus_floor_s}))
    # The page at the address is the navigation confirmed: return now. Waiting on for the
    # page's content cost 1.9 s on the 25 Sept live "open YouTube on Chrome" (16.4 s in all)
    # and bought nothing the caller needs; the window is noted fresh, so the next tree read
    # of it waits out a half-built page itself.
    del content_deadline
    content_note = "navigation confirmed; the page's content was not waited for (the next look waits for it)"
    details["content_ready"] = None
    if (fresh := window_info(hwnd)) is not None:
        details["title"] = fresh.title
    new_window = hwnd in appeared
    if already:
        return (
            f"{name} already showed {_page_phrase(seen.title, seen.url)} before the address was handed over and "
            f"still shows it (no new page of it was seen): hwnd {hwnd} ({details['process_name']}) "
            f"\"{details['title']}\"; {forward}"
        )
    return (
        f"{name} is now showing {_page_phrase(seen.title, seen.url)}"
        f"{' in a new window' if new_window else ''}: hwnd {hwnd} "
        f"({details['process_name']}) \"{details['title']}\"; {forward}"
        + (f"; {content_note}" if content_note else "")
    )


# ---------------------------------------------------------------------------
# An app running in the background with no window
# ---------------------------------------------------------------------------
def _open_new_window(chosen: dict[str, str], identity: _AppIdentity, budget_s: float) -> tuple[int | None, str, str]:
    """Ask a running app for a new window, the way the Start menu starts it.

    A packaged app is activated (``IApplicationActivationManager::
    ActivateApplication``, no arguments); a desktop app has the program its
    Start menu entry points at run again (``Start-Process``), which a running
    single-instance app answers by opening a window; with no program file
    known, the Start menu entry itself is started (``shell:AppsFolder``).

    Returns:
        ``(pid or None, via, error)``.
    """
    if (refused := _gate.refuse("launch")) is not None:   # D17: no app launch with a game in front
        return None, "refused", refused
    appid = chosen["appid"]
    if _is_packaged(appid):
        pid, error = _activate_packaged(appid, "", budget_s)
        return pid, "ActivateApplication", error
    exe = next((path for path in sorted(identity.exe_paths) if _is_program_file(path)), "")
    if exe:
        result = run_powershell(
            _start_process_command(exe, "", os.path.dirname(exe)), timeout_s=budget_s
        )
        stdout = str(result.details.get("stdout") or "").strip()
        last = stdout.splitlines()[-1].strip() if stdout else ""
        error = "" if result.ok else str(result.details.get("stderr") or result.summary)
        return (int(last) if last.isdigit() else None), f"Start-Process {exe}", error
    result = run_powershell(
        f"Start-Process -FilePath {_quote('shell:AppsFolder\\' + appid)}", timeout_s=budget_s
    )
    error = "" if result.ok else str(result.details.get("stderr") or result.summary)
    return None, "shell:AppsFolder", error


def _spawned_by(pid: int, running: set[int]) -> bool:
    """Whether ``pid`` was started (at any depth) by one of ``running`` - appOpen final check V1-riot: the running
    instance brought up its own interface in a new child process (the Riot Client's window, a child of the running
    ``RiotClientServices.exe``), which is the app's, not a second instance's "already running" prompt."""
    p = _proc(pid) if pid else None
    if p is None:
        return False
    return any(a.pid in running for a in _ancestors(p))


def _restore_background_app(
    chosen: dict[str, str],
    identity: _AppIdentity,
    hidden_hwnd: int | None,
    running: set[int],
    details: dict,
    timeout_s: float,
) -> tuple[bool, str]:
    """Bring up an app that is running with no window on screen.

    Its own hidden main window is shown when it has one (and it comes on
    screen); otherwise, or when showing it did nothing, a new window is asked
    for (:func:`_open_new_window`) and waited for (bounded by ``timeout_s``).

    Returns:
        ``(ok, summary)``.
    """
    name = chosen["name"]
    details["background"] = True
    details["window_state_before"] = "hidden" if hidden_hwnd else "none"
    deadline = time.monotonic() + timeout_s
    lead = f"{name} was running in the background with no window"
    if hidden_hwnd:
        shown = _show_window(hidden_hwnd)
        details["shown_hidden_window"] = shown
        if shown:
            info = window_info(hidden_hwnd)
            details.update(
                launched=True,
                hwnd=hidden_hwnd,
                window_reason="its hidden window was shown",
                title=info.title if info else "",
                process_name=info.process_name if info else "",
            )
            forward = _bring_forward(hidden_hwnd, deadline, details)
            return True, (
                f"{lead}; its hidden window was shown: hwnd {hidden_hwnd} "
                f"({details['process_name']}) \"{details['title']}\"; {forward}"
            )
    before, before_pids, before_titles = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    hidden_before = {h for h in _top_level_windows(running) if not is_user_window(h)}
    # appOpen second review (V1): the running instance's own main-style windows it keeps hidden (never seen on screen,
    # or showing them failed). If they stay hidden and the window that came is a new process's, that
    # window may be the program's "already running" prompt rather than the app: reported, not verified
    kept_hidden = [h for h in hidden_before if hidden_main_window(h) and identity.owns_window(h)]
    pid, via, error = _open_new_window(chosen, identity, max(2.0, min(timeout_s, 15.0)))
    details.update(via=via, pid=pid, launched=not error or pid is not None)
    if error:
        details["launch_error"] = error
    if via == "refused":
        details["refused"] = _gate.kind_of(error)   # fHands: the gate's refusal, by kind (a game or something else)
    if error and pid is None:
        return False, f"{lead}, and asking it for a new window ({via}) failed: {error}"
    if pid and pid not in running:
        identity.add_launched(pid)
    hwnd, reason, appeared = _wait_for_new_window(
        identity,
        before,
        before_pids,
        before_foreground,
        deadline,
        before_titles=before_titles,
        hidden_before=hidden_before,
    )
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
        return False, (
            f"{lead}; asked it for a new window ({via}) but none appeared within "
            f"{timeout_s:g}s - it is still running with no window"
        )
    info = window_info(hwnd)
    if kept_hidden and _window_pid(hwnd) not in running and any(psutil.pid_exists(p) for p in running) and not any(
            is_user_window(h) for h in kept_hidden) and not _spawned_by(_window_pid(hwnd), running):
        reason = (f"{reason} (not confirmed as {name!r}'s own window: its running instance kept its own window "
                  f"hidden, and this one came from the new process the launch started)")
    details.update(
        hwnd=hwnd,
        window_reason=reason,
        title=info.title if info else "",
        process_name=info.process_name if info else "",
    )
    _note_fresh(hwnd)
    forward = _bring_forward(hwnd, deadline, details)
    return True, (
        f"{lead}; opened a new window ({via}): hwnd {hwnd} ({details['process_name']}) "
        f"\"{details['title']}\" - {reason}; {forward}"
    )


# ---------------------------------------------------------------------------
# Launcher entries: a Start entry that has another program start the app (appOpen, 5 Oct)
# ---------------------------------------------------------------------------
#: ``PKEY_Link_Arguments``: the arguments a desktop Start entry's shortcut passes to its program.
_PKEY_LINK_ARGUMENTS = ("{436F2667-14E2-4FEB-B30A-146C53B5B674}", 100)
#: Schemes of web links: a Start entry that is a web page opens a page in the browser (``open_url``'s job), not an
#: app through a launcher. Parsing a fixed machine format (the URI's scheme), not judging an app.
_WEB_SCHEMES = ("http", "https")
#: How often the launch watches a launcher's hand-off within the call (a game's window takes seconds to minutes).
LAUNCHER_POLL_S = 0.25
#: Final check V4-wait: how often a command of an already running program has its own process looked for.
COMMAND_LOOK_S = 1.0
#: The background watch (:func:`watch_launch`): how often it looks, and for how long at most. Games here took about
#: 90 s from Play to a full-screen window (5 Oct: VALORANT 00:17:05 -> 00:18:37); light: one window list a second.
WATCH_POLL_S = 1.0
WATCH_MAX_S = 180.0
#: A launch handed to a launcher this recently is not handed to it again (a second "open it" while the game is still
#: starting waits for it instead: re-running the shortcut can raise the launcher's own "already running" prompt).
LAUNCH_PENDING_S = 180.0
#: Shortest squashed name that may be matched as a part of another (shorter ones only match whole).
_NAME_PART_MIN = 4

_links_lock = threading.Lock()
_links_cache: tuple[int, dict[str, dict[str, str]]] | None = None
_pending_lock = threading.Lock()
_pending: dict[str, float] = {}
_exe_names_cache: dict[str, frozenset[str]] = {}
_proc_cache: dict[int, tuple[float, "_Proc"]] = {}

#: ``(entry name) -> image names``: what practice memory learned for a Start entry (the program its app runs as),
#: set by the tray (:func:`set_learned_images`). ``None`` here (tests, the memory service): nothing learned is read.
_learned_images_provider = None


def set_learned_images(provider) -> None:  # noqa: ANN001 - a callable or None
    """Where :func:`launch_app` reads what practice memory learned an entry's app runs as (appOpen F3): a callable
    ``(entry name) -> [image names]`` (the tray wires memory's ``launch_identities``), or ``None``."""
    global _learned_images_provider
    _learned_images_provider = provider


def _learned_images(name: str) -> set[str]:
    """Image names (lowercased, ``.exe``) learned for this entry; never raises, ``set()`` when none."""
    provider = _learned_images_provider
    if provider is None:
        return set()
    try:
        found = provider(name) or []
    except Exception:
        return set()
    out = set()
    for image in found:
        image = os.path.basename(str(image or "").strip()).lower()
        if image:
            out.add(image if image.endswith(".exe") else image + ".exe")
    return out


def _start_links(apps: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """``{appid: {"target", "arguments"}}`` of every desktop Start entry (one COM pass, ~0.15 s for ~250 entries,
    cached for the Start list it was read for)."""
    global _links_cache
    with _links_lock:
        if _links_cache is not None and _links_cache[0] == id(apps):
            return _links_cache[1]

    def read() -> dict[str, dict[str, str]]:
        import pywintypes
        from win32com.shell import shell

        out: dict[str, dict[str, str]] = {}
        for app in apps:
            appid = app.get("appid") or ""
            if not appid or _is_packaged(appid):
                continue
            try:
                item = shell.SHCreateItemFromParsingName("shell:AppsFolder\\" + appid, None, shell.IID_IShellItem2)
            except Exception:
                continue
            values = {}
            for key, (fmtid, pid) in (("target", _PKEY_LINK_TARGET_PARSING_PATH), ("arguments", _PKEY_LINK_ARGUMENTS)):
                try:
                    values[key] = str(item.GetString((pywintypes.IID(fmtid), pid)) or "")
                except Exception:
                    values[key] = ""
            out[appid] = values
        return out

    links, error = _run_in_com_thread(read, timeout_s=10.0)
    if error or links is None:   # ("value", "") on success
        # review F5: a failed or timed-out read is not cached - the next launch reads again (a cached ``{}`` would turn
        # launcher detection off for the rest of the process and send "open <game>" down the plain path)
        return {}
    with _links_lock:
        _links_cache = (id(apps), links)
    return links


def _uri_scheme(value: str) -> str:
    """The scheme of a URI (``steam`` of ``steam://rungameid/730``), ``""`` for anything else (a path, a drive)."""
    if "://" not in (value or ""):
        return ""
    try:
        scheme = urlsplit(value).scheme
    except ValueError:
        return ""
    return scheme.lower() if len(scheme) > 1 else ""


def _launcher_entry(chosen: dict[str, str], apps: list[dict[str, str]]) -> dict | None:
    """Whether a Start entry has another program start its app, from facts of the entry (never which app it is).

    A *launcher entry* is a desktop entry that is either

    * a link (its AppID, or its shortcut's target) with a non-web scheme (``steam://rungameid/730``): the program
      registered for that scheme takes it (Steam starts the game); or
    * a shortcut that passes arguments to a program that another Start entry runs too (``VALORANT`` is
      ``RiotClientServices.exe --launch-product=valorant ...``, ``Riot Client`` the same program bare). A program
      inside the Windows folder is everybody's host (``cmd.exe /k ...``), never a launcher.

    Returns:
        ``{"kind": "link" | "shortcut", "carrier_exe", "arguments", "uri", "siblings", "bare_siblings"}`` or ``None``
        (``bare_siblings``: the siblings that run the program with no arguments - the launcher itself, ``Riot
        Client``; what a summary calls the carrier).
    """
    appid = chosen["appid"]
    if _is_packaged(appid):
        return None
    links = _start_links(apps)
    link = links.get(appid) or {}
    target, arguments = str(link.get("target") or ""), str(link.get("arguments") or "")
    for value in (appid, target):
        scheme = _uri_scheme(value)
        if scheme:
            if scheme in _WEB_SCHEMES:
                return None
            return {"kind": "link", "carrier_exe": _handler_path(value), "arguments": "", "uri": value,
                    "siblings": [], "bare_siblings": []}
    if not _is_program_file(target) or not arguments.strip():
        return None
    windows_dir = os.path.normcase(os.environ.get("SystemRoot") or r"C:\Windows")
    exe = os.path.normcase(os.path.abspath(target))
    if _is_within(exe, windows_dir):
        return None
    siblings = sorted(
        app["name"] for app in apps
        if app.get("appid") != appid
        and os.path.normcase(os.path.abspath(str((links.get(app.get("appid") or "") or {}).get("target") or "x")))
        == exe
    )
    if not siblings:
        return None
    bare = sorted(
        app["name"] for app in apps
        if app["name"] in siblings
        and not str((links.get(app.get("appid") or "") or {}).get("arguments") or "").strip()
    )
    return {"kind": "shortcut", "carrier_exe": target, "arguments": arguments, "uri": "", "siblings": siblings,
            "bare_siblings": bare}


def _squash(text: str) -> str:
    """Letters and digits only, lowercased: names compared without spaces, case or punctuation."""
    return "".join(ch for ch in str(text or "").casefold() if ch.isalnum())


def _exe_names(exe: str) -> frozenset[str]:
    """The names a program file goes by, squashed: its file name's stem and its version resource's ProductName and
    FileDescription (``VALORANT-Win64-Shipping.exe`` -> ``{"valorantwin64shipping", "valorant"}``). Cached."""
    key = os.path.normcase(exe or "")
    if not key:
        return frozenset()
    cached = _exe_names_cache.get(key)
    if cached is not None:
        return cached
    names = {_squash(os.path.splitext(os.path.basename(exe))[0])}
    try:
        import win32api

        lang, codepage = win32api.GetFileVersionInfo(exe, r"\VarFileInfo\Translation")[0]
        for field_name in ("ProductName", "FileDescription"):
            try:
                value = win32api.GetFileVersionInfo(exe, rf"\StringFileInfo\{lang:04x}{codepage:04x}\{field_name}")
            except Exception:
                value = ""
            names.add(_squash(value or ""))
    except Exception:
        pass
    out = frozenset(n for n in names if n)
    _exe_names_cache[key] = out
    return out


def _names_agree(names: frozenset[str] | set[str], entry: str) -> bool:
    """Whether a program's names and a Start entry's name are the same name: equal once squashed, or one a part of the
    other when that part is at least :data:`_NAME_PART_MIN` characters (``"fearstofathom"`` in ``"fearstofathom
    episode1"``). Deterministic name matching, no list of apps."""
    wanted = _squash(entry)
    if not wanted:
        return False
    for name in names:
        if not name:
            continue
        if name == wanted:
            return True
        if len(name) >= _NAME_PART_MIN and name in wanted:
            return True
        if len(wanted) >= _NAME_PART_MIN and wanted in name:
            return True
    return False


def _title_score(title: str, entry: str) -> int:
    """How a window title agrees with a name: 2 the same once squashed, 1 one a part of the other
    (:func:`_names_agree`), 0 not at all."""
    squashed = _squash(title)
    if not squashed:
        return 0
    if squashed == _squash(entry):
        return 2
    return 1 if _names_agree(frozenset({squashed}), entry) else 0


class _Proc:
    """One process's facts. ``created`` (its start time) is read only when asked: reading it is slow for protected
    processes (0.9 s for all of them cold), and only processes under a launcher need it."""

    __slots__ = ("pid", "ppid", "exe", "image", "_created")

    def __init__(self, pid: int, ppid: int, exe: str, image: str, created: float | None = None) -> None:
        self.pid, self.ppid, self.exe, self.image, self._created = pid, ppid, exe, image, created

    @property
    def created(self) -> float:
        if self._created is None:
            try:
                self._created = float(psutil.Process(self.pid).create_time())
            except Exception:
                self._created = 0.0
        return self._created


def _ppid_map() -> dict[int, int]:
    """``{pid: parent pid}`` of every process in one system snapshot (~12 ms). ``psutil.Process.ppid`` takes the same
    snapshot per call on Windows, so asking each process apart costs a snapshot each (3.5 s for all of them)."""
    try:
        return dict(psutil._psplatform.ppid_map())   # noqa: SLF001 - psutil's own one-snapshot reader
    except Exception:
        return {}


def _proc(pid: int, image_hint: str = "", ppids: dict[int, int] | None = None) -> _Proc | None:
    """The facts of a running process, or ``None`` (gone, or not readable). Cached per pid; ``image_hint`` (its image
    name as just listed) drops an entry of a pid reused since."""
    if not pid:
        return None
    pid = int(pid)
    cached = _proc_cache.get(pid)
    if cached is not None and (not image_hint or cached[1].image == image_hint.lower()):
        return cached[1]
    try:
        process = psutil.Process(pid)
        exe = process.exe() or ""
    except Exception:
        exe = ""
        process = None
    if process is None:
        return None
    try:
        image = (process.name() or os.path.basename(exe)).lower()
    except Exception:
        image = os.path.basename(exe).lower()
    if ppids is not None and pid in ppids:
        ppid = int(ppids[pid] or 0)
    else:
        try:
            ppid = int(process.ppid() or 0)
        except Exception:
            ppid = 0
    facts = _Proc(pid, ppid, os.path.normcase(exe), image)
    if len(_proc_cache) > 2048:
        _proc_cache.clear()
    _proc_cache[pid] = (0.0, facts)
    return facts


def _ancestors(p: _Proc, depth: int = 8, table: dict[int, _Proc] | None = None) -> list[_Proc]:
    """The process's parents, nearest first. ``table``: a process table read at once (:func:`_process_table`)
    instead of one lookup per parent. A pid reused by a newer process is not taken for the parent (only checked when
    both start times are known: reading them is slow)."""
    out: list[_Proc] = []
    current = p
    for _ in range(depth):
        if not current.ppid or current.ppid == current.pid:
            break
        parent = table.get(current.ppid) if table is not None else _proc(current.ppid)
        if parent is None:
            break
        if parent._created and current._created and parent._created > current._created:
            break
        out.append(parent)
        current = parent
    return out


def _process_table() -> dict[int, _Proc]:
    """Every process at once: one parent snapshot (:func:`_ppid_map`) and the image names and paths (~35 ms)."""
    ppids = _ppid_map()
    table: dict[int, _Proc] = {}
    try:
        for process in psutil.process_iter(["pid", "name", "exe"]):
            info = process.info
            try:
                pid = int(info["pid"])
                exe = info.get("exe") or ""
                table[pid] = _Proc(pid, int(ppids.get(pid) or 0), os.path.normcase(exe),
                                   str(info.get("name") or os.path.basename(exe)).lower())
            except (TypeError, ValueError):
                continue
    except Exception:
        return table
    return table


def _user_windows() -> list:
    """The user windows on screen now (:func:`yuki.perception.windows.list_windows`)."""
    try:
        return list_windows()
    except Exception:
        return []


def _foreground() -> int:
    try:
        return int(_user32.GetForegroundWindow() or 0)
    except Exception:
        return 0


def _start_entry(appid: str, timeout_s: float) -> tuple[bool, str]:
    """Start a Start entry the way the Start menu does (``shell:AppsFolder\\<AppID>``: a shortcut's own arguments, a
    link's scheme handler). ``(ok, error)``."""
    if (refused := _gate.refuse("launch")) is not None:   # D17: no app launch with a game in front
        return False, refused
    result = run_powershell(f"Start-Process -FilePath {_quote('shell:AppsFolder' + chr(92) + appid)}",
                            timeout_s=timeout_s)
    return bool(result.ok), ("" if result.ok else str(result.details.get("stderr") or result.summary))


class _LauncherApp:
    """What makes a process or window the app of a launcher entry, and what is only the launcher's (appOpen F1).

    The *carrier* is the program that takes the launch: the shortcut's program (``RiotClientServices.exe``) or the
    link's scheme handler (``steam.exe``). Its own windows - its tray-icon host, its client window with a Play button
    - are **never** the app (unless titled for the entry, last case below). The app is, in order:

    * a window that declares the entry's own explicit AppUserModelID;
    * a process running a program practice memory learned for this entry (``learned``: its image name), never the
      carrier's own (review F4: a wrong learned image never makes the launcher's own window "the app");
    * a process whose program's names agree with the entry's name (:func:`_names_agree`: ``VALORANT-Win64-
      Shipping.exe``'s product name is ``VALORANT``), wherever it runs (review F2: ``wallpaper64.exe`` runs under its
      own service, not under ``steam.exe``; a game whose launcher restarted has no ancestry left). Being started under
      the carrier is said, and preferred when several match;
    * the carrier's own window when the carrier *is* the entry's app (review F2: ``Razer Synapse`` is
      ``RazerAppEngine.exe --url-params=apps=synapse``): a window of it whose title is named for the entry (the
      launcher's tray host and client window, titled for the launcher, never are).

    A window under the carrier from a process that started after the launch, whose names do not agree, is a
    *candidate*: reported, never verified. Older ones under it are the launcher's own.
    """

    def __init__(self, name: str, appid: str, carrier_exe: str, learned: set[str] | frozenset[str] = frozenset(),
                 uri: str = "", siblings: Sequence[str] = ()) -> None:
        self.name = name
        #: The other Start entries that run the carrier: a program named for one of them too (``java.exe`` for "About
        #: Java" and "Configure Java", ``python.exe`` for "Python Module Docs" and "Python") is only the app when it
        #: was started under the carrier.
        self.siblings = [str(x) for x in siblings or () if str(x).strip()]
        self.appid = (appid or "").lower()
        self.carrier_exe = os.path.normcase(os.path.abspath(carrier_exe)) if carrier_exe else ""
        self.carrier_image = os.path.basename(self.carrier_exe).lower()
        self.learned = {i.lower() for i in learned}
        self.uri = uri
        self.launched_pids: set[int] = set()
        #: Processes under the carrier started after this (wall clock) are this launch's: candidates when their names
        #: do not agree; older ones are the launcher's own (its client window, its helpers).
        self.since = 0.0

    def describe(self) -> dict:
        return {"name": self.name, "appid": self.appid, "carrier_exe": self.carrier_exe,
                "learned": sorted(self.learned), "uri": self.uri, "since": self.since, "siblings": list(self.siblings)}

    def is_carrier(self, p: _Proc) -> bool:
        return bool(self.carrier_exe) and p.exe == self.carrier_exe

    def under_carrier(self, p: _Proc, table: dict[int, _Proc] | None = None) -> bool:
        """The carrier itself, or started (at any depth) by it or by this launch."""
        if self.is_carrier(p) or p.pid in self.launched_pids:
            return True
        return any(self.is_carrier(a) or a.pid in self.launched_pids for a in _ancestors(p, table=table))

    def owns(self, p: _Proc | None, table: dict[int, _Proc] | None = None) -> str:
        """How ``p`` is known to be the app (``""``: it is not)."""
        if p is None or self.is_carrier(p):
            return ""
        if p.image in self.learned:
            return "the program learned for it"
        names = _exe_names(p.exe)
        if _names_agree(names, self.name):
            if self.under_carrier(p, table):
                return "started under its launcher, and its program is named for it"
            if not self._shared_name(names):
                return "its program is named for it"
        return ""

    def _shared_name(self, names: frozenset[str]) -> bool:
        """Whether a program's names agree with another entry of the carrier, or with the carrier's own names (a
        generic name - ``java``, ``python`` - that is not this entry's alone)."""
        if any(_names_agree(names, other) for other in self.siblings):
            return True
        return bool(self.carrier_exe) and any(_names_agree(names, n) for n in _exe_names(self.carrier_exe))

    def titled_for_it(self, title: str, *, fresh: bool = False) -> bool:
        """Whether a window title names this entry rather than the launcher or another of its entries (second review
        V4: VLC's idle window "VLC media player" is the bare entry's, not "VLC media player skinned"'s). The title is
        this entry's name exactly, or agrees with it better than with every sibling entry and the carrier's own names;
        a window of a process started since this launch (``fresh``) only has to agree. Name matching, no app list."""
        mine = _title_score(title, self.name)
        if not mine:
            return False
        if mine == 2 or fresh:
            return True
        others = [*self.siblings, *(_exe_names(self.carrier_exe) if self.carrier_exe else ())]
        return mine > max((_title_score(title, other) for other in others), default=0)

    def window_how(self, hwnd: int, p: _Proc | None, title: str = "") -> str:
        explicit = self.appid and not self.appid.startswith("microsoft.autogenerated.") and not _uri_scheme(self.appid)
        if explicit and _window_appid(hwnd).lower() == self.appid:
            return "window AppUserModelID"
        if p is not None and self.is_carrier(p):
            # review F2: for some entries the carrier is the app itself (one program, several entries by argument)
            # (a fresh instance of the carrier is not enough: Steam started by a store link shows its own window)
            if title and self.titled_for_it(title, fresh=self._started_since(p)):
                return "its own program's window, titled for it"
            return ""
        how = self.owns(p)
        if how or p is None or not title:
            return how
        # second review V2: a game whose program has no names of its own (cs2.exe: no version resource), running under
        # the launcher, in a window titled for the entry ("Counter-Strike 2") - the same title test as the carrier's.
        # Final check V2-partial-title: titled exactly for it; a title that only contains the name ("Updating
        # Counter-Strike 2", a launcher helper's dialog) counts only for a process started since this launch that is
        # not one of the launcher's own programs (installed under the launcher's folder)
        if self.under_carrier(p) and self.titled_for_it(title):
            if _title_score(title, self.name) == 2 or (self._started_since(p) and not self._carrier_program(p)):
                return "started under its launcher, its window titled for it"
        return ""

    def _carrier_program(self, p: _Proc) -> bool:
        """A program installed under the carrier's own folder (its helpers: ``steamwebhelper.exe`` under Steam's)."""
        if not self.carrier_exe or not p.exe:
            return False
        folder = os.path.dirname(self.carrier_exe).rstrip("\\") + "\\"
        return os.path.normcase(p.exe).startswith(folder)

    def _started_since(self, p: _Proc) -> bool:
        return bool(self.since) and p.created >= self.since

    def _new(self, p: _Proc) -> bool:
        return bool(self.since) and p.created >= self.since and not self.is_carrier(p)

    def read(self, *, scan: bool = False) -> dict:
        """What is there now: ``{"app": [windows], "launcher": [windows], "candidates": [windows], "foreground"}``
        (and ``"app_procs"`` with ``scan``), each window ``{"hwnd", "title", "process_name", "pid", "fullscreen",
        "in_front", "how"}``."""
        windows = _user_windows()
        foreground = _foreground()
        app, launcher, candidates = [], [], []
        for w in windows:
            p = _proc(w.pid, str(w.process_name or ""))
            entry = {"hwnd": int(w.hwnd), "title": w.title, "process_name": w.process_name, "pid": int(w.pid),
                     "fullscreen": bool(getattr(w, "is_fullscreen", False)), "in_front": int(w.hwnd) == foreground}
            how = self.window_how(int(w.hwnd), p, str(w.title or ""))
            if how:
                app.append({**entry, "how": how, "under": bool(p is not None and self.under_carrier(p))})
            elif p is not None and self.under_carrier(p):
                (candidates if self._new(p) else launcher).append(entry)
        app.sort(key=lambda x: not x.get("under"))   # several named for it: the one under its launcher first
        out: dict = {"app": app, "launcher": launcher, "candidates": candidates, "foreground": foreground}
        if scan:
            table = _process_table()
            procs = []
            for p in table.values():
                how = self.owns(p, table)
                if how:
                    procs.append({"pid": p.pid, "image": p.image, "how": how, "since": p.created})
            out["app_procs"] = procs
        return out


def _launch_pending(appid: str) -> float | None:
    """Seconds since this entry was handed to its launcher, when that is within :data:`LAUNCH_PENDING_S`."""
    with _pending_lock:
        at = _pending.get(appid.lower())
    if at is None:
        return None
    age = time.monotonic() - at
    return age if age < LAUNCH_PENDING_S else None


def _mark_pending(appid: str) -> None:
    with _pending_lock:
        _pending[appid.lower()] = time.monotonic()


def _window_line(w: dict) -> str:
    return f"hwnd {w['hwnd']} ({w['process_name']}) \"{w['title']}\"" + (", full screen" if w.get("fullscreen") else "")


def _launcher_name(plan: dict) -> str:
    """How the carrier is called in a summary: the sibling entry that runs its program bare (``Riot Client``), else
    its program file (``RazerAppEngine.exe``). Review F2: never another sibling that passes its own arguments (``Razer
    Chroma`` is not what took ``Razer Synapse``; ``Check For Updates`` is not what took ``About Java``)."""
    bare = list(plan.get("bare_siblings") or [])
    return bare[0] if bare else (os.path.basename(plan.get("carrier_exe") or "") or "its launcher")


def _starting_summary(name: str, carrier: str, state: dict, waited_s: float, *, handed: str) -> str:
    """The plain words of a launcher launch whose app has no window yet: what took it, what is on screen instead."""
    parts = [handed]
    procs = state.get("app_procs") or []
    if procs:
        images = sorted({p["image"] for p in procs})
        parts.append(f"{', '.join(images)} is running for it, no window of it yet")
    else:
        parts.append(f"no process of {name} yet")
    front = next((w for w in state.get("launcher") or [] if w.get("in_front")), None)
    if front is not None:
        parts.append(f"the launcher's own window is in front: {_window_line(front)} - it may be waiting for Play, "
                     f"an update or a sign-in; that window is {carrier}'s, not {name}")
    for w in (state.get("candidates") or [])[:3]:
        parts.append(f"a new window under {carrier} appeared: {_window_line(w)} - not confirmed as {name} (its "
                     f"program is not named for it)")
    parts.append(f"after {waited_s:.0f} s it is still starting; a game can take a minute or two to show its window "
                 f"(not done yet; another launch_app waits for it and never hands it to the launcher twice)")
    return "; ".join(parts)


def _launch_via_launcher(chosen: dict[str, str], plan: dict, timeout_s: float, details: dict) -> tuple[bool, str]:
    """Open a launcher entry's app (appOpen F1): never its launcher's windows, never its hidden tray window.

    1. The app already running: its window is brought to the front (verified when it is in front), or, with no
       window yet, the call says it is starting and does not hand the launch over again.
    2. Handed over within :data:`LAUNCH_PENDING_S`: not handed again; waited on.
    3. Otherwise the entry is started the way the Start menu does (``shell:AppsFolder``, the shortcut's own
       arguments kept), and the app's window is waited for, within ``timeout_s``, at :data:`LAUNCHER_POLL_S`.

    ``details`` gets ``launcher`` (what to watch, for :func:`watch_launch`), ``verified`` (True only for the app's
    own window in front, None while it is starting), ``starting`` (no window of it yet), ``launcher_front`` and
    ``candidates``.

    Returns:
        ``(ok, summary)``: ``ok`` is False only when starting the entry failed.
    """
    name = chosen["name"]
    carrier = _launcher_name(plan)
    learned = _learned_images(name)
    app = _LauncherApp(name, chosen["appid"], plan.get("carrier_exe") or "", learned, plan.get("uri") or "",
                       siblings=plan.get("siblings") or [])
    app.since = time.time()
    # handed_to / handed_at (review F1): which launcher program took the launch and when (wall clock) - the callers
    # keep them with the opening's trace step, so practice memory tells a game Yuki's own launch started from the
    # user's own doing
    details["launcher"] = {**app.describe(), "carrier": carrier, "arguments": plan.get("arguments") or "",
                           "kind": plan.get("kind"), "handed_to": app.carrier_image, "handed_at": round(app.since, 2)}
    details["via"] = "launcher"
    # second review V4: the carrier is the app itself (its program is named for the entry: "VLC media player - reset
    # preferences", "Python 3.12 Module Docs"): a command of that program, which may run without a window of its own -
    # no game wording, no watch, not held as a pending hand-off
    carrier_is_app = bool(plan.get("carrier_exe")) and _names_agree(_exe_names(plan.get("carrier_exe") or ""), name)
    details["launcher"]["carrier_is_app"] = carrier_is_app
    started = time.monotonic()
    before_carrier: set[int] = set()
    state = app.read(scan=True)
    if state["app"]:
        return _launcher_found(app, state, details, started, already=True, timeout_s=timeout_s)
    pending = _launch_pending(chosen["appid"])
    # review F2: a process of the app with no window blocks a second hand-off only while it is starting (started
    # within LAUNCH_PENDING_S); one that has run longer without a window is a background instance (a wallpaper app's
    # renderer), and handing the entry over is what shows its window, as the Start menu would
    now_wall = time.time()
    starting_procs = [p for p in state.get("app_procs") or []
                      if now_wall - float(p.get("since") or 0.0) < LAUNCH_PENDING_S]
    if starting_procs or pending is not None:
        details["already_running"] = bool(starting_procs)
        details["launched"] = False
        earlier = [float(p.get("since") or 0.0) for p in starting_procs if p.get("since")]
        if pending is not None:
            earlier.append(now_wall - pending)
        if earlier:
            details["launcher"]["handed_at"] = round(min(earlier), 2)   # the hand-off this one waits on
        handed = (f"{name} is already starting" if starting_procs
                  else f"{name} was handed to {carrier} {pending:.0f} s ago, not handed again")
    else:
        details["already_running"] = False
        # final check V4-wait: a command of a program that is already running (VLC's "reset preferences" while VLC is
        # open) runs in a new process of it and may end with no window: the wait follows that process, as for any
        # app already running (:class:`_ReactClock`), instead of the whole launch budget
        if carrier_is_app:
            before_carrier = {p.pid for p in _process_table().values() if app.is_carrier(p)}
        ok, error = _start_entry(chosen["appid"], max(2.0, min(timeout_s, 15.0)))
        details["launched"] = ok
        if not ok:
            details["launch_error"] = error
            details["verified"] = False
            return False, f"could not start {name!r} through {carrier}: {error}"
        if not carrier_is_app:
            _mark_pending(chosen["appid"])
        args = plan.get("arguments") or plan.get("uri") or ""
        handed = f"started {name!r} the way the Start menu does: {carrier} took it" + (f" ({args})" if args else "")
    deadline = started + max(0.0, timeout_s)
    react_until = time.monotonic() + _RUNNING_REACT_S if before_carrier else None
    ran: set[int] = set()
    next_look = 0.0
    while time.monotonic() < (min(deadline, react_until) if react_until is not None else deadline):
        state = app.read()
        if state["app"]:
            return _launcher_found(app, state, details, started, already=False, timeout_s=timeout_s, lead=handed)
        if react_until is not None and time.monotonic() >= next_look:
            # the command's own process(es) of the program: the clock slides while one is alive (one table a second)
            next_look = time.monotonic() + COMMAND_LOOK_S
            alive = {p.pid for p in _process_table().values() if app.is_carrier(p) and p.pid not in before_carrier}
            ran |= alive
            if alive:
                react_until = time.monotonic() + _RUNNING_REACT_S
        time.sleep(LAUNCHER_POLL_S)
    state = app.read(scan=True)
    if state["app"]:
        return _launcher_found(app, state, details, started, already=False, timeout_s=timeout_s, lead=handed)
    waited = time.monotonic() - started
    if carrier_is_app:
        details.update(starting=False, verified=None, hwnd=None, waited_ms=round(waited * 1000.0, 1),
                       candidates=[{k: w[k] for k in ("hwnd", "title", "process_name")} for w in state["candidates"]])
        if before_carrier:
            details["command_ran"] = bool(ran)
            done = ("its command ran in a new process of the program and ended" if ran
                    else "no new process of the program stayed running")
            return True, (f"{handed}; {carrier} is the program of {name} itself and was already running; {done}, and "
                          f"no window titled for {name} came up within {waited:.0f} s (a command of the program can "
                          f"run without a window of its own) - not confirmed")
        return True, (f"{handed}; {carrier} is the program of {name} itself, and no window titled for {name} came up "
                      f"within {waited:.0f} s (a command of the program can run without a window of its own) - not "
                      f"confirmed")
    details.update(starting=True, verified=None, hwnd=None, waited_ms=round(waited * 1000.0, 1),
                   launcher_front=any(w.get("in_front") for w in state.get("launcher") or []),
                   candidates=[{k: w[k] for k in ("hwnd", "title", "process_name")} for w in state["candidates"]],
                   app_running=sorted({p["image"] for p in state.get("app_procs") or []}))
    return True, _starting_summary(name, carrier, state, waited, handed=handed)


def _launcher_found(app: _LauncherApp, state: dict, details: dict, started: float, *, already: bool,
                    timeout_s: float, lead: str = "") -> tuple[bool, str]:
    """The app's own window is there: bring it to the front (unless it is) and say so; verified when it is in front."""
    w = sorted(state["app"], key=lambda x: (not x.get("in_front"), not x.get("fullscreen")))[0]
    hwnd = w["hwnd"]
    details.update(hwnd=hwnd, title=w["title"], process_name=w["process_name"], window_reason=w["how"],
                   already_running=already if already else details.get("already_running", False),
                   fullscreen=bool(w.get("fullscreen")), starting=False,
                   waited_ms=round((time.monotonic() - started) * 1000.0, 1))
    if w.get("in_front"):
        details["foreground"] = True
        forward = "it is in front" + (", full screen" if w.get("fullscreen") else "")
    else:
        forward = _bring_forward(hwnd, time.monotonic() + max(_FOCUS_FLOOR_S, min(timeout_s, 4.0)), details)
    details["verified"] = bool(details.get("foreground"))
    head = f"{app.name} is already running" if already else (lead or f"started {app.name!r}")
    return True, f"{head}: its own window {_window_line(w)} ({w['how']}); {forward}"


def watch_launch(launcher: dict, *, timeout_s: float = WATCH_MAX_S, poll_s: float = WATCH_POLL_S,
                 stop: threading.Event | None = None) -> dict:
    """Watch, lightly and for a bounded time, for a launcher entry's app window after a launch that is still starting
    (``launch_app``'s ``details["launcher"]``). Never acts on the desktop: it reads the window list once every
    ``poll_s`` and stops at the app's window, at ``timeout_s``, or when ``stop`` is set (a new request).

    Returns:
        ``{"verified": True | False | None, "summary", "hwnd", "process_name", "title", "fullscreen", "in_front",
        "waited_s", ...}``: True - the app's own window showed; False - it did not within ``timeout_s`` (what is in
        front instead is said); None - the watch was stopped first.
    """
    name = str(launcher.get("name") or "it")
    carrier = str(launcher.get("carrier") or "its launcher")
    app = _LauncherApp(name, str(launcher.get("appid") or ""), str(launcher.get("carrier_exe") or ""),
                       set(launcher.get("learned") or ()) | _learned_images(name), str(launcher.get("uri") or ""),
                       siblings=launcher.get("siblings") or [])
    app.since = float(launcher.get("since") or time.time())
    started = time.monotonic()
    deadline = started + max(0.0, float(timeout_s))
    stopped = False
    while True:
        state = app.read()
        if state["app"]:
            w = sorted(state["app"], key=lambda x: (not x.get("in_front"), not x.get("fullscreen")))[0]
            where = ("in front" + (", full screen" if w.get("fullscreen") else "")) if w.get("in_front") else (
                "on screen, not in front")
            waited = round(time.monotonic() - started, 1)
            return {"verified": True, "summary": f"{name}'s own window is up after {waited:.0f} s: "
                                                 f"{_window_line(w)} ({w['how']}), {where}",
                    "hwnd": w["hwnd"], "process_name": w["process_name"], "title": w["title"],
                    "fullscreen": bool(w.get("fullscreen")), "in_front": bool(w.get("in_front")),
                    "waited_s": waited, "how": w["how"]}
        if stop is not None and stop.is_set():
            stopped = True
            break
        if time.monotonic() >= deadline:
            break
        if stop is not None:
            if stop.wait(poll_s):
                stopped = True
                break
        else:
            time.sleep(poll_s)
    state = app.read(scan=True)
    waited = round(time.monotonic() - started, 1)
    front = next((w for w in state.get("launcher") or [] if w.get("in_front")), None)
    running = sorted({p["image"] for p in state.get("app_procs") or []})
    base = {"hwnd": None, "process_name": front["process_name"] if front else "",
            "title": front["title"] if front else "", "fullscreen": False, "in_front": False, "waited_s": waited,
            "launcher_front": front is not None, "app_running": running}
    if stopped:
        return {**base, "verified": None, "summary": f"stopped watching for {name}'s window after {waited:.0f} s"}
    parts = [f"no window of {name} within {waited:.0f} s"]
    if running:
        parts.append(f"{', '.join(running)} is running for it, with no window")
    if front is not None:
        parts.append(f"in front instead: {_window_line(front)}, {carrier}'s own window, not {name}")
    for w in (state.get("candidates") or [])[:2]:
        parts.append(f"a window under {carrier}: {_window_line(w)}, not confirmed as {name}")
    return {**base, "verified": False, "summary": "; ".join(parts)}


def _launch_verdict(ok: bool, details: dict) -> bool | None:
    """``details["verified"]`` of a launch (appOpen F2): True only when a window of the app itself - recognised by
    its identity, not "not confirmed" - came up and is in front; False when the launch failed or the page it was
    handed is not there; None when no window of it appeared or nothing can recognise its windows. ``ok`` stays
    "nothing failed"; callers read this for "done"."""
    if not ok:
        return False
    page = details.get("page_verified")
    if page is not None:
        return bool(page) and bool(details.get("foreground", True))
    if details.get("page_targets"):
        return None   # percFix: handed an address and no page could be read: the window alone is not the page
    if not details.get("hwnd") or not details.get("foreground"):
        return None
    identity = details.get("identity") if isinstance(details.get("identity"), dict) else {}
    if identity and not identity.get("confirmable", True):
        return None
    if "not confirmed" in str(details.get("window_reason") or ""):
        return None
    return True


#: names21: how a launch's summary names what a heard name was taken as, a plain fact (the model reads it and says what
#: opened: "Opening Discord."). The user's decision 3: named, never a warning, so no doubt and no score here (the
#: score is in ``details["names_match"]``). The first when the launch went through, the second when it didn't.
MEASURED_OPENED_LINE = "Opened {name} (heard as {query!r}). "
MEASURED_TAKEN_LINE = "Took {query!r} as {name}. "


def _measured_name(query: str, apps: list[dict[str, str]], *, sites: bool) -> dict | None:
    """names21 (final-D S18; the user's decision 3, "it should automatically understand it and work"): the user's own
    app or site a launch's name is a mishearing of, when no Start entry goes by it (p24: "Porbex Kaladra" for their
    site excalidraw.com, "the scored" for Discord). Measured among the user's own names and every Start entry
    (:func:`yuki.skills.names.best_match` in name mode: one clear winner by spelling and sound, else none); a site only
    when ``sites`` (a launch handed no arguments). ``{"kind": "app" | "site", "name", "entry" (an app's Start entry),
    "url" and "app" (a site's front page and the program memory saw it in most), "query" (the name as given, for the
    summary's words: :data:`MEASURED_OPENED_LINE`), "log" (content-free: kind, how, score, margin, source)}``, or
    ``None``: as before. An app the measure names that no Start entry goes by (a program the Start menu lists under
    another name) is ``None`` too. A path, an address or a command is no name. Never raises."""
    text = " ".join(str(query or "").split())
    if not text or any(ch in text for ch in "\\/:"):
        return None
    try:
        from yuki.skills import names as N

        own = N.own_entries(start=apps)
        pool = [e.name for e in own if sites or e.kind == N.APP]
        listed = set(pool)
        pool += [n for n in N.start_names(apps) if n not in listed]
        got = N.best_match(text, pool, as_name=True)
        if got is None:
            return None
        name, score, margin = got
        entry = next((e for e in own if e.name == name), None)
        kind = entry.kind if entry is not None else N.APP
        log = {"kind": kind, "how": "name", "score": score, "margin": margin,
               "source": entry.source if entry is not None else N.START}
        if kind == N.SITE:
            url = N.site_url(name)
            if not url:
                return None
            return {"kind": kind, "name": name, "url": url, "app": entry.app if entry is not None else "", "log": log,
                    "query": text}
        chosen, _candidates = _match_apps(name, apps)
        if chosen is None:
            return None
        return {"kind": kind, "name": chosen["name"], "entry": chosen, "log": log, "query": text}
    except Exception:  # noqa: BLE001 - no measure: as before
        return None


def _act_off(app: str) -> bool:
    """names21: whether the user switched Act (clicking and typing) off for ``app`` (Settings, App access; the brain's
    own policy reader). An unreadable policy never blocks. Never raises."""
    try:
        from yuki.brain.util import _shared_reader

        return bool(_shared_reader().app_off(app))
    except Exception:  # noqa: BLE001
        return False


def _open_measured_site(took: dict, finish: Callable[[bool, str, dict], ActionResult], *, content_timeout_s: float,
                        deadline: float | None) -> ActionResult:
    """names21: a launch's name taken as one of the user's sites (:func:`_measured_name`): its front page opened in the
    browser the user opens that site in (:func:`yuki.skills.names.site_browser`: web_route's rule, the browser its
    visits are concentrated in; else the program memory saw its pages in most), a Start entry by that name, else the
    default handler (:func:`open_url`: the game gate, the page check and the address bar's rescue as for any open; no
    address typed into a browser whose Act switch is off). The result is ``finish``'s (the launch's own verdict and the
    measure's note)."""
    browser = ""
    try:
        from yuki.skills.names import site_browser

        browser = site_browser(took["name"]) or str(took.get("app") or "")
    except Exception:  # noqa: BLE001
        browser = str(took.get("app") or "")
    entry = None
    if browser:
        entry, _candidates = _match_apps(browser, get_start_apps())
    # the launch was judged as a launch, never as typing: with the browser's Act switch off (Settings, App access) its
    # address bar is never typed into here (the hand-off alone, as the web skill's first rung does)
    typing_ok = entry is None or not _act_off(entry["name"])
    opened = open_url(took["url"], app=entry["name"] if entry else None, content_timeout_s=content_timeout_s,
                      deadline=deadline, address_bar=typing_ok)
    details = dict(opened.details or {})
    details.setdefault("launched", bool(opened.ok))
    details.setdefault("hwnd", None)
    details["candidates"] = []
    details["site"] = took["name"]
    return finish(bool(opened.ok), opened.summary, details)


def launch_app(
    query: str,
    *,
    args: list[str] | None = None,
    timeout_s: float = 8.0,
    content_timeout_s: float = CONTENT_WAIT_S,
    deadline: float | None = None,
) -> ActionResult:
    """Launch a Start menu app by display name and wait for its window.

    Args:
        query: what the user called the app; matched against Start menu display
            names (exact, then substring). names21 (final-D S18): a name no
            entry goes by that is, measured by spelling and sound, clearly one
            of the user's own apps or sites (:func:`_measured_name`: "the
            scored" for Discord, "Porbex Kaladra" for their excalidraw.com) is
            that one - an app launched as by its own name, a site's front page
            opened in the browser they open it in - and the summary starts by
            naming it as a plain fact ("Opened Discord (heard as 'the
            scored')."; ``details["names_match"]``: kind, how, score, margin,
            source); with no clear winner, as before.
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
        deadline: x8WebArc: a ``time.monotonic()`` time the whole call answers
            by (a skill's rung passes its request's time left): every wait
            below - the start, the page, the window, bringing it forward (at
            most :data:`_DEADLINE_FOCUS_FLOOR_S` over) - ends by it. ``None``:
            the waits above only (on 10 Oct an Arc launch took 26-34 s).

    Handed a web address while it was already running, the main page of each
    of its windows is read right before the hand-off (:func:`_pages_before`):
    a page a window already showed is never the one opened, and when its
    windows show pages but none newly at the address the result is
    ``ok=False`` with ``details["reason"] = "page_unchanged"`` (Arc ignored
    the address and kept its old page, or showed a YouTube tab it already had).
    A window that already showed the very page asked for and still does is
    ``ok=True`` with ``details["page_state"] = "already"`` (review r1 M1).
    lSkills review r1 M1: a package whose protocol activation Windows refused
    (``details["no_protocol"]``), started through its alias or
    ``ActivateApplication``, is watched for the page :data:`_REFUSED_PAGE_S`
    from the hand-off (running) or from its first window (cold), is never
    handed the address again the same way, and windows of it showing no page
    at all are ``ok=False`` with ``details["reason"] = "page_blank"``.

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
    # names21 (S18): the user's own app or site the name given was taken as (:func:`_measured_name`), else empty
    measured: dict = {}

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        if "verified" not in details:
            details["verified"] = _launch_verdict(ok, details)
        if measured:
            details["names_match"] = dict(measured["log"])   # content-free: kind, how, score, margin, source
            line = MEASURED_OPENED_LINE if ok else MEASURED_TAKEN_LINE
            summary = line.format(name=measured["name"], query=measured["query"]) + summary
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
    # D17: no app is launched, shown or brought forward while a game is in front (re-checked before each step below)
    if (refused := _gate.refuse("launch")) is not None:
        return finish(False, refused, {"launched": False, "hwnd": None, "candidates": [],
                                       "refused": _gate.kind_of(refused)})
    focus_floor = _FOCUS_FLOOR_S
    if deadline is not None:
        # x8WebArc: the caller's deadline bounds every wait of the call (a skill rung answers within its request)
        left_s = max(0.0, float(deadline) - time.monotonic())
        timeout_s = max(0.5, min(float(timeout_s), left_s))
        content_timeout_s = max(0.0, min(float(content_timeout_s), left_s))
        focus_floor = _DEADLINE_FOCUS_FLOOR_S
    # Where the time goes, stage by stage (the 25 Sept "open YouTube on Chrome" took 16.4 s
    # and the log could not say where): ms since the call started, per stage end.
    timings: dict[str, float] = {}

    def mark(stage: str) -> None:
        timings[stage] = round((time.perf_counter() - started) * 1000.0, 1)

    apps = get_start_apps()
    mark("start_menu")
    chosen, candidates = _match_apps(query, apps)
    if chosen is None and not candidates:
        # names21 (final-D S18; the user's decision 3): a name no Start entry goes by ("the scored", "Porbex Kaladra")
        # that the measure takes, clearly, for one of the user's own apps or sites is that one, opened and named in
        # the summary; none clearly: as before (the model picks)
        took = _measured_name(query, apps, sites=not arg_list)
        mark("names")
        if took is not None:
            measured.update(took)
            if took["kind"] == "site":
                return _open_measured_site(took, finish, content_timeout_s=content_timeout_s, deadline=deadline)
            chosen = took["entry"]
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
        "timings_ms": timings,
    }
    start_budget = max(2.0, min(timeout_s, 15.0))
    if deadline is not None:
        start_budget = max(1.0, min(start_budget, float(deadline) - time.monotonic()))
    if not arg_list:
        # appOpen F1: a Start entry that has another program start its app (a game through its launcher) is started
        # the way the Start menu does and only the app's own window counts - never the launcher's, never a hidden
        # tray window of it (5 Oct: "open Valorant" showed the Riot Client's tray window and called it done)
        try:
            launcher = _launcher_entry(chosen, apps)
        except Exception as exc:  # noqa: BLE001 - then it launches as any other entry
            launcher = None
            details["launcher_error"] = f"{type(exc).__name__}: {exc}"
        mark("launcher")
        if launcher is not None:
            ok, summary = _launch_via_launcher(chosen, launcher, timeout_s, details)
            mark("launcher_wait")
            return finish(ok, summary, details)
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
        mark("plan")
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
    # x8WebArc: how it was started, as a code word a content-free log keeps (``via`` itself is a Windows name)
    details["via_word"] = VIA_WORDS.get(str(details.get("via") or ""), "" if arg_list else "start_menu")

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
    mark("identity")

    if not arg_list and already_running:
        # Running, but with no window on screen (closed to the background or the
        # tray): show its own hidden window, or ask it for a new one.  Starting
        # its Start menu entry again is not enough - a running instance may
        # just stay in the background.
        app_hwnd, state = _find_app_window(identity, running_before)
        if state in ("hidden", ""):
            ok, summary = _restore_background_app(
                chosen, identity, app_hwnd, running_before, details, timeout_s
            )
            details["identity"] = identity.describe()
            return finish(ok, summary, details)

    # x8WebArc: handed a web address while running, what each of its windows shows now is read first, so a page it
    # already showed is never taken for the one opened (10 Oct 20:55: Arc kept an old YouTube tab, "verified")
    page_targets = [address for address in (_page_address(arg) for arg in arg_list) if address]
    unchanged: dict[int, str] = {}
    if page_targets and already_running:
        budget = _PAGE_BEFORE_S if deadline is None else min(
            _PAGE_BEFORE_S, 0.25 * max(0.0, float(deadline) - time.monotonic()))
        unchanged = _pages_before(identity, budget)
        details["pages_before"] = len(unchanged)
        # the pages themselves stay in RAM with the result (a caller's own check compares with them; never logged)
        details["before_pages"] = [{"hwnd": hwnd, "url": url} for hwnd, url in unchanged.items()]
        mark("pages_before")
    before, before_pids, before_titles = _window_snapshot()
    before_foreground = _user32.GetForegroundWindow()
    def hand_over(into: dict) -> tuple[int | None, str]:
        """Start the app with its arguments (writes ``launched`` and the start's facts into ``into``): ``(pid,
        failure)``. percFix: also used once more to hand the address again (see below)."""
        launched_pid: int | None = None
        if (refused := _gate.refuse("launch")) is not None:   # D17: re-checked before every hand-over
            into["launched"] = False
            into["refused"] = _gate.kind_of(refused)
            return None, refused
        if plan is None:
            # shell:AppsFolder\<AppID> is the Start menu's own launch path and works
            # for both packaged apps (AppUserModelID) and desktop shortcuts.
            result = run_powershell(
                f"Start-Process -FilePath {_quote('shell:AppsFolder\\' + chosen['appid'])}",
                timeout_s=start_budget,
            )
            into["launched"] = result.ok
            into["launch_stderr"] = result.details.get("stderr", "")
            failure = result.details.get("stderr") or result.summary
        elif "command" in plan:
            # Start-Process on a desktop program file or on an App Execution Alias.
            result = run_powershell(plan["command"], timeout_s=start_budget)
            into["launched"] = result.ok
            into["launch_stderr"] = result.details.get("stderr", "")
            stdout = str(result.details.get("stdout") or "").strip()
            last_line = stdout.splitlines()[-1].strip() if stdout else ""
            into["pid"] = int(last_line) if last_line.isdigit() else None
            launched_pid = into["pid"]
            failure = result.details.get("stderr") or result.summary
        else:
            pid, error = _activate_packaged(
                plan["aumid"], plan["arguments"], start_budget, uris=plan.get("uris")
            )
            into["launched"] = pid is not None
            into["pid"] = pid
            launched_pid = pid
            if error:
                into["launch_error"] = error
                if (code := _hresult_of(error)) is not None:
                    into["launch_hresult"] = f"0x{code:08X}"
            failure = _activation_failure(chosen["name"], str(plan["via"]), error)
        return launched_pid, failure

    launched_pid, failure = hand_over(details)
    mark("start")
    if (not details["launched"] and plan is not None and plan.get("via") == "ActivateForProtocol"
            and not _gate.carried(details) and _hresult_of(str(details.get("launch_error") or "")) is not None):
        # only a refusal Windows answered with an HRESULT: an activation that merely didn't return in time may still
        # deliver the address, and a second hand-off would open it twice
        # lSkills (live 17 Oct): Windows refused protocol activation (Arc: 0x80270254 on every open; the model then
        # said "Arc isn't installed" or opened the page in Chrome). That is a fact about the installed package: kept
        # for this process, and the arguments go the next way at once - its App Execution Alias, else
        # ActivateApplication - with the page checked as before (a running Arc that ignores what its alias carries
        # comes back ``page_unchanged``, and ``open_url``'s address-bar step takes over)
        with _no_protocol_lock:
            _NO_PROTOCOL.add(str(chosen["appid"]).lower())
        refused = {"via": "protocol", "hresult": details.pop("launch_hresult", ""), "error": failure}
        details.pop("launch_error", None)
        again, again_notes = _plan_start_with_args(chosen, arg_list, protocol=False)
        if again is not None:
            plan = again
            details["protocol_refused"] = refused
            details["resolution_notes"] = list(details.get("resolution_notes") or []) + again_notes
            details["via"] = plan["via"]
            details["arguments"] = plan["arguments"]
            details.pop("uris", None)
            if "command" in plan:
                details["exe"] = plan["exe"]
                details["exe_source"] = plan["source"]
                details["start_command"] = plan["command"]
            details["via_word"] = VIA_WORDS.get(str(plan["via"]), "")
            launched_pid, failure = hand_over(details)
            mark("start_again")
    if not details["launched"]:
        return finish(False, f"could not start {chosen['name']!r}: {failure}", details)
    # review r1 M1: a package whose protocol activation Windows refused (now, or earlier in this process), its address
    # carried by its alias or ActivateApplication instead: Arc drops what a cold start carries (percFix, 5 Oct: its
    # new-tab view) and ignores what a running instance's alias carries (3, 5 and 10 Oct), so its page is watched
    # briefly, never handed again the same way, and windows showing no page are "not opened" (``page_blank``)
    no_protocol = bool(details.get("protocol_refused")) or (
        plan is not None and plan.get("via") in ("AppExecutionAlias", "ActivateApplication")
        and str(chosen.get("appid") or "").lower() in _NO_PROTOCOL)
    if no_protocol:
        details["no_protocol"] = True

    handed_off = time.monotonic()
    window_deadline = handed_off + timeout_s
    if deadline is not None:
        window_deadline = min(window_deadline, float(deadline))
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
    # Handed a web address: success is a window of the app showing a page at
    # that address - not its window title changing, which for some browsers
    # never happens whatever page they show.
    page_miss: tuple[bool, str] | None = None
    if page_targets:
        details["page_targets"] = page_targets
        content_deadline = handed_off + content_timeout_s + (0.0 if already_running else timeout_s)
        if no_protocol and already_running:
            # lSkills: a running package that refused protocol activation got the address through its alias, which Arc
            # ignored on 3, 5 and 10 Oct: a browser that takes it shows the page within the react time, so the
            # address bar (open_url) isn't kept waiting the whole content wait behind it
            content_deadline = min(content_deadline, handed_off + _REFUSED_PAGE_S)
        elif no_protocol and identity.confirmable:
            # review r1 M1: started cold (Arc's usual state): its window first, by a window list alone, then the page
            # for the same short watch from then on - not the whole 14 s cold page wait for an address a cold Arc drops
            up_at = _await_window_up(identity, before, content_deadline if deadline is None
                                     else min(content_deadline, float(deadline)))
            mark("window_up")
            if up_at is not None:
                details["window_up_ms"] = round((up_at - handed_off) * 1000.0, 1)
                content_deadline = min(content_deadline, up_at + _REFUSED_PAGE_S)
        if deadline is not None:
            content_deadline = min(content_deadline, float(deadline))
        if unchanged and any(_same_page(target, url) and not _past_root(target, url)
                             for url in unchanged.values() for target in page_targets):
            # fSkills: a window already showed the very page asked for before the hand-off: what it shows can't change
            # to it, so the wait is a short look (_ALREADY_PAGE_S), not the whole content wait, and the page is
            # reported at once (as "already", below, unless another window newly shows it)
            content_deadline = min(content_deadline, handed_off + _ALREADY_PAGE_S)
            details["page_already_before"] = True
        page_wait = _await_page(identity, page_targets, before, content_deadline - time.monotonic(),
                                unchanged=unchanged)
        mark("page")
        details["page_check"] = _page_details(page_wait)
        details["identity"] = identity.describe()
        if page_wait is not None and page_wait.matched is not None:
            details["waited_ms"] = round((time.monotonic() - handed_off) * 1000.0, 1)
            return finish(
                True,
                _page_opened(
                    chosen["name"], page_wait, before, details, content_deadline,
                    page_targets, identity, focus_floor_s=focus_floor,
                ),
                details,
            )
        if (page_wait is not None and page_wait.windows and not page_wait.seen and not already_running
                and not no_protocol   # review r1 M1: the same alias again, which a running Arc ignores (6 s for nothing)
                and (deadline is None or float(deadline) - time.monotonic() >= _AGAIN_MIN_S)
                and identity.running_pids()):
            # percFix (live 5 Oct): Arc started cold with an address came up on its own new-tab view and never
            # showed the page - the address handed to it at its start was dropped - and the agent spent 111 s and
            # 11 model calls typing it in. Its windows are up and show no page at all, and the app runs now: the
            # address is handed once more, to the running app, and the page waited for again.
            again: dict = {}
            handed_again = time.monotonic()
            _again_pid, again_failure = hand_over(again)
            details["handed_again"] = {"launched": bool(again.get("launched"))}
            if not again.get("launched"):
                details["handed_again"]["error"] = str(again_failure)[:160]
            else:
                again_deadline = handed_again + content_timeout_s
                if deadline is not None:
                    again_deadline = min(again_deadline, float(deadline))
                page_wait = _await_page(identity, page_targets, before, again_deadline - time.monotonic())
                mark("page_again")
                details["page_check_again"] = _page_details(page_wait)
                if page_wait is not None and page_wait.matched is not None:
                    details["waited_ms"] = round((time.monotonic() - handed_off) * 1000.0, 1)
                    opened = _page_opened(chosen["name"], page_wait, before, details,
                                          again_deadline, page_targets, identity, focus_floor_s=focus_floor)
                    return finish(True, f"{opened} (the address was handed again once {chosen['name']} was "
                                        f"running: its first start showed no page)", details)
        already_page = _already_page(page_wait, unchanged, page_targets)
        if already_page is not None:
            # review r1 M1: no page newly at the address, but a window that already showed the very page asked for
            # still shows it: the page is up (brought forward), said as "already", never as a page opened
            page_wait.matched = already_page
            details["waited_ms"] = round((time.monotonic() - handed_off) * 1000.0, 1)
            return finish(
                True,
                _page_opened(chosen["name"], page_wait, before, details, content_deadline, page_targets, identity,
                             focus_floor_s=focus_floor, already=True),
                details,
            )
        details["page_verified"] = False if page_wait is not None and page_wait.seen else None
        still = _unchanged_windows(page_wait, unchanged)
        page_miss = _page_miss(chosen["name"], page_targets, page_wait, still)
        if not page_miss[0]:
            # x8WebArc: its windows show pages and none newly at the address (the old page kept, or another
            # host): not opened, so a skill goes on to its next rung instead of counting this as acted
            details["reason"] = "page_unchanged"
            details["page_state"] = "same" if still else "other"
        elif no_protocol and page_wait is not None and (page_wait.windows or already_running):
            # (reached only when no window shows a main page: _page_miss said "not opened" for any that does)
            # review r1 M1: its windows are up and show no page at all after a start through its alias or
            # ActivateApplication (Windows refused it the address by protocol activation; a cold Arc shows its
            # new-tab view): not opened, so open_url's address bar or the web skill's next rung types it, instead of
            # an "opened, not known" that the skill counted as acted and handed to the thinker 6 s later.
            # chat19 (all-browser R8: a running Arc handed the address by its alias came back "unread", counted as
            # acted, and the request waited for a page that never came): its watch is already capped at its react
            # time (``_REFUSED_PAGE_S``, above); when that ends with no main page in any window (a second page beside
            # none, or none of its windows on screen while it runs: the listing skips minimised ones) it is
            # "not opened" too, so the address-bar rung runs (it brings the window up) and the ignored hand-off is
            # noted. Arc's pages read whenever one shows (final test 19 Oct), so no page means none opened.
            details["reason"] = "page_blank"
            details["page_state"] = "blank"
            page_miss = (False, (
                f"handed {chosen['name']} {page_targets[0]} through {details.get('via_word') or details.get('via')}, "
                f"but after {page_wait.waited_ms / 1000.0:.1f} s its windows show no page at all (Windows would not "
                f"hand it the address by protocol activation): the address did not open"))
        # The address is not on screen.  What did change, if anything, is
        # looked up once, without waiting any longer.
        window_deadline = time.monotonic() + 2 * _POLL_S
        react = None
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
            page_miss[0] if page_miss else True,
            _no_window_outcome(
                chosen,
                identity,
                details,
                arg_list,
                already_running,
                waited_s,
                window_deadline,
                lead=page_miss[1] if page_miss else None,
                focus_floor_s=focus_floor,
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
    forward = _bring_forward(hwnd, window_deadline, details, floor_s=focus_floor)
    content_note = ""
    if arg_list and not page_miss:
        # It was handed something to open: wait (bounded) until that is on
        # screen and readable, so the next look sees it rather than the frame.
        content_wait = content_timeout_s if deadline is None else max(
            0.0, min(content_timeout_s, float(deadline) - time.monotonic()))
        content_note = _wait_for_opened_content(hwnd, content_wait, details)
        if (fresh := window_info(hwnd)) is not None:
            details["title"] = fresh.title
    same_app = [w for w in details["new_windows"] if w["is_app_window"]]
    extra = len(same_app) - 1
    unrelated = len(details["new_windows"]) - len(same_app)
    with_args = ""
    if arg_list:
        shown = details["arguments"]
        with_args = f" with arguments {shown if len(shown) <= 160 else shown[:160] + '…'}"
    if page_miss:
        # The page check is the fact that matters; the window that changed is
        # reported after it.
        return finish(
            page_miss[0],
            f"{page_miss[1]}; hwnd {hwnd} ({details['process_name']}) \"{details['title']}\" "
            f"- {reason}; {forward}",
            details,
        )
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
    *,
    lead: str | None = None,
    focus_floor_s: float | None = None,
) -> str:
    """The summary (and ``details``) for a launch after which no window of the
    app appeared, changed or came forward by itself.

    The app's own window is looked up: one on screen or minimized is brought to
    the front, a hidden one (an app sitting in the tray) is shown first.  Only
    ever a window of the app - when it has none, the summary says so.  ``lead``
    replaces the opening clause (the page check's verdict, when the app was
    handed a web address).
    """
    name = repr(chosen["name"])
    via = details.get("via")
    if lead:
        pass  # the caller's verdict (the page check) opens the summary
    elif arg_list and already_running:
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
        if getattr(identity, "via_handler", False):
            # final check V3-wording: the program that shows it always runs for other things (Explorer)
            program = os.path.basename(sorted(identity.exe_paths or [""])[0]) or "the program that shows it"
            return f"{lead}; {program}, which shows it, is shared with other windows, and no new window of it came"
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
    forward = _bring_forward(app_hwnd, window_deadline, details,
                             **({} if focus_floor_s is None else {"floor_s": focus_floor_s}))
    return (
        f"{lead}; {how}: hwnd {app_hwnd} ({details['process_name']}) "
        f"\"{details['title']}\"; {forward}"
    )


#: Least time :func:`_bring_forward` gets, however little of the launch budget
#: the window's appearance left: :func:`focus_window`'s own default.
_FOCUS_FLOOR_S = 2.0
#: x8WebArc: the same floor for a call that has a deadline of its own (``deadline=``): a skill's rung gets its whole
#: answer within the request's time, so the focus may run over it by this much at most.
_DEADLINE_FOCUS_FLOOR_S = 0.5


def _bring_forward(hwnd: int, deadline: float, details: dict, *, floor_s: float = _FOCUS_FLOOR_S) -> str:
    """Activate ``hwnd`` as :func:`focus_window` does and say how that went.

    Budget: what is left until ``deadline``, but at least ``floor_s``
    (:data:`_FOCUS_FLOOR_S`; a call with a deadline of its own passes
    :data:`_DEADLINE_FOCUS_FLOOR_S`).
    Fills ``details`` with ``foreground``, ``ready``, ``ready_ms`` and ``focus``
    (the focus attempt's own details).

    Returns:
        A clause for the summary.
    """
    budget = max(deadline - time.monotonic(), floor_s)
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
    if _gate.refuse("focus", hwnd=hwnd) is not None:   # D17: never over a game, never into one
        return False
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
    # D17: no focus, restore or activation with a game in front or into a game (re-checked before every attempt)
    if (refused := _gate.refuse("focus", hwnd=hwnd)) is not None:
        return finish(False, refused, {"hwnd": hwnd, "refused": _gate.kind_of(refused)})

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
        if (refused := _gate.refuse("focus", hwnd=hwnd)) is not None:
            details["refused"] = _gate.kind_of(refused)   # a game came to the front meanwhile: no further try
            return finish(False, refused, details)
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


#: ``window_action`` verbs and the ``ShowWindow`` command each one sends.
_SHOW_COMMANDS = {"minimize": win32con.SW_MINIMIZE, "maximize": win32con.SW_MAXIMIZE,
                  "restore": win32con.SW_RESTORE}
#: Bound on the wait for a window to reach the state it was asked for (a condition poll).
_STATE_WAIT_S = 1.0
#: Bound on the wait for a window to go full screen after its F11 (a condition poll).
_FULLSCREEN_WAIT_S = 1.5


def _is_zoomed(hwnd: int) -> bool:
    """Maximized (``IsZoomed``). pywin32's win32gui has no IsZoomed: live 2026-09-29 every maximize by the voice's
    window hand failed with an AttributeError."""
    return bool(_user32.IsZoomed(hwnd))


def _is_fullscreen(hwnd: int) -> bool:
    """Full screen, measured: the window covers its whole monitor with no caption (not the same as maximized)."""
    from yuki.perception.windows import _covers_monitor_without_caption

    try:
        return _covers_monitor_without_caption(hwnd, tuple(win32gui.GetWindowRect(hwnd)))
    except Exception:  # noqa: BLE001
        return False


def _fullscreen(hwnd: int, name: str, details: dict, finish) -> ActionResult:  # noqa: ANN001
    """F11, the common full-screen key, then a check that the window really covers its monitor (v3.13).

    Live 2026-09-29 "make Warp full screen" was answered "Warp's full screen" after a maximize: maximized is not
    full screen. ``ok`` only when full screen was observed; otherwise the report says what the window is now.
    fa (A4): full screen is an ask to see the window: it comes to the front first (gated: :func:`focus_window`), and
    ``ok`` also needs it in view after (:func:`_in_view`: in front, and enough of it seen), already full screen too.
    """
    details["was_fullscreen"] = _is_fullscreen(hwnd)
    if _user32.GetForegroundWindow() != hwnd:
        focused = focus_window(hwnd)
        if not getattr(focused, "ok", False):
            return finish(False, f"{name}: could not bring it to the front"
                                 f"{'' if details['was_fullscreen'] else ' for its full-screen key'}: "
                                 f"{getattr(focused, 'summary', '')}", details)
    if details["was_fullscreen"]:
        behind = _in_view(hwnd, details)
        if behind:
            return finish(False, f"{name} is already full screen, but {behind}", details)
        return finish(True, f"{name} is already full screen", details)
    from yuki.actions.input import hotkey

    pressed = hotkey("f11", expect_hwnd=hwnd)
    details["key"] = getattr(pressed, "summary", "")
    if not getattr(pressed, "ok", False):
        return finish(False, f"{name}: F11 not sent: {details['key']}", details)
    deadline = time.monotonic() + _FULLSCREEN_WAIT_S
    while not _is_fullscreen(hwnd) and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    if _is_fullscreen(hwnd):
        behind = _in_view(hwnd, details)   # fa (A4): verified by front and visible share too
        if behind:
            return finish(False, f"{name} is now full screen (F11), but {behind}", details)
        return finish(True, f"{name} is now full screen (F11)", details)
    now = "maximized, not full screen" if _is_zoomed(hwnd) else "not full screen"
    return finish(False, f"{name}: pressed F11, still {now} after {_FULLSCREEN_WAIT_S:g} s (this app may use its "
                         f"own full-screen command)", details)
#: Bound on the wait for a window asked to close to go away (percFix: 2 s was too short for a browser saving its
#: session; a window that asks something first ends the wait at once, see :func:`_asks_before_closing`).
_CLOSE_WAIT_S = 8.0
#: How long a window asked to close must keep showing it is asking something before the wait ends on it (s).
_ASKING_HOLD_S = 0.8


def _is_cloaked(hwnd: int) -> bool:
    """DWM keeps the window but no longer shows it (a closing app's window can sit cloaked until it is destroyed)."""
    try:
        from yuki.perception.windows import is_cloaked

        return bool(is_cloaked(hwnd))
    except Exception:  # noqa: BLE001
        return False


def _asks_before_closing(hwnd: int) -> bool:
    """Whether a window asked to close shows it is asking something: it is disabled (a modal dialog of its own owns
    the input), or the window in front is owned by it (its own dialog). Another window of the same app coming to the
    front is not that: closing one of two browser windows brings the other forward. Window facts only."""
    try:
        if not win32gui.IsWindowEnabled(hwnd):
            return True
        front = int(_user32.GetForegroundWindow() or 0)
        if not front or front == hwnd:
            return False
        return int(win32gui.GetWindow(front, win32con.GW_OWNER) or 0) == int(hwnd) and bool(
            win32gui.IsWindowVisible(front))
    except Exception:  # noqa: BLE001
        return False


def _asking_window(hwnd: int, before: set[int] | frozenset[int] = frozenset()) -> int:
    """fa (A1): the window a window asked to close is asking with: one it owns that was not up before the close
    (``before``: ``tabs.owned_windows`` read right before it), in front or a user window (``tabs.raised_window``);
    ``0`` when none is known (the close is then reported asking, as before, with no window named). Window facts only;
    never raises."""
    try:
        from yuki.actions.tabs import raised_window

        return int(raised_window(int(hwnd), before) or 0)
    except Exception:  # noqa: BLE001
        return 0


#: fa (A4, final-F p1_dt_asked_place_maximize_spotify: "Spotify's maximized." while it sat behind Chrome, share 0): a
#: window asked to be maximised, restored or made full screen is in view when it is in front (the one front rule,
#: ``situation.front_window``) and at least this share of it is seen (the occlusion grid's share, 0..1).
_SHOWN_SHARE = 0.5


def _shown(hwnd: int) -> tuple[bool, float | None, int]:
    """fa (A4): ``(in front, visible share or None when unread, the window in front)`` of ``hwnd`` now: the one front
    rule (``situation.front_window``: an unlisted foreground stands for the listed window holding the keyboard or its
    owner) and the occlusion grid's share (``visibility.layout``). Read-only; never raises."""
    try:
        from yuki.perception.situation import front_window

        front = int(front_window()[0] or 0)
    except Exception:  # noqa: BLE001
        front = int(_user32.GetForegroundWindow() or 0)
    share: float | None = None
    try:
        from yuki.perception.visibility import layout

        surface = layout().by_hwnd(int(hwnd))
        share = float(surface.visible_share) if surface is not None else 0.0
    except Exception:  # noqa: BLE001 - not read: the front alone decides
        share = None
    return front == int(hwnd), share, front


def _in_view(hwnd: int, details: dict) -> str:
    """fa (A4): ``""`` when ``hwnd`` is in view (:func:`_shown`), else the measured words of where it is (behind the
    window in front, the share of it seen); the facts go in ``details`` (``front``, ``visible``: numbers only)."""
    front, share, now = _shown(hwnd)
    details["front"], details["visible"] = front, (round(share, 3) if share is not None else None)
    if front and (share is None or share >= _SHOWN_SHARE):
        return ""
    info = window_info(now) if now and now != int(hwnd) else None
    over = f" behind {info.process_name}" if info is not None and not front else ""
    seen = f" ({int(round((share or 0.0) * 100))}% of it visible)" if share is not None else ""
    details["behind"] = True
    return f"it is not in view: it is{over or ' not in front'}{seen}"


def window_action(hwnd: int, action: str) -> ActionResult:
    """Minimize, maximize, restore, close or focus one top-level window, and verify it happened.

    ``focus`` is :func:`focus_window`. ``minimize`` / ``maximize`` / ``restore``
    send ``ShowWindow`` and then wait (a condition poll, at most
    :data:`_STATE_WAIT_S`) until ``IsIconic`` / ``IsZoomed`` report the new state;
    ``close`` posts ``WM_CLOSE`` (what the window's own X does: the app may ask
    to save) and waits up to :data:`_CLOSE_WAIT_S` for the window to go. ``ok``
    is True only when the new state was observed, so the summary is the effect,
    not the request: "Discord.exe "..." is now minimized", or "... asked to close;
    it is asking something first" (a dialog of its own in front), or "still open after 8 s".

    fa (A4): ``maximize``, ``restore`` and ``fullscreen`` are asks to see the window: it is brought to the front first
    (:func:`focus_window`, gated), and ``ok`` needs it in view afterwards too (in front by the one front rule, and at
    least :data:`_SHOWN_SHARE` of it seen: ``details["front"]``, ``["visible"]``, ``["behind"]``); a window left behind
    another is reported where it is, never as done. fa (A1): a close the window asks about first carries the asking
    window's id (``details["dialog_raised"]``).
    """
    started = time.perf_counter()

    def finish(ok: bool, summary: str, details: dict) -> ActionResult:
        if "window_state_before" in details:
            # v3.14b: how the window is shown now, measured (normal / maximized / fullscreen / minimized), next to
            # how it was: the same fact a key's report carries, so every window change is read the same way.
            from yuki.perception.windows import window_state

            after = window_state(hwnd) if action != "close" else None
            details["window_state_after"] = after
            details["window_state_changed"] = after is not None and after != details["window_state_before"]
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    details: dict = {"hwnd": hwnd, "action": action}
    if action == "focus":
        return focus_window(hwnd)
    if action not in (*_SHOW_COMMANDS, "close", "fullscreen"):
        return finish(False, f"unknown window action {action!r} (minimize, maximize, fullscreen, restore, close, "
                             f"focus)", details)
    if not win32gui.IsWindow(hwnd):
        return finish(False, f"hwnd {hwnd} is not a window (it may have closed)", details)
    # D17: no window action ever on a game's own window; with a game in front only minimising another window (shown
    # without activating anything) is allowed, never maximise, restore, full screen or close (a save prompt would
    # come up over the game)
    if (refused := _gate.refuse(f"window_{action}", hwnd=hwnd, front_ok=action == "minimize")) is not None:
        details["refused"] = _gate.kind_of(refused)
        return finish(False, refused, details)
    info = window_info(hwnd)
    name = f"{info.process_name} \"{info.title}\"" if info else f"hwnd {hwnd}"
    details.update(title=info.title if info else "", process_name=info.process_name if info else "",
                   was_minimized=bool(win32gui.IsIconic(hwnd)), was_maximized=bool(_is_zoomed(hwnd)))
    from yuki.perception.windows import window_state

    details["window_state_before"] = window_state(hwnd)
    if action == "fullscreen":
        return _fullscreen(hwnd, name, details, finish)
    if action == "close":
        was_cloaked = _is_cloaked(hwnd)
        from yuki.actions.tabs import owned_windows

        owned_before = owned_windows(hwnd)   # fa (A1): its own windows before, so the one the close raises is told
        win32gui.PostMessage(hwnd, win32con.WM_CLOSE, 0, 0)
        # percFix: live 5 Oct Arc took longer than 2 s to close and was reported "still open" (it had closed a few
        # seconds later). The wait is longer now, and it ends at once on what "it is asking something" looks like:
        # the window disabled behind a dialog of its own (a modal save prompt), or a window it owns in front, for
        # :data:`_ASKING_HOLD_S` on end (an app can disable its window for a moment while it shuts down).
        deadline = time.monotonic() + _CLOSE_WAIT_S
        asking = False
        asking_since: float | None = None
        while time.monotonic() < deadline:
            if (not win32gui.IsWindow(hwnd) or not win32gui.IsWindowVisible(hwnd)
                    or (not was_cloaked and _is_cloaked(hwnd))):
                return finish(True, f"{name} is closed", details)
            if _asks_before_closing(hwnd):
                asking_since = asking_since if asking_since is not None else time.monotonic()
                asking = time.monotonic() - asking_since >= _ASKING_HOLD_S
                if asking:
                    break
            else:
                asking_since, asking = None, False
            time.sleep(_POLL_S)
        waited = round(time.monotonic() - (deadline - _CLOSE_WAIT_S), 1)
        details["close_waited_s"] = waited
        if asking:
            details["asking"] = True
            # fa (A1): the window it asks with, by id (the commit check's measured fact that the close raised it)
            details["dialog_raised"] = _asking_window(hwnd, owned_before) or None
            if details["dialog_raised"]:
                from yuki.actions.tabs import note_raised

                note_raised(int(details["dialog_raised"]), int(hwnd))
            return finish(False, f"{name}: asked to close; it is asking something first (a dialog of its own is in "
                                 f"front). Nothing in it was answered: what it asks is theirs to answer", details)
        return finish(False, f"{name}: asked to close, still open after {_CLOSE_WAIT_S:g} s", details)

    def reached() -> bool:
        iconic, zoomed = bool(win32gui.IsIconic(hwnd)), bool(_is_zoomed(hwnd))
        return {"minimize": iconic, "maximize": zoomed and not iconic, "restore": not iconic and not zoomed}[action]

    state = {"minimize": "minimized", "maximize": "maximized", "restore": "restored"}[action]
    shows = action in ("maximize", "restore")
    already = reached()   # before any front step: a minimised window the focus restores was not "already" restored
    if shows and _user32.GetForegroundWindow() != hwnd:
        # fa (A4, final-F one p1_dt_asked_place_maximize_spotify: ShowWindow from a background process maximised Spotify
        # behind Chrome and it was said as done): maximize and restore are asks to see the window, so it comes to the
        # front first, through the gated focus (a game in front, or into a game, refuses it there); nothing else is
        # done when it can't
        focused = focus_window(hwnd)
        if not getattr(focused, "ok", False):
            fdetails = getattr(focused, "details", None) or {}
            if fdetails.get("refused"):
                details["refused"] = fdetails["refused"]
            return finish(False, f"{name}: could not bring it to the front to be {state}: "
                                 f"{getattr(focused, 'summary', '')}; nothing else was done", details)
    if not reached():
        command = _SHOW_COMMANDS[action]
        if action == "minimize" and _gamegate.game_in_front():
            command = win32con.SW_SHOWMINNOACTIVE   # D17: minimised without activating the next window over the game
        win32gui.ShowWindow(hwnd, command)
    deadline = time.monotonic() + _STATE_WAIT_S
    while not reached() and time.monotonic() < deadline:
        time.sleep(_POLL_S)
    if reached():
        front = _user32.GetForegroundWindow() == hwnd
        # Maximized is not full screen (v3.13): the report says which it is, so neither voice claims the other.
        full = " and full screen" if action == "maximize" and _is_fullscreen(hwnd) else (
            " (not full screen)" if action == "maximize" else "")
        done = f"{name} is {'already ' if already else 'now '}{state}{full}"
        behind = _in_view(hwnd, details) if shows else ""
        if behind:
            # fa (A4): the state alone is not the ask: not in view is not done (no done line; the report says where)
            return finish(False, f"{done}, but {behind}", details)
        return finish(True, done + ("; in the foreground" if front else ""), details)
    return finish(False, f"{name}: asked to be {state}, not {state} after {_STATE_WAIT_S:g} s", details)


#: ``ASSOCSTR_EXECUTABLE``, ``ASSOCF_NOTRUNCATE`` and ``ASSOCF_IS_PROTOCOL`` for
#: ``AssocQueryStringW``.
_ASSOCSTR_EXECUTABLE = 2
_ASSOCF_NOTRUNCATE = 0x20
_ASSOCF_IS_PROTOCOL = 0x1000

#: How long :func:`open_url` watches for the default handler's window to appear,
#: come to the front or change its title.  A bound on a condition: the watch
#: ends as soon as one of those happens, which is the common case.
_HANDLER_WINDOW_S = 3.0


def _handler_path(target: str) -> str:
    """The program (full path) Windows opens ``target`` with, or ``""``.

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
    return buffer.value


def _default_handler(target: str) -> str:
    """Lowercased image name of the program Windows opens ``target`` with, or ``""`` (:func:`_handler_path`)."""
    return os.path.basename(_handler_path(target)).lower()


# ---------------------------------------------------------------------------
# lSkills (live 17 Oct): the address typed into the browser's own address bar
# ---------------------------------------------------------------------------
#: The keys every desktop browser opens a new tab with, its address field focused (in Arc they open its command bar,
#: which takes an address the same way: the path that worked in Arc on 5 Oct). The same fact as
#: ``yuki/skills/connections/web.toml``'s ``new_tab``.
_NEW_TAB_KEYS: tuple[str, ...] = ("ctrl", "t")
#: How long the address-bar step watches for the page after Enter (at least :data:`_BAR_PAGE_MIN_S`), and the least
#: time the whole step gets however much of a caller's deadline the hand-off before it spent.
_BAR_PAGE_S = 4.0
_BAR_PAGE_MIN_S = 1.5
_BAR_FLOOR_S = 6.0
#: How many address hand-offs in a row an app may ignore (keep the very page it had) before :func:`open_url` types the
#: address at once (the hand-off's page wait skipped), and for how long that holds. One when Windows already refused
#: the app's protocol activation (its link path failed once), else two: a single slow page must not turn a working
#: browser's link path into keystrokes (x8WebArc review r1 L1).
#:
#: desk22 (A16, final-D browser B4: the note lived in RAM, so the first Arc open after every start paid the whole
#: ignored hand-off again): the note is a measured per-browser fact kept across restarts in a small state file in
#: Yuki's own data folder (:data:`HANDOFF_FILE_NAME`: app ids, counts, times and the refused-protocol mark only; no
#: address, no page). It is capped and expired by measure: at most :data:`_IGNORED_CAP` browsers (the oldest go), each
#: count capped at :data:`_IGNORED_COUNT_MAX`, cleared the moment a hand-off is seen to open its page, and held only
#: :data:`_IGNORED_KEEP_S` after the last ignored hand-off measured. Once it has expired the next hand-off is measured
#: again with the full page wait (an updated browser may take addresses now; desk22 review L1: a measure, so a slow but
#: working hand-off is never followed by a typed second tab): ignored again, the note holds again at once; opened, it
#: is cleared. The web skill's own hand-off (``address_bar=False``) to a browser the note holds gets its page wait
#: capped at :data:`_NOTED_PAGE_S` (as a refused package's watch: the skill's next rung types it); the skill measures
#: that hand-off into the note itself (:func:`note_handoff`, review M3: one writer, never counted twice).
_IGNORED_AFTER = 2
_IGNORED_KEEP_S = 7 * 86400.0
_IGNORED_CAP = 16
_IGNORED_COUNT_MAX = 8
_NOTED_PAGE_S = 3.0
HANDOFF_FILE_NAME = "browser_handoffs.json"
#: Where the note is kept: ``None`` = :func:`yuki.core.paths.data_dir`'s :data:`HANDOFF_FILE_NAME` (a checker or a
#: test sets a path of its own here, or ``YUKI_HANDOFF_FILE``, so the user's file is never touched).
HANDOFF_FILE: str | None = None
#: ``{appid lowercased: (count, wall time of the last ignored hand-off, protocol refused)}``, read from the file once.
_ignored_handoffs: dict[str, tuple[int, float, bool]] = {}
_ignored_loaded: list[str] = []
_ignored_lock = threading.Lock()
#: ``Clients\StartMenuInternet``: where Windows lists the desktop browsers it knows (its own default-apps list reads
#: it).
_BROWSER_CLIENTS = (("HKEY_CURRENT_USER", r"SOFTWARE\Clients\StartMenuInternet"),
                    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\Clients\StartMenuInternet"),
                    ("HKEY_LOCAL_MACHINE", r"SOFTWARE\WOW6432Node\Clients\StartMenuInternet"))
_browser_images_cache: list[frozenset[str]] = []


def _appid_of(app: str) -> str:
    """The AppID of the Start menu app ``app`` names (exact or the one substring match), ``""`` when none or several."""
    try:
        chosen, _candidates = _match_apps(app, get_start_apps())
    except Exception:  # noqa: BLE001
        return ""
    return str(chosen.get("appid") or "") if chosen else ""


def _handoff_path() -> str:
    """The note's file (:data:`HANDOFF_FILE`, else ``YUKI_HANDOFF_FILE``, else the data folder's)."""
    if HANDOFF_FILE:
        return HANDOFF_FILE
    if os.environ.get("YUKI_HANDOFF_FILE"):
        return os.environ["YUKI_HANDOFF_FILE"]
    from yuki.core.paths import data_dir

    return str(data_dir() / HANDOFF_FILE_NAME)


def _load_ignored() -> None:
    """desk22 (A16): the kept note, read once per process (under the lock); a missing or unreadable file is no note.
    Entries past :data:`_IGNORED_KEEP_S` stay (their count re-arms the note on the next ignored hand-off measured)
    until the cap pushes them out. Never raises."""
    path = _handoff_path()
    if _ignored_loaded and _ignored_loaded[0] == path:
        return
    _ignored_loaded[:] = [path]
    _ignored_handoffs.clear()
    try:
        with open(path, encoding="utf-8") as fh:
            raw = json.load(fh)
        for key, value in (raw.get("browsers") or {}).items() if isinstance(raw, dict) else ():
            if not isinstance(key, str) or not key or not isinstance(value, dict):
                continue
            count = max(0, min(_IGNORED_COUNT_MAX, int(value.get("n") or 0)))
            at = float(value.get("at") or 0.0)
            if count and at > 0:
                _ignored_handoffs[key.lower()] = (count, at, bool(value.get("refused")))
    except Exception:  # noqa: BLE001 - no file yet, or one that can't be read: no note
        _ignored_handoffs.clear()


def _save_ignored() -> None:
    """desk22 (A16): the note written whole (under the lock), the newest :data:`_IGNORED_CAP` browsers only, by an
    atomic replace. A file that can't be written costs a slow hand-off after a restart, never the caller."""
    path = _handoff_path()
    try:
        newest = sorted(_ignored_handoffs.items(), key=lambda kv: kv[1][1], reverse=True)[:_IGNORED_CAP]
        _ignored_handoffs.clear()
        _ignored_handoffs.update(newest)
        body = {"v": 1, "browsers": {k: {"n": n, "at": round(at, 1), "refused": refused}
                                     for k, (n, at, refused) in newest}}
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(body, fh, separators=(",", ":"))
        os.replace(tmp, path)
    except Exception:  # noqa: BLE001
        pass


def _note_ignored(appid: str, ignored: bool) -> None:
    """One more address hand-off ``appid`` ignored (``ignored``), or one it opened (the note is cleared). desk22 (A16):
    kept across restarts (:func:`_save_ignored`); an ignored one re-arms an expired note at once (its count is kept),
    and marks whether Windows had refused the app's protocol activation (so one ignored hand-off is enough next time).
    """
    key = str(appid or "").lower()
    if not key:
        return
    with _ignored_lock:
        _load_ignored()
        got = _ignored_handoffs.get(key)
        if ignored:
            count = min(_IGNORED_COUNT_MAX, (got[0] if got else 0) + 1)
            _ignored_handoffs[key] = (count, time.time(), bool(key in _NO_PROTOCOL or (got and got[2])))
        elif got is None:
            return
        else:
            _ignored_handoffs.pop(key, None)
        _save_ignored()


def _handoff_state(appid: str) -> str:
    """desk22 (A16): the kept note for ``appid``: ``"ignores"`` (it ignored enough hand-offs in a row, the latest
    within :data:`_IGNORED_KEEP_S`: type the address at once), ``"recheck"`` (it did, but the note has expired: hand
    it over once more with the full page wait, and measure it again), else ``""``."""
    key = str(appid or "").lower()
    if not key:
        return ""
    with _ignored_lock:
        _load_ignored()
        got = _ignored_handoffs.get(key)
    if not got:
        return ""
    if got[0] < (1 if key in _NO_PROTOCOL or got[2] else _IGNORED_AFTER):
        return ""
    age = time.time() - got[1]
    # a wall clock set back (or the file's rounding) leaves a time a little ahead: still within the keep either way
    return "ignores" if -_IGNORED_KEEP_S < age < _IGNORED_KEEP_S else "recheck"


def _ignores_handoff(appid: str) -> bool:
    """Whether ``appid`` ignored enough address hand-offs in a row (:data:`_IGNORED_AFTER`; one once its protocol
    activation was refused, now or when the note was kept), the latest within :data:`_IGNORED_KEEP_S`
    (:func:`_handoff_state`)."""
    return _handoff_state(appid) == "ignores"


def handoff_note(app: str) -> str:
    """desk22 (A16, for the web skill's own hand-off: merge note to skill22): the kept note for the browser ``app``
    names (a Start menu name, as :func:`open_url` takes it): ``"ignores"``, ``"recheck"`` or ``""``
    (:func:`_handoff_state`). Never raises."""
    try:
        return _handoff_state(_appid_of(app))
    except Exception:  # noqa: BLE001
        return ""


def note_handoff(app: str, ignored: bool) -> None:
    """desk22 (A16, merge note to skill22): one address hand-off to the browser ``app`` names, measured ignored (it
    kept the page it had, or showed none) or opened: kept in the same note :func:`open_url` reads. The web skill's own
    hand-off (``open_url(address_bar=False)``) is noted only through here, by the skill (review M3: one writer per
    hand-off). Never raises."""
    try:
        _note_ignored(_appid_of(app), bool(ignored))
    except Exception:  # noqa: BLE001
        pass


def _registered_browser_images() -> frozenset[str]:
    """The program images (lowercased, ``chrome.exe``) Windows lists as desktop browsers
    (:data:`_BROWSER_CLIENTS`: each client's ``shell\\open\\command``), read once per process. Machine data; never
    raises (an empty set)."""
    if _browser_images_cache:
        return _browser_images_cache[0]
    out: set[str] = set()
    try:
        import ntpath
        import winreg

        for hive, path in _BROWSER_CLIENTS:
            try:
                root = winreg.OpenKey(getattr(winreg, hive), path)
            except OSError:
                continue
            with root:
                for i in range(256):
                    try:
                        client = winreg.EnumKey(root, i)
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(root, client + r"\shell\open\command") as key:
                            command = str(winreg.QueryValueEx(key, "")[0] or "").strip()
                    except OSError:
                        continue
                    end = command.casefold().find(".exe")
                    exe = command[1:].partition('"')[0] if command.startswith('"') else \
                        (command[:end + 4] if end >= 0 else "")
                    if exe.casefold().endswith(".exe"):
                        out.add(ntpath.basename(exe).lower())
    except Exception:  # noqa: BLE001 - not on Windows, or unreadable: none known
        pass
    got = frozenset(out)
    _browser_images_cache[:] = [got]
    return got


#: :func:`web_app`'s answers per program image (RAM only, this process).
_WEB_APPS: dict[str, bool] = {}


def web_app(process: str, pid: object = None) -> bool:
    """Whether a program is a web browser by machine data (kHands r1 M6; moved here from ``yuki.voice.hands`` by
    desk20 so the tab hand's keyboard rung asks the same): Windows lists it as a desktop browser
    (:func:`_registered_browser_images`), or (``pid`` of the real desktop given) it is a packaged app whose own manifest
    declares web addresses (a ``windows.protocol`` for http or https: Arc). Never a list of browsers. Read-only, kept
    per image; never raises (``False``)."""
    image = str(process or "").strip().lower()
    if not image:
        return False
    if image in _WEB_APPS:
        return _WEB_APPS[image]
    try:
        if image in _registered_browser_images():
            _WEB_APPS[image] = True
            return True
    except Exception:  # noqa: BLE001
        return False
    if pid is None:
        return False
    try:
        from yuki.core.appnames import WindowsProbe

        probe = WindowsProbe()
        full, _aumid = probe.package_of_process(int(pid))
        root = probe.package_path(full) if full else ""
        found = False
        if root:
            tree = ElementTree.parse(os.path.join(root, "AppxManifest.xml")).getroot()
            found = any(str(el.tag).rsplit("}", 1)[-1] == "Protocol"
                        and str(el.get("Name") or "").lower() in ("http", "https") for el in tree.iter())
    except Exception:  # noqa: BLE001 - unknown this time: asked again next time
        return False
    _WEB_APPS[image] = found
    return found


def _takes_addresses(chosen: dict[str, str], identity: _AppIdentity, target: str, details: dict) -> bool:
    """Whether the app is a web browser by machine data, so its own address bar may take the address: a packaged app
    whose manifest declares the address's scheme (``windows.protocol``: Arc declares http and https), or a desktop
    program Windows lists as a browser (:func:`_registered_browser_images`). Never a list of browsers; a chat app that
    draws one site (no such declaration) never qualifies."""
    try:
        scheme = urlsplit(target).scheme.lower()
    except ValueError:
        return False
    protocols = details.get("manifest_protocols")
    if protocols is None and _is_packaged(chosen.get("appid", "")):
        protocols = _package_app_facts(chosen["appid"]).get("protocols") or []
    if protocols and scheme in [str(p).lower() for p in protocols]:
        return True
    images = _registered_browser_images()
    if not images:
        return False
    exes = {os.path.basename(p).lower() for p in identity.exe_paths}
    if details.get("exe"):
        exes.add(os.path.basename(str(details["exe"])).lower())
    return bool(exes & images)


def _own_window(identity: _AppIdentity) -> tuple[int | None, str]:
    """The app's window to type an address into: ``(hwnd, how)``, ``how`` a code word: ``open`` (on screen, topmost
    first), ``minimized``, ``hidden`` (shown now: an app closed to the background), ``none``. By the app's identity
    (its AppUserModelID or package, its program file), never by its process name."""
    try:
        pids = identity.running_pids()
    except Exception:  # noqa: BLE001
        pids = set()
    if not pids:
        return None, "none"
    hwnd, state = _find_app_window(identity, pids)
    if hwnd is None:
        return None, "none"
    if state == "hidden":
        return (hwnd, "hidden") if _show_window(hwnd) else (None, "none")
    return hwnd, state


def app_window(app: str, *, deadline: float | None = None) -> tuple[int | None, str]:
    """``(hwnd, how)``: the Start menu app ``app``'s own window, for a caller that types into it (the web skill's
    address-bar rung): found by the app's identity (:func:`_own_window`: ``open``, ``minimized``, ``hidden``); with
    the app not running or showing no window, ``launch_app(app)`` brings one up (``launched``); ``(None, why)``
    otherwise (``no_app``: no such Start menu app; ``none``: no window came). Never raises; never with a game in front
    (``launch_app`` refuses it: ``(None, "refused")``, the word a caller holds as a game). fHands (review): a launch
    stopped by anything else (the gate failed closed, another guard) is ``(None, "blocked")``, never "refused": it
    is not said as a game."""
    try:
        chosen, _candidates = _match_apps(app, get_start_apps())
    except Exception:  # noqa: BLE001
        chosen = None
    if chosen is None:
        return None, "no_app"
    identity = _identity_for(chosen, None)
    hwnd, how = _own_window(identity)
    if hwnd:
        return hwnd, how
    try:
        started = launch_app(app, deadline=deadline)
    except Exception:  # noqa: BLE001
        return None, "none"
    if (kind := _gate.carried(started.details)) is not None:
        return None, "refused" if kind == _gate.GAME else _gate.BLOCKED
    got = (started.details or {}).get("hwnd")
    return (int(got), "launched") if started.ok and got else (None, "none")


def _address_bar_open(app: str, target: str, *, deadline: float | None, handed: ActionResult) -> ActionResult | None:
    """lSkills (live 17 Oct: every Arc open failed, then Yuki said "Arc isn't installed" or opened the page in Chrome):
    the address typed into the browser's own address bar, in its own window, after the address handed to it as a launch
    argument didn't open (``handed``: ``page_unchanged``, or a start that failed while the app runs). ``None`` when the
    step doesn't apply (the app isn't a browser by machine data, no window, a game in front): the hand-off's own result
    stands. Otherwise the result of this step (``details["via_word"] = "address_bar"``, the hand-off's details under
    ``handoff``).

    The steps (the web skill's address-bar rung, :func:`yuki.skills.web_route.route_address_bar`, in one call): the
    app's window by identity (:func:`_own_window`; one brought up when it shows none); what each of its windows shows
    first (:func:`_pages_before`: a page already there is never the page opened; its own window already on the very
    page asked for is "already"); the window to the front (``focus_window``); a new tab (:data:`_NEW_TAB_KEYS`); then
    only when focus is on a text field of the browser's own frame in that same program
    (:func:`yuki.skills.web_route.frame_field`: never a page's own box, so never a send) the address is typed and read
    back (no Enter on an address that didn't arrive whole), an inline completion after it deleted, and Enter
    navigates. Done only when one of its windows newly shows a page at the address (:func:`_await_page`). Every key
    goes through Yuki's guarded input (nothing with a game in front: D17)."""
    started = time.perf_counter()
    end = time.monotonic() + _BAR_FLOOR_S
    if deadline is not None:
        end = max(float(deadline), end)
    hd = dict(getattr(handed, "details", None) or {})
    try:
        chosen, _candidates = _match_apps(app, get_start_apps())
    except Exception:  # noqa: BLE001
        chosen = None
    if chosen is None:
        return None
    identity = _identity_for(chosen, {"exe": hd.get("exe")} if hd.get("exe") else None)
    if not identity.confirmable or not _takes_addresses(chosen, identity, target, hd):
        return None
    details: dict = {"url": target, "name": chosen["name"], "appid": chosen["appid"], "via": "address_bar",
                     "via_word": "address_bar", "page_targets": [target], "launched": bool(hd.get("launched")),
                     "handoff": {k: hd.get(k) for k in ("via", "via_word", "reason", "page_state", "launched",
                                                        "protocol_refused") if k in hd}}
    if isinstance(hd.get("timings_ms"), dict):
        # desk20 (final-D S21): the hand-off's own stage ends ride along, so a hand that logs them shows where its time
        # went before the address bar (code words and numbers only)
        details["timings_ms"] = dict(hd["timings_ms"])

    def finish(ok: bool, summary: str, stage: str) -> ActionResult:
        details["bar_stage"] = stage
        if isinstance(details.get("timings_ms"), dict):
            # the address bar's own duration (ms from its start, not the hand-off's clock)
            details["timings_ms"]["bar"] = round((time.perf_counter() - started) * 1000.0, 1)
        details.setdefault("verified", True if ok and details.get("page_verified") else (False if not ok else None))
        return ActionResult(ok=ok, summary=summary, details=details,
                            elapsed_ms=(time.perf_counter() - started) * 1000.0)

    hwnd, how = _own_window(identity)
    if not hwnd:
        # not running, or running with no window it can show: brought up the way the Start menu starts it (no
        # arguments), then its window takes the address
        try:
            up = launch_app(app, deadline=end)
        except Exception:  # noqa: BLE001
            up = None
        got = (up.details or {}).get("hwnd") if up is not None and up.ok else None
        if not got or _gate.carried(up.details):
            return None
        hwnd, how = int(got), "launched"
    details["bar_window"] = how
    if _gate.refuse("focus", hwnd=hwnd) is not None:
        return None
    pid = _window_pid(hwnd)
    befores = _pages_before(identity, min(_PAGE_BEFORE_S, max(0.2, 0.25 * (end - time.monotonic()))))
    details["before_pages"] = [{"hwnd": h, "url": u} for h, u in befores.items()]
    before, _before_pids, _titles = _window_snapshot()
    focused = focus_window(hwnd, timeout_s=min(2.0, max(0.5, end - time.monotonic())))
    if _gate.carried(focused.details):
        details["refused"] = _gate.carried(focused.details)
        return finish(False, str(focused.summary), "refused")
    if not focused.ok:
        return finish(False, f"could not bring {chosen['name']} forward to type the address ({focused.summary})",
                      "not_focused")
    own = befores.get(hwnd, "")
    if own and _same_page(target, own) and not _past_root(target, own):
        # its own window already shows the very page asked for (now in front): nothing is typed; "already"
        details.update(hwnd=hwnd, page_verified=True, page_state="already", page={"title": "", "url": own},
                       process_name=getattr(window_info(hwnd), "process_name", ""))
        return finish(True, f"{chosen['name']} already showed {own} and is in front now (nothing typed)", "already")
    from yuki.actions import input as keys

    opened = keys.hotkey(*_NEW_TAB_KEYS, expect_hwnd=hwnd)
    if _gate.carried(opened.details):
        details["refused"] = _gate.carried(opened.details)
        return finish(False, str(opened.summary), "refused")
    if not opened.ok:
        return finish(False, f"{chosen['name']} didn't take the new-tab keys ({opened.summary})", "keys_refused")
    try:
        field_hwnd = int((opened.details or {}).get("foreground_hwnd") or 0)
    except (TypeError, ValueError):
        field_hwnd = 0
    if not field_hwnd or _window_pid(field_hwnd) != pid:
        return finish(False, f"the new-tab keys took focus out of {chosen['name']}: nothing was typed", "other_window")
    try:
        from yuki.skills.web_route import frame_field

        field_ok, why = frame_field(pid)
    except Exception:  # noqa: BLE001 - can't tell: never typed
        field_ok, why = None, "unread"
    details["bar_field"] = why
    if field_ok is not True:
        keys.press("escape", expect_hwnd=field_hwnd)
        return finish(False, f"focus in {chosen['name']} is not on its own address field ({why}): nothing was typed",
                      "no_field")
    typed = keys.type_text(target, expect_hwnd=field_hwnd)
    if _gate.carried(typed.details):
        details["refused"] = _gate.carried(typed.details)
        return finish(False, str(typed.summary), "refused")
    facts = typed.details or {}
    if not typed.ok or facts.get("readback") != "arrived":
        keys.press("escape", expect_hwnd=field_hwnd)
        return finish(False, f"the address didn't arrive whole in {chosen['name']}'s address field: no Enter",
                      "not_typed")
    if facts.get("readback_source") == "caret" or \
            len(" ".join(str(facts.get("readback_text") or "").split())) > len(target):
        # the field completed the address inline: that rest goes, so Enter opens the address asked for
        keys.press("delete", expect_hwnd=field_hwnd)
    entered = keys.press("enter", expect_hwnd=field_hwnd)
    if not entered.ok:
        return finish(False, f"Enter in {chosen['name']}'s address field was refused ({entered.summary})",
                      "enter_refused")
    watch = max(_BAR_PAGE_MIN_S, min(_BAR_PAGE_S, end - time.monotonic()))
    wait = _await_page(identity, [target], before, watch, unchanged=befores)
    details["page_check"] = _page_details(wait)
    details["identity"] = identity.describe()
    if wait is not None and wait.matched is not None:
        opened_line = _page_opened(chosen["name"], wait, before, details, time.monotonic(), [target], identity,
                                   focus_floor_s=_DEADLINE_FOCUS_FLOOR_S)
        why_typed = ("Windows refused to hand it the address" if hd.get("protocol_refused") and not hd.get("launched")
                     else "it ignored the address handed to it")
        return finish(True, f"{opened_line} (typed into {chosen['name']}'s own address bar: {why_typed})", "page")
    details["page_verified"] = False if wait is not None and wait.seen else None
    details["hwnd"] = hwnd
    details["process_name"] = getattr(window_info(hwnd), "process_name", "")
    return finish(False, f"typed the address into {chosen['name']}'s own address bar and pressed Enter, but no page "
                         f"of it at that address was seen within {watch:.1f} s", "not_seen")


def _own_channel(url: str) -> dict | None:
    """integ22 (skill22 merge note 1, A6): :func:`yuki.skills.names.own_url` on a raw address (no store: this
    process's open memory, a bounded call), ``{"url", "name", "kind", "how", "score", "margin", "source"}`` or
    ``None`` (the address as given). Never raises."""
    try:
        from yuki.skills.names import own_url

        took = own_url(url)
    except Exception:  # noqa: BLE001 - no measure: the address as given
        return None
    return took if isinstance(took, dict) and took.get("url") else None


def _channel_named(result: ActionResult, took: dict, given: str) -> ActionResult:
    """integ22 (A6): an open of the user's own channel a raw address was a mishearing of: ``details["names_match"]``
    content-free (kind, how, score, margin, source) and the summary led by what opened, as a measured app or site is
    (:data:`MEASURED_OPENED_LINE` / :data:`MEASURED_TAKEN_LINE`: "Opened Deji's page (heard as 'Deezy')."). The heard
    value is read from the address as given (its page kind's one value). Never raises."""
    try:
        details = result.details if isinstance(result.details, dict) else {}
        if isinstance(details.get("names_match"), dict):
            # integ22 names-4: the browser's own measured name (launch_app's, "Ark" read as Arc) kept beside it
            details["names_match_app"] = dict(details["names_match"])
        details["names_match"] = {k: took[k] for k in ("kind", "how", "score", "margin", "source") if k in took}
        # integ22 fix names-2: the channel's name, for the hands' done line ("Opened Deji's channel."): a string fact
        # (as open_app's ``name``), never in the content-free match
        details["channel_name"] = " ".join(str(took.get("name") or "").split())[:60]
        heard = given
        try:
            from urllib.parse import unquote

            from yuki.connect.templates import page_of

            m = page_of(str(given or "").strip())
            if m is not None and m.entity:
                heard = " ".join(unquote(str(m.entity)).split()).lstrip("@") or given
        except Exception:  # noqa: BLE001
            pass
        line = MEASURED_OPENED_LINE if result.ok else MEASURED_TAKEN_LINE
        result.details = details
        named = f"{took.get('name') or 'their channel'}'s page"
        result.summary = line.format(name=named, query=heard) + str(result.summary or "")
    except Exception:  # noqa: BLE001 - the open's own result as it is
        pass
    return result


def open_url(
    url: str, *, app: str | None = None, content_timeout_s: float = CONTENT_WAIT_S,
    deadline: float | None = None, address_bar: bool = True, heard: bool = False,
) -> ActionResult:
    """Open a URL (or any shell target: a file, a folder).

    Args:
        url: the target.
        app: ``None`` opens it with the user's default handler.  Otherwise the
            Start menu app to open it with, which is ``launch_app(app,
            args=[url])`` - the target is handed to the app as an argument -
            including bringing the window to the front and waiting for the
            content.
        content_timeout_s: bound on the wait for the content to be on screen.
        deadline: x8WebArc: a ``time.monotonic()`` time the whole call answers
            by (see :func:`launch_app`); ``None``: the waits above only.
        address_bar: lSkills (live 17 Oct): with ``app`` and a web address, when
            the app didn't open the address handed to it (``page_unchanged``:
            it kept the page it had - another page counts only in a packaged
            or protocol-refused browser; ``page_blank``: a refused package
            showing no page; a packaged start whose windows show none; or the
            start failed while it runs) and it is a browser by machine data,
            the address is typed into its own address bar
            (:func:`_address_bar_open`; a new tab, a frame field only, verified
            by the page). After two ignored hand-offs in a row (one once its
            protocol activation was refused) the hand-off is skipped and the
            address typed at once; once that typing got as far as Enter, it is
            the answer (never handed over too). desk22 (A16): that note is
            kept across restarts and expires by measure (:data:`_IGNORED_KEEP_S`
            after the last ignored hand-off; then one hand-off is measured
            again, with the full page wait). ``False``: the hand-off's own
            result only (the web skill, whose next rung types it itself; its
            page wait capped at :data:`_NOTED_PAGE_S` while the note holds the
            browser). The skill measures that hand-off into the note itself
            (:func:`note_handoff`), so this path writes no note (review M3).
        heard: integ22 fix S5: the address is the talker's own, built from
            heard words (the hands' ``open_url`` and new tab): only then may a
            channel page's value be taken for one of the user's own channels
            (:func:`_own_channel`). An address read from a message or a page,
            the planner's, a told way's replay: opened exactly as given.

    A web address handed to a browser that was already running is checked
    against what its windows showed right before (:func:`_pages_before`): an
    unchanged page, or pages none of which is newly at the address, give
    ``ok=False`` with ``details["reason"] = "page_unchanged"``, never "verified";
    the very page asked for, already showing before and still, is ``ok=True``
    with ``details["page_state"] = "already"`` (review r1 M1).

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
    # D17: an opened link brings its handler forward (a browser, Spotify, Discord): never with a game in front
    if (refused := _gate.refuse("open_url")) is not None:
        return finish(False, refused, {"url": target, "refused": _gate.kind_of(refused)})
    if heard and address_bar and (took := _own_channel(target)) is not None:
        # integ22 (skill22 merge note 1, A6; final-D youtube_channel_deji_arc): a raw address of a page named by a
        # value that is none of the user's own channels as written but measures as one of them ("@Deezy" for their
        # Deji) opens that channel's own address, named as what opened (decision 3: never a question). integ22 fix S5:
        # the hands' heard address only (``heard``); the web skill's own call (``address_bar=False``) has skill19's
        # heard match already, and an address read, never heard, opens as given. The call below leaves ``heard`` out
        # (its address is one of theirs as written).
        return _channel_named(open_url(took["url"], app=app, content_timeout_s=content_timeout_s, deadline=deadline,
                                       address_bar=address_bar), took, target)
    if app is not None and app.strip():
        address = _page_address(target)
        typed_first = None
        capped = False
        if address:
            appid = _appid_of(app)
            state = _handoff_state(appid) if appid else ""
            # desk22 (A16): the hand-off's page wait is capped when the kept note says this browser ignores them and
            # the web skill (``address_bar=False``) still hands it over as it asked: its next rung types it once the
            # short wait shows nothing. desk22 review L1: an expired note's hand-off ("recheck") gets the full wait (it
            # is a measure: a slow but working hand-off is never followed by a typed second tab of the same page)
            capped = state == "ignores" and not address_bar
            if state == "ignores" and address_bar:
                # it ignored the last hand-offs (kept the very page it had): the address goes into its own address
                # bar at once, not after the whole page wait (x8WebArc review r1 L1, carried to the hand)
                typed_first = _address_bar_open(app, address, deadline=deadline,
                                                handed=ActionResult(ok=False, summary="", details={"launched": False}))
                if typed_first is not None and (typed_first.ok
                                                or typed_first.details.get("bar_stage") == "not_seen"):
                    # opened, or the address went in and Enter was pressed with no page seen yet (review r1 L2): the
                    # address isn't handed over again, so a slow page never opens twice
                    typed_first.details["url"] = target
                    typed_first.details["handoff_skipped"] = True
                    return typed_first
        wait_s = min(float(content_timeout_s), _NOTED_PAGE_S) if capped else content_timeout_s
        launched = launch_app(app, args=[target], content_timeout_s=wait_s, deadline=deadline)
        launched.details["url"] = target
        if capped:
            launched.details["handoff_capped"] = True   # code word only: the kept note capped the page wait
        if not address or not address_bar:
            # desk22 review M3: the web skill's own hand-off (``address_bar=False``) is measured into the note by the
            # skill itself (``web_route`` calls :func:`note_handoff` with what it saw): one writer per hand-off, so one
            # ignored hand-off is never counted twice
            return launched
        ld = launched.details
        appid = str(ld.get("appid") or "")
        if launched.ok and ld.get("page_verified") is True:
            _note_ignored(appid, False)
            return launched
        if _gate.carried(ld) or typed_first is not None:
            return launched
        same = ld.get("reason") == "page_unchanged" and ld.get("page_state") == "same"
        # review r1 L1: another page showing ("other") is an ignored hand-off only in a packaged browser or one whose
        # protocol activation was refused (Arc restoring old tabs); a desktop browser that took the address and
        # landed on another host (twitter.com -> x.com, a login elsewhere) gets no second tab of the same address
        ignored = same or (ld.get("reason") == "page_unchanged"
                           and (bool(ld.get("no_protocol")) or _is_packaged(appid)))
        blank_page = ld.get("reason") == "page_blank"
        if same or blank_page:
            # kept the very page it had, or (review r1 M1) a refused package's start showed no page at all: the next
            # address goes straight into its address bar (one such hand-off is enough once protocol was refused).
            # desk22 (A16): kept across restarts
            _note_ignored(appid, True)
        page_check = ld.get("page_check") if isinstance(ld.get("page_check"), dict) else {}
        # its windows show no page at all after a packaged app's alias or activation carried the address (percFix: Arc
        # started cold on its own new-tab view); a program started with the address on its command line is left alone
        blank = (bool(page_check.get("windows")) and not page_check.get("seen")
                 and ld.get("via") in ("AppExecutionAlias", "ActivateApplication"))
        failed_start = not ld.get("launched") and bool(ld.get("protocol_refused") or ld.get("launch_error"))
        if not (ignored or blank_page or failed_start or (blank and ld.get("page_verified") is not True)):
            return launched
        typed = _address_bar_open(app, address, deadline=deadline, handed=launched)
        if typed is None:
            return launched
        typed.details["url"] = target
        if typed.ok:
            return typed
        # neither got there: the hand-off's facts first (what it did), then what the address bar did
        launched.details["address_bar"] = {k: typed.details.get(k) for k in ("bar_stage", "bar_window", "bar_field")}
        return ActionResult(ok=False, summary=f"{launched.summary}; then {typed.summary}", details=launched.details,
                            elapsed_ms=launched.elapsed_ms + typed.elapsed_ms)

    focus_floor = _FOCUS_FLOOR_S
    if deadline is not None:
        content_timeout_s = max(0.0, min(float(content_timeout_s), float(deadline) - time.monotonic()))
        focus_floor = _DEADLINE_FOCUS_FLOOR_S
    handler = _default_handler(target)
    details: dict = {"url": target, "handler": handler, "via_word": "startfile"}
    # Only the handler's windows count; with no handler known, only a window
    # that newly appears does (said so in its reason), never an old one that
    # merely came to the front.
    identity = _AppIdentity(handler or "the default handler", image_names=[handler] if handler else [])
    # A web address: success is a page at that address in the handler's windows
    # (or, with no handler known, in a window that appeared or is in front).
    address = _page_address(target)
    unchanged: dict[int, str] = {}
    if address and handler:
        # x8WebArc: what the handler's windows show now, so a page one already showed is never the one opened
        budget = _PAGE_BEFORE_S if deadline is None else min(
            _PAGE_BEFORE_S, 0.25 * max(0.0, float(deadline) - time.monotonic()))
        unchanged = _pages_before(identity, budget)
        details["pages_before"] = len(unchanged)
        details["before_pages"] = [{"hwnd": hwnd, "url": url} for hwnd, url in unchanged.items()]
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
    if deadline is not None:
        window_deadline = min(window_deadline, float(deadline))
    page_miss: tuple[bool, str] | None = None
    if address:
        details["page_targets"] = [address]
        content_deadline = time.monotonic() + _HANDLER_WINDOW_S + content_timeout_s
        if deadline is not None:
            content_deadline = min(content_deadline, float(deadline))
        page_wait = _await_page(identity, [address], before, content_deadline - time.monotonic(),
                                unchanged=unchanged)
        details["page_check"] = _page_details(page_wait)
        who = os.path.splitext(handler)[0] if handler else "the default browser"
        if page_wait is not None and page_wait.matched is not None:
            matched = page_wait.matched
            process = getattr(window_info(matched.hwnd), "process_name", "")
            who = os.path.splitext(process)[0] if process else who
            return finish(
                True,
                f"{opened}; "
                + _page_opened(who, page_wait, before, details, content_deadline, [address],
                               focus_floor_s=focus_floor),
                details,
            )
        already_page = _already_page(page_wait, unchanged, [address])
        if already_page is not None:
            # review r1 M1: the very page asked for was already showing and still is: "already", brought forward
            page_wait.matched = already_page
            process = getattr(window_info(already_page.hwnd), "process_name", "")
            who = os.path.splitext(process)[0] if process else who
            return finish(
                True,
                f"{opened}; "
                + _page_opened(who, page_wait, before, details, content_deadline, [address],
                               focus_floor_s=focus_floor, already=True),
                details,
            )
        details["page_verified"] = False if page_wait is not None and page_wait.seen else None
        still = _unchanged_windows(page_wait, unchanged)
        page_miss = _page_miss(who, [address], page_wait, still)
        if not page_miss[0]:
            details["reason"] = "page_unchanged"
            details["page_state"] = "same" if still else "other"
        window_deadline = time.monotonic() + 2 * _POLL_S
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
        if page_miss:
            return finish(page_miss[0], f"{opened}; {page_miss[1]}", details)
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
    forward = _bring_forward(hwnd, window_deadline, details, floor_s=focus_floor)
    if page_miss:
        return finish(
            page_miss[0],
            f"{opened}; {page_miss[1]}; hwnd {hwnd} ({details['process_name']}) "
            f"\"{details['title']}\" - {reason}; {forward}",
            details,
        )
    content_wait = content_timeout_s if deadline is None else max(
        0.0, min(content_timeout_s, float(deadline) - time.monotonic()))
    content_note = _wait_for_opened_content(
        hwnd, content_wait, details, require_document=True
    )
    if (fresh := window_info(hwnd)) is not None:
        details["title"] = fresh.title
    return finish(
        True,
        f"{opened}: hwnd {hwnd} ({details['process_name']}) \"{details['title']}\" - "
        f"{reason}; {forward}; {content_note}",
        details,
    )
