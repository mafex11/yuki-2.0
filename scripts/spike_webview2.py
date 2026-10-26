"""E2 spike: measure the WebView2 Yuki window on this PC (the design notes §7.6).

Launches ``python -m yuki.ui.web_window --offscreen`` (a window beyond the left edge of every monitor that never
activates; it renders the Home page from the fixture of invented data) and measures, per GPU configuration:

* click-to-first-paint: from the spawn (the "click") to the page's own paint of the Home title with its data,
  cold (a fresh WebView2 profile) and warm (the same profile again);
* private bytes, private working set and working set over the whole process tree (the Python host and every
  msedgewebview2 child), 5 s, 30 s and 120 s after the paint;
* idle CPU over the minute from 120 s to 180 s after the paint, as % of one core.

A watcher checks the foreground window every 50 ms; if any window of the measured tree ever became the foreground
window the run stops (it never should). Results go to ``logs/spike/<stamp>/`` (gitignored).

    python scripts/spike_webview2.py [--quick] [--exe dist\\yuki-web-spike\\yuki-web-spike.exe]
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path
from typing import Any

import psutil

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))   # this checkout's yuki, not whichever one the venv points at
PYTHONW = Path(sys.executable).with_name("pythonw.exe")
CREATE_NO_WINDOW = 0x08000000
#: Chromium's own default (a separate GPU process) is an explicit empty value: the window itself now defaults to
#: ``--disable-gpu`` (yuki.ui.web_window.DEFAULT_BROWSER_ARGS), the winner of these runs.
CONFIGS: dict[str, str | None] = {"default": "", "in-process-gpu": "--in-process-gpu", "disable-gpu": "--disable-gpu"}
MB = 1024 * 1024


class FocusWatch(threading.Thread):
    """Fails the run if a window of the measured tree ever becomes the foreground window."""

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.pids: set[int] = set()
        self.stolen: list[tuple[float, int]] = []
        self.stop = threading.Event()

    def run(self) -> None:
        user32 = ctypes.windll.user32
        while not self.stop.wait(0.05):
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                continue
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in self.pids:
                self.stolen.append((time.time(), pid.value))


def kind_of(p: psutil.Process) -> str:
    try:
        name = p.name().lower()
        cmd = p.cmdline()
    except (psutil.Error, OSError):
        return "gone"
    if name.startswith("python") or name.endswith("spike.exe"):
        return "host"
    typ = next((a.split("=", 1)[1] for a in cmd if a.startswith("--type=")), "")
    if not typ:
        return "browser"
    if typ == "utility":
        sub = next((a.split("=", 1)[1] for a in cmd if a.startswith("--utility-sub-type=")), "")
        return "utility:" + sub.split(".")[0] if sub else "utility"
    return typ


def tree(root: int) -> list[psutil.Process]:
    try:
        p = psutil.Process(root)
        return [p] + p.children(recursive=True)
    except psutil.Error:
        return []


def sample(root: int) -> dict[str, Any]:
    rows = []
    for p in tree(root):
        try:
            mi = p.memory_info()
            uss = p.memory_full_info().uss
            cpu = p.cpu_times()
            rows.append({"pid": p.pid, "kind": kind_of(p), "private_mb": round(mi.private / MB, 1),
                         "wset_mb": round(mi.rss / MB, 1), "uss_mb": round(uss / MB, 1),
                         "cpu_s": round(cpu.user + cpu.system, 3)})
        except (psutil.Error, OSError):
            continue
    total = {k: round(sum(r[k] for r in rows), 1) for k in ("private_mb", "wset_mb", "uss_mb")}
    return {"at": time.time(), "processes": rows, "total": total, "count": len(rows)}


def wait_metrics(path: Path, timeout: float) -> dict[str, Any] | None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            m = json.loads(path.read_text(encoding="utf-8"))
            if m.get("derived"):
                return m
        except (OSError, ValueError):
            pass
        time.sleep(0.05)
    return None


def finish(proc: subprocess.Popen, pids: set[int], timeout: float = 20.0) -> bool:
    """Wait for the host and every child it had to exit; kill what is left of *our* tree after ``timeout``."""
    end = time.time() + timeout
    while time.time() < end:
        alive = [pid for pid in pids if psutil.pid_exists(pid)]
        if proc.poll() is not None and not alive:
            return True
        time.sleep(0.1)
    for pid in pids:
        try:
            psutil.Process(pid).kill()
        except psutil.Error:
            pass
    return False


class Runner:
    def __init__(self, out: Path, exe: Path | None, watch: FocusWatch) -> None:
        self.out = out
        self.exe = exe
        self.watch = watch
        self.n = 0

    def command(self, profile: Path, metrics: Path, exit_after: float, args: str | None, extra: list[str]) -> list[str]:
        base = [str(self.exe)] if self.exe else [str(PYTHONW), "-m", "yuki.ui.web_window"]
        cmd = base + ["--offscreen", "--profile", str(profile), "--metrics", str(metrics), "--exit-after",
                      str(exit_after)] + extra
        if args is not None:   # one token: a value starting with "--" would read as an option of its own
            cmd.append(f"--browser-args={args}")
        return cmd

    def launch(self, name: str, profile: Path, args: str | None, exit_after: float, *,
               at: tuple[tuple[str, float], ...] = (("1s", 1.0),), cpu: tuple[str, str] | None = None,
               extra: list[str] | None = None) -> dict[str, Any]:
        """One window: wait for its paint, sample the tree ``at`` seconds after it, idle CPU between the ``cpu``
        samples, then wait for every process of it to exit."""
        self.n += 1
        metrics = self.out / f"{self.n:02d}-{name}.json"
        env = {k: v for k, v in os.environ.items() if k != "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"}
        env["PYTHONPATH"] = str(ROOT)
        t0 = time.time()
        cmd = self.command(profile, metrics, exit_after, args, ["--t0", repr(t0)] + (extra or []))
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env, creationflags=CREATE_NO_WINDOW,
                                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        pids: set[int] = {proc.pid}
        result: dict[str, Any] = {"name": name, "args": args, "samples": {}}

        def track() -> None:
            for p in tree(proc.pid):
                pids.add(p.pid)
            self.watch.pids |= pids

        track()
        m = None
        end = time.time() + 30
        while time.time() < end and m is None:
            track()
            m = wait_metrics(metrics, 0.2)
        result["metrics"] = m
        if m is None:
            proc.kill()
            result["error"] = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace")[-2000:]
            finish(proc, pids, 5)
            return result
        painted = time.time()
        for label, secs in at:
            time.sleep(max(0.0, painted + secs - time.time()))
            track()
            result["samples"][label] = sample(proc.pid)
        if cpu is not None:
            a, b = result["samples"][cpu[0]], result["samples"][cpu[1]]
            before = {r["pid"]: r["cpu_s"] for r in a["processes"]}
            spent = sum(r["cpu_s"] - before.get(r["pid"], 0.0) for r in b["processes"])
            result["idle_cpu_pct_one_core"] = round(100.0 * spent / max(1e-6, b["at"] - a["at"]), 3)
            result["idle_cpu_s_per_min"] = round(spent * 60.0 / max(1e-6, b["at"] - a["at"]), 3)
        result["clean_exit"] = finish(proc, pids)
        result["exit_code"] = proc.returncode
        return result


def pywebview_probe() -> dict[str, Any]:
    """What pywebview's EdgeChromium backend would add to the host: .NET + WinForms + its WebView2 wrappers loaded
    into the process (no window is made: import only), against this host's own import."""
    code = ("import json, time, psutil; p = psutil.Process(); m0 = p.memory_info().private; t = time.perf_counter();"
            "import {mod}; dt = time.perf_counter() - t; m1 = p.memory_info().private;"
            "print(json.dumps({{'before_mb': round(m0 / 2**20, 1), 'after_mb': round(m1 / 2**20, 1),"
            " 'import_s': round(dt, 3)}}))")
    out = {}
    env = {**os.environ, "PYTHONPATH": str(ROOT)}
    for label, mod in (("thin_ctypes_host", "yuki.ui.webview2"), ("pywebview_winforms", "webview.platforms.winforms")):
        r = subprocess.run([sys.executable, "-c", code.format(mod=mod)], capture_output=True, text=True, env=env,
                           cwd=str(ROOT), timeout=120, creationflags=CREATE_NO_WINDOW)
        try:
            out[label] = json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            out[label] = {"error": (r.stderr or r.stdout)[-800:]}
    return out


