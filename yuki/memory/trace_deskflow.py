"""Deskflow's own screen switches, for the action trace (capQ2a, 9 Oct 2026; report 78 §4.2).

Why: this PC is a Deskflow server (one keyboard and mouse shared with another computer). While the cursor is on the
other computer's screen, this PC's Raw Input still hears the hand on the mouse and keyboard, and Deskflow's own window
is in front here, so the trace recorded the user's clicks *on the other computer* as anonymous clicks in Deskflow
(8 Oct: 24 at 18:07, 33 switches). Report 78 §4.2: read Deskflow's own log lines for the screen switches, like the Warp
history reader, and drop input while the cursor is on another screen. No guessing.

* **Where the log is** (measured read-only on this PC, 9 Oct): Deskflow 1.26 run by its daemon logs to
  ``%ProgramData%\\Deskflow\\deskflow-daemon.log``; Deskflow's own settings (``%APPDATA%\\Deskflow\\Deskflow.conf``,
  ``[log]`` ``toFile`` / ``file``) can send it to a file of the user's choosing, which is then read instead. The one
  written last is read.
* **What is read**: Deskflow's fixed log lines ``[<local time>] INFO: leaving screen`` (the cursor left this PC's
  screen) and ``... INFO: entering screen`` (it came back); every other line is passed over unread. Times are
  Deskflow's own (local, to the millisecond).
* **How**: on demand (the trace asks for an action's time), never polled: the file's size and write time are asked
  first (one ``stat``) and only bytes appended since the last read are read, at most :data:`READ_MAX` a time (a log
  that grew more is read from its last :data:`READ_MAX` bytes, the latest state being what matters). A log that
  shrank (rotated) is read again from its end. The spans away are kept in memory only (at most :data:`SPANS`).

Deskflow's own processes (:data:`DESKFLOW_IMAGES`, its fixed executable names) never give a row either.
"""

from __future__ import annotations

import configparser
import os
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path

#: Deskflow's own executables (its installer's fixed image names; ``deskflow-server`` / ``-client`` are older names).
DESKFLOW_IMAGES = frozenset({"deskflow.exe", "deskflow-core.exe", "deskflow-daemon.exe", "deskflow-server.exe",
                             "deskflow-client.exe"})
#: Deskflow's fixed log messages for the cursor leaving and entering this PC's screen.
LEAVING, ENTERING = "leaving screen", "entering screen"
#: Bytes read from the log at most per look.
READ_MAX = 256 * 1024
#: Spans away kept at most (memory only).
SPANS = 128


def log_candidates(environ: dict[str, str] | None = None) -> list[Path]:
    """Where Deskflow writes its log on this PC: the file its settings name (``[log] toFile`` true and ``file``),
    then its daemon's log in ``%ProgramData%\\Deskflow``."""
    env = os.environ if environ is None else environ
    out: list[Path] = []
    appdata = env.get("APPDATA")
    if appdata:
        conf = Path(appdata) / "Deskflow" / "Deskflow.conf"
        try:
            parser = configparser.ConfigParser(interpolation=None, strict=False)
            parser.read(conf, encoding="utf-8")
            if parser.has_section("log") and parser.get("log", "toFile", fallback="").strip().lower() == "true":
                named = parser.get("log", "file", fallback="").strip().strip('"')
                if named:
                    out.append(Path(os.path.expandvars(named)))
        except (OSError, configparser.Error, UnicodeDecodeError):
            pass
    program_data = env.get("ProgramData") or env.get("PROGRAMDATA")
    if program_data:
        out.append(Path(program_data) / "Deskflow" / "deskflow-daemon.log")
    return out


def parse_line(line: str) -> tuple[float, bool] | None:
    """``(epoch s, away)`` for a ``leaving screen`` (away) or ``entering screen`` line, else None. The time is
    Deskflow's own, ``[YYYY-MM-DDTHH:MM:SS.mmm]`` local time."""
    line = line.strip()
    if not line.startswith("[") or "]" not in line:
        return None
    stamp, _sep, rest = line[1:].partition("]")
    message = rest.split(":", 1)[1].strip() if ":" in rest else ""
    if message not in (LEAVING, ENTERING):
        return None
    whole, _dot, frac = stamp.partition(".")
    try:
        at = time.mktime(time.strptime(whole, "%Y-%m-%dT%H:%M:%S"))
        at += int(frac[:3].ljust(3, "0")) / 1000.0 if frac[:3].isdigit() else 0.0
    except (ValueError, OverflowError):
        return None
    return at, message == LEAVING


class DeskflowScreens:
    """Whether the cursor was on another computer's screen at a time, from Deskflow's own log (see the module
    docstring). ``away(at)``: True / False; with no Deskflow log at all, False (nothing is dropped).

    Args:
        paths: ``() -> candidate log files`` (default :func:`log_candidates`).
    """

    def __init__(self, paths: Callable[[], list[Path]] | None = None) -> None:
        self._paths = paths or log_candidates
        self._path: Path | None = None
        self._offset = 0
        self._size = -1
        self._mtime = 0.0
        self._partial = b""
        #: spans away: [start, end or None while still away]
        self._spans: deque[list[float | None]] = deque(maxlen=SPANS)
        self.counts: dict[str, int] = {"deskflow_reads": 0, "deskflow_switches": 0}

    def _pick(self) -> Path | None:
        best: tuple[float, Path] | None = None
        for path in self._paths():
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if best is None or mtime > best[0]:
                best = (mtime, path)
        return best[1] if best else None

    def _note(self, at: float, away: bool) -> None:
        spans = self._spans
        open_span = bool(spans) and spans[-1][1] is None
        if away and not open_span:
            spans.append([at, None])
        elif not away and open_span:
            spans[-1][1] = at
        self.counts["deskflow_switches"] += 1

    def refresh(self) -> None:
        """Read what Deskflow appended since the last look (one ``stat`` when nothing changed)."""
        path = self._path or self._pick()
        if path is None:
            return
        try:
            st = path.stat()
        except OSError:
            self._path = None
            return
        if path != self._path:
            self._path, self._offset, self._size, self._partial = path, max(0, st.st_size - READ_MAX), -1, b""
            self._spans.clear()
        if st.st_size == self._size and st.st_mtime == self._mtime:
            return
        if st.st_size < self._offset:                       # rotated or truncated: start again from its end
            self._offset, self._partial = max(0, st.st_size - READ_MAX), b""
        if st.st_size - self._offset > READ_MAX:
            self._offset, self._partial = st.st_size - READ_MAX, b""
        try:
            with open(path, "rb") as fh:
                fh.seek(self._offset)
                data = fh.read(READ_MAX)
        except OSError:
            return
        self.counts["deskflow_reads"] += 1
        self._offset += len(data)
        self._size, self._mtime = st.st_size, st.st_mtime
        data = self._partial + data
        lines = data.split(b"\n")
        self._partial = lines.pop() if lines else b""
        for raw in lines:
            if b"screen" not in raw:
                continue
            parsed = parse_line(raw.decode("utf-8", "replace"))
            if parsed is not None:
                self._note(*parsed)

    def away(self, at: float) -> bool:
        """Whether Deskflow had the cursor on another screen at epoch time ``at``."""
        self.refresh()
        for start, end in reversed(self._spans):
            if start is not None and start <= at and (end is None or at < end):
                return True
            if end is not None and end <= at:
                break
        return False


__all__ = ["DESKFLOW_IMAGES", "DeskflowScreens", "ENTERING", "LEAVING", "log_candidates", "parse_line"]
