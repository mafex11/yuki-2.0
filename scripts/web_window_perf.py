"""E3/E4/E16: measure the real Lattice window (WebView2) on this PC, off-screen, never focused.

Each run starts ``pythonw -m yuki.ui.web_window --offscreen`` (a window beyond every monitor that never activates)
with a fresh or a warm WebView2 profile, and measures, over the whole process tree (the Python host and every
msedgewebview2 child):

* click-to-first-paint (the page's own Element Timing of the page title, after its data and fonts);
* private bytes, private working set (USS) and working set, a few seconds after the paint;
* CPU as % of one core over a window: idle (nothing live), live (a live state animating, as if the window were in
  front: ``--assume-focus``), and live behind other windows (the same state, not in front: one still frame).

Data: the invented fixture (``--fixture``: no memory, no pipe) or a fresh empty memory in a temp folder (``--db``:
the real startup path: key, snapshot, memory open, every page built). The real memory database is never opened.
A focus watcher fails the run if any window of the tree ever becomes the foreground window.

    python scripts/web_window_perf.py [--quick] [--runs 3] [--exe dist\\Yuki\\Yuki.exe]
Results: ``%TEMP%\\yuki-web-perf\\<stamp>\\results.json`` and a summary on stdout.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from spike_webview2 import FocusWatch, Runner  # noqa: E402


class ExeRunner(Runner):
    """The packaged build: ``Yuki.exe -m yuki.ui.web_window`` (the frozen entry honours ``-m``)."""

    def command(self, profile: Path, metrics: Path, exit_after: float, args: str | None, extra: list[str]) -> list[str]:
        cmd = super().command(profile, metrics, exit_after, args, extra)
        if self.exe:
            cmd[1:1] = ["-m", "yuki.ui.web_window"]
        return cmd


def temp_memory(base: Path) -> Path:
    """A fresh, empty memory (its own key) in a temp folder: the real startup path without the real database."""
    import os

    folder = base / "memory"
    folder.mkdir(parents=True, exist_ok=True)
    os.environ["YUKI_MEMORY_DIR"] = str(folder)
    from yuki.memory.api import MemoryClient

    db = folder / "memory.db"
    MemoryClient.open(db).close()
    return db


def summarize(results: list[dict[str, Any]]) -> str:
    lines = []
    for r in results:
        m = r.get("metrics") or {}
        d = m.get("derived") or {}
        s = r.get("samples") or {}
        last = s.get(sorted(s)[-1]) if s else {}
        tot = (last or {}).get("total") or {}
        lines.append(f"{r['name']:<28} paint {d.get('first_paint')} s  private {tot.get('private_mb')} MB  uss "
                     f"{tot.get('uss_mb')} MB  wset {tot.get('wset_mb')} MB  procs {(last or {}).get('count')}  cpu "
                     f"{r.get('idle_cpu_pct_one_core')}%  focus {m.get('took_focus')}  exit {r.get('exit_code')}")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="one fixture launch with a screenshot")
    ap.add_argument("--try-db", action="store_true", help="one launch on a fresh empty memory, with a screenshot")
    ap.add_argument("--runs", type=int, default=3, help="warm first-paint runs")
    ap.add_argument("--exe", type=Path, default=None)
    ap.add_argument("--cpu-window", type=float, default=20.0)
    args = ap.parse_args()
    out = Path(tempfile.gettempdir()) / "yuki-web-perf" / time.strftime("%Y%m%d-%H%M%S")
    out.mkdir(parents=True, exist_ok=True)
    watch = FocusWatch()
    watch.start()
    runner = ExeRunner(out, args.exe, watch)
    results: list[dict[str, Any]] = []
    profile = out / "profile"
    win = args.cpu_window
    try:
        if args.try_db:
            db = temp_memory(out)
            results.append(runner.launch("try-empty-memory", profile, None, 7.0, at=(("6s", 6.0),),
                                         extra=["--db", str(db), "--no-connect", "--shot", str(out / "try.png"), "--shot-delay", "4"]))
            for e in (results[-1].get("metrics") or {}).get("events") or []:
                print(e)
        elif args.quick:
            shot = out / "quick.png"
            results.append(runner.launch("quick-fixture", profile, None, 3.0, at=(("2s", 2.0),),
                                         extra=["--fixture", "--shot", str(shot)]))
        else:
            db = temp_memory(out)
            # cold: a brand-new profile, the real startup path on an empty memory
            results.append(runner.launch("cold-empty-memory", out / "profile-cold", None, 6.0, at=(("5s", 5.0),),
                                         extra=["--db", str(db), "--no-connect"]))
            for i in range(args.runs):
                results.append(runner.launch(f"warm-empty-memory-{i + 1}", profile, None, 6.0, at=(("5s", 5.0),),
                                             extra=["--db", str(db), "--no-connect"]))
            # idle CPU with the fixture's page (no live state): nothing should redraw
            results.append(runner.launch("idle-home", profile, None, 10 + win, at=(("a", 8.0), ("b", 8.0 + win)),
                                         cpu=("a", "b"), extra=["--fixture", "--query", "runs=0&call=0", "--assume-focus"]))
            for state in ("listening", "working", "speaking"):
                results.append(runner.launch(f"live-{state}-in-front", profile, None, 10 + win,
                                             at=(("a", 8.0), ("b", 8.0 + win)), cpu=("a", "b"),
                                             extra=["--fixture", "--query", f"state={state}&runs=0&call=0", "--assume-focus"]))
            results.append(runner.launch("live-listening-behind", profile, None, 10 + win, at=(("a", 8.0), ("b", 8.0 + win)),
                                         cpu=("a", "b"), extra=["--fixture", "--query", "state=listening&runs=0&call=0"]))
            # the busiest real page: a call timer ticking and a run's dot blinking, in front
            results.append(runner.launch("live-call-and-run-in-front", profile, None, 10 + win,
                                         at=(("a", 8.0), ("b", 8.0 + win)), cpu=("a", "b"),
                                         extra=["--fixture", "--assume-focus"]))
    finally:
        watch.stop.set()
    paints = [((r.get("metrics") or {}).get("derived") or {}).get("first_paint") for r in results if r["name"].startswith("warm")]
    paints = [p for p in paints if p]
    report = {"results": results, "focus_stolen": watch.stolen,
              "warm_first_paint_median": statistics.median(paints) if paints else None}
    (out / "results.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    print(summarize(results))
    print("warm first paint median:", report["warm_first_paint_median"], " focus stolen:", watch.stolen)
    print(out)
    return 1 if watch.stolen else 0


if __name__ == "__main__":
    raise SystemExit(main())
