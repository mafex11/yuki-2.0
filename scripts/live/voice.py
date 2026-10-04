"""Voice checks for the user's 2 minutes back (report 73, group B), from the content-free voice log.

Read-only. Prints the voice sessions, each turn's timings and tool names (never words), the quick_memory uses
(op, ms, ok), the recent-exchanges refresh, errors; then PASS / FAIL / NOT RUN per step:
  B1 "what's on my list?"   quick_memory op=todos (after the leftVoice install also op=answer_now, a lead first)
  B2 "add X to my list"     quick_memory op=add_todos ok, one reply, no second reply right after
  B3 "what time is it?"     a plain turn, no tool, normal timing

    .venv\\Scripts\\python.exe scripts\\live\\voice.py --since 22:10
"""

from __future__ import annotations

import collections
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import (arg_parser, csv_rows, error_class, files, header, hhmm, jsonl, log_dir, num,  # noqa: E402
                     stats, verdict, window)

TOKEN = re.compile(r"^[a-z_]{2,40}$")


def tools_of(value) -> list[str]:
    parts = value if isinstance(value, list) else re.split(r"[;,| ]+", str(value or ""))
    return [p for p in (str(x).strip() for x in parts) if TOKEN.match(p)]


def main() -> int:
    args = arg_parser(__doc__).parse_args()
    since, until = window(args)
    header("voice (group B)", since, until)
    rows = sorted(jsonl(files("voice/voice-*.jsonl", since), since, until), key=lambda r: r["_ts"])
    sessions = [r for r in rows if r.get("type") == "session"]
    print(f" session events: {dict(collections.Counter(str(r.get('state')) for r in sessions))}")
    turns = [r for r in rows if r.get("type") == "turn"]
    qm = [r for r in rows if r.get("type") == "quick_memory"]
    for r in rows:
        t = r.get("type")
        if t == "turn":
            print(f"   turn {hhmm(r['_ts'])} trigger={r.get('trigger')} tools={tools_of(r.get('tool_calls'))} "
                  f"first_audio_ms={num(r.get('eos_to_first_audio_ms'))} first_token_ms="
                  f"{num(r.get('eos_to_first_token_ms'))} early_say_ms={num(r.get('llm_early_say_ms'))} "
                  f"reply_chars={r.get('reply_chars')} marker={r.get('marker') if TOKEN.match(str(r.get('marker'))) else '-'}"
                  f" interrupted={r.get('interrupted')} error={error_class(r.get('error')) or '-'}")
        elif t == "quick_memory":
            nums = {k: v for k, v in r.items() if k not in ("ts", "_ts", "_file", "type", "session", "op", "call", "at")
                    and isinstance(v, (int, float, bool))}
            print(f"   quick_memory {hhmm(r['_ts'])} op={r.get('op') if TOKEN.match(str(r.get('op'))) else '?'} {nums}")
        elif t == "recent":
            print(f"   recent {hhmm(r['_ts'])} op={r.get('op')} ok={r.get('ok')} used={r.get('used')}")
        elif t == "hands" and isinstance(r.get("op"), str) and TOKEN.match(r["op"]):
            print(f"   hand {hhmm(r['_ts'])} op={r['op']} ok={r.get('ok')} ms={num(r.get('ms'))}")
        elif t == "error":
            print(f"   error {hhmm(r['_ts'])} fatal={r.get('fatal')} {error_class(r.get('error'))}")
        elif t == "session" and r.get("state") in ("start", "stop", "woken"):
            print(f"   session {hhmm(r['_ts'])} {r.get('state')} turns={r.get('turns')} via={r.get('via') if TOKEN.match(str(r.get('via'))) else '-'}")
    csv_n = len(csv_rows(log_dir() / "voice_turns.csv", since, until))
    print(f" turns={len(turns)} (voice_turns.csv rows {csv_n}); first audio ms "
          f"{stats([t.get('eos_to_first_audio_ms') for t in turns if t.get('trigger') == 'user'])}")

    print("\nPer step")
    reads = [q for q in qm if q.get("op") in ("todos", "today")]
    answer_now = [q for q in qm if q.get("op") == "answer_now"]
    verdict("B1 list read by voice (quick_memory todos)", any(q.get("ok", True) is not False for q in reads)
            if reads else None, f"{len(reads)} reads, {len(answer_now)} answer_now (leftVoice build only)")
    adds = [q for q in qm if q.get("op") == "add_todos"]
    double = 0
    for q in adds:
        after = [t for t in turns if 0 <= t["_ts"] - q["_ts"] <= 20 and t.get("trigger") == "result"
                 and (t.get("reply_chars") or 0) > 0]
        double += 1 if len(after) > 1 else 0
    verdict("B2 add by voice ok, no second reply after it", (any(q.get("ok", True) is not False for q in adds)
                                                             and not double) if adds else None,
            f"{len(adds)} adds, {double} with a second reply")
    plain = [t for t in turns if t.get("trigger") == "user" and not tools_of(t.get("tool_calls"))]
    verdict("B3 a plain turn answered (no tool), first audio under 2.5 s",
            any((t.get("eos_to_first_audio_ms") or 1e9) < 2500 for t in plain) if plain else None,
            f"{len(plain)} plain turns")
    fatal = [r for r in rows if r.get("type") == "error" and r.get("fatal")]
    verdict("no fatal voice error", not fatal if rows else None, f"{len(fatal)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
