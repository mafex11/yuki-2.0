"""Voice v3.4 end to end, offline: the real VoiceSession, from the wake word to the goodbye, in a pretend room.

Nothing opens a device (ARCHITECTURE.md rule 7). The microphone is clip audio on a real-time
script plus what a pretend room carries back from a pretend speaker
(:mod:`yuki.voice.offline`: ``Room``, ``RoomSpeaker``, ``clip_mic``; the speaker's and the mic's
real latencies differ from what they report). Everything else is the app's own code: the wake
listener, ``VoiceSession`` (echo guard, call mode, timings, notices), the Pipecat pipeline, and the
real services (Deepgram Flux, Haiku on Bedrock, Deepgram Aura for Yuki's voice). The brain is a
timed stand-in; the mouth's hands act on the stub desktop of ``voice_scenarios.py``.

Usage::

    uv run python scripts/voice_session_offline.py <scenario> --clips <dir with v34 clips> --out <dir>
        echo     "Yuki, say something nice about my friend Rohan" on speakers; Yuki's answer comes back
                 into the mic. --echo aec | half | none (none: no guard, the control)
        call     in a Discord call: "Yuki, pause the music", then talk to friends. --headphones for full duplex
        call2    the same with questions the friends were asked, and Yuki addressed again mid-way
        latency  "Yuki." then "Pause the music.", twice (cold, then warm): the wake word to listening
        endings  vfix: "shut up", "go away", "bye bye" and a mistaken wake, each after the name: every one ends the
                 conversation at once and leaves the wake word on (clips: voice_clips.py --set vfix)

Each run writes ``<out>/<scenario>-<variant>.json`` (the timeline: what was heard, said, called and
done, the states, the session's timings) and prints a summary.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

from PySide6.QtCore import QCoreApplication, QTimer  # noqa: E402

from yuki.config import Settings  # noqa: E402


def run(scenario: str, args: argparse.Namespace) -> dict[str, Any]:
    import yuki.voice.audio as audio
    from voice_scenarios import Timeline, TimedBrain, _hands_desk  # the shared harness pieces
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.offline import Room, RoomSpeaker, clip_mic
    from yuki.voice.session import VoiceSession
    from yuki.voice.transport import load_clip, speech_end_offset

    app = QCoreApplication.instance() or QCoreApplication([])
    variant = args.echo if scenario == "echo" else ("headphones" if args.headphones else "speakers")
    tmp = Path(tempfile.mkdtemp(prefix=f"yuki-v34-{scenario}-"))
    echo_pref = {"aec": "auto", "half": "half", "none": "full"}.get(args.echo, "auto")
    settings = Settings(log_dir=tmp / "sessions", requests_csv=tmp / "requests.csv", voice_tts_provider="deepgram",
                        voice_tts_voice="aura-2-thalia-en", voice_grounding=False, voice_idle_s=args.idle,
                        voice_idle_max_s=max(args.idle, 12.0), voice_call_idle_s=args.call_idle,
                        voice_echo=echo_pref, **({"voice_standby": False} if getattr(args, "no_standby", False)
                                                  and "voice_standby" in Settings.__dataclass_fields__ else {}))
    manifest = {c["name"]: c for c in json.loads((args.clips / "manifest.json").read_text(encoding="utf-8"))}

    def clip(name: str) -> Any:
        return load_clip(Path(manifest[name]["file"]))

    timeline = Timeline()
    desk = _hands_desk(timeline)
    runtime = TimedBrain([[(3.0, "done", "Done.")]] * 10, timeline)
    memory = service = None
    if getattr(args, "fake_memory", False):
        args.db = fake_memory(tmp / "fake")   # leftVoice: an invented memory (to-dos, a morning exchange)
    if getattr(args, "db", None):
        # v3.18: memory on a COPY (practice memory's lines for the talker, the hands' run traces, the grounding
        # lookup), its embedder served from a stand-in service process over its pipe as live; never the real memory
        import os
        import shutil

        from voice_scenarios import _serve_embedder

        from yuki.agent.memory import MemoryAccess

        mem_dir = tmp / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        for name in ("memory.db", "memory.key", "privacy.toml"):
            if (args.db / name).exists():
                shutil.copy2(args.db / name, mem_dir / name)
        os.environ["YUKI_MEMORY_DIR"] = str(mem_dir)
        service = _serve_embedder(mem_dir / "memory.db")
        memory = MemoryAccess(path=mem_dir / "memory.db")
    session = VoiceSession(settings, runtime, memory, prefs_path=tmp / "voice.json")
    session.bridge = VoiceBridge(runtime, session.log, settings=settings, hands_backend=desk, foreground=lambda: None)
    session.play_tone = lambda *a: timeline.add("notice_tone")
    session.extra_observers = (timeline.observer(),)
    room = Room(echo_db=args.echo_db)
    # On headphones nothing Yuki plays reaches the mic: the speaker plays into a room of its own.
    speaker_room = Room(echo_db=args.echo_db) if args.headphones else room
    box: dict[str, Any] = {}

    def settled(quiet_s: float = 1.2) -> Any:
        """True once a conversation has had nothing said, owed or running for ``quiet_s``."""
        state: dict[str, Any] = {"since": None}

        def check() -> bool:
            conv = session._conversation
            if conv is None:
                state["since"] = None
                return False
            obs = conv.observer
            busy = (obs.bot_speaking or obs.generating or obs.user_speaking or conv.bridge.open_voice_jobs()
                    or conv.bridge.busy_tools or (conv.gate is not None and conv.gate.holding))
            now = time.monotonic()
            if busy or not obs.user_turns:
                state["since"] = None
                return False
            state["since"] = state["since"] or now
            return now - state["since"] >= quiet_s

        return check

    def no_conversation(for_s: float = 1.0) -> Any:
        state: dict[str, Any] = {"since": None}

        def check() -> bool:
            if session.in_conversation or not box.get("started"):
                state["since"] = None
                return False
            state["since"] = state["since"] or time.monotonic()
            return time.monotonic() - state["since"] >= for_s

        return check

    def prewarmed() -> Any:
        """True once the session's background prewarm has finished (v3.5: the first wake after it), and, with the
        v3.11 standby, once the conversation built ahead is connected and waiting for the name."""
        def check() -> bool:
            job = getattr(session, "_prewarm_job", None)
            if job is None or not job.done():
                return False
            if not getattr(settings, "voice_standby", False) or not hasattr(session, "_standby_conv"):
                return True
            conv = session._standby_conv
            return conv is not None and conv.ready
        return check

    def standby_again(for_s: float = 1.5) -> Any:
        """No conversation for ``for_s`` and (v3.11) the next standby waiting: a warm wake, as between two requests."""
        gone = no_conversation(for_s)
        ready = prewarmed()
        return lambda: gone() and ready()

    names: list[str] = []
    if scenario == "echo":
        script = [(clip("k_nice"), 1.0, None)]
        names = ["k_nice"]
    elif scenario == "call":
        lines = [("k_pause", 1.0, None), ("k_clutch", 1.0, settled(0.8)), ("k_insane", 0.6, None),
                 ("k_after", 0.9, None), ("k_hear", 0.7, None), ("k_lol", 0.8, None)]
        script = [(clip(n), after, wait) for n, after, wait in lines]
        names = [n for n, _a, _w in lines]
    elif scenario == "call2":
        # Questions the others were asked (Yuki could answer them), then Yuki addressed again
        # within the conversation (no wake needed), then more talk to the call.
        lines = [("k_pause", 1.0, None), ("k_hear", 0.8, settled(0.8)), ("k_after", 0.6, None),
                 ("k_turnup", 0.8, None), ("k_lol", 0.8, settled(0.8)), ("k_clutch", 0.6, None)]
        script = [(clip(n), after, wait) for n, after, wait in lines]
        names = [n for n, _a, _w in lines]
    elif scenario == "latency":
        gap = 0.35
        first = prewarmed() if args.prewarm else None
        if args.breath:
            # v3.11: a one-breath "Yuki <command>" clip with its true name end (recognition's word times), twice.
            lines = [(args.breath, args.first_after, first), (args.breath, 1.0, standby_again(1.5))]
        elif args.one_breath:
            # "Yuki, pause the music." in one breath, twice (v3.5): the words right after the name.
            lines = [("k_pause", args.first_after, first), ("k_pause", 1.0, standby_again(1.5))]
        else:
            lines = [("k_name", 1.0, first), ("k_pause_cmd", gap, None), ("k_name", 1.0, no_conversation(1.5)),
                     ("k_pause_cmd", gap, None)]
        script = [(clip(n), after, wait) for n, after, wait in lines]
        names = [n for n, _a, _w in lines]
        box["name_end_s"] = speech_end_offset(clip("k_name"))  # "Yuki" said alone; in k_pause it ends about as soon
        if args.breath:
            box["name_end_s"] = float(manifest[args.breath]["name_end"])
    elif scenario == "endings":
        # vfix (report 40, V1-V3): "shut up", "go away", "bye" and a mistaken wake each end the conversation at once
        # and leave Yuki waiting for her name (never quiet time); after "what's up?" got its answer.
        first = prewarmed() if args.prewarm else None
        lines = [("k_name", 1.0, first), ("e_whatsup", 0.35, None), ("e_shutup", 1.0, settled(0.8)),
                 ("k_name", 1.0, standby_again(1.5)), ("e_whatsup", 0.35, None), ("e_goaway", 1.0, settled(0.8)),
                 ("k_name", 1.0, standby_again(1.5)), ("e_bye", 0.35, None),
                 ("k_name", 1.0, standby_again(1.5)), ("e_mistake", 0.35, None)]
        script = [(clip(n), after, wait) for n, after, wait in lines]
        names = [n for n, _a, _w in lines]
    elif scenario == "lists":
        # leftVoice (report 65's "what's on my list" silence): each ask in a conversation of its own, after the name
        # (clips: voice_clips.py --set lists); with --fake-memory the quick memory tool reads an invented list
        first = prewarmed() if args.prewarm else None
        lines = []
        for i, ask in enumerate(a.strip() for a in args.asks.split(",") if a.strip()):
            lines += [("k_name", 1.0, first if i == 0 else standby_again(1.5)), (ask, 0.35, None)]
        script = [(clip(n), after, wait) for n, after, wait in lines]
        names = [n for n, _a, _w in lines]
    else:
        raise SystemExit(f"unknown scenario {scenario}")

    mics: list[Any] = []

    def make_mic(device: Any) -> Any:
        mic = clip_mic(device, room=room, script=script)
        mics.append(mic)
        return mic

    session.mic_factory = make_mic
    session.speaker_factory = lambda rate, device: box.setdefault("speaker", RoomSpeaker(rate, device, room=speaker_room))
    audio.headphones_check = lambda known=(), device=None: (
        args.headphones, {"form_factor": "headphones" if args.headphones else "speakers", "device": device})
    # The device watch reads Core Audio; here the devices are the pretend ones (never the real list).
    audio.audio_devices = lambda: {"capture": ["Microphone (clips)"], "render": ["Speakers (room)"],
                                   "default_capture": "Microphone (clips)", "default_render": "Speakers (room)"}
    states: list[tuple[float, str, str]] = []
    session.state_changed.connect(lambda s, d: states.append((time.monotonic(), s, d)))
    wakes: list[tuple[float, float]] = []
    session._wake_heard.connect(lambda at, heard, *_: wakes.append((at, heard)))
    convs: list[tuple[float, bool]] = []
    timings: list[dict[str, Any]] = []

    def on_conversation(on: bool) -> None:
        convs.append((time.monotonic(), on))
        box["started"] = True
        if not on:
            timings.append(dict(session.last_timings))

    session.conversation_changed.connect(on_conversation)
    if scenario in ("call", "call2"):
        session.on_call_status({"call": {"app": "Discord", "status": "recording"}})
    session.wake_enabled = True
    io_seen = install_io_audit() if getattr(args, "audit_io", False) else None
    t_start = time.monotonic()
    session.start()
    if args.prewarm:
        session.prewarm()

    def poll() -> None:
        mic = mics[0] if mics else None
        done = mic is not None and mic.done.is_set() and not session.in_conversation and convs and not convs[-1][1]
        if done and time.monotonic() - convs[-1][0] > 1.5:
            app.quit()
        if time.monotonic() - t_start > args.max_s:
            session.stop_conversation("test_timeout")
            app.quit()

    timer = QTimer()
    timer.timeout.connect(poll)
    timer.start(200)
    app.exec()
    timer.stop()
    session.stop_conversation("end", wait_s=8.0)
    for _ in range(20):
        app.processEvents()
        time.sleep(0.05)
    session.shutdown()
    if service is not None:
        service.kill()
    t0 = timeline.t0
    mic = mics[0] if mics else None
    line_starts = [ln["started_at"] - t0 for ln in (mic.lines if mic else [])]
    report: dict[str, Any] = {
        "scenario": scenario, "variant": variant, "echo_db": args.echo_db,
        "lines": [{"clip": n, "t": round(t, 2)} for n, t in zip(names, line_starts)],
        "timeline": timeline.events,
        "states": [(round(t - t0, 2), s, d) for t, s, d in states],
        "wakes": [{"heard": round(h - t0, 2), "batch_at": round(a - t0, 2)} for a, h in wakes],
        "conversations": [(round(t - t0, 2), on) for t, on in convs],
        "session_timings_ms": timings,
        "speaker_played_s": round(speaker_room.played_s, 1),
        "mic_levels": mic.stats.levels() if mic else None,
        "brain_requests": getattr(runtime, "requests", []),
        "desk_calls": [c[0] for c in desk.calls],
    }
    if scenario == "latency" and mic is not None:
        rows = []
        for i, (a, heard) in enumerate(wakes):
            one = args.one_breath or bool(args.breath)
            k = (0 if i == 0 else 1) if one else (0 if i == 0 else 2)
            if k >= len(mic.lines):
                break
            word_end = mic.lines[k]["started_at"] + box["name_end_s"]
            row = {"wake": ("cold" if i == 0 else "warm"), "word_end_to_heard_ms": round((heard - word_end) * 1000)}
            if i < len(timings):
                row.update({f"heard_to_{key}_ms": v for key, v in timings[i].items() if isinstance(v, (int, float))
                            and key != "replayed_s"})
                row["replayed_s"] = timings[i].get("replayed_s")
            # The request's own end (the last line of this wake): when it was transcribed and acted on.
            step = 0 if one else 1
            hearing = [t for t, st, _d in states if st == "hearing" and t > heard]
            row["name_end_to_hearing_ms"] = round((hearing[0] - word_end) * 1000) if hearing else None
            last = mic.lines[k + step] if k + step < len(mic.lines) else None
            if last is not None:
                said_end = last["started_at"] + speech_end_offset(clip(names[k + step])) - t0
                nxt = heard + 60
                heard_t = [e["t"] for e in timeline.events if e["kind"] == "user" and e["t"] > said_end - 0.2]
                acted = [e["t"] for e in timeline.events if e["kind"] == "action" and e["t"] > said_end - 0.2]
                row["request_end_to_transcript_ms"] = round((heard_t[0] - said_end) * 1000) if heard_t else None
                row["request_end_to_action_ms"] = round((acted[0] - said_end) * 1000) if acted else None
                row["heard"] = [e["text"] for e in timeline.events if e["kind"] == "user"
                                and said_end - 3 < e["t"] < said_end + 15]
                del nxt
            rows.append(row)
        report["latency"] = rows
    if scenario == "endings" and mic is not None:
        rows = []
        ends = [t - t0 for t, on in convs if not on]
        for i, name in enumerate(names):
            if not name.startswith("e_") or name == "e_whatsup" or i >= len(mic.lines):
                continue
            said_end = mic.lines[i]["started_at"] + speech_end_offset(clip(name)) - t0
            end = next((e for e in ends if e > said_end), None)
            upto = end if end is not None else said_end + 30
            said = [e["text"] for e in timeline.events if e["kind"] == "yuki" and said_end < e["t"] <= upto]
            tools = [e["name"] for e in timeline.events if e["kind"] == "mouth_tool" and said_end - 0.5 < e["t"] <= upto]
            rows.append({"line": name, "ended": end is not None,
                         "speech_end_to_conversation_end_ms": round((end - said_end) * 1000) if end else None,
                         "yuki_said_after": said, "tools": tools})
        report["endings"] = rows
        report["quiet_after"] = bool(getattr(session, "quiet", False))
    if scenario == "lists" and mic is not None:
        report["asks"] = ask_rows(names, mic.lines, timeline.events, t0,
                                  lambda n: speech_end_offset(clip(n)))
    if io_seen is not None:
        report["loop_io"] = sorted(({"event": k[0], "where": k[1], "what": k[2], "n": n} for k, n in io_seen.items()),
                                   key=lambda r: -r["n"])
    out = args.out / f"{scenario}-{variant}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return report


#: leftVoice: the invented memory of the "lists" scenario (never a real person's items).
FAKE_TODOS = ["Buy printer ink", "Call the plumber about the kitchen leak", "Renew the gym pass",
              "Email Tara the slides", "Book the dentist for Friday"]
FAKE_MORNING = ("The plumber said he can only come on Thursday after four, so remind me to be home by then.",
                "Got it, Thursday after four for the plumber.")


def fake_memory(folder: Path) -> Path:
    """A new memory in ``folder`` (its own key) holding :data:`FAKE_TODOS` (added yesterday) and one exchange two hours
    ago (both outside the last hour's view, so a question about them needs a look). Returns the folder, for ``--db``."""
    from yuki.memory.api import MemoryClient

    folder.mkdir(parents=True, exist_ok=True)
    client = MemoryClient.open(folder / "memory.db")
    try:
        for i, text in enumerate(FAKE_TODOS):   # added yesterday: not in the last hour's view, so a look is needed
            client.store.add_todo(text, at=time.time() - 86_400 + i)
        client.log_turn("20261004-070000-fakefa", "voice-1", time.time() - 7200, FAKE_MORNING[0], FAKE_MORNING[1], [],
                        "voice")
        client.store.flush_turns(5.0)
    finally:
        client.close()
    return folder


def ask_rows(names: list[str], lines: list[dict[str, Any]], events: list[dict[str, Any]], t0: float,
             end_of: Any) -> list[dict[str, Any]]:
    """leftVoice: per ask (each line after the name), from the end of its speech: the first audio Yuki played, the
    talker's tool call and its result, and the first audio after the result (the answer itself)."""
    rows = []
    for i, name in enumerate(names):
        if name == "k_name" or i >= len(lines):
            continue
        said_end = lines[i]["started_at"] + end_of(name) - t0
        nxt = lines[i + 1]["started_at"] - t0 if i + 1 < len(lines) else float("inf")
        mine = [e for e in events if said_end - 0.3 < e["t"] < nxt]

        def first(kind: str, after: float = said_end, rows_: list[dict[str, Any]] = mine) -> Any:
            return next((e for e in rows_ if e["kind"] == kind and e["t"] >= after), None)

        audio = first("audio_start")
        tool = first("mouth_tool", said_end - 0.3)
        result = first("to_mouth", tool["t"]) if tool else None
        answer = first("audio_start", result["t"]) if result else None
        heard = first("user", said_end - 0.3)

        def ms(e: Any) -> Any:
            return round((e["t"] - said_end) * 1000) if e else None

        rows.append({"ask": name, "heard": heard["text"] if heard else None, "transcript_ms": ms(heard),
                     "first_audio_ms": ms(audio), "tool": tool["name"] if tool else None,
                     "tool_args": tool.get("args") if tool else None, "tool_ms": ms(tool), "result_ms": ms(result),
                     "answer_audio_ms": ms(answer),
                     "said_by_code": [e["text"] for e in mine if e["kind"] == "yuki_code"],
                     "said": [e["text"] for e in mine if e["kind"] == "yuki"]})
    return rows


#: Audit events that touch the disk (v3.17, D7: none may run on the voice loop's own thread).
IO_EVENTS = frozenset({"open", "os.mkdir", "os.rename", "os.replace", "os.remove", "os.rmdir", "os.listdir",
                       "os.scandir", "shutil.copyfile", "shutil.move", "sqlite3.connect"})


def install_io_audit() -> dict[tuple[str, str, str], int]:
    """Count every disk access made on a thread that runs an asyncio loop (the voice loop), by event, by where in the
    code (the innermost frames of Yuki's and Pipecat's own files) and by what (the file's name, its folder cut away).
    A process-wide audit hook (``sys.addaudithook``); content-free."""
    import asyncio
    import os
    import threading

    seen: dict[tuple[str, str, str], int] = {}
    get_loop = asyncio._get_running_loop

    def hook(event: str, args: tuple) -> None:
        if event not in IO_EVENTS or get_loop() is None:
            return
        first = args[0] if args else ""
        if isinstance(first, int):
            return   # a file descriptor: no new file is touched
        what = os.path.basename(str(first or "")) or str(first or "")[-40:]
        frame = sys._getframe(1)
        where: list[str] = []
        while frame is not None and len(where) < 4:
            name = frame.f_code.co_filename.replace(chr(92), "/")
            if "/yuki/" in name or "/pipecat/" in name or "/scripts/" in name:
                where.append(f"{os.path.basename(name)}:{frame.f_lineno}:{frame.f_code.co_name}")
            frame = frame.f_back
        key = (event, " < ".join(where) or threading.current_thread().name, what)
        seen[key] = seen.get(key, 0) + 1

    sys.addaudithook(hook)
    return seen


def summary(report: dict[str, Any]) -> str:
    lines = [f"== {report['scenario']} ({report['variant']}, room echo {report['echo_db']} dB)"]
    for ln in report["lines"]:
        lines.append(f"{ln['t']:7.2f}  [clip {ln['clip']}]")
    for w in report["wakes"]:
        lines.append(f"{w['heard']:7.2f}  WAKE heard")
    for t, on in report["conversations"]:
        lines.append(f"{t:7.2f}  conversation {'start' if on else 'end'}")
    for e in report["timeline"]:
        k = e["kind"]
        if k == "user":
            lines.append(f"{e['t']:7.2f}  USER  {e['text']}")
        elif k == "yuki":
            lines.append(f"{e['t']:7.2f}  YUKI  {e['text']}")
        elif k == "mouth_tool":
            lines.append(f"{e['t']:7.2f}   tool {e['name']} {json.dumps(e['args'], ensure_ascii=False)[:120]}")
        elif k == "action":
            lines.append(f"{e['t']:7.2f}   ACTION {e['name']} {e.get('args')}")
    for t, s, d in report["states"]:
        if s in ("error", "note"):
            lines.append(f"{t:7.2f}  pill {s}: {d}")
    if report.get("latency"):
        lines.append("latency: " + json.dumps(report["latency"]))
    if report.get("endings") is not None:
        for row in report["endings"]:
            lines.append("ending: " + json.dumps(row, ensure_ascii=False))
        lines.append(f"quiet time after (go_quiet): {report.get('quiet_after')}")
    for row in report.get("asks") or []:
        lines.append("ask: " + json.dumps(row, ensure_ascii=False))
    lines.append("session timings: " + json.dumps(report["session_timings_ms"]))
    lines.append(f"mic levels: {report['mic_levels']}")
    if report.get("loop_io") is not None:
        lines.append(f"disk on the voice loop: {len(report['loop_io'])} places")
        for row in report["loop_io"][:20]:
            lines.append(f"   {row['n']:4d} {row['event']:16s} {row['what'][:40]:40s} {row['where']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("scenario", choices=("echo", "call", "call2", "latency", "endings", "lists"))
    ap.add_argument("--clips", type=Path, required=True, help="the folder with manifest.json of the v34 clips")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--echo", default="aec", choices=("aec", "half", "none"))
    ap.add_argument("--echo-db", dest="echo_db", type=float, default=-8.0, help="the room's echo level")
    ap.add_argument("--headphones", action="store_true")
    ap.add_argument("--idle", type=float, default=8.0)
    ap.add_argument("--call-idle", dest="call_idle", type=float, default=10.0)
    ap.add_argument("--max-s", dest="max_s", type=float, default=120.0)
    ap.add_argument("--first-after", dest="first_after", type=float, default=1.0,
                    help="latency --one-breath: seconds of room before the first line")
    ap.add_argument("--prewarm", action="store_true", help="latency: prewarm in the background before the first wake")
    ap.add_argument("--breath", default="", help="latency: a one-breath clip with its name_end in the manifest (v3.11)")
    ap.add_argument("--no-standby", dest="no_standby", action="store_true", help="v3.11: build on the wake (before)")
    ap.add_argument("--audit-io", dest="audit_io", action="store_true",
                    help="v3.17: count every disk access on the voice loop's thread (where in the code, which file)")
    ap.add_argument("--db", type=Path, default=None,
                    help="v3.18: a folder with a COPY of memory (memory.db, memory.key): practice lines, hand traces")
    ap.add_argument("--fake-memory", dest="fake_memory", action="store_true",
                    help="leftVoice: a new memory of invented to-dos and one exchange two hours ago (lists)")
    ap.add_argument("--asks", default="l_time,l_list",
                    help="lists: the clips to ask, each after the name in a conversation of its own")
    ap.add_argument("--one-breath", dest="one_breath", action="store_true",
                    help="latency: \"Yuki, pause the music.\" in one breath instead of the name, a pause, the words")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    report = run(args.scenario, args)
    text = summary(report)
    (args.out / f"{args.scenario}-{report['variant']}.txt").write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
