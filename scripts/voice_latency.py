"""Latency percentiles from the voice log (``logs/voice_turns.csv``, content-free).

Usage::

    uv run python scripts/voice_latency.py [CSV ...] [--since 2026-09-25] [--all-turns]

By default only turns that started with the user speaking are counted (turns
the mouth opened to speak a result have no "end of speech"). Prints, per
stage, the count, p50, p90, p95 and max in milliseconds, then the languages
seen and the estimated mouth and TTS cost.
"""

from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STAGES = (
    ("eos_to_eager_ms", "end of speech -> Flux eager end of turn"),
    ("eos_to_eot_ms", "end of speech -> Flux end of turn"),
    ("eos_to_transcript_ms", "end of speech -> final transcript"),
    ("transcript_to_context_ms", "transcript -> context to the mouth"),
    ("llm_start_after_eos_ms", "end of speech -> mouth call starts"),
    ("grounding_wait_ms", "waiting for the memory lookup"),
    ("llm_prep_ms", "building the request"),
    ("llm_open_ms", "request out -> stream open"),
    ("llm_first_event_ms", "request out -> first event"),
    ("llm_api_ttft_ms", "request out -> first content"),
    ("mouth_ttft_ms", "mouth TTFB (Pipecat's metric)"),
    ("llm_first_text_ms", "request out -> first text"),
    ("text_to_tts_ms", "first text -> TTS request"),
    ("tts_ttfb_ms", "TTS time to first byte"),
    ("tts_audio_to_speaking_ms", "first TTS audio -> playing"),
    ("eos_to_first_token_ms", "end of speech -> mouth's first token"),
    ("eos_to_first_audio_ms", "end of speech -> first audio"),
    ("eos_to_action_ms", "end of speech -> a hand's action (v3.3)"),
    ("desk_tabs_ms", "tabs line for the desktop block"),
)


def pct(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return float("nan")
    k = (len(ordered) - 1) * q
    lo, hi = int(k), min(int(k) + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("csv", nargs="*", type=Path, default=[ROOT / "logs" / "voice_turns.csv"])
    ap.add_argument("--since", default="", help="only rows with timestamp >= this (ISO date or date-time)")
    ap.add_argument("--all-turns", action="store_true", help="include result-only turns")
    args = ap.parse_args(argv)
    rows = []
    for path in args.csv:
        if not path.exists():
            print(f"(no such file: {path})", file=sys.stderr)
            continue
        with path.open(encoding="utf-8") as fh:
            rows += [r for r in csv.DictReader(fh) if r.get("timestamp", "") >= args.since]
    turns = rows if args.all_turns else [r for r in rows if r.get("trigger") == "user"]
    print(f"{len(turns)} turns ({len(rows)} rows) from {', '.join(str(p) for p in args.csv)}")
    grounding = Counter(r.get("grounding") or "-" for r in turns)
    eager = sum(1 for r in turns if r.get("eager") == "True")
    print(f"grounding: {dict(grounding)}; eager replies: {eager}; "
          f"end of speech from: {dict(Counter(r.get('eos_source') or '-' for r in turns))}")
    print(f"{'stage':40s} {'n':>4s} {'p50':>7s} {'p90':>7s} {'p95':>7s} {'max':>7s}")
    for key, label in STAGES:
        values = [float(r[key]) for r in turns if r.get(key) not in (None, "")]
        if values:
            print(f"{label:40s} {len(values):4d} {pct(values, .5):7.0f} {pct(values, .9):7.0f} "
                  f"{pct(values, .95):7.0f} {max(values):7.0f}")
        else:
            print(f"{label:40s} {0:4d}")
    langs = Counter(r.get("language") or "-" for r in turns)
    mouth = sum(float(r.get("mouth_cost_usd") or 0) for r in rows)
    tts = sum(float(r.get("tts_cost_usd") or 0) for r in rows)
    errors = sum(1 for r in rows if r.get("error"))
    interrupted = sum(1 for r in rows if r.get("interrupted") == "True")
    print(f"languages: {dict(langs)}; interrupted {interrupted}; errors {errors}")
    print(f"estimated cost: mouth ${mouth:.4f}, TTS ${tts:.4f} (all rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
