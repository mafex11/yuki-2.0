"""Acceptance tooling for Record how I work (report 78 §10.1, phase C1; report 96).

Two parts:

* ``--checklist``: the 30-minute supervised session, in plain words, for the user to follow (Chrome, T3 Code, Warp,
  Discord, Explorer; a Yuki run with a takeover, Handy dictation, a G Hub macro, Deskflow both ways, Valorant
  windowed, FL Studio maximised, a password field and a blocked site).
* the analyzer: on a **copy** of the memory database, prints the §10.1 pass/fail numbers. ``--copy-real`` makes the
  copy itself with the sqlite backup API into ``%TEMP%`` (the real database is only read) and deletes it afterwards.
  It scans every column of every table of the copy (decrypting the encrypted ones in memory) **and** the memory logs
  for the marker phrase the checklist has you type (``--typed``): the pass is that it is found nowhere.

Usage::

    python scripts/trace_acceptance.py --checklist
    python scripts/trace_acceptance.py --copy-real --since 21:00 --until 21:35 --typed "violet-otter-4417" \\
        --quiet password=21:12-21:13 --quiet private=21:14-21:15 --quiet blocked=21:16-21:17
    python scripts/trace_acceptance.py --db C:\\path\\to\\copy.db --since ... [--steps]

Output is counts and reason codes; ``--steps`` also prints the session's step lines with element names on this
console only (to compare with what you did; nothing is written anywhere).
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CHECKLIST = """\
Record how I work: the 30-minute check (about half an hour; Claude runs the numbers afterwards)

Before you start
  1. Settings > Privacy and pause > Record how I work: on. Note the time.
  2. Pick a made-up marker phrase Claude gives you (two words and four digits, like violet-otter-4417).
  3. Keep a clock in view: some steps ask you to note the minute.

Chrome (5 min)
  4. Open a new tab, search for anything, open two results, go back once, switch tabs with Ctrl+Tab.
  5. Open a page's menu (the three dots), pick an item. Right-click a link, pick "Open link in new tab".
  6. Copy a sentence (Ctrl+C) and paste it into the address bar (Ctrl+V). Don't press Enter.
  7. Type your marker phrase into the search box, then delete it.

T3 Code (5 min)
  8. Open a file from the sidebar, scroll, use Ctrl+P to open another, type a few characters in the editor.
  9. Press a toolbar button you use often. Hover over a button and wait for its tooltip, then move on.

Warp (3 min)
  10. Run two commands: one that works, one that fails (a made-up command name). Click once inside the window.

