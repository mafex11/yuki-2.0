"""Live probe for the reviewer: process loopback of one app, and who takes the microphone, for N seconds.

Builders never run this (it opens a live capture stream). Run from the repository root:

    python scripts/loopback_probe.py --exe Discord.exe --seconds 10
    python scripts/loopback_probe.py --pid 12345 --seconds 30          # a game: its full-screen window's pid
    python scripts/loopback_probe.py --exe chrome.exe --seconds 10 --no-tree

What it does (docs/research/revamp-2026-10-02/60-build-call-others-capture.md):

1. Finds the app's root process from one Toolhelp snapshot (nothing is opened), or takes ``--pid`` as given (for
   VALORANT: the pid of ``VALORANT-Win64-Shipping.exe`` from Task Manager's Details tab; Yuki uses the pid of the
   full-screen window and never opens the game's process).
2. Opens process loopback on that process tree (:class:`yuki.memory.loopback.LoopbackSource`), prints the format the
   engine accepted, then one line a second: level (RMS and peak dBFS), samples, packets, silent packets, gaps filled,
   timestamp errors and the stamp lag (arrival minus the block's QPC-stamped end: should stay small and steady).
3. Meanwhile watches every capture session (:class:`yuki.memory.loopback.CaptureWatch`, event-driven) and prints each
   process's microphone going ACTIVE / inactive with its time. Press and release push-to-talk in the game during the
   run: if its line toggles with the key, ``heard_by`` can tell a reply to teammates from talk on the call.
4. Writes what was captured to ``%TEMP%\\yuki-loopback-<pid>-<time>.wav`` (16 kHz mono 16-bit; a silence of more than
   3 s is shortened to 3 s, as the recorder does) and prints a summary. Nothing goes into Yuki's memory.
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _db(x: float) -> float:
    return 20.0 * np.log10(max(float(x), 1e-9))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/loopback_probe.py")
    who = parser.add_mutually_exclusive_group(required=True)
    who.add_argument("--exe", help="image name of the app (Discord.exe, chrome.exe)")
    who.add_argument("--pid", type=int, help="the process to capture (with its tree)")
    parser.add_argument("--seconds", type=float, default=10.0)
    parser.add_argument("--no-tree", dest="tree", action="store_false", help="this process only, not its children")
    parser.add_argument("--wav", type=Path, default=None, help="where to write the capture (default: %%TEMP%%)")
    args = parser.parse_args(argv)

    from yuki.memory.loopback import MIN_BUILD, RATE, CaptureWatch, LoopbackSource, app_root_pid, process_table

    build = getattr(sys.getwindowsversion(), "build", 0)
    print(f"Windows build {build} (process loopback needs {MIN_BUILD} or later)")
    table = process_table()
    names = {pid: image for pid, _parent, image in table}
    if args.pid:
        pid = int(args.pid)
    else:
        from yuki.perception import audio

        try:
            playing = [int(s["pid"]) for s in audio.render_sessions() if s.get("state") == "active"]
        except Exception:
            playing = []
        pid = app_root_pid(args.exe, table, playing)
        if pid is None:
            print(f"no process runs {args.exe}")
            return 2
    print(f"target pid {pid} ({names.get(pid, '?')}), process tree: {'yes' if args.tree else 'no'}")

    t0 = time.time()
    changes: list[tuple[float, int, bool]] = []

    def name_of(p: int) -> str:
        if p not in names:
            names.update({q: image for q, _parent, image in process_table()})
        return names.get(p, "?")

    def on_change(at: float, p: int, active: bool) -> None:
        changes.append((at, p, active))
        print(f"  microphone {'ACTIVE  ' if active else 'inactive'} at +{at - t0:7.2f} s  pid {p:>6}  {name_of(p)}",
              flush=True)

    watch = CaptureWatch(on_change, exclude_pids={os.getpid()})
    watch.start()
    print(f"capture-session watch: {watch.mode}")

    source = LoopbackSource(pid, include_tree=args.tree)
    try:
        source.start()
    except Exception as exc:
        print(f"process loopback unavailable: {exc}")
        watch.stop()
        return 3
    print(f"negotiated: {source.opened_as}   (tried: {', '.join(source.tried)})")

    blocks: list[tuple[float, np.ndarray]] = []
    second: list[np.ndarray] = []
    lags: list[float] = []
    st = source.stats
    last = dict(packets=0, silent=0, gaps=0, ts=0)
    tick = time.monotonic()
    end = tick + args.seconds
    try:
        while time.monotonic() < end:
            got = source.read(0.1)
            if got is not None:
                at, samples = got
                blocks.append(got)
                second.append(samples)
                lags.append(time.time() - (at + len(samples) / RATE))
            if time.monotonic() - tick >= 1.0:
                tick = time.monotonic()
                data = np.concatenate(second) if second else np.zeros(1, np.float32)
                rms = float(np.sqrt(np.mean(data.astype(np.float64) ** 2)))
                gaps = source._restamper.gaps
                print(f"+{time.time() - t0:5.1f} s  level {_db(rms):6.1f} dBFS rms, {_db(np.max(np.abs(data))):6.1f} peak"
                      f"  samples {len(data) if second else 0:6d}  packets +{st.packets - last['packets']:3d}"
                      f"  silent +{st.silent_packets - last['silent']:3d}  gaps +{gaps - last['gaps']}"
                      f"  ts errors +{st.timestamp_errors - last['ts']}"
                      f"  stamp lag {np.median(lags) * 1000 if lags else float('nan'):6.1f} ms", flush=True)
                last = dict(packets=st.packets, silent=st.silent_packets, gaps=gaps, ts=st.timestamp_errors)
                second, lags = [], []
    finally:
        source.stop()
        watch.stop()

    audio_all = np.concatenate([b for _, b in blocks]) if blocks else np.zeros(0, np.float32)
    path = args.wav or Path(tempfile.gettempdir()) / f"yuki-loopback-{pid}-{time.strftime('%Y%m%d-%H%M%S')}.wav"
    with wave.open(str(path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(RATE)
        out.writeframes((np.clip(audio_all, -1, 1) * 32767).astype(np.int16).tobytes())
    rs = source._restamper
    print(f"\nwrote {path} ({len(audio_all) / RATE:.1f} s)")
    print(f"packets {st.packets}, silent {st.silent_packets}, discontinuities {st.discontinuities}, timestamp errors "
          f"{st.timestamp_errors}; gaps filled {rs.gaps} ({rs.gap_s:.1f} s), jumps {rs.jumps}, early packets {rs.early};"
          f" capture thread CPU {st.cpu_s * 1000:.0f} ms")
    if blocks:
        print(f"first block stamped +{blocks[0][0] - t0:.2f} s after the start; last ends +"
              f"{blocks[-1][0] + len(blocks[-1][1]) / RATE - t0:.2f} s")
    per_pid: dict[int, int] = {}
    for _at, p, active in changes:
        per_pid[p] = per_pid.get(p, 0) + int(active)
    print("microphone sessions going active, per process: "
          + (", ".join(f"{name_of(p)} ({p}) x{n}" for p, n in sorted(per_pid.items())) or "none"))
    print(f"watch counters {watch.counters}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
