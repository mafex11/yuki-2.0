"""Behaviour scenarios for voice v3, offline: clips in, WAVs out, real Deepgram and Bedrock (docs/VOICE.md §23).

Nothing opens the microphone or plays on the speakers (ARCHITECTURE.md rule 7):
audio comes from clip files on a real-time script and Yuki's voice goes into
WAV files. The brain is either the real one (Sonnet on Bedrock, on
:class:`yuki.voice.offline.StubDesktop`, with memory on a *copy* of a database
given with ``--db``, never the real one) or a timed stand-in whose results land
at chosen moments (for the narrator-gate scenarios, where timing is the point).

Usage::

    uv run python scripts/voice_scenarios.py <scenario> --clips <dir> --out <dir> [--db <copy of memory.db>]
        scenarios: talk (a-d + rest), steer (e, f), gate (g, h, i), all

Each run writes ``<out>/<scenario>.json``: a timeline of what the user said,
what Yuki said (and why: user turn or result turn), every tool the mouth
called with its arguments, every payload the mouth was handed, the brain's
real steps and outcomes, the narrator's decisions and the per-turn stage
timings, so every spoken claim can be traced to a result or an event.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from PySide6.QtCore import QCoreApplication, QObject, QTimer, Signal  # noqa: E402

from yuki.config import Settings  # noqa: E402
from yuki.voice.log import VoiceLog  # noqa: E402


class Timeline:
    """Extra observer: everything said and passed, on one clock (test report only, never the voice log)."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.events: list[dict[str, Any]] = []
        self._seen: set[int] = set()
        self._reply: dict[str, Any] | None = None
        self._keys: set[Any] = set()

    def add(self, kind: str, **fields: Any) -> None:
        self.events.append({"t": round(time.monotonic() - self.t0, 2), "kind": kind, **fields})

    def observer(self) -> Any:
        from pipecat.observers.base_observer import BaseObserver

        tl = self

        class _Obs(BaseObserver):
            async def on_push_frame(self, data: Any) -> None:
                from pipecat.frames.frames import (
                    BotStartedSpeakingFrame, BotStoppedSpeakingFrame, FunctionCallInProgressFrame,
                    FunctionCallResultFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame,
                    TranscriptionFrame,
                )

                frame = data.frame
                if frame.id in tl._seen:
                    return
                tl._seen.add(frame.id)
                if isinstance(frame, TranscriptionFrame):
                    tl.add("user", text=frame.text)
                elif isinstance(frame, LLMFullResponseStartFrame):
                    tl._reply = {"text": ""}
                elif isinstance(frame, LLMTextFrame):
                    if tl._reply is None:
                        tl._reply = {"text": ""}
                    tl._reply["text"] += frame.text or ""
                elif isinstance(frame, LLMFullResponseEndFrame):
                    if tl._reply is not None and tl._reply["text"].strip():
                        tl.add("yuki", text=tl._reply["text"].strip())
                    tl._reply = None
                elif isinstance(frame, FunctionCallInProgressFrame):
                    key = ("call", frame.tool_call_id)
                    if key not in tl._keys:
                        tl._keys.add(key)
                        tl.add("mouth_tool", name=frame.function_name, args=frame.arguments)
                elif isinstance(frame, FunctionCallResultFrame):
                    key = ("result", frame.tool_call_id, json.dumps(frame.result, sort_keys=True, default=str))
                    if key in tl._keys:
                        return
                    tl._keys.add(key)
                    tl.add("to_mouth", name=frame.function_name, result=frame.result,
                           final=bool(frame.properties.is_final) if frame.properties else True,
                           run_llm=getattr(frame.properties, "run_llm", None) if frame.properties else None)
                elif isinstance(frame, BotStartedSpeakingFrame):
                    tl.add("audio_start")
                elif isinstance(frame, BotStoppedSpeakingFrame):
                    tl.add("audio_stop")

        return _Obs()


