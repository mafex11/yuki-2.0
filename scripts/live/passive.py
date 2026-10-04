"""Passive checks (report 73, group P): what the installed Yuki did on its own since a time, from its logs only.

Read-only. Prints counts, costs, timings and pass/fail; never text, names or screen content.

    .venv\\Scripts\\python.exe scripts\\live\\passive.py --since 18:00 [--doctor]

P1 work board (Work page check) runs, cost, cadence     P5 tray / window logs: errors, boots, failures
P2 other memory model calls (cost per job)              P6 memory service health (stats records)
P3 service log: error-type records                      P7 crash logs (memory-crash.log, ui-boot.log)
P4 voice: sessions, turns, errors (fatal or not)        P8 the doctor (installed yuki-cli, statuses only)
"""

from __future__ import annotations

import collections
import json
import os
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (arg_parser, csv_rows, error_class, files, header, hhmm, jsonl, log_dir, num,  # noqa: E402
                     session_records, stats, verdict, window)


def p1_work_board(since: float, until: float) -> None:
    print("\nP1 work board (memory/conversations-*.jsonl, type=work_board)")
    rows = [r for r in jsonl(files("memory/conversations-*.jsonl", since), since, until)]
    runs = [r for r in rows if r.get("type") == "work_board"]
    errs = [r for r in rows if r.get("type") == "conversation_error" and r.get("purpose") == "work_board"]
    starts = [r for r in rows if r.get("type") == "worker_start"]
    cost = sum(float(r.get("cost_usd") or 0) for r in runs + errs)
    print(f"  conversation worker starts: {len(starts)}; work_board runs: {len(runs)}; errors: {len(errs)}; "
          f"cost ${cost:.4f}")
    for r in runs:
        print(f"   {hhmm(r['_ts'])} chats={r.get('chats')} replies={r.get('replies')} loops={r.get('loops')} "
              f"meetings={r.get('meetings')} rejected={len(r.get('rejected') or [])} "
              f"latency_ms={num(r.get('latency_ms'))} cost=${float(r.get('cost_usd') or 0):.4f}"
              f"{' DRY' if r.get('dry_run') else ''}")
    gaps = [(b["_ts"] - a["_ts"]) / 60 for a, b in zip(runs, runs[1:])]
    if gaps:
        print(f"  minutes between runs: {stats(gaps)} (the check is due at most every 15 min, only when "
              f"something is new to judge; meetings at most hourly)")
    checks = [r for r in rows if r.get("type") == "commitment_check"]
    print(f"  commitment checks: {len(checks)} cost ${sum(float(r.get('cost_usd') or 0) for r in checks):.4f} "
          f"closed={sum(len(r.get('closed') or []) for r in checks)}")
    verdict("work board ran after the install", bool(runs) if rows else None, f"{len(runs)} runs")
    verdict("no work board errors", not errs, f"{len(errs)} errors")


def p2_model_calls(since: float, until: float) -> None:
    print("\nP2 memory model calls (memory/*.jsonl): calls and cost per log")
    per = collections.defaultdict(lambda: [0, 0.0, 0])
    for r in jsonl(files("memory/*.jsonl", since), since, until):
        t = str(r.get("type") or "")
        name = r["_file"].split("-")[0]
        c = r.get("cost_usd")
        if t.endswith("_call") or t in ("work_board", "commitment_check"):
            per[name][0] += 1
            per[name][1] += float(c or 0)
        if r.get("error") or t.endswith("_error"):
            per[name][2] += 1
    total = 0.0
    for name, (n, cost, errs) in sorted(per.items()):
        total += cost
        print(f"  {name:14} calls={n:4} cost=${cost:.4f} error_records={errs}")
    print(f"  total ${total:.4f} (agent/voice requests are in requests.csv / voice_turns.csv, P4/P5)")


def p3_service(since: float, until: float) -> None:
    print("\nP3 memory service log (memory-*.jsonl): error-like records")
    types = collections.Counter()
    errors = collections.Counter()
    starts = 0
    for r in jsonl(files("memory-*.jsonl", since), since, until):
        t = str(r.get("type") or r.get("kind") or "?")
        types[t] += 1
        if t == "service_start":
            starts += 1
        if "error" in t or "crash" in t or "fail" in t or (r.get("error") not in (None, "", False)):
            errors[(t, error_class(r.get("error")))] += 1
    print(f"  records={sum(types.values())} service_start={starts} distinct types={len(types)}")
    for (t, cls), n in errors.most_common(15):
        print(f"   {n:4} {t} {cls}")
    verdict("no error records in the service log", not errors, f"{sum(errors.values())} records")
    verdict("no service restarts after the install", starts <= 1, f"{starts} starts")


