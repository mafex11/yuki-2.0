"""Wall-clock benchmark for Yuki: five real requests, one real agent.

Not a test. It talks to Bedrock, moves the real desktop and costs money, so it
lives in ``scripts/`` and is only ever run on purpose::

    uv run python scripts/bench.py --effort medium --model sonnet

What it measures, per request: how many model round-trips it took, how long the
user waited, which tools ran in what order, and whether the request actually
succeeded -- checked against the machine (mute state, Notepad's text, Notepad's
process) rather than against the model's own claim that it worked.

It obeys the architecture contract's input rules: it sends no keystrokes of its
own, never Alt+F4, and closes any Notepad *it* caused to exist by terminating that
pid, so a Notepad the user already had open is left alone.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yuki.agent.loop import Agent  # noqa: E402
from yuki.config import EFFORT_LEVELS, MODEL_ALIASES, Settings  # noqa: E402
from yuki.log.events import (  # noqa: E402
    AskUser,
    ErrorEvent,
    Final,
    SessionLogger,
    ToolCall,
)

#: What the bench answers any question with, so a run never blocks on a human.
STANDING_ANSWER = "yes, go ahead"

#: Stem of the text the Notepad request asks for. A per-run tag is appended (see
#: :func:`notepad_phrase`) because Windows 11 Notepad restores unsaved tabs on
#: launch: with a fixed phrase, a run could "pass" on text a previous run typed.
NOTEPAD_STEM = "hello from yuki"


def notepad_phrase(tag: str) -> str:
    """The exact text this run asks Notepad to contain."""
    return f"{NOTEPAD_STEM} {tag}"


#: Reads the default playback device's mute flag through the Core Audio API.
#: PowerShell because there is no smaller way to ask Windows this question.
#:
#: The unnamed ``f()``..``n()`` methods are vtable padding: a COM interface is
#: called by slot, so every method before ``GetMute`` has to be declared even
#: though it is never used, and the count has to be exactly right. (An earlier
#: version of this probe had one placeholder too many, which silently called
#: ``GetChannelVolumeLevelScalar`` instead and reported "not muted" forever.)
MUTE_PROBE = r"""
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
  int f(); int g(); int h(); int i();
  int SetMasterVolumeLevelScalar(float a, System.Guid b);
  int j();
  int GetMasterVolumeLevelScalar(out float a);
  int k(); int l(); int m(); int n();
  int SetMute([MarshalAs(UnmanagedType.Bool)] bool mute, System.Guid b);
  int GetMute([MarshalAs(UnmanagedType.Bool)] out bool mute);
}
[Guid("D666063F-1587-4E43-81F1-B948E807363F"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
  int Activate(ref System.Guid id, int clsCtx, System.IntPtr act,
               [MarshalAs(UnmanagedType.IUnknown)] out object o);
}
[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"),InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
  int f(); int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice endpoint);
}
[ComImport,Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumeratorComObject { }
public class YukiBenchAudio {
  public static bool IsMuted() {
    IMMDeviceEnumerator e = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
    IMMDevice dev; e.GetDefaultAudioEndpoint(0, 1, out dev);
    System.Guid iid = typeof(IAudioEndpointVolume).GUID; object o;
    dev.Activate(ref iid, 23, System.IntPtr.Zero, out o);
    bool m; ((IAudioEndpointVolume)o).GetMute(out m); return m;
  }
}
'@ -ErrorAction SilentlyContinue
[YukiBenchAudio]::IsMuted()
"""


# ---------------------------------------------------------------------------
# Machine probes (the bench's own eyes -- it never trusts the model's word)
# ---------------------------------------------------------------------------


def powershell(script: str, *, timeout_s: float = 30.0) -> str:
    """Run a PowerShell script out-of-process and return its stdout, stripped.

    Deliberately not :func:`yuki.actions.run_powershell`: the bench must be able
    to check the machine even if Yuki's own shell session is wedged, and it must
    not perturb the session whose warm-up it is measuring.
    """
    proc = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=timeout_s,
    )
    return (proc.stdout or "").strip()


def is_muted() -> bool | None:
    """Whether the default playback device is muted, or None if unreadable."""
    try:
        out = powershell(MUTE_PROBE).splitlines()
    except Exception:
        return None
    for line in reversed(out):
        token = line.strip().lower()
        if token in {"true", "false"}:
            return token == "true"
    return None


def notepad_pids() -> set[int]:
    """Pids of every running Notepad, empty when none are."""
    out = powershell(
        "(Get-Process -Name notepad -ErrorAction SilentlyContinue | "
        "Select-Object -ExpandProperty Id) -join ','"
    )
    return {int(p) for p in out.split(",") if p.strip().isdigit()}


def notepad_text() -> str:
    """Everything the UI Automation tree exposes inside any Notepad window.

    One string, so a checker can simply ask whether the typed text is in there.
    Returns an empty string when there is no Notepad or its tree is unreadable.
    """
    from yuki.perception import get_desktop_overview, get_window_tree

    chunks: list[str] = []
    for window in get_desktop_overview().windows:
        if window.process_name.lower() != "notepad.exe":
            continue
        try:
            tree = get_window_tree(window.hwnd)
        except Exception:
            continue
        chunks.append(window.title)
        for element in tree.elements:
            chunks.append(element.name or "")
            chunks.append(element.value or "")
    return "\n".join(chunks)


def clock_mentioned(text: str, *, slack_minutes: int = 3) -> bool:
    """Whether ``text`` states the current wall-clock time.

    Accepts 24-hour and 12-hour spellings, with or without a leading zero, and a
    few minutes of slack for the round-trip.
    """
    now = datetime.now()
    for offset in range(-slack_minutes, slack_minutes + 1):
        moment = now + timedelta(minutes=offset)
        for fmt in ("%H:%M", "%I:%M"):
            stamp = moment.strftime(fmt)
            if stamp in text or stamp.lstrip("0") in text:
                return True
    return False


# ---------------------------------------------------------------------------
# The five requests
# ---------------------------------------------------------------------------


@dataclass
class Case:
    """One benchmark request and the machine check that says it worked.

    Attributes:
        request: Exactly what a user would type.
        check: Given the final text, return ``(ok, note)``. Runs after the
            request finishes, and may look at the machine.
    """

    request: str
    check: Callable[[str], tuple[bool, str]]


def _check_time(final: str) -> tuple[bool, str]:
    return (True, "") if clock_mentioned(final) else (False, "no current time in the reply")


def _check_muted(final: str) -> tuple[bool, str]:
    del final
    state = is_muted()
    if state is None:
        return False, "could not read the mute state"
    return (True, "") if state else (False, "device is not muted")


def _check_unmuted(final: str) -> tuple[bool, str]:
    del final
    state = is_muted()
    if state is None:
        return False, "could not read the mute state"
    return (False, "device is still muted") if state else (True, "")


def build_cases(tag: str, pre_existing: set[int] | None = None) -> tuple[Case, ...]:
    """The five benchmark requests, with this run's Notepad phrase baked in.

    Args:
        tag: Short per-run marker, so the Notepad check cannot be satisfied by
            text an earlier run left in a restored tab.
        pre_existing: Notepad pids that were already running. The closing request
            is about the Notepad *this run* opened, so one the user (or an earlier
            test) left open must not count against it.
    """
    phrase = notepad_phrase(tag)
    before = set(pre_existing or ())

    def check_typed(final: str) -> tuple[bool, str]:
        del final
        if not notepad_pids():
            return False, "no notepad running"
        if phrase in notepad_text():
            return True, ""
        return False, f"notepad text does not contain {phrase!r}"

    def check_closed(final: str) -> tuple[bool, str]:
        del final
        left = notepad_pids() - before
        return (True, "") if not left else (False, f"notepad {sorted(left)} is still running")

    return (
        Case("what time is it?", _check_time),
        Case("mute my pc", _check_muted),
        Case("unmute it", _check_unmuted),
        Case(f"open notepad and type: {phrase}", check_typed),
        Case("close that notepad without saving", check_closed),
    )


# ---------------------------------------------------------------------------
# Running one case
# ---------------------------------------------------------------------------


@dataclass
class Row:
    """Everything measured for one request."""

    request: str
    calls: int = 0
    wall_s: float = 0.0
    model_s: float = 0.0
    tools: list[str] = field(default_factory=list)
    final: str = ""
    ok: bool = False
    note: str = ""
    questions: int = 0
    output_tokens: int = 0
    thinking_tokens: int = 0
    cache_read_tokens: int = 0

    def as_dict(self) -> dict[str, Any]:
        """Plain dict, for ``--json``."""
        return {
            "request": self.request,
            "calls": self.calls,
            "wall_s": round(self.wall_s, 2),
            "model_s": round(self.model_s, 2),
            "tools": list(self.tools),
            "final": self.final,
            "ok": self.ok,
            "note": self.note,
            "questions": self.questions,
            "output_tokens": self.output_tokens,
            "thinking_tokens": self.thinking_tokens,
            "cache_read_tokens": self.cache_read_tokens,
        }


def run_case(agent: Agent, case: Case) -> Row:
    """Drive one request to the end and measure it.

    Answers any question with :data:`STANDING_ANSWER` so the clock keeps running
    -- the wait for a human is not what this bench is timing.
    """
    row = Row(case.request)
    events: Iterator[Any] = agent.run(case.request)
    started = time.perf_counter()
    while True:
        try:
            event = next(events)
        except StopIteration:
            break
        if isinstance(event, ToolCall):
            row.tools.append(event.name)
        elif isinstance(event, AskUser):
            row.questions += 1
            agent.answer(STANDING_ANSWER)
        elif isinstance(event, Final):
            row.final = event.text
        elif isinstance(event, ErrorEvent):
            row.final = f"[error] {event.text}"
    row.wall_s = time.perf_counter() - started

    usage = agent.logger.usage
    row.calls = usage.requests
    row.model_s = usage.wall_ms / 1000
    row.output_tokens = usage.output_tokens
    row.thinking_tokens = usage.thinking_tokens
    row.cache_read_tokens = usage.cache_read_tokens

    if row.final.startswith("[error]"):
        row.ok, row.note = False, "run ended in an error"
    else:
        row.ok, row.note = case.check(row.final)
    return row


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def print_table(rows: list[Row], *, label: str) -> None:
    """Print the per-request table and the totals."""
    print()
    print(f"=== {label} ===")
    head = f"{'request':<38} {'calls':>5} {'wall s':>7} {'model s':>8} {'ok':>4}  tools"
    print(head)
    print("-" * len(head))
    for row in rows:
        print(
            f"{_clip(row.request, 38):<38} {row.calls:>5} {row.wall_s:>7.1f} "
            f"{row.model_s:>8.1f} {('yes' if row.ok else 'NO'):>4}  "
            f"{' -> '.join(row.tools) or '(none)'}"
        )
        print(f"{'':<38} {'':>5} {'':>7} {'':>8} {'':>4}  \"{_clip(row.final, 110)}\"")
        if row.note:
            print(f"{'':<38} {'':>5} {'':>7} {'':>8} {'':>4}  ! {row.note}")
    print("-" * len(head))
    calls = sum(r.calls for r in rows)
    wall = sum(r.wall_s for r in rows)
    model = sum(r.model_s for r in rows)
    passed = sum(1 for r in rows if r.ok)
    print(
        f"{'TOTAL':<38} {calls:>5} {wall:>7.1f} {model:>8.1f} "
        f"{passed}/{len(rows)} succeeded"
    )
    if calls:
        print(f"{'':<38} {'':>5} per call {model / calls:.1f}s   overhead {wall - model:.1f}s")


def _clip(text: str, limit: int) -> str:
    """One-line, length-capped rendering."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark.

    Returns:
        ``0`` when every request succeeded, ``1`` otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--effort", choices=EFFORT_LEVELS, default=None)
    parser.add_argument("--model", default=None, help=f"alias ({', '.join(MODEL_ALIASES)}) or id")
    parser.add_argument("--label", default=None, help="Heading for the table.")
    parser.add_argument("--json", default=None, help="Write the rows to this file too.")
    parser.add_argument("--quiet", action="store_true", help="Silence the per-event log.")
    parser.add_argument(
        "--no-wait-warm",
        action="store_true",
        help="Start the first request immediately instead of letting the warm-up finish.",
    )
    parser.add_argument(
        "--only",
        type=int,
        action="append",
        help="Run only these 1-based cases (repeatable).",
    )
    args = parser.parse_args(argv)

    settings = Settings()
    if args.model:
        settings = settings.with_model(args.model)
    if args.effort:
        settings = settings.with_effort(args.effort)

    before = notepad_pids()
    logger = SessionLogger(settings.sessions_dir, quiet=args.quiet)
    all_cases = build_cases(logger.session_id.split("-")[-1], before)
    cases = [all_cases[i - 1] for i in args.only] if args.only else list(all_cases)
    label = args.label or f"{settings.model.split('.')[-1]} effort={settings.effort}"
    print(f"bench: {label}  session {logger.session_id}")
    if before:
        print(f"note: notepad was already running before this run (pids {sorted(before)})")

    construction_started = time.perf_counter()
    agent = Agent(settings, logger)
    construct_s = time.perf_counter() - construction_started
    print(f"agent constructed in {construct_s:.2f}s (warm-up runs in the background)")

    # A real user spends a few seconds typing, by which time the background
    # warm-up has finished; a bench that fires instantly would race it and
    # measure a cold first request no matter how good the warm-up is. Waiting
    # here (and reporting the wait) keeps the first row comparable.
    warm_s = 0.0
    if not args.no_wait_warm:
        warm_started = time.perf_counter()
        finished = agent.wait_for_prewarm(90)
        warm_s = time.perf_counter() - warm_started
        print(f"waited {warm_s:.1f}s for warm-up ({'done' if finished else 'still running'})")

    rows: list[Row] = []
    try:
        for case in cases:
            rows.append(run_case(agent, case))
    finally:
        closed = close_new_notepads(before)
        print_table(rows, label=label)
        print(
            f"construct {construct_s:.2f}s | warm-up wait {warm_s:.1f}s | "
            f"notepads closed by pid: {closed or 'none'}"
        )
        print(f"log: {logger.path}")
        if args.json:
            Path(args.json).write_text(
                json.dumps(
                    {
                        "label": label,
                        "model": settings.model,
                        "effort": settings.effort,
                        "session": logger.session_id,
                        "construct_s": round(construct_s, 2),
                        "warm_wait_s": round(warm_s, 2),
                        "rows": [r.as_dict() for r in rows],
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        logger.close()
    return 0 if all(r.ok for r in rows) else 1


def close_new_notepads(before: set[int]) -> list[int]:
    """Terminate only the Notepads that appeared during this run.

    By pid, per the architecture contract: no Alt+F4, no keystrokes, and a
    Notepad the user already had open is never touched.
    """
    leftover = sorted(notepad_pids() - before)
    if leftover:
        powershell(
            "Stop-Process -Id " + ",".join(str(p) for p in leftover) + " -Force "
            "-ErrorAction SilentlyContinue"
        )
    return leftover


if __name__ == "__main__":
    sys.exit(main())