class TimedBrain(QObject):
    """A stand-in brain whose steps and results land when a plan says (the gate scenarios).

    ``plans`` is one list per submitted job, in submission order, of
    ``(when, event, payload)``: ``when`` is seconds after submission, or a
    callable that becomes true; ``event`` is ``tool`` (payload ``(name, input)``),
    ``say``, ``done`` or ``failed``.
    """

    started = Signal(str, int, str)
    tool_called = Signal(str, int, str, dict)
    tool_finished = Signal(str, int, str, bool, str)
    said = Signal(str, int, str)
    asked = Signal(str, int, str)
    finished = Signal(str, int, str)
    failed = Signal(str, int, str)
    lane_done = Signal(str, int, str, bool, list)
    handed_off = Signal(int, int, int)
    steer_unread = Signal(str, int, list)

    def __init__(self, plans: list[list[tuple[Any, str, Any]]], timeline: Timeline) -> None:
        super().__init__()
        self.plans = plans
        self.tl = timeline
        self._id = 0
        self.requests: list[str] = []
        self._timer = QTimer()
        self._timer.timeout.connect(self._tick)
        self._timer.start(50)
        self._live: list[dict[str, Any]] = []

    def submit(self, text: str, *, origin_hwnd: Any = None, source: str = "text") -> tuple[str, int]:
        self._id += 1
        job = self._id
        self.requests.append(text)
        plan = list(self.plans[job - 1]) if job - 1 < len(self.plans) else [(1.0, "done", "Done.")]
        self._live.append({"job": job, "at": time.monotonic(), "plan": plan, "text": text})
        self.started.emit("worker" if job == 1 else "front_desk", job, text)
        self.tl.add("brain", event="submitted", job=job)
        return ("worker" if job == 1 else "front_desk"), job

    def _tick(self) -> None:
        now = time.monotonic()
        for live in list(self._live):
            while live["plan"]:
                when, event, payload = live["plan"][0]
                due = when() if callable(when) else now - live["at"] >= when
                if not due:
                    break
                live["plan"].pop(0)
                lane = "worker" if live["job"] == 1 else "front_desk"
                self.tl.add("brain", event=event, job=live["job"], detail=payload)
                if event == "tool":
                    self.tool_called.emit(lane, live["job"], payload[0], payload[1])
                elif event == "say":
                    self.said.emit(lane, live["job"], payload)
                elif event in ("done", "failed"):
                    (self.finished if event == "done" else self.failed).emit(lane, live["job"], payload)
                    self.lane_done.emit(lane, live["job"], live["text"], False, [])
                    self._live.remove(live)
                    break

    def steer(self, job_id: int, text: str) -> bool:
        return any(live["job"] == job_id for live in self._live)

    def answer(self, lane: str, text: str) -> None:
        pass

    def cancel(self, job_id: int) -> str:
        for live in list(self._live):
            if live["job"] == job_id:
                live["plan"] = [(0, "failed", "Cancelled.")]
                return "running"
        return "unknown"


def _copy_db(src: Path, tmp: Path) -> Path:
    dst = tmp / "memory"
    dst.mkdir(parents=True, exist_ok=True)
    for name in ("memory.db", "memory.key", "privacy.toml"):
        if (src.parent / name).exists():
            shutil.copy(src.parent / name, dst / name)
    return dst / "memory.db"


