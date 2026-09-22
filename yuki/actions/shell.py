"""PowerShell runner -- Yuki's improvisation surface.

This is deliberately not a sandbox and not a "safe subset". Windows itself is the
toolbox: .NET types, COM objects, WMI/CIM, the registry, scheduled tasks and
every command-line tool on the machine are reachable from here, and the whole
point is that the model can compose them into something nobody wrote a tool for.
So a script is handed over *verbatim* and whatever it prints comes back whole.

Two paths, same contract:

* **Persistent session** (default).  ``powershell.exe`` costs ~1.3 s to start on
  a normal desktop (2-4.7 s measured to first usable command on this one), which
  is unacceptable for an assistant that prefers shell commands over clicking, so
  :func:`prewarm` pays that cost at start-up instead of in front of the user.
  One long-lived ``-Command -`` process is kept alive
  and each script is handed to it as base64 (so multi-line scripts, quotes and
  pipes survive untouched), framed by unique sentinels on stdout and stderr so
  output can be attributed to exactly one command.  Working directory,
  variables, functions and imported modules persist between calls, as a session
  should.
* **One-shot** (``session=False``, or automatically when the session cannot be
  used).  A fresh process per command with ``-EncodedCommand`` (UTF-16LE
  base64).

What passes through intact (verified live on 2026-09-22 on both transports):
multi-line scripts, ``;``-chained statements, pipelines, single and double
quotes, here-strings (``@" ... "@``), ``if``/``foreach``/``while`` blocks,
``function`` definitions, ``[System.Math]::Sqrt(2)`` and other .NET calls,
``New-Object -ComObject WScript.Shell`` and other COM objects, and non-ASCII
text in both the script and its output (UTF-8 end to end, so Japanese titles
survive).  Nothing here parses, splits, escapes or rewrites the script.

Failure reporting: a non-zero exit code, a native tool's non-zero exit, and a
terminating PowerShell error record all come back as ``ok=False`` with the
complete error text in ``details["stderr"]`` and the message itself leading the
one-line summary, so the model can read what went wrong and try something else.
A *non-terminating* error record (``Get-ChildItem -Recurse`` over one folder it
may not read) is not a failure -- PowerShell carried on and so does this -- but
it is never hidden either: it is on ``stderr`` in full and named in the summary.

Output size: full stdout/stderr are captured, but text over
:data:`MAX_STREAM_CHARS` is clipped to a head and a tail with an explicit marker
naming the exact number of characters dropped, and the total length is always
reported.  Output is never silently shortened.

Timeouts: ``timeout_s`` is a wall-clock budget.  On expiry the child's whole
process tree is killed, the persistent session is discarded (a timed-out shell
is in an unknown state and must never be silently reused), and the result says
how long it ran and carries whatever partial output arrived first.
"""

from __future__ import annotations

import atexit
import base64
import os
import queue
import re
import shutil
import subprocess
import threading
import time
import uuid
from xml.etree import ElementTree

from yuki.actions import ActionResult

_CREATE_NO_WINDOW = 0x08000000
_SUMMARY_CHARS = 200
_ERROR_SUMMARY_CHARS = 400

#: Clip stdout (and stderr) handed back to the model above this many characters.
#: A chatty ``Get-ChildItem -Recurse`` or ``Get-Process | Format-List *`` can run
#: to hundreds of kilobytes, which would evict everything else the model knows
#: from its context for no benefit -- but a runner that quietly drops the end of
#: a listing teaches the model to trust a lie, so the clip is always announced,
#: always keeps both ends (the header *and* the tail, which is where a script's
#: conclusion usually is), and always reports the true total length.  20k chars
#: is roughly 5k tokens: big enough for a full directory listing, an installed
#: -apps table or a long playlist dump to arrive complete, small enough that a
#: runaway dump cannot take the turn down with it.
MAX_STREAM_CHARS = 20_000
_CLIP_HEAD_CHARS = 12_000
_CLIP_TAIL_CHARS = 6_000

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


