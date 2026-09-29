"""How a voice replay felt, in numbers (docs/VOICE.md v3.14): per request and for the whole run.

Reads the report ``voice_scenarios.py`` writes (``<out>/<scenario>.json``) and the run's content-free voice log
(``<out>/voice-log/voice-*.jsonl``). Per scripted line (a request): end of speech -> first audio, -> first desktop
action, -> the last thing Yuki started saying for it (the outcome), the longest silence while the request was open
(between the end of speech and the last audio), the step lines said and the lines said twice. For the run: the same
at p50 / p90, loop stalls, and how long ready results waited in the narrator gate.

Usage::

    .venv\\Scripts\\python.exe scripts/voice_feel.py <out dir> <scenario> [--json]
"""

from __future__ import annotations

import glob
import json
import sys
from pathlib import Path
from typing import Any

#: Audio stretches closer than this are one utterance (the voice's pieces: sentence gaps measure 0.11-0.26 s).
UTTERANCE_GAP_S = 0.6


def _pct(values: list[float], p: float) -> float | None:
    values = sorted(v for v in values if v is not None)
    return round(values[min(len(values) - 1, int(len(values) * p))]) if values else None


def utterances(segments: list[list[float]]) -> list[tuple[float, float]]:
    """``(start, end)`` of each utterance, from the report's audio stretches ``[start, seconds]``."""
    out: list[tuple[float, float]] = []
    for start, dur in sorted(segments):
        end = start + dur
        if out and start - out[-1][1] < UTTERANCE_GAP_S:
            out[-1] = (out[-1][0], max(out[-1][1], end))
        else:
            out.append((start, end))
    return out


