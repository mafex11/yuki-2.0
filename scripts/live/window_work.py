"""Window, Work page and to-do list checks (report 73: away step A3 and the user's glance, B4), from the logs.

Read-only. Prints the window processes, page builds (page, result, ms), page actions (action name, ok), the
thinker's open_yuki uses (page asked, ok), the tray's window ops, and the work board runs; then PASS / FAIL /
NOT RUN per step. Never prints list items, chat names or any text.

    .venv\\Scripts\\python.exe scripts\\live\\window_work.py --since 21:40
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (arg_parser, files, header, hhmm, jsonl, num, session_records, stats, verdict,  # noqa: E402
                     window)

TOKEN = re.compile(r"^[a-z_\-/:#]{1,40}$")


def tok(v) -> str:
    return str(v) if TOKEN.match(str(v or "")) else ("-" if not v else "other")


def main() -> int:
    args = arg_parser(__doc__).parse_args()
    since, until = window(args)
    header("window / Work / list", since, until)
    win = session_records(since, until, "window")
    starts = [r for r in win if r.get("type") == "session_start"]
    pages = [r for r in win if r.get("event") == "web_page"]
    acts = [r for r in win if r.get("event") == "web_act"]
    fails = [r for r in win if str(r.get("event", "")).endswith("_failed")]
    print(f" window processes started={len(starts)} page builds={len(pages)} actions={len(acts)} failures={len(fails)}")
    per_page = collections.defaultdict(list)
    for r in pages:
        per_page[tok(r.get("page"))].append(r.get("ms"))
    for page, ms in sorted(per_page.items()):
        print(f"   page {page:9} builds={len(ms)} ms {stats(ms)}")
    print(f"   page results: {dict(collections.Counter((tok(r.get('page')), tok(r.get('result'))) for r in pages))}")
    print(f"   actions: {dict(collections.Counter((tok(r.get('action')), bool(r.get('ok'))) for r in acts))}")
    print(f"   failures: {dict(collections.Counter(tok(r.get('event')) for r in fails))}")
    worker = session_records(since, until, "worker")
    calls = {str(r.get("tool_use_id")): r for r in worker if r.get("type") == "tool_call" and r.get("name") == "open_yuki"}
    oy = [r for r in worker if r.get("type") == "tool_result" and r.get("name") == "open_yuki"]
    for r in oy:
        inp = (calls.get(str(r.get("tool_use_id"))) or {}).get("input") or {}
        print(f"   open_yuki {hhmm(r['_ts'])} page={tok(inp.get('page'))} ok={r.get('ok')}")
    ui = [r for r in session_records(since, until, "ui") if r.get("type") == "ui"]
    ops = collections.Counter((tok(r.get("op")), r.get("ok")) for r in ui if r.get("event") == "window_op")
    print(f"   tray window ops: {dict(ops)}")
    conv = list(jsonl(files("memory/conversations-*.jsonl", since), since, until))
    wb = [r for r in conv if r.get("type") == "work_board"]
    print(f"   work board runs={len(wb)} cost=${sum(float(r.get('cost_usd') or 0) for r in wb):.4f} "
          f"latest={hhmm(wb[-1]['_ts']) if wb else '-'} chats={wb[-1].get('chats') if wb else '-'} "
          f"replies={wb[-1].get('replies') if wb else '-'} loops={wb[-1].get('loops') if wb else '-'}")

    print("\nPer step")
    todo = [r for r in oy if ((calls.get(str(r.get("tool_use_id"))) or {}).get("input") or {}).get("page")
            in ("todos", "home")]
    verdict("A3.1 'open my to-do list' -> open_yuki todos ok", any(r.get("ok") for r in todo) if oy else None,
            f"{len(todo)} of {len(oy)} open_yuki")
    home = [r for r in pages if r.get("page") == "home" and r.get("result") == "posted"]
    verdict("A3.1b the window built Home (Your list) after it", bool(home) if (oy or pages) else None,
            f"{len(home)} Home builds")
    work = [r for r in pages if r.get("page") == "work" and r.get("result") in ("posted", "unchanged_stamps")]
    slow = [r for r in work if (r.get("ms") or 0) > 1500]
    verdict("A3.2 Work page builds, none over 1.5 s", (bool(work) and not slow) if pages else None,
            f"{len(work)} builds, {len(slow)} slow, ms {stats([r.get('ms') for r in work])}")
    verdict("A3.3 no window failures (page, act, memory, pipe)", not fails if win else None, f"{len(fails)}")
    bad = [r for r in acts if not r.get("ok")]
    verdict("B4 page actions the user pressed worked", not bad if acts else None,
            f"{len(acts) - len(bad)}/{len(acts)} ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
