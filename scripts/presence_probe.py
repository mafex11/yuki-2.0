"""Read-only probe of the memory service's presence signal on the real input stream: counts and times only.

Runs :class:`yuki.memory.userpresence.Presence` (the Raw Input reader, as the memory service holds it) in this process
for ``--seconds`` and prints, every ``--every`` seconds, the user's idle time it reports next to Windows' own idle time
(``GetLastInputInfo``), and the counts of keys, clicks, wheel turns, mouse reports, mouse travels that counted, Yuki's
own input. Nothing about which key or where the cursor was is read or kept. It sends nothing and changes nothing.

``--old`` runs the previous sampler instead (the idle-timer burst rule, :class:`yuki.memory.watcher._PresenceSampler`),
for comparison. At the end: the process's CPU time and memory while it listened, and per mouse report.

    python scripts/presence_probe.py --seconds 60
    python scripts/presence_probe.py --seconds 60 --old
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--every", type=float, default=5.0)
    ap.add_argument("--old", action="store_true", help="the previous sampler (burst rule) instead")
    args = ap.parse_args(argv)

    import psutil

    from yuki.memory import watcher

    proc = psutil.Process()
    if args.old:
        sampler = watcher._PresenceSampler()
        idle_fn, facts_fn = sampler.idle_s, (lambda: {"source": "old_sampler"})
    else:
        idle_fn, facts_fn = watcher.user_idle_s, watcher.presence_facts
    rss0 = proc.memory_info().rss
    idle_fn()                                   # starts the reader (or the sampler's thread)
    worker = sampler._thread if args.old else watcher._presence._reader._thread
    tid = getattr(worker, "native_id", None)

    def thread_cpu() -> float:
        for th in proc.threads():
            if th.id == tid:
                return th.user_time + th.system_time
        return 0.0

    cpu0, tcpu0 = proc.cpu_times(), thread_cpu()
    t0 = time.monotonic()
    rows = []
    while time.monotonic() - t0 < args.seconds:
        time.sleep(args.every)
        facts = facts_fn()
        row = {"t": round(time.monotonic() - t0), "idle_s": round(idle_fn(), 1),
               "windows_idle_s": round(watcher.raw_idle_s(), 1), "counts": facts.get("counts"),
               "max_travel": facts.get("max_travel"), "answering": facts.get("answering")}
        rows.append(row)
        print(json.dumps(row), flush=True)
    cpu1, tcpu1 = proc.cpu_times(), thread_cpu()
    took = time.monotonic() - t0
    cpu = (cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)
    facts = facts_fn()
    moves = int((facts.get("counts") or {}).get("moves", 0))
    summary = {"source": facts.get("source"), "error": facts.get("error"), "seconds": round(took, 1),
               "process_cpu_s": round(cpu, 3), "reader_thread_cpu_s": round(tcpu1 - tcpu0, 3),
               "reader_pct_of_one_core": round(100.0 * (tcpu1 - tcpu0) / took, 3),
               "rss_mb": round(proc.memory_info().rss / 2**20, 1), "rss_added_mb": round((proc.memory_info().rss - rss0) / 2**20, 2),
               "mouse_reports_per_s": round(moves / took, 1),
               "reader_us_per_report": round(1e6 * (tcpu1 - tcpu0) / moves, 1) if moves else None,
               "move_px": facts.get("move_px"), "final_idle_s": round(idle_fn(), 1)}
    print(json.dumps({"summary": summary}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