def _first_line(text: str, limit: int = _SUMMARY_CHARS) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def _clip(text: str) -> tuple[str, bool]:
    """Shorten over-long output to head + tail, saying so in the middle.

    Args:
        text: full captured stream.

    Returns:
        ``(text_for_the_model, was_clipped)``.  When clipped, the returned text
        carries a marker line stating the total length and exactly how many
        characters were dropped, so the model knows the middle is missing and can
        re-run with ``Select-Object``/``Select-String``/``Out-File`` to get the
        part it actually wants.
    """
    if len(text) <= MAX_STREAM_CHARS:
        return text, False
    dropped = len(text) - _CLIP_HEAD_CHARS - _CLIP_TAIL_CHARS
    marker = (
        f"\n\n[... {dropped} of {len(text)} characters omitted here: this output was "
        f"clipped to the first {_CLIP_HEAD_CHARS} and last {_CLIP_TAIL_CHARS} "
        f"characters. Re-run filtering or paging the output if you need the middle. "
        f"...]\n\n"
    )
    return text[:_CLIP_HEAD_CHARS] + marker + text[-_CLIP_TAIL_CHARS:], True


def _b64_utf8(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


#: ``powershell.exe`` with a redirected stderr serialises its error stream as
#: CLIXML rather than text (``-OutputFormat Text`` only governs stdout), so a
#: one-shot failure would otherwise reach the model as ``#< CLIXML <Objs ...``
#: with the actual message buried in XML entities. Verified on this machine on
#: 2026-09-22.
_CLIXML_MARKER = "#< CLIXML"
_CLIXML_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _decode_clixml(text: str) -> str:
    """Turn PowerShell's CLIXML error stream back into the text a human wrote.

    Left completely alone when the marker is absent (the persistent session
    writes plain text), and falls back to the raw text if anything about the XML
    is unexpected -- losing the error message would be far worse than showing it
    in an ugly form.
    """
    if _CLIXML_MARKER not in text:
        return text
    pieces: list[str] = []
    for chunk in text.split(_CLIXML_MARKER):
        if not chunk.strip():
            continue
        try:
            root = ElementTree.fromstring(chunk.strip())
        except ElementTree.ParseError:
            pieces.append(chunk)
            continue
        strings = [
            element.text
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "S" and element.text
        ]
        pieces.append("".join(strings) if strings else chunk)
    decoded = _CLIXML_ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), "".join(pieces))
    return decoded.replace("\r\n", "\n")


