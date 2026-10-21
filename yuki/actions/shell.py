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

Timeouts: ``timeout_s`` is a wall-clock budget.  On expiry the shell and
what the command started are killed (apps launched by earlier commands are not), the persistent session is discarded (a timed-out shell
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
from typing import Any
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

#: A3 (Z7): the measured backstop, only for a command the model wrote (``run_powershell(..., reap=True)``: the
#: Dispatcher's ``run_powershell`` tool; code's own commands, launch.py's Start-Process among them, never get it).
#: After such a command returns, each branch it started (a process the session shell started since the command
#: began, with everything below it) that is still alive after a short settle and has no visible window anywhere in it
#: is ended whole, children first; and the event subscriptions and jobs that can still act which it added to the
#: session are removed. The result then says so (``left_running``: branches + subscriptions + jobs) and ``ok`` is
#: False. Process-tree and window facts only, never the command's words.
#:
#: The settle polls up to :data:`_REAP_SETTLE_S` and ends early the moment no started branch is windowless, so an
#: app's windowless helpers (a renderer, a GPU process, a terminal's shell) are kept with the window above them.
#: Nothing is ended when the windows can't be read, or when a new visible window came up during the command whose
#: owner is outside the shell's tree (a console Windows Terminal hosts, a single-instance app's existing process):
#: whose it is can't be told, and uncertainty never kills. Known gaps (A2's ``later`` check is the guard there): a
#: process whose middle parent already exited (``cmd /c start``, WMI's ``Win32_Process.Create``, a scheduled task),
#: in-process .NET timers, and the one-shot transport.
_REAP_SETTLE_S = 4.0
_REAP_POLL_S = 0.25
#: The jobs that can no longer act (finished): never counted or removed (the model may still Receive-Job one).
_JOBS_DONE = ("Completed", "Failed", "Stopped")
#: The session probe that lists what could fire later (ids only, content-free): event subscriptions, and the jobs that
#: can still act. It runs in its own script block (``& { }``) under Yuki-prefixed names, so nothing it assigns lands
#: in the session's scope (the model's own ``$s`` / ``$j`` stay theirs).
_SESSION_STATE_PROBE = (
    "& { $__yuki_subs=@(Get-EventSubscriber -Force -ErrorAction Ignore | ForEach-Object { $_.SubscriptionId }); "
    "$__yuki_jobs=@(Get-Job -ErrorAction Ignore | Where-Object { @("
    + ",".join(f"'{s}'" for s in _JOBS_DONE)
    + ") -notcontains [string]$_.State } | ForEach-Object { $_.Id }); "
    "Write-Output ('YUKISUBS=' + ($__yuki_subs -join ',')); Write-Output ('YUKIJOBS=' + ($__yuki_jobs -join ',')) }"
)

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


def _kill_tree(pid: int, *, since: float | None = None) -> None:
    """Kill a shell, and those of its descendants started at or after ``since``.

    ``since`` is when the command being stopped began (epoch seconds): what it
    started is killed with it. Descendants started earlier are apps an earlier
    command launched (``Start-Process notepad``) - the user's windows now -
    and are left running, as is everything when ``since`` is ``None`` (the
    session closing at exit). Until 2026-09-24 this was ``taskkill /F /T``,
    which killed every app Yuki had ever launched from the session when Yuki
    exited or a command timed out (seen live: Notepad, opened with a file,
    closed the moment the launching process ended).
    """
    try:
        import psutil

        root = psutil.Process(pid)
    except Exception:
        return
    victims = []
    if since is not None:
        try:
            for child in root.children(recursive=True):
                try:
                    if child.create_time() >= since - 0.5:
                        victims.append(child)
                except Exception:
                    continue
        except Exception:
            pass
    for proc in [*victims, root]:
        try:
            proc.kill()
        except Exception:
            pass


def _visible_windows() -> dict[int, int]:
    """A3: ``{hwnd: owner pid}`` of the visible top-level windows now (read-only Win32). Empty when it can't be read,
    and the backstop then ends nothing (it ends only a branch it confirmed windowless)."""
    found: dict[int, int] = {}
    try:
        import win32gui
        import win32process
    except Exception:  # noqa: BLE001 - no pywin32: nothing can be confirmed windowless, so nothing is ended
        return found

    def _cb(hwnd: int, _: object) -> bool:
        try:
            if win32gui.IsWindowVisible(hwnd):
                _tid, owner = win32process.GetWindowThreadProcessId(hwnd)
                if owner:
                    found[int(hwnd)] = int(owner)
        except Exception:  # noqa: BLE001
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:  # noqa: BLE001
        return {}
    return found


def _ppid_map() -> dict[int, int]:
    """A3: ``{pid: parent pid}`` of every process in one system snapshot (as ``launch._ppid_map``: psutil's own
    per-process ``ppid`` takes this same snapshot once per process on Windows). Empty when it can't be read."""
    try:
        import psutil

        return dict(psutil._psplatform.ppid_map())   # noqa: SLF001 - psutil's own one-snapshot reader
    except Exception:  # noqa: BLE001
        return {}


def _below(root: int, kids: dict[int, list[int]]) -> list[int]:
    """Every pid below ``root`` in ``kids`` (parent -> children), parents before their children; cycle-safe."""
    out: list[int] = []
    seen = {root}
    queue_ = list(kids.get(root, ()))
    while queue_:
        pid = queue_.pop(0)
        if pid in seen:
            continue
        seen.add(pid)
        out.append(pid)
        queue_.extend(kids.get(pid, ()))
    return out


def _reap_plan(shell: int, ppids: dict[int, int], is_new, windows: dict[int, int],
               before: frozenset[int] | None) -> list[list[int]] | None:
    """A3 (Z7), the decision alone (pure: the caller reads the facts). Which branches a command started are to be
    ended: a branch is a process the session shell started since the command began (``is_new(pid)``: its create time)
    with everything below it, and it is ended only when no process in it owns a visible window. ``ppids``: one process
    snapshot; ``windows``: the visible top-level windows now (``{hwnd: owner pid}``); ``before``: the visible windows'
    hwnds just before the command (``None``: not read).

    Returns the branches to end, each listed children first (``[]``: none), or ``None`` to keep everything: the
    windows couldn't be read, or a window that was not up before the command came up owned by a process outside the
    shell's tree (it may host what the command started: Windows Terminal hosting a console, a single-instance app
    taking a launch over), so no branch can be confirmed windowless."""
    if not windows or not ppids:
        return None
    kids: dict[int, list[int]] = {}
    for pid, parent in ppids.items():
        if pid != parent:
            kids.setdefault(parent, []).append(pid)
    tree = set(_below(shell, kids))
    if before is not None and any(h not in before and pid != shell and pid not in tree for h, pid in windows.items()):
        return None
    owners = set(windows.values())
    out: list[list[int]] = []
    for top in kids.get(shell, ()):
        if not is_new(top):
            continue   # an app an earlier command launched (the user's now), and all below it: never this command's
        members = [top, *_below(top, kids)]
        if any(pid in owners for pid in members):
            continue   # a window anywhere in the branch: kept whole (an app's windowless helpers belong to it)
        # what is ended is only what this command started (a pid reused by an older process is left alone)
        out.append([pid for pid in reversed(members) if pid == top or is_new(pid)])
    return out


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

    __slots__ = ("exit_code", "stdout", "stderr", "timed_out", "transport", "left_running", "shell_reset")

    def __init__(
        self,
        exit_code: int,
        stdout: str,
        stderr: str,
        timed_out: bool,
        transport: str,
        left_running: int = 0,
        shell_reset: bool = False,
    ) -> None:
        self.exit_code = exit_code
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out
        self.transport = transport
        #: A3 (Z7): how many things the backstop ended that the command left running unseen: windowless process
        #: branches, event subscriptions, jobs that could still act.
        self.left_running = left_running
        #: A3 (review M3): the backstop's own session probe or removal didn't finish, so the shell was ended after the
        #: command (a shell in an unknown state is never reused): the next command starts a fresh one.
        self.shell_reset = shell_reset


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
        #: A3 (Z7): the event-subscription and job ids already in the session (a fresh shell starts with none), so the
        #: backstop removes only what a command adds and never touches state Yuki itself put there.
        self._known_subs: set[str] = set()
        self._known_jobs: set[str] = set()

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

    def kill(self, *, command_started: float | None = None) -> None:
        """End the shell; with ``command_started``, also what that command started."""
        if self._process.poll() is None:
            _kill_tree(self._process.pid, since=command_started)
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

    # -- A3 (Z7): the measured backstop ------------------------------------
    def reap_after(self, command_started: float, before: frozenset[int] | None) -> tuple[int, bool]:
        """End what the command just left running unseen: its windowless process branches, and the event
        subscriptions or jobs that can still act which it added to the session. ``before``: the visible windows'
        hwnds just before the command. Returns ``(how many were ended, broken)``; ``broken``: the session probe or its
        removal didn't finish (or the shell broke), so the shell is in an unknown state and the caller must discard
        it. Never raises: the backstop never breaks a command that otherwise worked."""
        try:
            killed = self._reap_processes(float(command_started), before)
        except Exception:  # noqa: BLE001
            killed = 0
        removed, broken = self._reap_session_state()
        return killed + removed, broken

    def _reap_processes(self, command_started: float, before: frozenset[int] | None) -> int:
        """Poll up to :data:`_REAP_SETTLE_S` for each branch the command started to show a window (:func:`_reap_plan`);
        at the deadline, end the branches still windowless, each whole and children first. Returns how many branches
        were ended."""
        try:
            import psutil
        except Exception:  # noqa: BLE001 - no psutil: nothing is ended
            return 0
        shell = int(self._process.pid)
        created: dict[int, bool] = {}

        def is_new(pid: int) -> bool:
            # created since the command began (as _kill_tree tells it); one read per pid per command
            if pid not in created:
                try:
                    created[pid] = psutil.Process(pid).create_time() >= command_started - 0.5
                except Exception:  # noqa: BLE001 - gone or unreadable: not this command's to end
                    created[pid] = False
            return created[pid]

        deadline = time.monotonic() + _REAP_SETTLE_S
        while True:
            ppids = _ppid_map()
            if not any(parent == shell and is_new(pid) for pid, parent in ppids.items() if pid != parent):
                return 0   # nothing the command started is still running under the shell
            plan = _reap_plan(shell, ppids, is_new, _visible_windows(), before)
            if not plan:
                return 0   # every branch shows a window, or nothing could be confirmed windowless: all kept
            if time.monotonic() >= deadline:
                return sum(1 for branch in plan if _end_branch(branch, command_started))
            time.sleep(_REAP_POLL_S)

    def _session_state(self, timeout_s: float = 2.0) -> tuple[set[str], set[str]]:
        """The ids of the event subscriptions, and of the jobs that can still act, the session holds now (digits only;
        content-free). Raises when the probe didn't finish (:class:`_SessionTimeout`) or the shell broke."""
        outcome = self.run(_SESSION_STATE_PROBE, timeout_s)
        subs: set[str] = set()
        jobs: set[str] = set()
        for line in (outcome.stdout or "").splitlines():
            if line.startswith("YUKISUBS="):
                subs = {t.strip() for t in line[len("YUKISUBS="):].split(",") if t.strip().isdigit()}
            elif line.startswith("YUKIJOBS="):
                jobs = {t.strip() for t in line[len("YUKIJOBS="):].split(",") if t.strip().isdigit()}
        return subs, jobs

    def _reap_session_state(self) -> tuple[int, bool]:
        """Remove the event subscriptions and the still-acting jobs the command added (ids not in the session before
        it ran); leave any that pre-dated it, and finished jobs, untouched. Returns ``(how many were removed,
        broken)``: ``broken`` when the probe or the removal didn't finish (review M3: the caller then discards the
        shell, whose subscriptions and in-process actions end with it)."""
        try:
            subs, jobs = self._session_state()
        except Exception:  # noqa: BLE001 - a timed-out or broken probe: the shell is in an unknown state
            return 0, True
        new_subs = subs - self._known_subs
        new_jobs = jobs - self._known_jobs
        removed = 0
        if new_subs or new_jobs:
            parts = [f"Unregister-Event -SubscriptionId {int(s)} -Force -ErrorAction Ignore" for s in sorted(new_subs)]
            parts += [f"Remove-Job -Id {int(j)} -Force -ErrorAction Ignore" for j in sorted(new_jobs)]
            try:
                self.run("& { " + "; ".join(parts) + " }", 5.0)
            except Exception:  # noqa: BLE001 - the removal didn't finish: unknown state, the caller ends the shell
                return len(new_subs) + len(new_jobs), True
            removed = len(new_subs) + len(new_jobs)
        # the ids that pre-dated the command stay the baseline (what was added is gone now)
        self._known_subs = subs - new_subs
        self._known_jobs = jobs - new_jobs
        return removed, False


def _end_branch(branch: list[int], command_started: float) -> bool:
    """A3: end one windowless branch the command started, children first (``branch`` is listed so), each pid read
    again so a pid reused since the plan is never ended. True when any of it was ended."""
    try:
        import psutil
    except Exception:  # noqa: BLE001
        return False
    ended = False
    for pid in branch:
        try:
            proc = psutil.Process(pid)
            if proc.create_time() < command_started - 0.5:
                continue   # the pid now belongs to an older process: not this command's
            proc.kill()
            ended = True
        except Exception:  # noqa: BLE001 - already gone, or not ours to end
            continue
    return ended


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


def _run_in_session(command: str, timeout_s: float, *, reap: bool = False) -> _RunOutcome | None:
    """Run via the persistent session; ``None`` when it is unavailable.

    A timeout leaves the shell in an unknown state, so the session is killed
    and the caller is told it timed out - never silently retried, because the
    script may well have had side effects already.

    ``reap`` (A3, Z7): after the command, the measured backstop ends what it
    left running unseen (:meth:`_Session.reap_after`); only for a command the
    model wrote.
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
        # A3: the visible windows just before the command (a window that comes up outside the shell's tree during it
        # keeps every branch: _reap_plan)
        before = frozenset(_visible_windows()) if reap else None
        command_started = time.time()
        try:
            outcome = _session.run(command, timeout_s)
        except _SessionTimeout as expired:
            _session.kill(command_started=command_started)
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
        if not reap:
            return outcome
        # A3 (Z7): the command the model wrote returned; end what it left acting on its own after it (windowless
        # process branches, session subscriptions, jobs that can still act). Measured, never the command's words.
        try:
            ended, broken = _session.reap_after(command_started, before)
        except Exception:  # noqa: BLE001 - the backstop never breaks a command that worked
            ended, broken = 0, False
        outcome.left_running = ended
        if broken:
            # review M3: the backstop's probe or removal didn't finish, so the shell is in an unknown state (a late
            # probe's lines would land in the next command's output): never reused. Only the shell is ended
            # (``since=None``): what the command started and was kept (a windowed app) stays; the session's event
            # subscriptions and in-process actions end with the shell.
            _session.kill()
            _session = None
            outcome.shell_reset = True
        return outcome


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
    started = time.time()
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
        _kill_tree(process.pid, since=started)
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
    command: str, *, timeout_s: float = 20.0, session: bool = True, reap: bool = False
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
        reap: A3 (Z7): a command the model wrote (the Dispatcher's
            ``run_powershell`` tool passes True). After it returns, the session's
            measured backstop ends what it left running unseen: each windowless
            process branch it started, and the event subscriptions and jobs that
            can still act which it added. Code's own commands (launch.py's
            Start-Process) keep the default False. The one-shot transport has no
            backstop.

    Returns:
        ActionResult with ``details = {"command", "exit_code", "stdout",
        "stderr", "stdout_chars", "stderr_chars", "clipped", "timed_out",
        "timeout_s", "transport", "left_running", "shell_reset"}``.  ``ok`` is
        True only for exit code 0 without a timeout and with nothing left running
        (A3: the backstop ended ``left_running`` windowless process branches /
        session subscriptions / jobs the command left acting on its own;
        ``shell_reset``: the backstop could not finish its session probe, so the
        shell was ended after the command and the next one starts fresh); every
        failure carries the full error text in ``stderr`` and
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
        outcome = _run_in_session(command, timeout_s, reap=bool(reap))
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

    # A3 (Z7): the backstop ended things the command left acting on its own; say so, and fail the step (its words
    # asked for a result, not an unattended mechanism). The content-free log line (a count) is the caller's: the brain
    # executor's ``shell_backstop`` event, which a session log records.
    left_running = int(getattr(outcome, "left_running", 0) or 0)
    shell_reset = bool(getattr(outcome, "shell_reset", False))
    if left_running:
        summary += f" [ended {left_running} thing(s) it left running with no window]"
    if shell_reset:
        summary += " [Yuki's shell was restarted after it: variables from earlier commands are gone]"

    extra: dict[str, Any] = {"shell_reset": True} if shell_reset else {}
    return ActionResult(
        ok=not outcome.timed_out and outcome.exit_code == 0 and left_running == 0,
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
            "left_running": left_running,
            **extra,
        },
        elapsed_ms=elapsed_ms,
    )