def summarize(results: dict[str, Any]) -> str:
    lines = ["| Config | Cold first paint | Warm first paint (median of runs) | Processes | Private MB at 120 s "
             "(host + WebView2) | Private working set MB | Working set MB | Idle CPU 120-180 s (% of one core) |",
             "|---|---|---|---|---|---|---|---|"]
    for name, r in results["configs"].items():
        if not r["cold"].get("metrics"):
            lines.append(f"| {name} | no paint: {str(r['cold'].get('error') or '')[-120:]} | | | | | | |")
            continue
        cold = r["cold"]["metrics"]["derived"]["first_paint"]
        warm = [x["metrics"]["derived"]["first_paint"] for x in r["warm"] if x.get("metrics")] or [float("nan")]
        long = r["long"]
        s = long["samples"].get("120s") or {}
        host = sum(p["private_mb"] for p in s.get("processes", []) if p["kind"] == "host")
        tot = s.get("total", {})
        lines.append(f"| {name} | {cold:.2f} s | {statistics.median(warm):.2f} s ({', '.join(f'{w:.2f}' for w in warm)}) "
                     f"| {s.get('count')} | {tot.get('private_mb')} ({host:.0f} + {tot.get('private_mb', 0) - host:.0f}) "
                     f"| {tot.get('uss_mb')} | {tot.get('wset_mb')} | {long.get('idle_cpu_pct_one_core')} |")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true", help="one warm run and a 40 s long run (smoke test)")
    ap.add_argument("--exe", type=Path, default=None, help="measure a PyInstaller build instead of python -m")
    ap.add_argument("--configs", default=",".join(CONFIGS))
    ap.add_argument("--no-memory-low", action="store_true", help="skip the memory-target-Low lever run")
    ap.add_argument("--no-live", action="store_true", help="skip the run with a live task on the page")
    a = ap.parse_args()
    a.memory_low = not a.no_memory_low
    stamp = time.strftime("%Y%m%d-%H%M%S")
    out = ROOT / "logs" / "spike" / (stamp + ("-exe" if a.exe else ""))
    out.mkdir(parents=True, exist_ok=True)
    watch = FocusWatch()
    watch.start()
    runner = Runner(out, a.exe, watch)
    results: dict[str, Any] = {"stamp": stamp, "exe": str(a.exe) if a.exe else None, "configs": {},
                               "cpu": {"logical": psutil.cpu_count(), "physical": psutil.cpu_count(logical=False)},
                               "machine_ram_gb": round(psutil.virtual_memory().total / 2**30, 1)}
    warm_runs = 1 if a.quick else 3
    # idle means nothing live on the page: the fixture without its running tasks (the live dot blinks)
    from yuki.ui.web_window import FIXTURE

    quiet = json.loads(FIXTURE.read_text(encoding="utf-8"))
    quiet["inbox"]["running"] = []
    quiet_path = out / "home-quiet.json"
    quiet_path.write_text(json.dumps(quiet), encoding="utf-8")
    long_at = (("5s", 5), ("120s", 10), ("180s", 20)) if a.quick else \
        (("5s", 5), ("30s", 30), ("120s", 120), ("180s", 180))
    long_exit = 25.0 if a.quick else 185.0
    for name in a.configs.split(","):
        args = CONFIGS[name]
        profile = out / "profiles" / name
        r = {"cold": runner.launch(f"{name}-cold", profile, args, 1.0)}
        r["warm"] = [runner.launch(f"{name}-warm{i + 1}", profile, args, 1.0) for i in range(warm_runs)]
        r["long"] = runner.launch(f"{name}-long", profile, args, long_exit, at=long_at, cpu=("120s", "180s"),
                                  extra=["--fixture", str(quiet_path)])
        results["configs"][name] = r
        print(name, "cold", (r["cold"].get("metrics") or {}).get("derived", {}).get("first_paint"),
              "warm", [(x.get("metrics") or {}).get("derived", {}).get("first_paint") for x in r["warm"]],
              "private@120s", r["long"]["samples"].get("120s", {}).get("total"),
              "idle cpu %", r["long"].get("idle_cpu_pct_one_core"), flush=True)
        if watch.stolen:
            print("FOCUS TAKEN - stopping", watch.stolen)
            break
    if not a.quick and not a.no_live and not watch.stolen and "default" in results["configs"]:
        # the same window with a task running (its live dot blinks at 800 ms) and the face idle (blinks)
        results["live"] = runner.launch("default-live", out / "profiles" / "default", None, long_exit, at=long_at,
                                        cpu=("120s", "180s"))
        print("default with a live task: idle cpu %", results["live"].get("idle_cpu_pct_one_core"), flush=True)
    if a.memory_low and not a.quick and not watch.stolen:
        # the minimised-window lever: memory target Low 30 s after the paint, sampled before and after
        results["memory_low"] = runner.launch("default-memory-low", out / "profiles" / "default", None, 65.0,
                                              at=(("25s", 25), ("40s", 40), ("60s", 60)),
                                              extra=["--memory-low-after", "30"])
    results["focus_taken"] = watch.stolen
    if not a.exe:
        results["pywebview_probe"] = pywebview_probe()
    (out / "results.json").write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    table = summarize(results) if not watch.stolen else "focus was taken; no table"
    (out / "summary.md").write_text(table + "\n", encoding="utf-8")
    print(table)
    print("pywebview probe:", json.dumps(results.get("pywebview_probe"), indent=1))
    print("results in", out)
    watch.stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