Discord (5 min)
  11. Open two channels, react to a message, start a reply and send it. Paste something you copied in Chrome.
  12. Type with an accent using a dead key (for example ' then e) and check the accent still works.
  13. Type a symbol with Alt and the number pad (Alt+0169) somewhere you can delete it after.

Explorer (3 min)
  14. Open a folder, drag a file onto another folder (and undo with Ctrl+Z), open a file by double-clicking.

Yuki and other input (5 min)
  15. Ask Yuki to do three small things on screen (open an app, click something). On the third, take over
      halfway with a click of your own.
  16. Dictate one sentence with Handy into a text box.
  17. Run one G Hub macro (a key or button bound to keys).
  18. Deskflow: work on this PC with the shared mouse for a moment, then move to the other PC and type a word
      there, then come back.

Games and music (3 min)
  19. Start VALORANT, stay windowed on the menu for a minute, Alt+Tab out and back, then close it.
  20. Open FL Studio maximised, click a few buttons and the playlist, save the project.

Things that must leave nothing (note the minute of each; do only this during that minute)
  21. Type something into a password field (any sign-in page; don't submit).   minute: ____
  22. Open a private browser window and click around.                           minute: ____
  23. Open a site from the blocked list and click around.                       minute: ____

When done
  24. Note the end time. Tell Claude the start and end times, the marker phrase and the three minutes.
      Claude makes a copy of the memory database, runs the check on the copy and deletes the copy.
"""

TREE_APPS = ("chrome", "t3 code", "t3code", "discord", "explorer")


def _span(text: str, day: datetime) -> float:
    hh, mm = (int(x) for x in text.split(":"))
    return day.replace(hour=hh, minute=mm, second=0, microsecond=0).timestamp()


def copy_real(dest_dir: Path) -> Path:
    """A copy of the real memory database (sqlite backup API; the real file is only read) and its key."""
    from yuki.memory.store import KEY_FILENAME, default_db_path

    real = default_db_path()
    dest = dest_dir / "memory.db"
    src = sqlite3.connect(f"file:{real}?mode=ro", uri=True, timeout=10)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    key = real.parent / KEY_FILENAME
    if key.exists():
        shutil.copyfile(key, dest_dir / KEY_FILENAME)
    return dest


def _cipher(db: Path):
    from yuki.memory.crypto import FieldCipher, load_or_create_key
    from yuki.memory.store import KEY_FILENAME

    key = db.parent / KEY_FILENAME
    if not key.exists():
        return None
    return FieldCipher(load_or_create_key(key))


def scan_for_marker(db: Path, marker: str, logs: Path | None) -> dict[str, int]:
    """Where the typed marker appears: every column of every table of the copy (encrypted ones decrypted in memory)
    and the memory logs. Only counts leave."""
    needles = {marker.casefold(), re.sub(r"[^0-9a-z]", "", marker.casefold())}
    needles.discard("")
    cipher = _cipher(db)
    found: Counter[str] = Counter()
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")]
        for table in tables:
            try:
                rows = conn.execute(f"SELECT * FROM \"{table}\"")
            except sqlite3.Error:
                continue
            names = [d[0] for d in rows.description]
            for row in rows:
                for name, value in zip(names, row):
                    if not isinstance(value, str) or not value:
                        continue
                    text = value
                    if cipher is not None and name.endswith("_ciphertext"):
                        try:
                            text = cipher.decrypt(value)
                        except Exception:  # noqa: BLE001
                            continue
                    low = text.casefold()
                    if any(n in low or n in re.sub(r"[^0-9a-z]", "", low) for n in needles):
                        found[f"{table}.{name}"] += 1
    finally:
        conn.close()
    if logs is not None:
        for path in memory_logs(logs):
            try:
                low = path.read_text(encoding="utf-8", errors="replace").casefold()
            except OSError:
                continue
            if any(n in low for n in needles):
                found[f"log:{path.name}"] += 1
    return dict(found)


def split_hits(hits: dict[str, int]) -> tuple[dict[str, int], dict[str, int]]:
    """``(trace tables and logs, other tables)``: the ambient capture may legitimately have seen the marker on screen
    (a chat it reads), which is not the trace keeping typed text."""
    trace = {k: v for k, v in hits.items() if k.startswith(("ui_", "log:"))}
    return trace, {k: v for k, v in hits.items() if k not in trace}


def memory_logs(folder: Path | None) -> list[Path]:
    return sorted(folder.glob("memory-*.jsonl")) if folder is not None and folder.is_dir() else []


def default_logs() -> Path | None:
    """Where the memory service writes ``memory-*.jsonl``: the installed app's data folder first (a dev checkout's
    own logs folder is not where the running service logs), then the checkout's; None when neither has any."""
    from yuki.core.paths import data_dir, logs_dir

    for folder in (data_dir() / "logs", logs_dir()):
        if memory_logs(folder):
            return folder
    return None


def analyze(db: Path, since: float, until: float, quiet: dict[str, tuple[float, float]], *, steps: bool) -> dict:
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute("SELECT * FROM ui_actions WHERE at >= ? AND at < ? ORDER BY at",
                                          (since, until))]
    report: dict = {"rows": len(rows)}
    by_app = Counter(r["process"] for r in rows)
    report["by_app"] = dict(by_app.most_common())
    report["by_kind"] = dict(Counter(r["kind"] for r in rows).most_common())
    report["by_source"] = dict(Counter(r["source"] for r in rows))
    report["takeovers"] = sum(1 for r in rows if r["source"] == "user" and r["during_run"])
    clicks = [r for r in rows if r["kind"] in ("click", "dclick", "rclick", "menu", "drag")]
    per_app: dict[str, Counter] = defaultdict(Counter)
    for r in clicks:
        per_app[r["process"]][r["resolved"] or "unresolved"] += 1
    report["clicks_resolved_by_app"] = {a: dict(c) for a, c in per_app.items()}
    tree = [r for r in clicks if any(t in (r["process"] or "") for t in TREE_APPS)]
    named = sum(1 for r in tree if r["resolved"] in ("event", "point", "snapshot"))
    report["tree_app_clicks"] = len(tree)
    report["tree_app_named_share"] = round(named / len(tree), 3) if tree else None
    report["event_path_share_by_app"] = {a: round(c["event"] / sum(c.values()), 3) for a, c in per_app.items()
                                         if sum(c.values())}
    report["stale_share"] = round(sum(1 for r in clicks if r["resolved"] == "stale") / len(clicks), 3) if clicks else None
    report["chords"] = dict(Counter(r["chord"] for r in rows if r["kind"] in ("keys", "paste") and r["chord"]))
    report["typed_total"] = sum(int(r["typed"] or 0) for r in rows)
    report["hovers"] = sum(1 for r in rows if r["kind"] == "hover")
    report["copies"] = sum(1 for r in rows if r["kind"] == "copy")
    report["pastes"] = sum(1 for r in rows if r["kind"] == "paste")
    report["commands"] = sum(1 for r in rows if r["kind"] == "command")
    report["files"] = sum(1 for r in rows if r["kind"] in ("file_open", "file_save"))
    # fragmentation: distinct element keys per (app, role, name digest): <= 1.2 a control
    keys: dict[tuple, set] = defaultdict(set)
    for r in rows:
        if r["element_key"] and r["name_digest"]:
            keys[(r["process"], r["role"], r["name_digest"])].add(r["element_key"])
    frag: dict[str, list[int]] = defaultdict(list)
    for (app, _role, _n), ks in keys.items():
        frag[app].append(len(ks))
    report["keys_per_control"] = {a: round(sum(v) / len(v), 2) for a, v in frag.items()}
    # AutomationId stability across an app restart (proc_started differs)
    aid: dict[tuple, dict] = defaultdict(dict)
    for r in conn.execute("SELECT process, role, name_digest, aid_digest, proc_started FROM ui_actions WHERE"
                          " aid_digest IS NOT NULL AND name_digest IS NOT NULL"):
        aid[(r["process"], r["role"], r["name_digest"])][r["proc_started"]] = r["aid_digest"]
    stable: dict[str, list[bool]] = defaultdict(list)
    for (app, _r, _n), by_session in aid.items():
        if len(by_session) >= 2:
            stable[app].append(len(set(by_session.values())) == 1)
    report["aid_stable_share"] = {a: round(sum(v) / len(v), 2) for a, v in stable.items()}
    report["valorant_rows"] = sum(n for a, n in by_app.items() if a and a.startswith("valorant"))
    report["fl_studio_rows"] = sum(n for a, n in by_app.items() if a and a.startswith("fl"))
    report["quiet_spans"] = {}
    for name, (a, b) in quiet.items():
        n = conn.execute("SELECT count(*) FROM ui_actions WHERE at >= ? AND at < ? AND source != 'yuki'"
                         " AND kind NOT IN ('command')", (a, b)).fetchone()[0]
        report["quiet_spans"][name] = int(n)
    try:
        health = conn.execute("SELECT value FROM nudge_state WHERE name='trace:health'").fetchone()
        report["health"] = json.loads(health[0]) if health else None
    except sqlite3.Error:
        report["health"] = None
    if steps:
        cipher = _cipher(db)
        print("\nSteps (this console only):")
        for r in rows:
            name = ""
            if cipher is not None and r["name_ciphertext"]:
                try:
                    name = cipher.decrypt(r["name_ciphertext"])
                except Exception:  # noqa: BLE001
                    name = "?"
            print(f"  {datetime.fromtimestamp(r['at']):%H:%M:%S} {r['process']:<14.14} {r['kind']:<9} "
                  f"{(r['role'] or '-'):<10} {name[:40]!r:<42} {r['chord'] or ''} [{r['resolved']}, {r['source']}]")
    conn.close()
    return report


