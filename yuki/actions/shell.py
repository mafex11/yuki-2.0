"""PowerShell runner.

Two paths, same contract:

* **Persistent session** (default).  ``powershell.exe`` costs ~1.3 s to start on
  a normal desktop (2-4.7 s measured to first usable command on this one), which
  is unacceptable for an assistant that prefers shell commands over clicking, so
  :func:`prewarm` pays that cost at start-up instead of in front of the user.
  One long-lived ``-Command -`` process is kept alive
  and each script is handed to it as base64 (so multi-line scripts, quotes and
  pipes survive untouched), framed by unique sentinels on stdout and stderr so
  output can be attributed to exactly one command.  Working directory,
  variables and imported modules persist between calls, as a session should.
* **One-shot** (``session=False``, or automatically when the session cannot be
  used).  A fresh process per command with ``-EncodedCommand`` (UTF-16LE
  base64).

Either way: nothing is ever split on whitespace, output is UTF-8 on both sides,
stdout/stderr/exit code are captured, and a run that overstays its welcome has
its whole process tree killed.
"""

from __future__ import annotations

import atexit
import base64
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid

from yuki.actions import ActionResult

_CREATE_NO_WINDOW = 0x08000000
_SUMMARY_CHARS = 200

#: UTF-8 in and out (so non-ASCII output survives) and no progress records,
#: which PowerShell otherwise serialises onto stderr.
_PRELUDE_LINES = (
    "[Console]::OutputEncoding=[System.Text.Encoding]::UTF8",
    "$OutputEncoding=[System.Text.Encoding]::UTF8",
    "$ProgressPreference='SilentlyContinue'",
)


def _powershell_path() -> str:
    """Absolute path to powershell.exe, falling back to a PATH lookup."""
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    candidate = os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )
    if os.path.isfile(candidate):
        return candidate
    return shutil.which("powershell") or "powershell.exe"


def _kill_tree(pid: int) -> None:
    """Kill a process and its children (``Popen.kill`` leaves children alive)."""
    subprocess.run(
        ["taskkill", "/F", "/T", "/PID", str(pid)],
        capture_output=True,
        creationflags=_CREATE_NO_WINDOW,
        check=False,
    )


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:_SUMMARY_CHARS]
    return ""


