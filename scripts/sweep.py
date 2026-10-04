"""Run the test suite the way it has to run here: one pytest process per test file, then a summary.

The suite cannot run in one process (it dies silently about a fifth of the way in: report 76, R5), so every builder
hand-assembled a "one file per process" list. This is that list, the same every time:

1. every ``tests/test_*.py`` (or the files given) in its own ``python -m pytest <file> -q`` process, ``--jobs`` at a
   time (default 1: the PC may be in use), each with a time limit;
2. each failure is run again once, alone: the failing tests by id when the first run named them, else the whole file;
3. a test that failed and then passed alone is **flaky**; one that failed both times is **failed**; a file that did
   not finish in time is **timeout**.

The summary is printed; ``--json PATH`` writes it too (per file: result, seconds, counts, failed and flaky test ids).
``--clock HH:MM`` pins every test's wall clock (``YUKI_TEST_CLOCK``, tests/conftest.py): a sweep at ``00:30`` finds
the tests that fail after midnight. Exit code: 0 when nothing failed (flaky only is 0 too, with a warning), else 1.

Usage::

    python scripts/sweep.py                      # everything, one file at a time
    python scripts/sweep.py tests/test_cost*.py  # some files
    python scripts/sweep.py --jobs 3 --json %TEMP%\\sweep.json --clock 00:30

Run it with the project's interpreter from the worktree (``C:/Users/yukidev/yuki/.venv/Scripts/python.exe``), never
through ``uv run`` while the app may be running.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FileRun:
    """One test file's sweep result."""

    file: str
    result: str = "passed"          # passed | failed | flaky | timeout | error | empty
    seconds: float = 0.0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failed_ids: list[str] = field(default_factory=list)
    flaky_ids: list[str] = field(default_factory=list)
    note: str = ""


@dataclass
class Outcome:
    code: int
    seconds: float
    timed_out: bool
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    failed_ids: list[str] = field(default_factory=list)
    tail: str = ""


def _junit_counts(path: Path, file: str = "") -> tuple[int, int, int, list[str]] | None:
    """``(passed, failed, skipped, failed test ids)`` from pytest's junit XML, or None when there is none."""
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError):
        return None
    passed = failed = skipped = 0
    ids: list[str] = []
    for case in root.iter("testcase"):
        kids = {child.tag for child in case}
        if kids & {"failure", "error"}:
            failed += 1
            ids.append(_node_id(case.get("classname") or "", case.get("name") or "", file))
        elif "skipped" in kids:
            skipped += 1
        else:
            passed += 1
    return passed, failed, skipped, ids


def _node_id(classname: str, name: str, file: str = "") -> str:
    """pytest's test id from junit's ``classname`` (``tests.test_x`` or ``tests.test_x.TestCase``) and ``name``;
    ``file``: the test file the run was given (its id is that path, wherever the file is)."""
    parts = [p for p in classname.split(".") if p]
    stem = Path(file).stem if file else ""
    if stem and stem in parts:
        i = parts.index(stem)
        return "::".join([file, *parts[i + 1:], name])
    for i in range(len(parts), 0, -1):
        module = "/".join(parts[:i]) + ".py"
        if (ROOT / module).is_file():
            return "::".join([module, *parts[i:], name])
    return name


#: Up to this many failing tests are run again one process each; more go again together, in one process.
ALONE_MAX = 5


def run_pytest(targets: list[str], *, timeout_s: float, python: str, env: dict[str, str], file: str = "") -> Outcome:
    """One pytest process on ``targets`` (a file, or test ids of one file); never raises."""
    fd, junit = tempfile.mkstemp(prefix="sweep-", suffix=".xml")
    os.close(fd)
    junit_path = Path(junit)
    cmd = [python, "-m", "pytest", *targets, "-q", "-p", "no:cacheprovider", f"--junitxml={junit_path}"]
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=timeout_s)
        code, out, timed_out = proc.returncode, (proc.stdout or "") + (proc.stderr or ""), False
    except subprocess.TimeoutExpired as exc:
        code, timed_out = -1, True
        out = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
    seconds = round(time.monotonic() - started, 1)
    counts = _junit_counts(junit_path, file)
    try:
        junit_path.unlink()
    except OSError:
        pass
    outcome = Outcome(code, seconds, timed_out, tail="\n".join(out.strip().splitlines()[-3:]))
    if counts is not None:
        outcome.passed, outcome.failed, outcome.skipped, outcome.failed_ids = counts
    return outcome


