"""Footprint of recording both sides of a call, offline (the design notes).

    python scripts/call_tracks_bench.py [--seconds 60]

No live audio: every track is an :class:`yuki.memory.calls.ArraySource` replay of a synthetic scene built from the
speech clips in ``tests/data/voice`` and generated noise, recorded into a temp database exactly as the call worker
records (speech gate with Silero VAD, Opus, encryption, spool). Measured per track: CPU of the recording thread (gate,
VAD, encoding, encryption, spool writes), the VAD's share, chunks and speech kept. The process loopback capture loop
itself cannot run without a live stream; its Python side (packet conversion, restamping, queueing) is timed on
synthetic 10 ms packets, and the two native calls per packet (GetBuffer, ReleaseBuffer) are estimated from timing
plain ctypes calls into kernel32.
"""

from __future__ import annotations

import argparse
import ctypes
import queue
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

RATE = 16_000


def clip(name: str) -> np.ndarray:
    with wave.open(str(ROOT / "tests" / "data" / "voice" / name), "rb") as w:
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return data


def place(scene: np.ndarray, sound: np.ndarray, at_s: float, gain: float = 1.0) -> None:
    i = int(at_s * RATE)
    n = min(len(sound), len(scene) - i)
    if n > 0:
        scene[i:i + n] += gain * sound[:n]


def scenes(seconds: float, rng: np.random.Generator) -> dict[str, np.ndarray]:
    n = int(seconds * RATE)
    speech = [clip("y_reply.wav"), clip("one_breath_pause.wav"), clip("k_name.wav")]
    # the user's microphone: room noise at -56 dBFS, speech about a third of the time
    mic = (rng.standard_normal(n) * 10 ** (-56 / 20)).astype(np.float32)
    t = 1.0
    while t < seconds - 8:
        s = speech[int(rng.integers(len(speech)))]
        place(mic, s, t)
        t += len(s) / RATE + float(rng.uniform(4.0, 9.0))
    # the call app's own sound: digital silence between the friends' talk (noise suppression), ~40% talk
    call = np.zeros(n, np.float32)
    t = 3.0
    while t < seconds - 8:
        s = speech[int(rng.integers(len(speech)))]
        place(call, s, t, 0.8)
        t += len(s) / RATE + float(rng.uniform(2.0, 6.0))
    # a game: a loud bed all the time (noise at -30 dBFS, a chord at -35 dBFS), gunfire bursts, footsteps, and
    # teammates' voice chat about a fifth of the time
    tt = np.arange(n) / RATE
    game = (rng.standard_normal(n) * 10 ** (-30 / 20)).astype(np.float32)
    for hz in (110.0, 165.0, 220.0):
        game += (10 ** (-35 / 20) * np.sin(2 * np.pi * hz * tt)).astype(np.float32)
    for at in np.arange(0.5, seconds, 0.9):
        burst = (rng.standard_normal(int(0.05 * RATE)) * 10 ** (-8 / 20)).astype(np.float32)
        place(game, burst * np.exp(-np.arange(len(burst)) / 200.0).astype(np.float32), float(at))
    for at in np.arange(0.2, seconds, 0.45):
        step = (rng.standard_normal(int(0.03 * RATE)) * 10 ** (-24 / 20)).astype(np.float32)
        place(game, step, float(at))
    t = 6.0
    while t < seconds - 8:
        s = speech[int(rng.integers(len(speech)))]
        place(game, s, t, 0.7)
        t += len(s) / RATE + float(rng.uniform(10.0, 20.0))
    # the same bed with nobody talking: what the gate lets through is all non-speech
    bed = (rng.standard_normal(n) * 10 ** (-30 / 20)).astype(np.float32)
    for hz in (110.0, 165.0, 220.0):
        bed += (10 ** (-35 / 20) * np.sin(2 * np.pi * hz * tt)).astype(np.float32)
    for at in np.arange(0.5, seconds, 0.9):
        burst = (rng.standard_normal(int(0.05 * RATE)) * 10 ** (-8 / 20)).astype(np.float32)
        place(bed, burst * np.exp(-np.arange(len(burst)) / 200.0).astype(np.float32), float(at))
    return {"mic (user)": mic, "call app (others)": call, "game (teammates)": game, "game, nobody talking": bed}


def record(store, audio: np.ndarray, side: str, cfg) -> dict:
    from yuki.memory.calls import ArraySource, RecordingSession, Track

    session = RecordingSession(store, call_id=1, app="bench", process="bench", cfg=cfg,
                               source_factory=lambda: ArraySource(audio, start_at=time.time()),
                               log=lambda *a, **k: None,
                               track=Track(side=side, app="bench", process="bench", kind="call", call_id=1))
    t0 = time.perf_counter()
    session.start()
    session.wait_drained(120.0)
    stats = session.stop("bench")
    wall = time.perf_counter() - t0
    vad = session.chunker.vad
    return {"cpu_s": stats.cpu_s, "vad_s": vad.cpu_s if vad else 0.0, "vad_windows": vad.windows if vad else 0,
            "chunks": stats.chunks, "speech_s": stats.speech_s, "audio_s": stats.audio_s, "wall_s": wall,
            "encode_ms_p50": float(np.median(stats.encode_ms)) if stats.encode_ms else 0.0}


