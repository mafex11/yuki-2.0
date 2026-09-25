"""Run voice conversations offline: clips in, WAVs out, real Deepgram, real Bedrock mouth, real TTS.

Nothing opens the microphone or plays on the speakers: audio comes from clip
files on a script (:class:`yuki.voice.transport.ScriptedInput`, real-time paced)
and Yuki's voice goes into WAV files (:class:`WavOutput`). Logs, memory and the
brain's transcripts go to a temporary folder, never the real ones.

Modes::

    uv run python scripts/voice_offline.py stt --clips <dir> --out <dir> [--tts deepgram|elevenlabs]
        Each STT clip as its own short conversation. The brain is a stub that
        answers at once (so only the mouth and the voice cost money). Reports the
        transcript accuracy per language, which script Hindi came back in, and
        per-stage latency, and writes each conversation's WAVs.

    uv run python scripts/voice_offline.py scenario --clips <dir> --out <dir>
        The end-to-end run with the real brain (Sonnet on Bedrock) on a stub
        desktop (yuki.voice.offline.StubDesktop) and a temporary memory
        database holding the user's standing rules: a task, small talk while
        it runs, a check-in, the brain's spoken progress, the result when quiet.

    uv run python scripts/voice_offline.py wake-cpu [--seconds 60]
        The always-on wake-word cost: the spotter fed noise and speech at real
        time from memory (no device), process CPU and decode time measured.

Every mode prints a JSON report and saves it as ``<out>/report-<mode>.json``.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal  # noqa: E402

from yuki.config import Settings  # noqa: E402
from yuki.voice.log import VoiceLog  # noqa: E402

RATE = 16_000


# ---------------------------------------------------------------------------
# measuring
# ---------------------------------------------------------------------------


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFC", text).casefold()
    return " ".join("".join(ch if not unicodedata.category(ch).startswith("P") else " " for ch in text).split())


def _edit(a: list, b: list) -> int:
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def accuracy(ref: str, hyp: str) -> dict[str, Any]:
    r, h = _norm(ref), _norm(hyp)
    rw, hw = r.split(), h.split()
    rc, hc = list(r.replace(" ", "")), list(h.replace(" ", ""))
    devanagari = sum(1 for ch in hyp if "ऀ" <= ch <= "ॿ")
    latin = sum(1 for ch in hyp if ch.isascii() and ch.isalpha())
    return {
        "wer": round(_edit(rw, hw) / max(1, len(rw)), 3),
        "cer": round(_edit(rc, hc) / max(1, len(rc)), 3),
        "script": "devanagari" if devanagari > latin else ("latin" if latin else "none"),
        "devanagari_chars": devanagari, "latin_chars": latin,
    }


class Sampler:
    """Process CPU (percent of one core) and RSS every 0.5 s, labelled with the voice state."""

    def __init__(self, label: Any) -> None:
        import psutil

        self.proc = psutil.Process()
        self.label = label
        self.samples: list[tuple[str, float, float]] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        self.proc.cpu_percent(None)
        while not self._stop.wait(0.5):
            self.samples.append((self.label(), self.proc.cpu_percent(None), self.proc.memory_info().rss / 1e6))

    def start(self) -> "Sampler":
        self._thread.start()
        return self

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        self._thread.join(2)
        out: dict[str, Any] = {}
        for state in sorted({s for s, _, _ in self.samples}):
            cpu = [c for s, c, _ in self.samples if s == state]
            rss = [m for s, _, m in self.samples if s == state]
            out[state] = {"n": len(cpu), "cpu_pct_mean": round(float(np.mean(cpu)), 1),
                          "cpu_pct_p95": round(float(np.percentile(cpu, 95)), 1), "rss_mb_max": round(max(rss), 1)}
        return out


class Tap:
    """Extra observer: what was heard and said, kept in memory for the test report only."""

    def __init__(self) -> None:
        self.heard: list[dict[str, Any]] = []
        self.said: list[str] = []
        self.tools: list[str] = []
        self._seen: set[int] = set()

    def observer(self) -> Any:
        from pipecat.observers.base_observer import BaseObserver

        tap = self

        class _Obs(BaseObserver):
            async def on_push_frame(self, data: Any) -> None:
                from pipecat.frames.frames import FunctionCallInProgressFrame, LLMTextFrame, TranscriptionFrame

                frame = data.frame
                if frame.id in tap._seen:
                    return
                tap._seen.add(frame.id)
                if isinstance(frame, TranscriptionFrame):
                    lang = getattr(frame, "language", None)
                    tap.heard.append({"text": frame.text, "language": str(getattr(lang, "value", lang)),
                                      "at": time.monotonic()})
                    tap.said.append("")
                elif isinstance(frame, LLMTextFrame):
                    if not tap.said:
                        tap.said.append("")
                    tap.said[-1] += frame.text or ""
                elif isinstance(frame, FunctionCallInProgressFrame):
                    tap.tools.append(frame.function_name)

        return _Obs()


# ---------------------------------------------------------------------------
# the harness: Qt on this thread, the conversation on a voice thread
# ---------------------------------------------------------------------------


class StubRuntime(QObject):
    """A brain that answers every request after a second, for the STT runs."""

    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self) -> None:
        super().__init__()
        self._id = 0

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text") -> tuple[str, int]:
        self._id += 1
        job = self._id
        self.started.emit("worker", job, text)
        QTimer.singleShot(1000, lambda: (self.finished.emit("worker", job, "I could not do that: this is a test machine, so nothing was checked or changed."),
                                         self.lane_done.emit("worker", job, text, False, [])))
        return "worker", job

    def steer(self, job_id: int, text: str) -> bool:
        return False

    def answer(self, lane: str, text: str) -> None:
        pass

    def cancel(self, job_id: int) -> str:
        return "unknown"


def run_conversation(app: QCoreApplication, *, settings: Settings, bridge: Any, lines: list[Any], memory: Any,
                     out_dir: Path, name: str, vlog: VoiceLog, max_s: float = 240.0) -> dict[str, Any]:
    """One conversation from ``lines`` (ScriptLine list); returns what was heard, said and measured."""
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice.mouth import fetch_memory
    from yuki.voice.pipeline import Conversation
    from yuki.voice.transport import PairTransport, Script, ScriptedInput, WavOutput, conversation_wav, write_wav

    import asyncio

    tap = Tap()
    import os

    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000, audio_out_enabled=True,
                             audio_out_sample_rate=24_000,
                             audio_out_auto_silence=os.environ.get("YUKI_VOICE_AUTO_SILENCE", "1") == "1")
    box: dict[str, Any] = {}
    states: list[tuple[float, str]] = []

    def main() -> None:
        async def go() -> str:
            script = Script(lines)
            output = WavOutput(params, rate=24_000)
            box["script"], box["output"] = script, output

            def speech_end() -> float | None:
                now = time.monotonic()
                ends = [line.speech_end_at for line in script.lines if line.speech_end_at and line.speech_end_at <= now]
                return max(ends) if ends else None

            conv = Conversation(
                settings=settings, bridge=bridge, transport=PairTransport(ScriptedInput(script, params), output),
                memory=memory, mouth_memory=box["mouth_memory"], log=vlog, speech_end=speech_end,
                on_state=lambda s, d: states.append((time.monotonic(), s)), session=name,
                extra_observers=[tap.observer()],
            )
            box["conv"] = conv
            for line in lines:
                if line.wait_for == "reply_done":
                    line.wait_for = _reply_done(conv, line)
            timer = asyncio.get_running_loop().call_later(max_s, conv.request_stop, "test_timeout")
            try:
                return await conv.run()
            finally:
                timer.cancel()

        try:
            box["reason"] = asyncio.run(go())
        except Exception as exc:  # noqa: BLE001 - reported
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            box["done"] = True

    box["mouth_memory"] = fetch_memory(memory) if memory is not None else _empty_memory()
    started = time.monotonic()
    sampler = Sampler(lambda: states[-1][1] if states else "starting").start()
    thread = threading.Thread(target=main, name="yuki-voice-test", daemon=True)
    thread.start()
    poll = QTimer()
    poll.timeout.connect(lambda: app.quit() if box.get("done") else None)
    poll.start(200)
    app.exec()
    poll.stop()
    thread.join(10)
    usage = sampler.stop()
    out_dir.mkdir(parents=True, exist_ok=True)
    if "output" in box:
        conversation_wav(out_dir / f"{name}-conversation.wav", box["script"], box["output"], rate=24_000)
        for i, (_at, samples) in enumerate(box["output"].replies(), 1):
            write_wav(out_dir / f"{name}-reply{i}.wav", samples, 24_000)
    conv = box.get("conv")
    return {
        "name": name, "reason": box.get("reason"), "error": box.get("error"),
        "seconds": round(time.monotonic() - started, 1), "heard": tap.heard, "said": tap.said, "tools": tap.tools,
        "turn_totals": conv.observer.totals if conv else None, "resources": usage,
        "states": [s for _t, s in states],
    }


def _reply_done(conv: Any, line: Any) -> Any:
    """A condition: Yuki has spoken since the previous line and has stopped (the user's turn again)."""
    state = {"spoke": False}

    def check() -> bool:
        obs = conv.observer
        if obs.bot_speaking:
            state["spoke"] = True
        return state["spoke"] and not obs.bot_speaking and not obs.generating and not obs.user_speaking

    return check


def _empty_memory() -> Any:
    from yuki.voice.mouth import MouthMemory

    return MouthMemory(source="empty")


def _clips(clips_dir: Path) -> dict[str, dict[str, Any]]:
    return {c["name"]: c for c in json.loads((clips_dir / "manifest.json").read_text(encoding="utf-8"))}


def _settings(tmp: Path, args: argparse.Namespace, **over: Any) -> Settings:
    voice = args.voice or ("" if args.tts == "elevenlabs" else "aura-2-thalia-en")
    return Settings(log_dir=tmp / "sessions", requests_csv=tmp / "requests.csv", voice_tts_provider=args.tts,
                    voice_tts_voice=voice, voice_idle_s=args.idle, voice_mouth_model=args.mouth,
                    voice_eager_eot_threshold=args.eager, **over)


def _temp_memory(tmp: Path, rules: list[str]) -> Any:
    """A fresh encrypted memory database in ``tmp`` holding the given standing rules."""
    from yuki.agent.memory import MemoryAccess
    from yuki.memory.api import MemoryClient

    path = tmp / "memory" / "memory.db"
    client = MemoryClient.open(path)
    for rule in rules:
        client.remember_rule(rule)
    client.close()
    return MemoryAccess(path=path)


RULES = [
    "Call me captain in every reply, even short ones.",
    "Keep replies short and human, and never use em dashes.",
]


def mode_stt(args: argparse.Namespace) -> dict[str, Any]:
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.transport import ScriptLine, load_clip

    app = QCoreApplication.instance() or QCoreApplication([])
    clips = _clips(args.clips)
    tmp = Path(tempfile.mkdtemp(prefix="yuki-voice-stt-"))
    settings = _settings(tmp, args)
    vlog = VoiceLog(args.out / "voice-log", args.out / "voice_turns_stt.csv")
    memory = _temp_memory(tmp, RULES)
    bridge = VoiceBridge(StubRuntime(), vlog, foreground=lambda: None)
    results = []
    for name in [c for c in clips if clips[c]["set"] == "stt"]:
        clip = clips[name]
        lines = [ScriptLine(load_clip(Path(clip["file"])), name, after_s=1.0)]
        run = run_conversation(app, settings=settings, bridge=bridge, lines=lines, memory=memory,
                               out_dir=args.out / "stt", name=name, vlog=vlog, max_s=60)
        heard = " ".join(h["text"] for h in run["heard"])
        run["reference"] = clip["text"]
        run["accuracy"] = accuracy(clip["text"], heard)
        run["language_expected"] = clip["language"]
        results.append(run)
        print(json.dumps({k: run[k] for k in ("name", "reason", "error", "accuracy", "heard", "said")},
                         ensure_ascii=False))
    return {"mode": "stt", "runs": results, "turns_csv": str(args.out / "voice_turns_stt.csv")}


def mode_scenario(args: argparse.Namespace) -> dict[str, Any]:
    from yuki.agent.loop import Agent
    from yuki.agent.tools import Dispatcher
    from yuki.log.events import SessionLogger
    from yuki.ui.runtime import FRONT_DESK_INSTRUCTIONS, LOOK_ONLY_TOOLS, AgentRuntime, HandsBlockedBackend
    from yuki.ui.uilog import UiLog
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.offline import StubDesktop
    from yuki.voice.transport import ScriptLine, load_clip

    app = QCoreApplication.instance() or QCoreApplication([])
    clips = _clips(args.clips)
    tmp = Path(tempfile.mkdtemp(prefix="yuki-voice-scenario-"))
    settings = _settings(tmp, args)
    memory = _temp_memory(tmp, RULES)
    ui_log = UiLog(sessions_dir=settings.sessions_dir, session_id=None)
    session = ui_log.logger.session_id
    desk = StubDesktop(action_delay_s=args.action_delay)
    worker = Agent(settings, SessionLogger(settings.sessions_dir, console=None, session_id=f"{session}-worker"),
                   backend=desk, lane="worker", memory=memory, prewarm=True)
    blocked = HandsBlockedBackend(StubDesktop(action_delay_s=0))
    front = Agent(settings, SessionLogger(settings.sessions_dir, console=None, session_id=f"{session}-frontdesk"),
                  dispatcher=Dispatcher(blocked, screenshot_policy=settings.screenshot_policy,
                                        tool_timeout_s=settings.tool_timeout_s),
                  tool_names=LOOK_ONLY_TOOLS, extra_instructions=FRONT_DESK_INSTRUCTIONS, prewarm=False,
                  lane="front_desk", memory=memory)
    runtime = AgentRuntime(settings, ui_log, worker_agent=worker, front_desk_agent=front,
                           front_desk_blocked=blocked, memory=memory)
    runtime.start()
    worker.wait_for_prewarm(60)
    vlog = VoiceLog(args.out / "voice-log", args.out / "voice_turns_scenario.csv")
    bridge = VoiceBridge(runtime, vlog, foreground=lambda: None)
    brain_events: list[dict[str, Any]] = []
    t0 = time.monotonic()
    for name in ("started", "said", "asked", "finished", "failed"):
        getattr(runtime, name).connect(
            lambda *a, n=name: brain_events.append({"t": round(time.monotonic() - t0, 2), "event": n,
                                                     "detail": a[-1] if isinstance(a[-1], str) else None}))
    first = args.task_clip or "s1_task"
    load = {n: load_clip(Path(clips[n]["file"])) for n in (first, "s2_chat", "s3_status", "s4_thanks")}
    lines = [
        ScriptLine(load[first], first, after_s=1.0),
        ScriptLine(load["s2_chat"], "s2_chat", after_s=1.0, wait_for="reply_done"),
        ScriptLine(load["s3_status"], "s3_status", after_s=1.5, wait_for="reply_done"),
    ]
    run = run_conversation(app, settings=settings, bridge=bridge, lines=lines, memory=memory,
                           out_dir=args.out / "scenario", name="scenario", vlog=vlog, max_s=args.max_s)
    runtime.stop()
    run["brain_events"] = brain_events
    run["stub_desktop_calls"] = [c[0] for c in desk.calls]
    run["brain_requests_csv"] = str(settings.requests_csv_path)
    try:
        import csv

        rows = list(csv.DictReader(settings.requests_csv_path.open(encoding="utf-8")))
        run["brain_cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in rows), 4)
        run["brain_requests"] = [{k: r.get(k) for k in ("lane", "wall_s", "calls", "cost_usd", "outcome")} for r in rows]
    except OSError:
        pass
    print(json.dumps(run, ensure_ascii=False, indent=1))
    return {"mode": "scenario", "run": run, "turns_csv": str(args.out / "voice_turns_scenario.csv")}


def mode_idle(args: argparse.Namespace) -> dict[str, Any]:
    """A conversation that is open and silent: what voice-on costs while nobody talks."""
    import psutil

    from yuki.voice.bridge import VoiceBridge

    proc = psutil.Process()
    before = proc.memory_info().rss / 1e6
    app = QCoreApplication.instance() or QCoreApplication([])
    tmp = Path(tempfile.mkdtemp(prefix="yuki-voice-idle-"))
    settings = _settings(tmp, args)
    vlog = VoiceLog(args.out / "voice-log", args.out / "voice_turns_idle.csv")
    bridge = VoiceBridge(StubRuntime(), vlog, foreground=lambda: None)
    run = run_conversation(app, settings=settings, bridge=bridge, lines=[], memory=None, out_dir=args.out / "idle",
                           name="idle", vlog=vlog, max_s=args.idle + 30)
    run["rss_before_mb"] = round(before, 1)
    run["rss_after_mb"] = round(proc.memory_info().rss / 1e6, 1)
    run["threads_after"] = proc.num_threads()
    print(json.dumps(run, indent=1))
    return run


def mode_wake_cpu(args: argparse.Namespace) -> dict[str, Any]:
    """Feed the spotter from memory at real time (the device callback's job) and measure it."""
    import psutil

    from yuki.voice.audio import BLOCK_SAMPLES, SharedMic
    from yuki.voice.transport import load_clip
    from yuki.voice.wake import WakeListener, WakeSpotter

    mic = SharedMic()  # never opened: blocks are pushed with feed(), as the device callback would
    hits: list[float] = []
    listener = WakeListener(mic, lambda: WakeSpotter(threshold=args.threshold, boost=args.boost),
                            on_wake=lambda at, kw: hits.append(at))
    speech = []
    if args.clips:
        clips = _clips(args.clips)
        speech = [load_clip(Path(c["file"])) for c in clips.values() if c["set"] == "background"]
    rng = np.random.default_rng(3)
    audio = np.concatenate(speech) if speech else np.zeros(0, np.float32)
    total = int(args.seconds * RATE)
    stream = np.concatenate([audio, (rng.standard_normal(max(0, total - len(audio))) * 0.003).astype(np.float32)])
    proc = psutil.Process()
    listener.start()
    listener.ready.wait(60)  # measure after the model has loaded
    cpu0, t0 = proc.cpu_times(), time.monotonic()
    clock = time.monotonic()
    for i in range(0, len(stream) - BLOCK_SAMPLES, BLOCK_SAMPLES):
        mic.feed(stream[i:i + BLOCK_SAMPLES], clock)
        clock += BLOCK_SAMPLES / RATE
        delay = clock - time.monotonic()
        if delay > 0:
            time.sleep(delay)  # pacing a stand-in device clock
    cpu1, t1 = proc.cpu_times(), time.monotonic()
    listener.stop()
    used = (cpu1.user - cpu0.user) + (cpu1.system - cpu0.system)
    report = {"mode": "wake-cpu", "seconds": round(t1 - t0, 1), "process_cpu_pct_one_core": round(100 * used / (t1 - t0), 2),
              "decode_cpu_pct_one_core": round(100 * listener.decode_s / max(1e-6, listener.heard_s), 2),
              "decoded_s": round(listener.audio_s, 1), "heard_s": round(listener.heard_s, 1),
              "rss_mb": round(proc.memory_info().rss / 1e6, 1), "false_wakes": len(hits),
              "speech_s": round(len(audio) / RATE, 1)}
    print(json.dumps(report))
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=("stt", "scenario", "wake-cpu", "idle"))
    ap.add_argument("--clips", type=Path)
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "yuki-voice-offline")
    ap.add_argument("--tts", default="deepgram", choices=("deepgram", "elevenlabs"))
    ap.add_argument("--voice", default="")
    ap.add_argument("--mouth", default=Settings().voice_mouth_model)
    ap.add_argument("--idle", type=float, default=8.0)
    ap.add_argument("--max-s", dest="max_s", type=float, default=300.0)
    ap.add_argument("--seconds", type=float, default=60.0)
    ap.add_argument("--threshold", type=float, default=Settings().voice_wake_threshold)
    ap.add_argument("--boost", type=float, default=Settings().voice_wake_boost)
    ap.add_argument("--task-clip", dest="task_clip", default="")
    ap.add_argument("--eager", type=float, default=None, help="Flux eager end-of-turn threshold (off when unset)")
    ap.add_argument("--action-delay", dest="action_delay", type=float, default=0.6)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    report = {"stt": mode_stt, "scenario": mode_scenario, "wake-cpu": mode_wake_cpu, "idle": mode_idle}[args.mode](args)
    (args.out / f"report-{args.mode}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())


_ = re  # (kept for ad-hoc filtering when reading reports)