def _epoch(iso: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(iso).timestamp()


def feel(report: dict[str, Any], log_events: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    ev = report.get("timeline") or []
    ends = report.get("speech_ends") or {}
    starts = [(e["t"], e["label"]) for e in ev if e["kind"] == "line"]
    utts = utterances(report.get("audio_segments") or [])
    step_texts = {" ".join(str(x).casefold().split()) for e in ev if e["kind"] == "brain"
                  and e.get("event") == "step_line" for x in (e.get("detail") or [])}
    for e in ev:   # a step line that came with its finished call (not streamed ahead)
        if e["kind"] == "brain" and e.get("event") == "tool_called":
            detail = e.get("detail") or []
            args = detail[1] if len(detail) > 1 and isinstance(detail[1], dict) else {}
            if isinstance(args.get("say_now"), str):
                step_texts.add(" ".join(args["say_now"].casefold().split()))
    spoken_all: list[str] = []
    rows = []
    for i, (t0, label) in enumerate(starts):
        t1 = starts[i + 1][0] if i + 1 < len(starts) else float("inf")
        eos = ends.get(label, t0)
        mine = [(a, b) for a, b in utts if eos <= a < t1]
        actions = [e["t"] for e in ev if e["kind"] == "action" and eos <= e["t"] < t1]
        said = [e["text"] for e in ev if e["kind"] in ("yuki", "yuki_code") and eos <= e["t"] < t1
                and str(e.get("text") or "").strip()]
        spoken_all += said
        steps = [s for s in said if " ".join(s.casefold().split()) in step_texts]
        thinker = any(e["kind"] == "brain" and e.get("event") == "submitted" and eos <= e["t"] < t1 for e in ev)
        gaps = [b[0] - a[1] for a, b in zip(mine, mine[1:])]
        first_gap = (mine[0][0] - eos) if mine else None
        rows.append({
            "line": label,
            "first_audio_ms": round(first_gap * 1000) if first_gap is not None else None,
            "first_action_ms": round((actions[0] - eos) * 1000) if actions else None,
            "last_audio_ms": round((mine[-1][0] - eos) * 1000) if mine else None,
            "utterances": len(mine),
            "max_silence_ms": round(max(gaps) * 1000) if gaps else 0,
            "step_lines": len(steps),
            "thinker": thinker,
            "said": said,
        })
    keys = [" ".join(s.casefold().split()) for s in spoken_all]
    dupes = sorted({k for k in keys if keys.count(k) > 1})
    out: dict[str, Any] = {
        "scenario": report.get("scenario"), "reason": report.get("reason"), "rows": rows,
        "first_audio_p50": _pct([r["first_audio_ms"] for r in rows], 0.5),
        "first_audio_p90": _pct([r["first_audio_ms"] for r in rows], 0.9),
        "first_action_p50": _pct([r["first_action_ms"] for r in rows], 0.5),
        "last_audio_p50": _pct([r["last_audio_ms"] for r in rows], 0.5),
        "max_silence_ms": max((r["max_silence_ms"] for r in rows), default=0),
        "step_lines": sum(r["step_lines"] for r in rows),
        # The requests the thinker worked on (a job was submitted for them): the complaint of 2026-09-29.
        "thinker_rows": sum(1 for r in rows if r["thinker"]),
        "thinker_first_audio_p50": _pct([r["first_audio_ms"] for r in rows if r["thinker"]], 0.5),
        "thinker_outcome_p50": _pct([r["last_audio_ms"] for r in rows if r["thinker"]], 0.5),
        "thinker_max_silence_ms": max((r["max_silence_ms"] for r in rows if r["thinker"]), default=0),
        "repeated_lines": dupes,
        "brain_cost_usd": report.get("brain_cost_usd"),
    }
    if log_events:
        lags = [e.get("ms") for e in log_events if e.get("type") == "loop_lag" and e.get("op") != "summary"]
        # In the conversation: from the first user turn's start on (start-up imports come before anyone speaks);
        # a stall while the replay's own embedder loads in this process is set apart (live, the memory service
        # serves it from its own process; ``voice_scenarios.py --serve-embedder`` does the same offline).
        opened = [e.get("opened_at") for e in log_events if e.get("type") == "turn" and e.get("opened_at")]
        start = _epoch(min(opened)) if opened else None
        talk, embed = [], []
        for e in log_events:
            if e.get("type") != "loop_lag" or e.get("op") == "summary" or start is None or e.get("ts", 0) < start:
                continue
            where = e.get("where") or []
            threads = where[0].get("threads", {}) if where and isinstance(where[0], dict) else {}
            (embed if "yuki-embed-load" in threads else talk).append(e.get("ms") or 0)
        waits = []
        for e in log_events:
            if e.get("type") == "narrator" and e.get("op") == "spoke":
                kinds, w = e.get("kinds") or [], e.get("waited_ms") or []
                waits += [w[k] for k, kind in enumerate(kinds) if kind in ("final", "ask") and k < len(w)]
        lines = [e for e in log_events if e.get("type") == "step_line"]
        out.update(loop_lag_max_ms=max(lags) if lags else 0,
                   talk_lag_max_ms=max(talk) if talk else 0, talk_lags_over_300=sum(1 for x in talk if x >= 300),
                   embed_load_lag_max_ms=max(embed) if embed else 0,
                   result_wait_p50_ms=_pct(waits, 0.5), result_wait_max_ms=max(waits) if waits else None,
                   step_line_ops={op: sum(1 for e in lines if e.get("op") == op) for op in ("offered", "spoken", "dropped")},
                   step_line_dropped={why: sum(1 for e in lines if e.get("op") == "dropped" and e.get("why") == why)
                                      for why in sorted({str(e.get("why")) for e in lines if e.get("op") == "dropped"})})
    return out


def main(argv: list[str]) -> int:
    out_dir, scenario = Path(argv[0]), argv[1]
    report = json.loads((out_dir / f"{scenario}.json").read_text(encoding="utf-8"))
    events: list[dict[str, Any]] = []
    for path in sorted(glob.glob(str(out_dir / "voice-log" / "voice-*.jsonl"))):
        for line in open(path, encoding="utf-8"):
            try:
                events.append(json.loads(line))
            except ValueError:
                continue
    result = feel(report, events)
    if "--json" in argv:
        print(json.dumps(result, ensure_ascii=False, indent=1))
        return 0
    print(f"== {result['scenario']} ({result['reason']}): first audio p50 {result['first_audio_p50']} / p90 "
          f"{result['first_audio_p90']} ms, first action p50 {result['first_action_p50']} ms, last audio p50 "
          f"{result['last_audio_p50']} ms, longest silence {result['max_silence_ms']} ms, step lines "
          f"{result['step_lines']}, repeated {len(result['repeated_lines'])}, loop lag max "
          f"{result.get('loop_lag_max_ms')} ms, result wait p50 {result.get('result_wait_p50_ms')} / max "
          f"{result.get('result_wait_max_ms')} ms, brain ${result['brain_cost_usd']}")
    print(f"   thinker requests {result['thinker_rows']}: first audio p50 {result['thinker_first_audio_p50']} ms, "
          f"outcome p50 {result['thinker_outcome_p50']} ms, longest silence {result['thinker_max_silence_ms']} ms; "
          f"stalls in the conversation max {result.get('talk_lag_max_ms')} ms ({result.get('talk_lags_over_300')} "
          f">= 300 ms), replay embedder load max {result.get('embed_load_lag_max_ms')} ms")
    for r in result["rows"]:
        print(f"  {r['line']:12s}{'*' if r['thinker'] else ' '}audio {r['first_audio_ms']} action {r['first_action_ms']} "
              f"last {r['last_audio_ms']} "
              f"silence {r['max_silence_ms']} steps {r['step_lines']} :: {' | '.join(r['said'])[:200]}")
    if result["repeated_lines"]:
        print("  repeated:", result["repeated_lines"])
    if result.get("step_line_ops"):
        print("  step lines:", result["step_line_ops"], result.get("step_line_dropped"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