def p4_voice(since: float, until: float) -> None:
    print("\nP4 voice (voice/voice-*.jsonl, voice_turns.csv, voice/pipecat-*.log)")
    rows = list(jsonl(files("voice/voice-*.jsonl", since), since, until))
    types = collections.Counter(str(r.get("type")) for r in rows)
    sessions = collections.Counter(str(r.get("state")) for r in rows if r.get("type") == "session")
    errs = [r for r in rows if r.get("type") == "error"]
    fatal = [r for r in errs if r.get("fatal")]
    print(f"  events={len(rows)} types={dict(types.most_common(12))}")
    print(f"  session states: {dict(sessions)}")
    by = collections.Counter(error_class(r.get("error")) for r in errs)
    for cls, n in by.most_common(8):
        times = [hhmm(r["_ts"]) for r in errs if error_class(r.get("error")) == cls]
        print(f"   error x{n} {cls} at {', '.join(times[:8])}")
    turns = csv_rows(log_dir() / "voice_turns.csv", since, until)
    first_audio = [float(t["eos_to_first_audio_ms"]) for t in turns if (t.get("eos_to_first_audio_ms") or "").strip()]
    print(f"  voice turns: {len(turns)}; eos->first audio ms {stats(first_audio)}; "
          f"turn errors={sum(1 for t in turns if (t.get('error') or '').strip())}")
    pc = collections.Counter()
    for path in files("voice/pipecat-*.log", since):
        for line in path.open(encoding="utf-8", errors="replace"):
            m = re.match(r"(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", line)
            if not m:
                continue
            try:
                ts = datetime.fromisoformat(m.group(1).replace(" ", "T")).timestamp()
            except ValueError:
                continue
            if since <= ts < until:
                for level in ("ERROR", "WARNING", "CRITICAL"):
                    if f"| {level}" in line:
                        pc[level] += 1
    print(f"  pipecat log lines: {dict(pc) or 'none'}")
    verdict("no fatal voice errors", not fatal, f"{len(fatal)} fatal, {len(errs)} non-fatal")


def p5_tray(since: float, until: float) -> None:
    print("\nP5 tray and window (sessions/*.jsonl, requests.csv)")
    recs = session_records(since, until)
    ui = collections.Counter()
    errors = collections.Counter()
    users = finals = 0
    for r in recs:
        t = r.get("type")
        if t == "ui":
            ui[str(r.get("event"))] += 1
            ev = str(r.get("event") or "")
            if ev.endswith("_failed") or ev.endswith("_error"):
                errors[(r["_file"].split("-")[-1].split(".")[0], ev)] += 1
        elif t == "error":
            errors[("agent", "error")] += 1
        elif t == "user_message":
            users += 1
        elif t == "final":
            finals += 1
    print(f"  records={len(recs)} requests(user_message)={users} finals={finals}")
    print(f"  ui events: {dict(ui.most_common(14))}")
    for (where, ev), n in errors.most_common(10):
        print(f"   {n:4} {where} {ev}")
    req = csv_rows(log_dir() / "requests.csv", since, until)
    print(f"  requests.csv rows={len(req)} cost=${sum(float(r.get('cost_usd') or 0) for r in req):.4f} "
          f"outcomes={dict(collections.Counter(r.get('outcome') for r in req))}")
    warm = [r for r in recs if r.get("type") == "startup_cost"]
    print(f"  warm-ups (startup_cost): {len(warm)} cost=${sum(float(r.get('cost_usd') or 0) for r in warm):.4f}")
    verdict("no agent/window error records", not errors, f"{sum(errors.values())}")