def _discord_desk() -> Any:
    """The stub desktop with a Discord window in front that really minimizes (the 25 Sept replay)."""
    from yuki.voice.offline import StubResult, StubDesktop

    class DiscordDesk(StubDesktop):
        def __init__(self) -> None:
            super().__init__(action_delay_s=0.6)
            self.minimized = False

        def get_desktop_overview(self) -> dict[str, Any]:
            o = super().get_desktop_overview()
            for w in o["windows"]:
                w["is_foreground"] = False
            o["windows"].insert(0, {"hwnd": 0x2002, "process_name": "Discord.exe", "title": "#general | Friends - Discord",
                                    "is_foreground": not self.minimized, "is_minimized": self.minimized, "pid": 99,
                                    "bounds": (-32000, -32000, -31840, -31972) if self.minimized else (0, 0, 1920, 1040)})
            if self.minimized:
                o["windows"][1]["is_foreground"] = True
            o["foreground_hwnd"] = o["windows"][0 if not self.minimized else 1]["hwnd"]
            return o

        def format_overview(self, overview: dict[str, Any]) -> str:
            lines = [f"Open windows ({len(overview['windows'])}), foreground first:"]
            for w in overview["windows"]:
                mark = (" [foreground]" if w["is_foreground"] else "") + (" [minimized]" if w["is_minimized"] else "")
                lines.append(f"- [{w['hwnd']}] {w['process_name']} \"{w['title']}\"{mark}")
            return "\n".join(lines)

        def _act(self, name: str, summary: str, *args: Any, **kwargs: Any) -> StubResult:
            result = super()._act(name, summary, *args, **kwargs)
            self.minimized = True
            return result

        def run_powershell(self, command: str, *, timeout_s: float = 20.0) -> StubResult:
            result = self._act("run_powershell", "powershell exit 0; stdout: True", command)
            result.details = {"stdout": "True", "stderr": "", "exit_code": 0}
            return result

    return DiscordDesk()


def _serve_embedder(db: Path) -> Any:
    """A stand-in memory service serving its (loaded) embedder on ``db``'s pipe, in its own process."""
    import subprocess

    code = ("import sys,time; sys.path.insert(0, r'%s'); from yuki.memory.store import Store; "
            "from yuki.memory.embed import get_embedder; from yuki.memory.embed_ipc import EmbedServer; "
            "s=Store.open(r'%s'); get_embedder().embed(['warm']); EmbedServer(s.cipher, s.path).start(); "
            "print('serving', flush=True); time.sleep(900)") % (ROOT, db)
    proc = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    while "serving" not in (proc.stdout.readline() or "serving"):
        pass
    return proc


def _real_brain(settings: Settings, memory: Any, timeline: Timeline, desk: Any = None) -> tuple[Any, Any]:
    from yuki.agent.loop import Agent
    from yuki.agent.tools import Dispatcher
    from yuki.log.events import SessionLogger
    from yuki.ui.runtime import FRONT_DESK_INSTRUCTIONS, LOOK_ONLY_TOOLS, AgentRuntime, HandsBlockedBackend
    from yuki.ui.uilog import UiLog
    from yuki.voice.offline import StubDesktop

    ui_log = UiLog(sessions_dir=settings.sessions_dir, session_id=None)
    session = ui_log.logger.session_id
    desk = desk or StubDesktop(action_delay_s=0.6)
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
    for name in ("tool_called", "tool_finished", "said", "asked", "finished", "failed"):
        getattr(runtime, name).connect(
            lambda *a, n=name: timeline.add("brain", event=n, job=a[1],
                                            detail=[x for x in a[2:] if not isinstance(x, bool)]))
    return runtime, desk


def _since_result_spoken(timeline: Timeline, after: str) -> Any:
    """A condition: Yuki has spoken a result turn (a final payload was handed over) after line ``after`` started."""
    def check() -> bool:
        marks = [e["t"] for e in timeline.events if e["kind"] == "line" and e.get("label") == after]
        if not marks:
            return False
        finals = [e for e in timeline.events if e["kind"] == "to_mouth" and e.get("final")
                  and e["name"] == "tell_yuki" and e["t"] >= marks[-1]]
        spoken = [e for e in timeline.events if e["kind"] == "audio_stop" and finals and e["t"] > finals[0]["t"]]
        return bool(spoken)
    return check


