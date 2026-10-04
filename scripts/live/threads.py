"""Yuki's own threads and CPU between runs (report 73: takeover step 9, perception step 9), read-only.

Finds the tray process (the installed Yuki.exe, or pythonw -m yuki.ui.boot), samples its CPU for a few seconds
(psutil), and lists its Python threads with py-spy (``dump``: a brief pause of the process, no code runs in it).
Prints only thread names, idle flags and CPU numbers; never stack locals (``--locals`` is never passed).

    .venv\\Scripts\\python.exe scripts\\live\\threads.py [--pid N] [--seconds 5]

Pass: between runs no ``yuki-takeover-watch`` thread exists, and ``yuki-uia-events`` (if present) is idle.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

import psutil

PYSPY = Path(__file__).resolve().parents[2] / ".venv" / "Scripts" / "py-spy.exe"


def tray_pid() -> int | None:
    for p in psutil.process_iter(["pid", "name", "cmdline", "exe"]):
        try:
            name = (p.info["name"] or "").lower()
            cmd = " ".join(p.info["cmdline"] or [])
        except (psutil.Error, TypeError):
            continue
        # the installed tray is Yuki.exe with no arguments (its children run "-m yuki.memory.service", the wake
        # process, ...); from source it is pythonw -m yuki.ui.boot (the venv launcher's child holds Python)
        if "yuki.ui.boot" in cmd or (name == "yuki.exe" and "Yuki Assistant" in str(p.info["exe"] or "")
                                         and len(p.info["cmdline"] or []) == 1):
            return int(p.info["pid"])
    return None


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--seconds", type=float, default=5.0)
    args = ap.parse_args()
    pid = args.pid or tray_pid()
    if pid is None:
        print("tray process not found")
        return 1
    proc = psutil.Process(pid)
    t0 = proc.cpu_times()
    time.sleep(args.seconds)
    t1 = proc.cpu_times()
    cpu_s = (t1.user + t1.system) - (t0.user + t0.system)
    print(f"tray pid={pid} cpu over {args.seconds:.0f}s = {cpu_s * 1000:.0f} ms ({100 * cpu_s / args.seconds:.2f}% of "
          f"one core) rss={proc.memory_info().rss / 2**20:.0f} MB os_threads={proc.num_threads()}")
    if not PYSPY.is_file():
        print("py-spy not found; thread names skipped")
        return 0
    out = subprocess.run([str(PYSPY), "dump", "--pid", str(pid)], capture_output=True, text=True, timeout=60,
                         encoding="utf-8", errors="replace")
    names = []
    for line in out.stdout.splitlines():
        m = re.match(r'Thread\s+\S+\s*(\(([^)]*)\))?:?\s*"([^"]*)"', line.strip())
        if m:
            name = m.group(3) if re.fullmatch(r"[\w .:\-]{1,60}", m.group(3) or "") else "other"
            names.append((name, "idle" in (m.group(2) or "")))
    if not names:
        print(f"py-spy gave no threads (exit {out.returncode}; it may need the same rights as the process)")
        return 0
    yuki = [(n, idle) for n, idle in names if n.lower().startswith("yuki")]
    print(f"python threads={len(names)} idle={sum(1 for _n, i in names if i)}; yuki-named: "
          + ", ".join(f"{n}{'(idle)' if i else '(active)'}" for n, i in sorted(yuki)))
    watch = [n for n, _i in names if n == "yuki-takeover-watch"]
    events = [(n, i) for n, i in names if n == "yuki-uia-events"]
    print(f"  [{'PASS' if not watch else 'CHECK':7}] no takeover watch thread between runs ({len(watch)})")
    if events:
        print(f"  [{'PASS' if all(i for _n, i in events) else 'CHECK':7}] UIA events thread idle while nothing is in play")
    return 0


if __name__ == "__main__":
    sys.exit(main())