def sweep_file(path: str, *, timeout_s: float, python: str, env: dict[str, str]) -> FileRun:
    """Run one file; run its failures again once, alone; classify."""
    rel = Path(path).resolve().relative_to(ROOT).as_posix() if Path(path).resolve().is_relative_to(ROOT) else path
    first = run_pytest([rel], timeout_s=timeout_s, python=python, env=env, file=rel)
    run = FileRun(rel, seconds=first.seconds, passed=first.passed, failed=first.failed, skipped=first.skipped)
    if first.timed_out:
        run.result, run.note = "timeout", f"no end within {timeout_s:g} s"
        return run
    if first.code == 5 or (first.code == 0 and first.passed + first.failed + first.skipped == 0):
        run.result = "empty"
        return run
    if first.code == 0:
        return run
    # again once, alone: the failing tests by id when the first run named them, else the whole file
    ids = [i for i in first.failed_ids if "::" in i]
    if not ids:
        again = run_pytest([rel], timeout_s=timeout_s, python=python, env=env)
        run.seconds += again.seconds
        if again.code == 0:
            run.result, run.note = "flaky", "the file failed, then passed alone"
        else:
            run.result = "timeout" if again.timed_out else ("failed" if first.failed else "error")
            run.note = again.tail or first.tail
        return run
    still: list[str] = []
    if len(ids) <= ALONE_MAX:
        for test_id in ids:
            again = run_pytest([test_id], timeout_s=timeout_s, python=python, env=env)
            run.seconds += again.seconds
            (run.flaky_ids if again.code == 0 else still).append(test_id)
    else:
        again = run_pytest(ids, timeout_s=timeout_s, python=python, env=env, file=rel)
        run.seconds += again.seconds
        failing = set(again.failed_ids) if not again.timed_out and again.code in (0, 1) else set(ids)
        for test_id in ids:
            (still if test_id in failing else run.flaky_ids).append(test_id)
    run.failed_ids = still
    run.result = "failed" if still else "flaky"
    if still:
        run.note = first.tail
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("files", nargs="*", help="test files or globs (default: tests/test_*.py)")
    parser.add_argument("--jobs", "-j", type=int, default=1, help="files at a time (default 1)")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds per pytest process (default 600)")
    parser.add_argument("--json", type=Path, default=None, help="write the summary here too")
    parser.add_argument("--clock", default="", help="pin every test's wall clock to HH:MM (YUKI_TEST_CLOCK)")
    args = parser.parse_args(argv)

    patterns = args.files or [str(ROOT / "tests" / "test_*.py")]
    files = sorted({str(Path(p)) for pat in patterns for p in (glob.glob(pat) or [pat]) if Path(p).is_file()})
    if not files:
        print("no test files found", file=sys.stderr)
        return 2
    env = dict(os.environ)
    if args.clock:
        env["YUKI_TEST_CLOCK"] = args.clock
    started = time.monotonic()
    runs: list[FileRun] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {pool.submit(sweep_file, f, timeout_s=args.timeout, python=sys.executable, env=env): f
                   for f in files}
        for fut in concurrent.futures.as_completed(futures):
            run = fut.result()
            runs.append(run)
            print(f"{run.result:8s} {run.seconds:7.1f}s  {run.file}  ({run.passed} passed, {run.failed} failed"
                  f"{', ' + str(run.skipped) + ' skipped' if run.skipped else ''})", flush=True)
    runs.sort(key=lambda r: r.file)
    by = {k: [r for r in runs if r.result == k] for k in ("passed", "failed", "flaky", "timeout", "error", "empty")}
    wall = round(time.monotonic() - started, 1)
    print()
    print(f"{len(runs)} files in {wall:.0f} s{' (clock pinned at ' + args.clock + ')' if args.clock else ''}: "
          + ", ".join(f"{len(v)} {k}" for k, v in by.items() if v))
    for kind in ("failed", "timeout", "error"):
        for r in by[kind]:
            print(f"  {kind.upper()} {r.file}" + (f": {', '.join(r.failed_ids)}" if r.failed_ids else "")
                  + (f"\n      {r.note}" if r.note else ""))
    for r in by["flaky"]:
        print(f"  FLAKY {r.file}" + (f": {', '.join(r.flaky_ids)}" if r.flaky_ids else f" ({r.note})"))
    if args.json is not None:
        args.json.write_text(json.dumps({"clock": args.clock or None, "seconds": wall,
                                         "files": [asdict(r) for r in runs]}, indent=1), encoding="utf-8")
    return 1 if by["failed"] or by["timeout"] or by["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