class _SessionTimeout(TimeoutError):
    """The session did not finish a script in time; carries partial output."""

    def __init__(self, reason: str, stdout: str = "", stderr: str = "") -> None:
        super().__init__(reason)
        self.stdout = stdout
        self.stderr = stderr


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

    @staticmethod
    def _drain_available(sink: queue.Queue[str | None], sentinel: str) -> list[str]:
        """Whatever is already buffered, without waiting for more to arrive.

        Used on the timeout path: the script is being killed, but everything it
        managed to print is real evidence and the model should see it.
        """
        lines: list[str] = []
        while True:
            try:
                line = sink.get_nowait()
            except queue.Empty:
                return lines
            if line is None or line.startswith(sentinel):
                return lines
            lines.append(line)

    # -- execution ----------------------------------------------------------
    def run(self, command: str, timeout_s: float) -> _RunOutcome:
        """Run one script in the session.

        The script text is base64'd and revived inside the shell, then run with
        ``Invoke-Expression`` in this scope -- so it is parsed by PowerShell
        itself, exactly as if it had been typed, and anything it defines or
        changes (variables, functions, ``$PWD``, imported modules) is still there
        for the next call.

        Raises:
            _SessionTimeout: the script did not finish inside ``timeout_s``, or
                the shell died.  Carries whatever partial output arrived.
        """
        token = uuid.uuid4().hex[:12]
        out_sentinel = f"@@YUKI-OUT-{token}@@"
        err_sentinel = f"@@YUKI-ERR-{token}@@"
        # Three lines, each with balanced braces, because this goes down the
        # stdin of a live shell that executes whatever parses:
        #  1. decode the script -- on its own line so the (long) base64 never
        #     shows up in an error record's source context;
        #  2. define the frame as a one-line function -- try/finally guarantees
        #     the sentinels are written even when the script throws;
        #  3. dot-source it, so the call site PowerShell blames for an error
        #     record is the three-character ". __yuki_call" rather than a
        #     screenful of Yuki's plumbing, and so the script runs in the global
        #     scope where its variables and functions live on after this call.
        script = (
            f"$__yuki_src=[Text.Encoding]::UTF8.GetString("
            f"[Convert]::FromBase64String('{_b64_utf8(command)}'))\n"
            f"function __yuki_call{{ $__yuki_ok=$true; $global:LASTEXITCODE=0;"
            f" try{{ Invoke-Expression $__yuki_src }}"
            f"catch{{ $__yuki_ok=$false; [Console]::Error.WriteLine(($_ | Out-String).Trim()) }}"
            f"finally{{ [Console]::Error.WriteLine('{err_sentinel}');"
            # $(...) around the variables: "$var:" would be parsed as a drive
            # qualifier and blow up at parse time.
            f" Write-Output \"{out_sentinel}:$($__yuki_ok):$($LASTEXITCODE)\" }} }}\n"
            f". __yuki_call\n"
        )
        deadline = time.monotonic() + timeout_s
        self._write(script)

        stdout_lines, marker, died = self._drain(self._stdout, out_sentinel, deadline)
        if marker is None:
            # No sentinel: the script is still running (timeout) or the session
            # died.  Either way this session is unusable -- but hand back what it
            # printed before it hung, which is often the clue to why it hung.
            raise _SessionTimeout(
                "session died" if died else "timeout",
                stdout="\n".join(stdout_lines),
                stderr="\n".join(self._drain_available(self._stderr, err_sentinel)),
            )
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
        except _SessionTimeout as expired:
            _session.kill()
            _session = None
            return _RunOutcome(
                exit_code=-1,
                stdout=expired.stdout,
                stderr=expired.stderr,
                timed_out=True,
                transport="session",
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
    # powershell.exe exits 1 for "something failed" and throws the real code
    # away, while the persistent session reports $LASTEXITCODE; forwarding it
    # here keeps the two transports from disagreeing about the same script.
    script = (
        "\n".join(_PRELUDE_LINES)
        + "\n"
        + command
        + "\nif ($null -ne $LASTEXITCODE) { exit $LASTEXITCODE }"
    )
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
        stderr=_decode_clixml(decode(raw_err)),
        timed_out=timed_out,
        transport="one-shot",
    )