def loopback_python_side(seconds: float) -> dict:
    """The capture loop's work per packet without the device: convert, restamp, queue (10 ms packets)."""
    from yuki.memory.loopback import FORMATS, Restamper, to_mono

    fmt = FORMATS[0]
    repeat = 10                 # Windows' thread clock ticks every ~15.6 ms: run 10x as long, then divide
    packets = int(seconds * 100)
    rng = np.random.default_rng(1)
    raw = [(rng.standard_normal(160) * 0.1).astype(np.float32).tobytes() for _ in range(64)]
    stamper = Restamper()
    q: queue.Queue = queue.Queue()
    c0 = time.thread_time()
    at = time.time()
    for k in range(packets * repeat):
        samples = to_mono(raw[k % 64], fmt)
        for block in stamper.push(at + k * 0.01, samples):
            q.put(block)
        if k % 4 == 3:          # the reader drains about every 40 ms: one wait and one clock read per drain
            time.time()
        if q.qsize() > 1000:    # the recording thread reads them
            while not q.empty():
                q.get_nowait()
    cpu = (time.thread_time() - c0) / repeat
    # two native calls per packet (GetBuffer, ReleaseBuffer) and one wait per drain, timed as plain ctypes calls
    k32 = ctypes.WinDLL("kernel32")
    fn = ctypes.WINFUNCTYPE(ctypes.c_ulonglong)(("GetTickCount64", k32))
    n = packets * 2 + packets // 4
    t1 = time.perf_counter()
    for _ in range(n * repeat):
        fn()
    native = (time.perf_counter() - t1) / repeat
    return {"packets": packets, "python_cpu_s": cpu, "native_calls": n, "native_call_cpu_s": native}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python scripts/call_tracks_bench.py")
    parser.add_argument("--seconds", type=float, default=60.0)
    args = parser.parse_args(argv)
    import psutil

    from yuki.memory.calls import CallConfig, SileroVad
    from yuki.memory.store import Store

    proc = psutil.Process()
    rss0 = proc.memory_info().rss
    vads = [SileroVad() for _ in range(3)]
    rss_vad = (proc.memory_info().rss - rss0) / 3 / 2**20
    del vads
    cfg = CallConfig()
    rng = np.random.default_rng(7)
    sc = scenes(args.seconds, rng)
    print(f"{args.seconds:.0f} s per track; Silero VAD on (vad_threshold {cfg.vad_threshold}, gate {cfg.gate_dbfs} dBFS)")
    print(f"RAM per extra track (its own Silero session): ~{rss_vad:.1f} MB")
    rows = []
    with tempfile.TemporaryDirectory() as tmp:
        store = Store.open(Path(tmp) / "bench.db")
        try:
            for name, audio in sc.items():
                side = "user" if name.startswith("mic") else "others"
                r = record(store, audio, side, cfg)
                rows.append((name, r))
                pct = 100.0 * r["cpu_s"] / args.seconds
                print(f"{name:22s} cpu {r['cpu_s'] * 1000:7.0f} ms ({pct:.2f}% of one core)  vad {r['vad_s'] * 1000:6.0f} ms"
                      f" / {r['vad_windows']} windows  chunks {r['chunks']:2d}  speech kept {r['speech_s']:5.1f} s"
                      f" of {r['audio_s']:5.1f} s chunked  opus p50 {r['encode_ms_p50']:.1f} ms")
        finally:
            store.close()
    lb = loopback_python_side(args.seconds)
    lb_cpu = lb["python_cpu_s"] + lb["native_call_cpu_s"]
    print(f"loopback capture loop, per track: python side {lb['python_cpu_s'] * 1000:.0f} ms for {lb['packets']} packets,"
          f" {lb['native_calls']} native calls ~{lb['native_call_cpu_s'] * 1000:.0f} ms -> ~{lb_cpu * 1000:.0f} ms"
          f" ({100 * lb_cpu / args.seconds:.2f}% of one core; the audio engine's own conversion is not in this process)")
    by = dict(rows)
    total = by["mic (user)"]["cpu_s"] + by["call app (others)"]["cpu_s"] + by["game (teammates)"]["cpu_s"] + 2 * lb_cpu
    print(f"call + game at once (mic, Discord's track, the game's track, two capture loops): ~{total * 1000:.0f} ms per"
          f" {args.seconds:.0f} s = {100 * total / args.seconds:.2f}% of one core")
    return 0


if __name__ == "__main__":
    sys.exit(main())
