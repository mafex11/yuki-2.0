"""Takeover live checks (report 73, group A2 = report 63's checklist), from the worker and tray logs.

Read-only. Prints each pause and resume (time, why, how resumed, seconds), whether anything ran or was said while
paused, the resume_view read after it, and spurious pauses: a pause with no deliberate poke from
``scripts/live/poke.py`` in the 3 s before it (the poke log holds times only). Never prints text.

    .venv\\Scripts\\python.exe scripts\\live\\takeover.py --since 21:30
"""

from __future__ import annotations

import collections
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import arg_parser, header, hhmm, session_records, verdict, window  # noqa: E402

POKE_LOG = Path(os.environ.get("TEMP", ".")) / "yuki-live" / "pokes.jsonl"


def pokes(since: float, until: float) -> list[dict]:
    out = []
    if POKE_LOG.is_file():
        for line in POKE_LOG.open(encoding="utf-8"):
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if since <= float(r.get("ts") or 0) < until:
                out.append(r)
    return out


def main() -> int:
    args = arg_parser(__doc__).parse_args()
    since, until = window(args)
    header("takeover (report 63)", since, until)
    worker = sorted(session_records(since, until, "worker"), key=lambda r: r["_ts"])
    ui = [r for r in session_records(since, until, "ui") if r.get("type") == "ui"]
    poked = pokes(since, until)
    tk = [r for r in worker if r.get("type") == "takeover"]
    print(f" takeover records={len(tk)} states={dict(collections.Counter(str(r.get('state')) for r in tk))}"
          f" pokes logged={len(poked)} {dict(collections.Counter(p.get('kind') for p in poked))}")
    paused_at = None
    episodes = []
    for r in worker:
        t, st = r.get("type"), r.get("state")
        if t == "takeover" and st == "paused":
            paused_at = r["_ts"]
            episodes.append({"at": r["_ts"], "why": r.get("why"), "acts": 0, "says": 0, "end": None, "end_state": None,
                             "by": None, "words": None, "view": False, "seconds": None})
        elif t == "takeover" and st == "resume_asked" and episodes:
            episodes[-1].update(by=r.get("by"), words=r.get("words"))
        elif t == "takeover" and st in ("resumed", "stopped", "ended") and episodes and episodes[-1]["end"] is None:
            episodes[-1].update(end=r["_ts"], end_state=st, seconds=r.get("seconds"), end_why=r.get("why"))
            paused_at = None
        elif paused_at is not None and t == "tool_result":
            episodes[-1]["acts"] += 1
        elif paused_at is not None and t == "say":
            episodes[-1]["says"] += 1
        elif t == "perception" and r.get("kind") == "resume_view" and episodes:
            episodes[-1]["view"] = True
    for e in episodes:
        poke = any(0 <= e["at"] - float(p.get("ts") or 0) <= 3.0 for p in poked)
        e["poked"] = poke
        print(f"   pause {hhmm(e['at'])} why={e['why']} poked={poke} acts_while_paused={e['acts']} "
              f"says_while_paused={e['says']} -> {e['end_state']} by={e['by']} words={e['words']} "
              f"after_s={e['seconds']} resume_view={e['view']}")
    ev = collections.Counter(str(r.get("event")) for r in ui if str(r.get("event", "")).startswith(
        ("takeover", "submit", "cancel", "strip_stop", "steer", "stop", "keep_scrolling", "send_")))
    clicks = [r for r in ui if r.get("event") == "takeover_resume_click"]
    print(f" tray events: {dict(ev)}; resume clicks delivered={sum(1 for r in clicks if r.get('delivered'))}/"
          f"{len(clicks)}")
    skipped = [r for r in tk if r.get("state") == "skipped"]
    failed = [r for r in tk if r.get("state") in ("watch_failed", "resume_view_failed")]

    print("\nPer step (report 63 checklist)")
    mouse = [e for e in episodes if e["why"] == "mouse" and e["poked"]]
    verdict("1 a hand-width mouse move pauses; nothing runs or is said while paused",
            any(e["acts"] == 0 and e["says"] == 0 for e in mouse) if mouse else None,
            f"{len(mouse)} poked mouse pauses")
    isl = [e for e in episodes if e["by"] == "island"]
    verdict("2 Resume on the island: resumed + resume_view", any(e["end_state"] == "resumed" and e["view"]
                                                                for e in isl) if isl else None, f"{len(isl)}")
    keys = [e for e in episodes if e["why"] in ("keys", "click") and e["poked"]]
    words = [e for e in keys if e["by"] == "words" and e["words"]]
    verdict("3 a key pauses; 'go on' typed resumes with the words",
            any(e["end_state"] == "resumed" for e in words) if words else None,
            f"{len(keys)} key pauses, {len(words)} resumed by words")
    voice = [e for e in episodes if e["by"] == "voice" or (e["end_state"] == "stopped" and e["by"] != "island")]
    verdict("4 voice 'go on' / 'stop' (user group)", (bool(voice)) if voice else None, f"{len(voice)}")
    spurious = [e for e in episodes if not e["poked"]]
    acted = sum(1 for r in worker if r.get("type") == "tool_result" and r.get("name") in
                ("click", "type_text", "press", "hotkey", "scroll", "set_slider", "launch_app", "focus_window"))
    verdict("5/6/7 no pause without a deliberate poke (jitter, Yuki's own input, island clicks)",
            not spurious if acted else None,
            f"{len(spurious)} unpoked pauses {dict(collections.Counter(e['why'] for e in spurious))} over {acted} actions")
    verdict("watch never failed", not failed if tk else None, f"{len(failed)} watch/resume_view failures")
    print(f"  skipped (caught) actions while paused: {len(skipped)}")
    stopped = [e for e in episodes if e["end_state"] == "ended"]
    print(f"  8 limit (unresumed pause ended quietly): {len(stopped)} ended after "
          f"{[e['seconds'] for e in stopped]} s (skipped in the runbook unless takeover_idle_stop_s is lowered)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