def run(app: Any, scenario: str, args: argparse.Namespace) -> dict[str, Any]:
    from voice_offline import Sampler, _reply_done  # noqa: F401 - the shared harness

    from pipecat.transports.base_transport import TransportParams

    from yuki.agent.memory import MemoryAccess
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.mouth import fetch_memory
    from yuki.voice.pipeline import Conversation
    from yuki.voice.transport import PairTransport, Script, ScriptedInput, ScriptLine, WavOutput, load_clip
    from yuki.voice.transport import conversation_wav, write_wav

    import asyncio

    clips = {c["name"]: c for c in json.loads((args.clips / "manifest.json").read_text(encoding="utf-8"))}
    for extra in args.more_clips or []:
        clips.update({c["name"]: c for c in json.loads((extra / "manifest.json").read_text(encoding="utf-8"))})
    tmp = Path(tempfile.mkdtemp(prefix=f"yuki-voice-{scenario}-"))
    over: dict[str, Any] = {}
    if args.eager is not None:
        over["voice_eager_eot_threshold"] = args.eager
    if args.eot is not None:
        over["voice_eot_threshold"] = args.eot
    if scenario in ("pauses", "wake1"):
        over["voice_hold_max_s"] = 8.0
    if args.grounding is not None:
        over["voice_grounding"] = args.grounding == "on"
    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "requests.csv", voice_tts_provider=args.tts,
                        voice_tts_voice=args.voice or ("" if args.tts == "elevenlabs" else "aura-2-thalia-en"),
                        voice_idle_s=args.idle, **over)
    db_copy = _copy_db(args.db, tmp) if args.db else None
    service = _serve_embedder(db_copy) if db_copy is not None and args.serve_embedder else None
    if db_copy is not None and args.add_rule:
        from yuki.memory.api import MemoryClient

        with MemoryClient.open(db_copy) as client:  # the temp copy only
            client.remember_rule(args.add_rule)
    memory = MemoryAccess(path=db_copy) if db_copy is not None else None
    timeline = Timeline()
    vlog = VoiceLog(args.out / "voice-log", args.out / f"voice_turns_{scenario}.csv")

    def clip(name: str) -> Any:
        return load_clip(Path(clips[name]["file"]))

    box: dict[str, Any] = {}

    def done_after(label: str) -> Any:
        return _since_result_spoken(timeline, label)

    def reply_done() -> str:
        return "reply_done"

    runtime = desk = None
    if scenario == "live925":
        # The user's first live conversation (2026-09-25), replayed: a one-action task, a question
        # while it runs, an acknowledgement, a rule change, small talk, thanks.
        runtime, desk = _real_brain(settings, memory, timeline, desk=_discord_desk())
        lines = [
            ScriptLine(clip("l_hello"), "l_hello", after_s=1.0),
            ScriptLine(clip("l_minimize"), "l_minimize", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("l_time"), "l_time", after_s=0.6, wait_for=reply_done()),
            ScriptLine(clip("l_okay"), "l_okay", after_s=1.0, wait_for=done_after("l_minimize")),
            ScriptLine(clip("l_babe"), "l_babe", after_s=1.0, wait_for=reply_done()),
            ScriptLine(clip("l_work"), "l_work", after_s=1.0, wait_for=reply_done()),
            ScriptLine(clip("l_thanks"), "l_thanks", after_s=1.0, wait_for=reply_done()),
        ]
    elif scenario == "talk":
        runtime, desk = _real_brain(settings, memory, timeline)
        lines = [
            ScriptLine(clip("v3_hello"), "v3_hello", after_s=1.0),
            ScriptLine(clip("v3_run"), "v3_run", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("v3_today"), "v3_today", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("v3_time"), "v3_time", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("v3_status"), "v3_status", after_s=1.0, wait_for=reply_done()),
            ScriptLine(clip("v3_rest"), "v3_rest", after_s=1.2, wait_for=done_after("v3_today")),
            ScriptLine(clip("v3_thanks"), "v3_thanks", after_s=1.0, wait_for=reply_done()),
        ]
    elif scenario == "steer":
        runtime, desk = _real_brain(settings, memory, timeline)
        lines = [
            ScriptLine(clip("v3_invoices"), "v3_invoices", after_s=1.0),
            ScriptLine(clip("v3_steer"), "v3_steer", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("v3_music"), "v3_music", after_s=1.5, wait_for=done_after("v3_invoices")),
            ScriptLine(clip("v3_cancel"), "v3_cancel", after_s=0.8, wait_for=reply_done()),
            ScriptLine(clip("v3_thanks"), "v3_thanks", after_s=4.0, wait_for=reply_done()),
        ]
    elif scenario == "gate":
        started: dict[str, float] = {}

        def line_started(label: str, plus: float) -> Any:
            return lambda: label in started and time.monotonic() - started[label] >= plus

        plans = [
            # (g) two jobs finishing 0.7 s apart
            [(1.5, "tool", ("activity", {"since": "2026-09-25", "group_by": "site"})),
             (line_started("v3_two_b", 5.0), "done",
              "You spent 1 hour 12 minutes on YouTube today, mostly this afternoon.")],
            [(1.0, "tool", ("recall", {"query": "Rohan"})),
             (line_started("v3_two_b", 5.7), "done", "You last talked to Rohan on Tuesday, about the flat deposit.")],
            # (h) lands 2 s into the user's long utterance
            [(1.0, "tool", ("launch_app", {"query": "Spotify"})),
             (line_started("v3_long", 2.0), "done", "Lo-fi is playing on Spotify now.")],
            # (i) lands while the user keeps chatting; goes stale
            [(1.0, "tool", ("run_powershell", {"command": "ls"})),
             (line_started("v3_chat1", 1.0), "done", "Your September invoices add up to Rs 48,200.")],
        ]
        runtime = TimedBrain(plans, timeline)
        lines = [
            ScriptLine(clip("v3_two_a"), "v3_two_a", after_s=1.0),
            ScriptLine(clip("v3_two_b"), "v3_two_b", after_s=0.3, wait_for=reply_done()),
            ScriptLine(clip("v3_music"), "v3_music", after_s=1.5, wait_for=done_after("v3_two_b")),
            ScriptLine(clip("v3_long"), "v3_long", after_s=0.5, wait_for=reply_done()),
            ScriptLine(clip("v3_invoices"), "v3_invoices", after_s=1.5, wait_for=done_after("v3_long")),
            ScriptLine(clip("v3_chat1"), "v3_chat1", after_s=0.4, wait_for=reply_done()),
            ScriptLine(clip("v3_chat2"), "v3_chat2", after_s=0.4, wait_for=reply_done()),
            ScriptLine(clip("v3_chat3"), "v3_chat3", after_s=0.4, wait_for=reply_done()),
            ScriptLine(clip("v3_ask_back"), "v3_ask_back", after_s=0.6, wait_for=reply_done()),
            ScriptLine(clip("v3_thanks"), "v3_thanks", after_s=1.0, wait_for=reply_done()),
        ]
        box["started"] = started
    elif scenario == "wake1":
        # One clip heard right after a wake: what the mouth does with it.
        runtime = TimedBrain([[(900.0, "done", "Done.")]], timeline)
        lines = [ScriptLine(clip(args.clip), args.clip, after_s=0.3)]
    elif scenario == "pauses":
        # End-of-turn tuning: sentences with natural pauses in them, and plain ones.
        runtime = TimedBrain([[(900.0, "done", "Done.")]] * 20, timeline)  # no results: turns only
        names = [n for n, c in clips.items() if c["set"] == "pause"]
        lines = [ScriptLine(clip(names[0]), names[0], after_s=1.0)]
        lines += [ScriptLine(clip(n), n, after_s=2.0, wait_for=reply_done()) for n in names[1:]]
        box["pause_clips"] = {n: clips[n].get("pauses", 0) for n in names}
    else:
        raise SystemExit(f"unknown scenario {scenario}")

    bridge = VoiceBridge(runtime, vlog, foreground=lambda: None, settings=settings)
    params = TransportParams(audio_in_enabled=True, audio_in_sample_rate=16_000, audio_out_enabled=True,
                             audio_out_sample_rate=24_000, audio_out_auto_silence=True)
    states: list[tuple[float, str]] = []

    def main() -> None:
        async def go() -> str:
            script = Script(lines)
            output = WavOutput(params, rate=24_000)
            box["script"], box["output"] = script, output

            def speech_end() -> float | None:
                now = time.monotonic()
                ends = [ln.speech_end_at for ln in script.lines if ln.speech_end_at and ln.speech_end_at <= now]
                return max(ends) if ends else None

            def voice_activity() -> tuple[float, float]:
                now = time.monotonic()
                for ln in script.lines:
                    if ln.started_at is not None and ln.speech_end_at is not None and ln.started_at <= now:
                        if now <= ln.speech_end_at:
                            return ln.started_at, now
                ends = [(ln.started_at, ln.speech_end_at) for ln in script.lines
                        if ln.speech_end_at and ln.speech_end_at <= now]
                return max(ends, key=lambda x: x[1]) if ends else (0.0, 0.0)

            conv = Conversation(
                settings=settings, bridge=bridge, transport=PairTransport(ScriptedInput(script, params), output),
                memory=memory, mouth_memory=box["mouth_memory"], log=vlog, speech_end=speech_end,
                voice_activity=voice_activity,
                on_state=lambda s, d: states.append((time.monotonic(), s)), session=scenario,
                extra_observers=[timeline.observer()],
            )
            box["conv"] = conv
            for line in lines:
                if line.wait_for == "reply_done":
                    line.wait_for = _reply_done(conv, line)

            async def mark() -> None:  # line starts on the timeline (and for the timed brain)
                seen = set()
                while True:
                    for ln in script.lines:
                        if ln.started_at is not None and ln.label not in seen:
                            seen.add(ln.label)
                            timeline.add("line", label=ln.label)
                            if "started" in box:
                                box["started"][ln.label] = ln.started_at
                    await asyncio.sleep(0.02)

            marker = asyncio.create_task(mark())
            timer = asyncio.get_running_loop().call_later(args.max_s, conv.request_stop, "test_timeout")
            try:
                return await conv.run()
            finally:
                timer.cancel()
                marker.cancel()

        try:
            box["reason"] = asyncio.run(go())
        except Exception as exc:  # noqa: BLE001 - reported
            import traceback

            box["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc()[-1500:]}"
        finally:
            box["done"] = True

    box["mouth_memory"] = fetch_memory(memory) if memory is not None else None
    if box["mouth_memory"] is None:
        from yuki.voice.mouth import MouthMemory

        box["mouth_memory"] = MouthMemory(source="empty")
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
    if hasattr(runtime, "stop"):
        runtime.stop()
    if service is not None:
        service.kill()
    out = args.out / scenario
    out.mkdir(parents=True, exist_ok=True)
    if "output" in box:
        conversation_wav(out / f"{scenario}-conversation.wav", box["script"], box["output"], rate=24_000)
        for i, (_at, samples) in enumerate(box["output"].replies(), 1):
            write_wav(out / f"{scenario}-reply{i}.wav", samples, 24_000)
    conv = box.get("conv")
    report: dict[str, Any] = {
        "scenario": scenario, "reason": box.get("reason"), "error": box.get("error"),
        "timeline": timeline.events, "resources": usage,
        "turn_totals": conv.observer.totals if conv else None,
        "narrator": conv.gate.stats if conv and conv.gate else None,
        "progress_cost_usd": round(conv.progress_cost_usd, 5) if conv else None,
        "grounding_ms": sorted(round(x, 1) for x in (conv.grounder.timings if conv and conv.grounder else [])),
        "brain_requests_sent": getattr(runtime, "requests", None),
        "stub_desktop_calls": [c[0] for c in desk.calls] if desk is not None else None,
    }
    if scenario == "pauses" and "script" in box:
        # Per clip: did the turn end inside the sentence (a transcript before its last word), and
        # did Yuki start talking before the user had finished?
        per = []
        base = timeline.t0
        for ln in box["script"].lines:
            if ln.started_at is None:
                continue
            start, end = ln.started_at - base, ln.speech_end_at - base
            texts = [e for e in timeline.events if e["kind"] == "user" and start < e["t"] <= end + 0.15]
            over = [e for e in timeline.events if e["kind"] == "audio_start" and start < e["t"] < end]
            after = [e for e in timeline.events if e["kind"] == "audio_start" and e["t"] >= end]
            after = after if not over else []
            per.append({"clip": ln.label, "pauses": box["pause_clips"].get(ln.label, 0),
                        "cut_off": bool(texts), "spoke_over": bool(over),
                        "first_audio_after_end_ms": round((after[0]["t"] - end) * 1000) if after else None})
        report["pauses"] = per
    try:
        import csv

        rows = list(csv.DictReader(settings.requests_csv_path.open(encoding="utf-8")))
        report["brain_cost_usd"] = round(sum(float(r.get("cost_usd") or 0) for r in rows), 4)
    except OSError:
        pass
    (args.out / f"{scenario}.json").write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str),
                                               encoding="utf-8")
    return report


