"""Entry point of the frozen (PyInstaller) build: ``Yuki.exe`` and ``yuki-cli.exe``.

Both executables are this one script (``packaging/yuki.spec``); they differ only in the PE
subsystem (``Yuki.exe`` is windowless, ``yuki-cli.exe`` has a console).

* ``Yuki.exe -m <module> [args]`` / ``yuki-cli.exe -m <module> [args]`` run a module exactly like
  ``python -m``. Every spawn in Yuki is ``[sys.executable, "-m", module]`` (or
  :func:`yuki.core.procs.module_command`), so the tray starts the memory service and the window,
  and the service starts the call transcriber, the same way frozen as in dev, and every command
  line still names its module (:mod:`yuki.core.procs`).
* ``Yuki.exe`` with no arguments (the Start-menu shortcut, the installer's "Start with Windows"):
  first-run setup when no model key is found (:mod:`yuki.onboarding`, its own process so its Qt
  application is gone before the tray's starts), then the tray (``yuki.ui.boot``).
* ``yuki-cli.exe`` with no ``-m``: the ``yuki`` command (``yuki-cli doctor``, ``yuki-cli cost today``).
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _quiet_streams() -> None:
    """A windowless process has no stdout/stderr; code that prints must not crash for it."""
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")


def _run_module(module: str, args: list[str]) -> int:
    sys.argv = [module, *args]
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit as exc:
        code = exc.code
        return code if isinstance(code, int) else (0 if code is None else 1)
    return 0


def _needs_onboarding() -> bool:
    try:
        from yuki.onboarding import needs_onboarding
    except Exception:
        return False   # a build without the onboarding package: go straight to the tray
    try:
        return bool(needs_onboarding())
    except Exception:
        return False


def _onboard() -> int:
    """First-run setup in its own process; returns its exit code (0 done, 1 cancelled)."""
    import subprocess

    from yuki.core.procs import ONBOARDING, module_command

    try:
        return subprocess.call(module_command(ONBOARDING))
    except OSError:
        return 0


def main() -> int:
    import multiprocessing

    multiprocessing.freeze_support()
    _quiet_streams()
    argv = sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "-m":
        return _run_module(argv[1], argv[2:])
    if argv[:1] in (["--version"], ["-V"]):
        from yuki import __version__

        print(f"Yuki {__version__}")
        return 0
    console = Path(sys.executable).name.casefold().startswith("yuki-cli")
    if console:
        from yuki.cli import main as cli_main

        return int(cli_main(argv) or 0)
    if _needs_onboarding() and _onboard() != 0:
        return 0   # cancelled: nothing starts; the next launch asks again
    return _run_module("yuki.ui.boot", argv)


if __name__ == "__main__":
    sys.exit(main())