def run_powershell(
    command: str, *, timeout_s: float = 20.0, session: bool = True
) -> ActionResult:
    """Run a PowerShell script and capture its output.

    The script is run by PowerShell's own parser, so it can be as involved as it
    needs to be: several lines, ``;``-chained statements, pipelines, quotes,
    here-strings, loops, ``function`` definitions, .NET static calls, COM objects,
    WMI/CIM queries, registry providers, other command-line tools.  Nothing is
    split, escaped or rewritten on the way in.

    Args:
        command: the script text exactly as a user would type it - paths with
            spaces, quotes, pipes and newlines all work, because it is never
            parsed or split here.
        timeout_s: wall-clock budget.  On expiry the child's process tree is
            killed, the persistent session is thrown away (it is in an unknown
            state), and the result reports how long it ran plus any partial
            output; it is never silently re-run, because the script may already
            have had side effects.
        session: use the persistent session (fast, and variables, functions,
            ``$PWD`` and imported modules carry over to the next call).  Pass
            False for a pristine process.

    Returns:
        ActionResult with ``details = {"command", "exit_code", "stdout",
        "stderr", "stdout_chars", "stderr_chars", "clipped", "timed_out",
        "timeout_s", "transport"}``.  ``ok`` is True only for exit code 0 without
        a timeout; every failure carries the full error text in ``stderr`` and
        names its first line in ``summary``.  ``stdout``/``stderr`` are the full
        streams unless they exceeded :data:`MAX_STREAM_CHARS`, in which case they
        are head+tail around a marker that says how much was dropped and
        ``clipped`` lists which streams that happened to; ``stdout_chars`` and
        ``stderr_chars`` are always the true totals.
    """
    started = time.perf_counter()
    if not isinstance(command, str) or not command.strip():
        return ActionResult(
            ok=False,
            summary="no command given",
            details={
                "command": command,
                "exit_code": -1,
                "stdout": "",
                "stderr": "",
                "stdout_chars": 0,
                "stderr_chars": 0,
                "clipped": [],
                "timed_out": False,
                "timeout_s": timeout_s,
                "transport": "none",
            },
            elapsed_ms=(time.perf_counter() - started) * 1000.0,
        )

    outcome: _RunOutcome | None = None
    if session:
        outcome = _run_in_session(command, timeout_s)
    if outcome is None:
        outcome = _run_one_shot(command, timeout_s)

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    full_stdout = outcome.stdout.strip("\n")
    full_stderr = outcome.stderr.strip("\n")
    stdout, stdout_clipped = _clip(full_stdout)
    stderr, stderr_clipped = _clip(full_stderr)
    clipped = [
        name
        for name, was_clipped in (("stdout", stdout_clipped), ("stderr", stderr_clipped))
        if was_clipped
    ]
    sizes = f"stdout {len(full_stdout)} chars, stderr {len(full_stderr)} chars"

    if outcome.timed_out:
        partial = _first_line(stdout) or _first_line(stderr)
        summary = (
            f"powershell hit its {timeout_s:g}s timeout after running "
            f"{elapsed_ms:.0f} ms and was killed ({outcome.transport}); "
            + (
                f"partial output before the kill: {sizes}: {partial}"
                if partial
                else f"it had printed nothing yet ({sizes})"
            )
        )
    elif outcome.exit_code != 0:
        # The model recovers well from a clear failure and badly from a vague
        # one, so name the exit code and quote the error rather than just
        # reporting stream lengths.
        reason = _first_line(full_stderr, _ERROR_SUMMARY_CHARS) or _first_line(
            full_stdout, _ERROR_SUMMARY_CHARS
        )
        summary = (
            f"powershell FAILED with exit code {outcome.exit_code} after "
            f"{elapsed_ms:.0f} ms; {sizes}; "
            + (
                f"error: {reason}"
                if reason
                else "it printed nothing at all, so the exit code is all there is"
            )
        )
    else:
        head = _first_line(stdout) or _first_line(stderr) or "(no output)"
        summary = f"powershell exit 0 in {elapsed_ms:.0f} ms; {sizes}: {head}"
        if full_stdout and full_stderr:
            # Exit 0 with error records on stderr: a non-terminating error, or a
            # partial success (a recursive listing that hit one denied folder).
            # Not a failure, but the model must not miss it under the stdout.
            summary += (
                f" -- and wrote to stderr: "
                f"{_first_line(full_stderr, _ERROR_SUMMARY_CHARS)}"
            )
    if clipped:
        summary += f" [{' and '.join(clipped)} clipped to head+tail, see the marker]"

    return ActionResult(
        ok=not outcome.timed_out and outcome.exit_code == 0,
        summary=summary,
        details={
            "command": command,
            "exit_code": outcome.exit_code,
            "stdout": stdout,
            "stderr": stderr,
            "stdout_chars": len(full_stdout),
            "stderr_chars": len(full_stderr),
            "clipped": clipped,
            "timed_out": outcome.timed_out,
            "timeout_s": timeout_s,
            "transport": outcome.transport,
        },
        elapsed_ms=elapsed_ms,
    )