def _b64_utf8(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


class _RunOutcome:
    """Raw result of one script, before it becomes an ActionResult."""

    __slots__ = ("exit_code", "stdout", "stderr", "timed_out", "transport")

    def __init__(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
        transport: str,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.transport = transport


class _Session:
    """A live PowerShell process driven over stdin with sentinel framing."""

    def __init__(self) -> None:
        self._process = subprocess.Popen(  # noqa: S603 - fixed executable
            [
                _powershell_path(),
                "-NoProfile",
                "-NonInteractive",
                "-OutputFormat",
                "Text",
                "-Command",
                "-",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=_CREATE_NO_WINDOW,
        )
        self._stdout: queue.Queue[str | None] = queue.Queue()
        self._stderr: queue.Queue[str | None] = queue.Queue()
        self._pump(self._process.stdout, self._stdout)
        self._pump(self._process.stderr, self._stderr)
        self._write("\n".join(_PRELUDE_LINES) + "\n")

    # -- plumbing -----------------------------------------------------------
    def _pump(self, stream, sink: queue.Queue[str | None]) -> None:
        def reader() -> None:
            try:
                for line in iter(stream.readline, b""):
                    sink.put(line.decode("utf-8", errors="replace").rstrip("\r\n"))
            except Exception:
                pass
            finally:
                sink.put(None)  # EOF marker

        threading.Thread(
            target=reader, name="yuki-powershell-reader", daemon=True
        ).start()

    def _write(self, text: str) -> None:
        assert self._process.stdin is not None
        self._process.stdin.write(text.encode("utf-8"))
        self._process.stdin.flush()

    @property
    def alive(self) -> bool:
        return self._process.poll() is None

    def kill(self) -> None:
        if self._process.poll() is None:
            _kill_tree(self._process.pid)
        try:
            if self._process.stdin is not None:
                self._process.stdin.close()
        except Exception:
            pass

    @staticmethod
    def _drain(sink: queue.Queue[str | None], sentinel: str, deadline: float) -> tuple[list[str], str | None, bool]:
        """Collect lines until ``sentinel`` (returned separately) or deadline."""
        lines: list[str] = []
        while time.monotonic() < deadline:
            try:
                line = sink.get(timeout=0.02)
            except queue.Empty:
                continue
            if line is None:
                return lines, None, True  # the process died
            if line.startswith(sentinel):
                return lines, line, False
            lines.append(line)
        return lines, None, False

    # -- execution ----------------------------------------------------------
    def run(self, command: str, timeout_s: float) -> _RunOutcome:
        """Run one script in the session.  Raises on a broken session."""
        token = uuid.uuid4().hex[:12]
        out_sentinel = f"@@YUKI-OUT-{token}@@"
        err_sentinel = f"@@YUKI-ERR-{token}@@"
        # Decoding on its own line keeps the (long) base64 out of PowerShell's
        # error context, and try/finally guarantees the sentinels are written
        # even when the script throws a terminating error.
        script = (
            f"$__yuki_src=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64_utf8(command)}'))\n"
            f"$__yuki_ok=$true; $global:LASTEXITCODE=0\n"
            f"try{{ Invoke-Expression $__yuki_src }}"
            f"catch{{ $__yuki_ok=$false; [Console]::Error.WriteLine(($_ | Out-String).Trim()) }}"
            f"finally{{ [Console]::Error.WriteLine('{err_sentinel}');"
            # $(...) around the variables: "$var:" would be parsed as a drive
            # qualifier and blow up at parse time.
            f" Write-Output \"{out_sentinel}:$($__yuki_ok):$($LASTEXITCODE)\" }}\n"
        )
        deadline = time.monotonic() + timeout_s
        self._write(script)

        stdout_lines, marker, died = self._drain(self._stdout, out_sentinel, deadline)
        if marker is None:
            # No sentinel: the script is still running (timeout) or the session
            # died.  Either way this session is unusable.
            raise TimeoutError("session died" if died else "timeout")
        stderr_lines, _, _ = self._drain(
            self._stderr, err_sentinel, max(deadline, time.monotonic() + 0.5)
        )
        # marker == "<sentinel>:<$?>:<$LASTEXITCODE>"; the sentinel has no colons.
        fields = marker.split(":")
        ok_text = fields[-2] if len(fields) >= 3 else "True"
        code_text = fields[-1] if len(fields) >= 3 else "0"
        try:
            exit_code = int(code_text or 0)
        except ValueError:
            exit_code = 0
        if ok_text.strip().lower() == "false" and exit_code == 0:
            exit_code = 1  # terminating error that set no native exit code
        return _RunOutcome(
            exit_code=exit_code,
            stdout="\n".join(stdout_lines),
            stderr="\n".join(stderr_lines),
            timed_out=False,
            transport="session",
        )


_session_lock = threading.Lock()
_session: _Session | None = None


def close_session() -> None:
    """Terminate the persistent PowerShell session, if one is running."""
    global _session
    with _session_lock:
        if _session is not None:
            _session.kill()
            _session = None


atexit.register(close_session)


def _run_in_session(command: str, timeout_s: float) -> _RunOutcome | None:
    """Run via the persistent session; ``None`` when it is unavailable.

    A timeout leaves the shell in an unknown state, so the session is killed
    and the caller is told it timed out - never silently retried, because the
    script may well have had side effects already.
    """
    global _session
    with _session_lock:
        if _session is not None and not _session.alive:
            _session.kill()
            _session = None
        if _session is None:
            try:
                _session = _Session()
            except Exception:
                return None
        try:
            return _session.run(command, timeout_s)
        except TimeoutError:
            _session.kill()
            _session = None
            return _RunOutcome(
                exit_code=-1, stdout="", stderr="", timed_out=True, transport="session"
            )
        except Exception:
            # Broken pipe before the script could run: fall back to one-shot.
            _session.kill()
            _session = None
            return None


def prewarm(*, timeout_s: float = 30.0) -> bool:
    """Start the persistent session now, so the first real command is fast.

    Starting ``powershell.exe`` and getting it to the point where it will run a
    script measured 2-4.7 s on this machine, and whoever asks for the first
    command of a session pays all of it. Calling this at start-up moves that cost
    off the critical path.

    It runs one trivial script rather than only spawning the process: the
    ``_Session`` constructor returns as soon as ``Popen`` does, while the shell is
    still loading, so a session that has never round-tripped is not actually warm.

    Blocks for as long as the shell takes, so call it from a background thread.
    Never raises: a machine where PowerShell cannot start still gets a working
    Yuki, it just finds out when something really needs the shell.

    Args:
        timeout_s: Budget for the warm-up round-trip. Generous, because a cold
            shell on a busy machine is exactly the case being waited on.

    Returns:
        True when the session answered and is live.
    """
    try:
        outcome = _run_in_session("$null", timeout_s)
    except Exception:
        return False
    if outcome is None or outcome.timed_out:
        return False
    with _session_lock:
        return _session is not None and _session.alive


def _run_one_shot(command: str, timeout_s: float) -> _RunOutcome:
    """Run in a fresh process with -EncodedCommand (UTF-16LE base64)."""
    script = "\n".join(_PRELUDE_LINES) + "\n" + command
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    process = subprocess.Popen(  # noqa: S603 - fixed executable, encoded script
        [
            _powershell_path(),
            "-NoProfile",
            "-NonInteractive",
            "-OutputFormat",
            "Text",
            "-EncodedCommand",
            encoded,
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=_CREATE_NO_WINDOW,
    )
    timed_out = False
    try:
        raw_out, raw_err = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(process.pid)
        try:
            raw_out, raw_err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:  # pragma: no cover - taskkill failed
            process.kill()
            raw_out, raw_err = b"", b""
    decode = lambda raw: (raw or b"").decode("utf-8", errors="replace").replace(  # noqa: E731
        "\r\n", "\n"
    )
    return _RunOutcome(
        exit_code=-1 if timed_out else (process.returncode or 0),
        stdout=decode(raw_out),
        stderr=decode(raw_err),
        timed_out=timed_out,
        transport="one-shot",
    )


def run_powershell(
    command: str, *, timeout_s: float = 20.0, session: bool = True
) -> ActionResult:
    """Run a PowerShell script and capture its output.

    Args:
        command: the script text exactly as a user would type it - paths with
            spaces, quotes, pipes and newlines all work, because it is never
            parsed or split here.
        timeout_s: wall-clock budget; on expiry the shell is killed.
        session: use the persistent session (fast, keeps state).  Pass False for
            a pristine process.

    Returns:
        ActionResult with ``details = {"exit_code", "stdout", "stderr",
        "timed_out", "transport", "command"}``.  ``ok`` is True only for exit
        code 0 without a timeout.
    """
    started = time.perf_counter()
    if not isinstance(command, str) or not command.strip():
        return ActionResult(
            ok=False,
            summary="no command given",
            details={"command": command, "exit_code": -1, "stdout": "", "stderr": "", "timed_out": False, "transport": "none"},
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    outcome: _RunOutcome | None = None
    if session:
        outcome = _run_in_session(command, timeout_s)
    if outcome is None:
        outcome = _run_one_shot(command, timeout_s)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    stdout = outcome.stdout.strip("\n")
    stderr = outcome.stderr.strip("\n")
    if outcome.timed_out:
        summary = (
            f"powershell timed out after {timeout_s:g}s and was killed "
            f"({outcome.transport})"
        )
    else:
        head = _first_line(stdout) or _first_line(stderr) or "(no output)"
        summary = (
            f"powershell exit {outcome.exit_code} in {elapsed_ms:.0f} ms; "
            f"stdout {len(stdout)} chars, stderr {len(stderr)} chars: {head}"
        )
    return ActionResult(
        ok=not outcome.timed_out and outcome.exit_code == 0,
        summary=summary,
        details={
            "command": command,
            "exit_code": outcome.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "timed_out": outcome.timed_out,
            "transport": outcome.transport,
        },
        elapsed_ms=elapsed_ms,
    )