def p6_health(since: float, until: float) -> None:
    print("\nP6 memory service health (stats every 10 min)")
    st = [r for r in jsonl(files("memory-*.jsonl", since), since, until) if r.get("type") == "stats"]
    if not st:
        verdict("stats records present", None)
        return
    cpu = [float(r.get("cpu_percent_total") or 0) for r in st]
    rss = [float(r.get("rss_mb") or 0) for r in st]
    werr = max(int((r.get("watcher") or {}).get("errors") or 0) for r in st)
    perr = max(int((r.get("timeline") or {}).get("page_errors") or 0) for r in st)
    serr = max(int((r.get("screen_recall") or {}).get("errors") or 0) for r in st)
    print(f"  samples={len(st)} cpu% of all cores avg={sum(cpu) / len(cpu):.2f} max={max(cpu):.2f}; "
          f"rss MB min={min(rss):.0f} max={max(rss):.0f}; threads {min(r.get('threads', 0) for r in st)}-"
          f"{max(r.get('threads', 0) for r in st)}")
    print(f"  watcher errors={werr} page_errors={perr} screen_recall errors={serr}")
    verdict("service light (avg CPU < 1% of the PC)", sum(cpu) / len(cpu) < 1.0)
    verdict("no watcher / page / recall errors", werr == perr == serr == 0)


def p7_crashes(since: float, until: float) -> None:
    print("\nP7 crash logs")
    path = log_dir() / "memory-crash.log"
    sections: list[tuple[float, int, int]] = []   # (start, fatal lines, traceback lines)
    if path.is_file():
        cur = None
        for line in path.open(encoding="utf-8", errors="replace"):
            m = re.match(r"=== yuki-memory start (\S+) pid", line)
            if m:
                try:
                    cur = [datetime.fromisoformat(m.group(1)).timestamp(), 0, 0]
                except ValueError:
                    cur = [0.0, 0, 0]
                sections.append(cur)   # type: ignore[arg-type]
            elif cur is not None:
                if "fatal exception" in line:
                    cur[1] += 1
                elif line.startswith("Traceback"):
                    cur[2] += 1
    after = [s for s in sections if since <= s[0] < until]
    before = [s for s in sections if s[0] < since][-1:]
    print(f"  memory-crash.log: service starts in window={len(after)}; "
          f"fault lines in those runs={sum(s[1] for s in after)}; tracebacks={sum(s[2] for s in after)}")
    if before and not after:
        print(f"  (the run that started {hhmm(before[0][0])} logged {before[0][1]} undated fault lines)")
    boots = other = 0
    ub = log_dir() / "ui-boot.log"
    if ub.is_file():
        for line in ub.open(encoding="utf-8", errors="replace"):
            m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s+(\S+)", line)
            if m:
                ts = datetime.fromisoformat(m.group(1)).timestamp()
                if since <= ts < until:
                    if m.group(2) == "boot":
                        boots += 1
                    else:
                        other += 1
    print(f"  ui-boot.log: boots in window={boots}; other dated lines={other}")
    fault = sum(s[1] + s[2] for s in after)
    verdict("no crash lines since the window start", fault == 0 and other == 0,
            f"{fault} memory fault/traceback lines, {other} ui-boot non-boot lines")


def p8_doctor() -> None:
    print("\nP8 doctor (installed yuki-cli doctor --json; statuses only)")
    exe = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Yuki Assistant" / "yuki-cli.exe"
    if not exe.is_file():
        verdict("doctor", None, "installed yuki-cli.exe not found")
        return
    try:
        out = subprocess.run([str(exe), "doctor", "--json"], capture_output=True, text=True, timeout=120,
                             encoding="utf-8", errors="replace").stdout
        checks = json.loads(out)
    except Exception as exc:  # noqa: BLE001
        verdict("doctor", False, f"could not run: {type(exc).__name__}")
        return
    status = collections.Counter(str(c.get("status")) for c in checks)
    for c in checks:
        if c.get("status") != "ok":
            print(f"   {c.get('status')}: {c.get('name')}")
    print(f"  {len(checks)} checks: {dict(status)}")
    verdict("doctor all ok", status.get("ok", 0) == len(checks), f"{status.get('ok', 0)}/{len(checks)}")


def main() -> int:
    p = arg_parser(__doc__)
    p.add_argument("--doctor", action="store_true", help="also run the installed doctor (read-only)")
    args = p.parse_args()
    since, until = window(args)
    header("passive checks", since, until)
    p1_work_board(since, until)
    p2_model_calls(since, until)
    p3_service(since, until)
    p4_voice(since, until)
    p5_tray(since, until)
    p6_health(since, until)
    p7_crashes(since, until)
    if args.doctor:
        p8_doctor()
    return 0


if __name__ == "__main__":
    sys.exit(main())
