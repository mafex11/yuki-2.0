"""Evaluate the "Yuki" wake word on generated clips (files only; no microphone).

For each keyword-spotting model and each (threshold, boost) pair it reports:

* misses: clips that contain "Yuki" and did not fire,
* false accepts: clips without it that fired, per group (similar-sounding
  words like "you key" / "cookie" / "yucky", plain English, Hindi),
* false wakes per hour on one continuous stream of every negative clip and
  the background talk,
* CPU: decode time per second of audio on one core (the always-on cost).

Usage::

    uv run python scripts/voice_wake_eval.py --clips <dir with manifest.json> [<more dirs>] [--models ...] [--babble-snr 10]

``--babble-snr`` mixes the background-talk clips into every clip at that signal-to-noise
ratio (the wake word in a room where people talk). The spotter's competing variants
(``yuki.voice.wake.MODELS``) are in force, so a sound-alike reported instead of the name
counts as no wake.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yuki.voice.transport import load_clip  # noqa: E402
from yuki.voice.wake import MODELS, WakeSpotter  # noqa: E402

RATE = 16_000
SIMILAR = ("you_key", "cookie", "yucky", "ukulele", "youtube", "tokyo", "you_key2", "cookie2", "yucky2", "you_kid",
           "euchre", "ukulele2", "fw_story", "fw_mom")


def group(name: str) -> str:
    base = name.split("__")[0]
    if base in SIMILAR:
        return "similar"
    if base.startswith("neg_hi"):
        return "hindi"
    if base.startswith("bg_"):
        return "background"
    return "plain"


def run_clip(spotter: WakeSpotter, audio: np.ndarray) -> tuple[int, float]:
    spotter.reset()
    hits = 0
    started = time.process_time()
    tail = np.zeros(int(0.6 * RATE), np.float32)  # let a word at the very end finish decoding
    for chunk in np.array_split(np.concatenate([audio, tail]), max(1, (len(audio) + len(tail)) // 1600)):
        if spotter.feed(chunk.astype(np.float32)):
            hits += 1
    return hits, time.process_time() - started


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--clips", required=True, type=Path, nargs="+")
    ap.add_argument("--babble-snr", dest="babble_snr", type=float, default=None)
    ap.add_argument("--models", default=",".join(MODELS))
    ap.add_argument("--thresholds", default="0.15,0.2,0.25,0.3")
    ap.add_argument("--boosts", default="2.0")
    ap.add_argument("--json", type=Path, help="write the results table here")
    args = ap.parse_args(argv)
    manifest = [c for d in args.clips for c in json.loads((d / "manifest.json").read_text(encoding="utf-8"))]
    wake = [c for c in manifest if c["set"] in ("wake", "background")]
    audio = {c["name"]: load_clip(Path(c["file"])) for c in wake}
    if args.babble_snr is not None:
        babble = np.concatenate([audio[c["name"]] for c in wake if c["set"] == "background"])
        rng = np.random.default_rng(1)
        for c in wake:
            if c["set"] != "wake":
                continue
            x = audio[c["name"]]
            start = int(rng.integers(0, max(1, len(babble) - len(x))))
            n = np.resize(babble[start:start + len(x)], len(x))
            gain = np.sqrt((np.mean(x ** 2) + 1e-9) / (np.mean(n ** 2) + 1e-9) / 10 ** (args.babble_snr / 10))
            audio[c["name"]] = (x + n * gain).astype(np.float32)
    positives = [c for c in wake if c["wake"]]
    negatives = [c for c in wake if not c["wake"]]
    gap = np.zeros(int(0.8 * RATE), np.float32)
    stream = np.concatenate([x for c in negatives for x in (audio[c["name"]], gap)])
    stream_h = len(stream) / RATE / 3600
    print(f"{len(positives)} clips with the name, {len(negatives)} without; negative stream {stream_h * 60:.1f} min")
    rows = []
    for model in args.models.split(","):
        for threshold in (float(x) for x in args.thresholds.split(",")):
            for boost in (float(x) for x in args.boosts.split(",")):
                spotter = WakeSpotter(model, threshold=threshold, boost=boost)
                missed = [c["name"] for c in positives if run_clip(spotter, audio[c["name"]])[0] == 0]
                fa: dict[str, list[str]] = {}
                for c in negatives:
                    if run_clip(spotter, audio[c["name"]])[0]:
                        fa.setdefault(group(c["name"]), []).append(c["name"])
                hits, cpu = run_clip(spotter, stream)
                row = {
                    "model": model, "threshold": threshold, "boost": boost,
                    "miss": len(missed), "positives": len(positives),
                    "fa_similar": len(fa.get("similar", [])), "fa_plain": len(fa.get("plain", [])),
                    "fa_hindi": len(fa.get("hindi", [])), "fa_background": len(fa.get("background", [])),
                    "stream_wakes": hits, "wakes_per_hour": round(hits / stream_h, 1),
                    "cpu_pct_one_core": round(100 * cpu / (len(stream) / RATE), 2),
                    "missed": missed, "false_accepts": fa,
                }
                rows.append(row)
                print(f"{model:16s} th={threshold:<4} boost={boost:<3} miss {row['miss']:2d}/{len(positives)}  "
                      f"FA similar {row['fa_similar']:2d} plain {row['fa_plain']:2d} hindi {row['fa_hindi']:2d} "
                      f"bg {row['fa_background']}  stream {hits:2d} ({row['wakes_per_hour']}/h)  "
                      f"cpu {row['cpu_pct_one_core']}%")
    if args.json:
        args.json.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
