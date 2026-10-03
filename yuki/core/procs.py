"""Yuki's own processes: how to start one, and how to find the running ones, dev and frozen alike.

Every Yuki process is "run module M": the tray (``yuki.ui.boot``), the memory service
(``yuki.memory.service``), the window (``yuki.ui.window``), the deferred call transcriber
(``yuki.memory.transcribe``), the MCP server (``yuki.mcp.server``).

* **Dev**: ``<venv>\\Scripts\\pythonw.exe -m M`` (``python.exe`` for a console). A uv venv's
  ``pythonw.exe`` is a launcher that starts the base interpreter with the same command line,
  so each Yuki process shows up twice (launcher + interpreter), both with ``-m M``.
* **Frozen** (the PyInstaller onedir build, ``packaging/yuki.spec``): one program folder with two
  executables built from the same entry (:mod:`yuki.frozen`): ``Yuki.exe`` (windowless: the tray
  and every background process) and ``yuki-cli.exe`` (console: ``yuki doctor``, maintenance
  commands). Both accept ``-m M [args]`` exactly like ``python -m``, so a frozen command line is
  ``"...\\Yuki.exe" -m yuki.memory.service`` and still names its module.

Because both shapes carry ``-m M`` as two argv elements, finding a process is one rule: its
image is one of Yuki's executables and its argv has ``-m`` followed by exactly ``M`` (parsing
a fixed machine format, the argv, not guessing from names).

Callers that spawn with ``[sys.executable, "-m", M]`` keep working frozen unchanged (the frozen
``sys.executable`` is ``Yuki.exe``, which honours ``-m``); :func:`module_command` is the explicit
form that also picks the windowless or console flavour.

Stdlib only at import; psutil is imported inside the functions that need it.

CLI (operating Yuki, the HANDBOOK's restart procedure)::

    python -m yuki.core.procs list
    python -m yuki.core.procs stop yuki.ui.boot yuki.memory.service     # --db runs are never touched
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Iterable

from yuki.core.paths import PROJECT_ROOT, app_dir, is_frozen

#: The frozen executables (``packaging/yuki.spec`` builds both into one folder).
FROZEN_APP_EXE = "Yuki.exe"
FROZEN_CLI_EXE = "yuki-cli.exe"

#: Module names of Yuki's processes (the tray's Run value, the tray's spawns, the service's spawn).
TRAY = "yuki.ui.boot"
SERVICE = "yuki.memory.service"
WINDOW = "yuki.ui.window"
#: The Lattice window in HTML (WebView2): what a tray click opens unless ui.json says window_engine "qt".
WEB_WINDOW = "yuki.ui.web_window"
TRANSCRIBER = "yuki.memory.transcribe"
MCP = "yuki.mcp.server"
ONBOARDING = "yuki.onboarding"


def interpreter(*, console: bool = False) -> Path:
    """The program that runs a module: ``Yuki.exe`` / ``yuki-cli.exe`` frozen, the venv's pythonw/python in dev."""
    if is_frozen():
        folder = app_dir()
        wanted = folder / (FROZEN_CLI_EXE if console else FROZEN_APP_EXE)
        return wanted if wanted.exists() else Path(sys.executable)
    here = Path(sys.executable)
    name = "python.exe" if console else "pythonw.exe"
    for candidate in (here.with_name(name), PROJECT_ROOT / ".venv" / "Scripts" / name):
        if candidate.exists():
            return candidate
    return here


def module_command(module: str, *args: str, console: bool = False) -> list[str]:
    """argv that runs ``module`` with ``args`` in this installation (windowless unless ``console``)."""
    return [str(interpreter(console=console)), "-m", module, *[str(a) for a in args]]


def command_line(module: str, *args: str, console: bool = False) -> str:
    """:func:`module_command` as one Windows command line (for the Run value, a shortcut)."""
    import subprocess

    return subprocess.list2cmdline(module_command(module, *args, console=console))


def yuki_executables() -> frozenset[str]:
    """Casefolded image paths Yuki's processes run as, in this installation.

    Frozen: the two executables of the program folder. Dev: the venv's python/pythonw and the
    base interpreter they launch (``sys._base_executable``).
    """
    paths: set[str] = set()
    if is_frozen():
        folder = app_dir()
        paths |= {str(folder / FROZEN_APP_EXE), str(folder / FROZEN_CLI_EXE), sys.executable}
    else:
        for exe in (sys.executable, getattr(sys, "_base_executable", "") or ""):
            if exe:
                p = Path(exe)
                paths |= {str(p), str(p.with_name("python.exe")), str(p.with_name("pythonw.exe"))}
        venv = PROJECT_ROOT / ".venv" / "Scripts"
        paths |= {str(venv / "python.exe"), str(venv / "pythonw.exe")}
    return frozenset(str(Path(p)).casefold() for p in paths if p)


def module_of(argv: Iterable[str]) -> str | None:
    """The module a command line runs (the argv element after ``-m``), or None."""
    items = list(argv)
    for i, item in enumerate(items[:-1]):
        if item == "-m":
            return items[i + 1]
    return None


def effective_module(argv: Iterable[str], exe: str | None = None) -> str | None:
    """:func:`module_of`, or the tray for ``Yuki.exe`` started with no ``-m`` (the Start-menu shortcut,
    the installer's "Start Yuki now": :mod:`yuki.frozen` runs the tray then)."""
    items = list(argv)
    found = module_of(items)
    if found is not None:
        return found
    image = Path(exe or (items[0] if items else "")).name.casefold()
    if image == FROZEN_APP_EXE.casefold() and not any(a.startswith("-") for a in items[1:]):
        return TRAY
    return None