def summary(report: dict[str, Any]) -> str:
    lines = [f"== {report['scenario']} ({report['reason']}) brain ${report.get('brain_cost_usd')} "
             f"narrator {report.get('narrator')}"]
    for e in report["timeline"]:
        k = e["kind"]
        if k == "user":
            lines.append(f"{e['t']:7.1f}  USER  {e['text']}")
        elif k == "yuki":
            lines.append(f"{e['t']:7.1f}  YUKI  {e['text']}")
        elif k == "mouth_tool":
            lines.append(f"{e['t']:7.1f}   tool {e['name']} {json.dumps(e['args'], ensure_ascii=False)}")
        elif k == "to_mouth":
            lines.append(f"{e['t']:7.1f}   ->mouth {e['name']} final={e['final']} run_llm={e['run_llm']} "
                         f"{json.dumps(e['result'], ensure_ascii=False)[:300]}")
        elif k == "brain":
            lines.append(f"{e['t']:7.1f}   brain {e['event']} job={e.get('job')} "
                         f"{json.dumps(e.get('detail'), ensure_ascii=False)[:240]}")
        elif k == "line":
            lines.append(f"{e['t']:7.1f}  [clip {e['label']}]")
    if report.get("error"):
        lines.append("ERROR " + report["error"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=("talk", "steer", "gate", "pauses", "wake2", "live925", "all"))
    ap.add_argument("--grounding", choices=("on", "off"), default=None)
    ap.add_argument("--serve-embedder", dest="serve_embedder", action="store_true",
                    help="serve the embedder from a stand-in memory service process on the DB copy")
    ap.add_argument("--add-rule", dest="add_rule", default="", help="a standing rule added to the temp DB copy")
    ap.add_argument("--more-clips", dest="more_clips", type=Path, nargs="*", help="more clip folders (manifest.json)")
    ap.add_argument("--wake-clips", dest="wake_clips", default="", help="comma-separated clip names for wake2")
    ap.add_argument("--clips", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--db", type=Path, help="a COPY of memory.db (it is copied again into a temp folder)")
    ap.add_argument("--tts", default="deepgram", choices=("deepgram", "elevenlabs"))
    ap.add_argument("--voice", default="")
    ap.add_argument("--idle", type=float, default=8.0)
    ap.add_argument("--max-s", dest="max_s", type=float, default=240.0)
    ap.add_argument("--eager", type=float, default=None)
    ap.add_argument("--eot", type=float, default=None)
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    app = QCoreApplication.instance() or QCoreApplication([])
    if args.scenario == "wake2":
        results = []
        for name in [n for n in args.wake_clips.split(",") if n]:
            args.clip = name
            report = run(app, "wake1", args)
            spoke = [e["text"] for e in report["timeline"] if e["kind"] == "yuki"]
            tools = [e["name"] for e in report["timeline"] if e["kind"] == "mouth_tool"]
            heard = [e["text"] for e in report["timeline"] if e["kind"] == "user"]
            results.append({"clip": name, "reason": report["reason"], "heard": heard, "said": spoke, "tools": tools})
            print(json.dumps(results[-1], ensure_ascii=False))
        (args.out / "wake2.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        return 0
    for scenario in (("talk", "steer", "gate") if args.scenario == "all" else (args.scenario,)):
        report = run(app, scenario, args)
        text = summary(report) + ("\n" + json.dumps(report.get("pauses"), indent=1) if report.get("pauses") else "")
        (args.out / f"{scenario}.txt").write_text(text, encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
