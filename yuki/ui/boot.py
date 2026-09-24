"""Start the tray at logon: ``pythonw.exe -m yuki.ui.boot`` (the Run value, :mod:`yuki.ui.startup`).

Windowless (``pythonw.exe``: no console to close by mistake), with the project
as the working directory, then :func:`yuki.ui.app.main` as ``yuki-ui`` would
run it. The tray starts the memory service itself (``ensure_service``).

At logon the shell's notification area can come up after this process: the
tray waits for it (the taskbar window, a condition polled for at most
:data:`SHELL_WAIT_S`), then starts regardless - Qt re-adds the icon when the
taskbar appears. Nothing here needs the network; model calls that fail while it
is not up yet are retried by their own workers. A crash before the tray is up
is written to ``logs/ui-boot.log`` (content-free) instead of vanishing.
"""

from __future__ import annotations

import ctypes
import os
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
#: The longest the tray waits for the taskbar at logon.
SHELL_WAIT_S = 120.0
_POLL_S = 0.5


def _shell_ready() -> bool:
    try:
        return bool(ctypes.windll.user32.FindWindowW("Shell_TrayWnd", None))
    except Exception:
        return True


def wait_for_shell(timeout_s: float = SHELL_WAIT_S) -> float:
    """Wait until the taskbar exists (or ``timeout_s``); returns the seconds waited."""
    started = time.monotonic()
    while not _shell_ready() and time.monotonic() - started < timeout_s:
        time.sleep(_POLL_S)   # condition poll with a deadline, not a settle delay
    return time.monotonic() - started


def _note(line: str) -> None:
    try:
        path = PROJECT_ROOT / "logs" / "ui-boot.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now().isoformat(timespec='seconds')} {line}\n")
    except OSError:
        pass


def main() -> int:
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8")
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8")
    try:
        os.chdir(PROJECT_ROOT)
    except OSError:
        pass
    waited = wait_for_shell()
    _note(f"boot pid {os.getpid()} shell_wait_s {waited:.1f}")
    try:
        from yuki.ui.app import main as ui_main

        return int(ui_main([]) or 0)
    except SystemExit as exc:
        return int(exc.code or 0) if isinstance(exc.code, int) else 0
    except BaseException as exc:   # never a silent death at logon
        _note(f"crash {type(exc).__name__}: {exc}\n{traceback.format_exc()}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