def is_yuki_process(argv: Iterable[str], exe: str | None, module: str | None = None) -> bool:
    """Whether a process (its argv and image path) is a Yuki process, of ``module`` when given.

    The image must be one of :func:`yuki_executables` (so another project's ``python -m
    yuki.x`` is not ours), or be named like one of the frozen executables when the paths differ
    (an installed build seen from a dev checkout, and the other way round).
    """
    items = list(argv)
    found = effective_module(items, exe)
    if found is None or not found.startswith("yuki"):
        return False
    if module is not None and found != module:
        return False
    image = str(Path(exe)).casefold() if exe else ""
    if image and image in yuki_executables():
        return True
    if is_frozen():
        return False   # the installed app manages its own folder's processes only
    name = Path(image).name if image else ""
    # From a dev checkout: the installed app's processes count too (one tray per session either way),
    # recognised by its pair of executables (another program called Yuki.exe has no yuki-cli.exe beside it).
    if name in (FROZEN_APP_EXE.casefold(), FROZEN_CLI_EXE.casefold()):
        return _is_install(Path(exe or ""))
    return name in ("python.exe", "pythonw.exe") and _under(image, PROJECT_ROOT)


def _is_install(exe: Path) -> bool:
    try:
        return exe.with_name(FROZEN_APP_EXE).exists() and exe.with_name(FROZEN_CLI_EXE).exists()
    except OSError:
        return False


def _under(path: str, root: Path) -> bool:
    try:
        Path(path).resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def find(module: str | None = None, *, include_db_runs: bool = False) -> list[Any]:
    """Running Yuki processes (``psutil.Process``), of ``module`` when given.

    Runs pointed at another database (``--db``: tests, harnesses, the transcriber's own
    argument excepted) are left out unless ``include_db_runs``.
    """
    import os

    import psutil

    me = {os.getpid(), os.getppid()}
    out = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline"]):
        try:
            if proc.pid in me:
                continue
            argv = proc.info.get("cmdline") or []
            if not is_yuki_process(argv, proc.info.get("exe"), module):
                continue
            if not include_db_runs and "--db" in argv and module_of(argv) != TRANSCRIBER:
                continue
            out.append(proc)
        except (psutil.Error, OSError):
            continue
    return out


def _own_child(child: Any) -> bool:
    """Whether a child process is part of Yuki itself (a Yuki module, or a process whose image is inside
    Yuki's own install or checkout), not an app Yuki started for the user."""
    try:
        argv = child.cmdline() or []
        exe = child.exe() or ""
    except Exception:
        return False
    if is_yuki_process(argv, exe):
        return True
    image = str(Path(exe)).casefold() if exe else ""
    return bool(image) and any(_under(image, root) for root in _own_roots())


def _own_roots() -> list[Path]:
    import sys

    roots = [PROJECT_ROOT]
    if is_frozen():
        roots.append(Path(sys.executable).parent)
    return roots


def stop(modules: Iterable[str], *, timeout_s: float = 10.0) -> list[int]:
    """Terminate the running processes of ``modules`` (and their children); returns the pids ended."""
    import psutil

    targets: dict[int, Any] = {}
    for module in modules:
        for proc in find(module):
            targets[proc.pid] = proc
            try:
                for child in proc.children(recursive=True):
                    # Only Yuki's own children (the window, the transcriber, a helper python). An app Yuki
                    # launched for the user (Chrome, WhatsApp, a game) is a child too, and must never be ended.
                    if _own_child(child):
                        targets[child.pid] = child
            except psutil.Error:
                pass
    for proc in targets.values():
        try:
            proc.terminate()
        except psutil.Error:
            pass
    gone, alive = psutil.wait_procs(list(targets.values()), timeout=timeout_s)
    for proc in alive:
        try:
            proc.kill()
        except psutil.Error:
            pass
    return sorted(targets)


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m yuki.core.procs", description="List or stop Yuki's processes.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    st = sub.add_parser("stop")
    st.add_argument("modules", nargs="+")
    args = parser.parse_args(argv)
    if args.cmd == "list":
        for proc in find():
            try:
                mem = proc.memory_info()
                private = getattr(mem, "private", mem.rss) / 2**20
                print(f"{proc.pid:>7}  {private:7.0f} MB  {effective_module(proc.cmdline(), proc.exe()) or '?':<24} "
                      f"{subprocess_line(proc.cmdline())}")
            except Exception:
                continue
        return 0
    ended = stop(args.modules)
    print(f"stopped {len(ended)} process(es): {ended}")
    return 0


def subprocess_line(argv: list[str]) -> str:
    import subprocess

    return subprocess.list2cmdline(argv)


__all__ = [
    "FROZEN_APP_EXE", "FROZEN_CLI_EXE", "TRAY", "SERVICE", "WINDOW", "WEB_WINDOW", "TRANSCRIBER", "MCP", "ONBOARDING",
    "interpreter", "module_command", "command_line", "yuki_executables", "module_of", "effective_module",
    "is_yuki_process",
    "find", "stop",
]

if __name__ == "__main__":
    sys.exit(_main())