def verdict(report: dict, marker_hits: dict | None) -> list[tuple[str, bool | None, str]]:
    out = []
    share = report.get("tree_app_named_share")
    out.append(("element named on >= 85% of clicks in tree apps (right element: compare with --steps)",
                None if share is None else share >= 0.85, f"{share}"))
    out.append(("stale (structure changed before the answer) kept out of learning", None,
                f"{report.get('stale_share')}"))
    if marker_hits is not None:
        trace_hits, other_hits = split_hits(marker_hits)
        out.append(("zero typed characters in the trace's tables or the memory logs", not trace_hits,
                    json.dumps(trace_hits)))
        out.append(("the marker in other tables (what the ambient capture saw on screen, not the trace)", None,
                    json.dumps(other_hits)))
    for name, n in report.get("quiet_spans", {}).items():
        out.append((f"nothing recorded during '{name}'", n == 0, str(n)))
    out.append(("VALORANT: zero rows", report["valorant_rows"] == 0, str(report["valorant_rows"])))
    out.append(("FL Studio maximised: rows present", report["fl_studio_rows"] > 0, str(report["fl_studio_rows"])))
    frag = report.get("keys_per_control") or {}
    out.append(("<= 1.2 element keys per control", all(v <= 1.2 for v in frag.values()) if frag else None,
                json.dumps(frag)))
    out.append(("a takeover during a Yuki run labelled user", report["takeovers"] > 0, str(report["takeovers"])))
    src = report.get("by_source", {})
    out.append(("Yuki's own steps labelled yuki", src.get("yuki", 0) > 0, str(src.get("yuki", 0))))
    out.append(("Handy / G Hub labelled user_injected", src.get("user_injected", 0) > 0,
                str(src.get("user_injected", 0))))
    return out


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--checklist", action="store_true", help="print the supervised session's checklist")
    p.add_argument("--db", type=Path, help="a COPY of the memory database")
    p.add_argument("--copy-real", action="store_true", help="copy the real database into %%TEMP%% and delete it after")
    p.add_argument("--since", help="session start HH:MM (today)")
    p.add_argument("--until", help="session end HH:MM (today)")
    p.add_argument("--day", help="YYYY-MM-DD (default today)")
    p.add_argument("--typed", help="the marker phrase typed during the session")
    p.add_argument("--quiet", action="append", default=[], help="NAME=HH:MM-HH:MM: a span that must record nothing")
    p.add_argument("--logs", type=Path, default=None,
                   help="the memory service's logs folder (default: the installed app's, %%LOCALAPPDATA%%/Yuki/logs)")
    p.add_argument("--steps", action="store_true", help="also print the step lines (with names) on this console")
    args = p.parse_args(argv)
    if args.checklist:
        print(CHECKLIST)
        return 0
    if not (args.db or args.copy_real) or not (args.since and args.until):
        p.error("give --db (a copy) or --copy-real, and --since/--until")
    day = datetime.strptime(args.day, "%Y-%m-%d") if args.day else datetime.now()
    since, until = _span(args.since, day), _span(args.until, day)
    quiet = {}
    for q in args.quiet:
        name, span = q.split("=", 1)
        a, b = span.split("-", 1)
        quiet[name] = (_span(a, day), _span(b, day))
    tmp = None
    db = args.db
    try:
        if args.copy_real:
            tmp = Path(tempfile.mkdtemp(prefix="yuki-trace-check-"))
            db = copy_real(tmp)
        t0 = time.perf_counter()
        report = analyze(db, since, until, quiet, steps=args.steps)
        logs = args.logs if args.logs is not None else default_logs()
        if args.typed and not memory_logs(logs):
            print(f"No memory-*.jsonl logs found in {logs or 'the usual folders'}: the log check cannot run. Pass"
                  " --logs with the memory service's logs folder.", file=sys.stderr)
            return 2
        if logs is not None:
            print(f"Logs read from {logs} ({len(memory_logs(logs))} memory logs).")
        hits = scan_for_marker(db, args.typed, logs) if args.typed else None
        print(json.dumps(report, indent=2, default=str))
        print("\nReport 78 §10.1:")
        for what, ok, value in verdict(report, hits):
            mark = "PASS" if ok else ("----" if ok is None else "FAIL")
            print(f"  [{mark}] {what}: {value}")
        print(f"(checked in {time.perf_counter() - t0:.1f} s)")
    finally:
        if tmp is not None:
            shutil.rmtree(tmp, ignore_errors=True)
            print("The copy was deleted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

