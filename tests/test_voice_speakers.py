"""Voice v3.4 offline checks: speakers (echo guard), call mode, never failing silently, a mic that
survives device changes, and the deferred transcriber's "present" path (docs/VOICE.md §11, §16, §25).

Rule 7: no microphone, no speakers, no desktop. Yuki's reply is played into a simulated room
(:class:`yuki.voice.offline.Room`: delay, reverberation, a speaker's distortion, device latencies
that differ from what the devices report) and mixed into the mic path; PortAudio is a fake module;
the transcriber's facts are stubs; the pill runs on Qt's offscreen platform in a subprocess.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])
DATA = Path(__file__).parent / "data" / "voice"
RATE = 16_000


def _clip(name: str) -> np.ndarray:
    from yuki.voice.transport import load_clip

    return load_clip(DATA / f"{name}.wav")


def _has_livekit() -> bool:
    try:
        import livekit.rtc  # noqa: F401
    except Exception:  # noqa: BLE001
        return False
    return True


# ---------------------------------------------------------------------------
# The echo guard, on a simulated room (simulated time: no sleeping)
# ---------------------------------------------------------------------------


def _speaker_room(mode: str | None, *, echo_db: float = -8.0, apm: Any = None, user_after_s: float = 1.0,
                  user_during: bool = False, offset_s: float = 0.0) -> dict[str, Any]:
    """Yuki says ``y_reply`` (her name twice) on speakers; then the user says "Yuki." Returns what
    the real wake listener (its sound gate, pre-roll and spotter) heard on the mic path, the
    cleaned audio and the echo alone, on one clock.

    Device latencies: the speaker reports 20 ms and really plays 45 ms after a write; the mic
    reports 10 ms and really delivers a block 20 ms after its sound.
    """
    from yuki.voice.audio import SharedMic
    from yuki.voice.echo import EchoGuard, PlaybackTimeline
    from yuki.voice.offline import Room
    from yuki.voice.wake import WakeListener, WakeSpotter

    t0 = 1000.0
    room = Room(t0=t0, rt60=0.3, echo_db=echo_db)
    timeline = PlaybackTimeline(latency_s=0.02)
    reply, name = _clip("y_reply"), _clip("k_name")
    true_out, true_in = 0.045, 0.02
    reply_start = t0 + 0.5 + offset_s
    chunk = int(0.04 * RATE)
    writes = [(reply_start + k * 0.04, reply[i:i + chunk]) for k, i in enumerate(range(0, len(reply), chunk))]
    reply_end = reply_start + len(reply) / RATE + true_out
    user_at = (reply_start + 2.0) if user_during else (reply_end + user_after_s)
    total = user_at - t0 + len(name) / RATE + 1.5
    mic = SharedMic(None)
    guard = None
    if mode is not None:
        guard = EchoGuard(mode, timeline, tail_s=0.4, input_latency_s=0.01, apm=apm)
        mic.set_echo(guard)
    hits: list[float] = []
    out: list[bytes] = []
    mic.subscribe(lambda pcm, at: out.append(pcm))
    listener = WakeListener(mic, WakeSpotter, on_wake=lambda at, _kw: hits.append(at))
    listener.start()
    assert listener.ready.wait(20)
    rng = np.random.default_rng(5)
    echo_only = []
    w = 0
    for i in range(int(total / 0.02)):
        c = t0 + i * 0.02
        while w < len(writes) and writes[w][0] <= c:  # the speaker thread's writes up to now
            at, part = writes[w]
            timeline.played((np.clip(part, -1, 1) * 32767).astype("<i2").tobytes(), RATE, at=at)
            room.play(part, at + true_out)
            w += 1
        echo = room.pick(c - true_in, 320)
        block = echo + rng.standard_normal(320).astype(np.float32) * 10 ** (-62 / 20)
        k = int(round((c - true_in - user_at) * RATE))
        if 0 <= k < len(name) or (k < 0 < k + 320):
            seg = np.zeros(320, np.float32)
            a, b = max(0, k), min(len(name), k + 320)
            seg[a - k:b - k] = name[a:b] * 0.7
            block = block + seg
        echo_only.append(echo)
        mic.feed(block.astype(np.float32), c)
        while listener._q.qsize() > 50:  # faster than real time, but never past the listener's queue
            time.sleep(0.002)
    deadline = time.monotonic() + 10
    while not listener._q.empty() and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.1)
    listener.stop()
    clean = np.frombuffer(b"".join(out), dtype="<i2").astype(np.float32) / 32768.0
    return {"hits": hits, "reply": (reply_start, reply_end), "user_at": user_at, "t0": t0, "guard": guard,
            "clean": clean, "echo": np.concatenate(echo_only), "stats": mic.stats}


def _hits_during(run: dict[str, Any], a: float, b: float) -> list[float]:
    return [h for h in run["hits"] if a <= h <= b]


#: Where Yuki's reply starts against the spotter's 100 ms batches: its score on a reverberant
#: echo depends on the alignment, so every check runs at several.
OFFSETS = (0.0, 0.06, 0.09, 0.12, 0.2)


def test_without_a_guard_yukis_own_voice_wakes_her() -> None:
    """The control: on speakers, Yuki saying her name reaches the wake word (why v3.3 refused speakers)."""
    woke = 0
    for offset in OFFSETS:
        run = _speaker_room(None, offset_s=offset)
        start, end = run["reply"]
        woke += bool(_hits_during(run, start, end + 0.6))
    assert woke >= 1, "the echo of 'Yuki here!' never woke an unguarded listener"  # measured: 2 of 5 alignments


def test_half_duplex_keeps_yukis_voice_out_and_hears_the_user_after() -> None:
    for offset in OFFSETS:
        run = _speaker_room("half", offset_s=offset)
        start, end = run["reply"]
        assert not _hits_during(run, start, end + 0.5), offset
        assert _hits_during(run, run["user_at"], run["user_at"] + 2.0), f"the user's own 'Yuki.' after the tail ({offset})"
        assert run["stats"].gated_blocks > (end - start) / 0.02 * 0.9  # the whole reply was silenced


@pytest.mark.skipif(not _has_livekit(), reason="livekit (WebRTC APM) not installed")
def test_echo_canceller_removes_yukis_voice_and_keeps_the_users() -> None:
    from yuki.voice.echo import load_apm

    apm, why = load_apm()
    assert apm is not None, why
    for offset in OFFSETS:
        run = _speaker_room("aec", apm=apm, echo_db=-4.0, offset_s=offset)
        guard = run["guard"]
        start, end = run["reply"]
        assert guard.converged and guard.effective == "aec"
        assert not _hits_during(run, start, end + 0.5), offset
        assert _hits_during(run, run["user_at"], run["user_at"] + 2.0), offset
        # Echo reduction over the reply once converged (from its second second on).
        a, b = int((start + 1.0 - run["t0"]) * RATE), int((end - run["t0"]) * RATE)
        erle = 10 * np.log10(np.mean(run["echo"][a:b] ** 2) / np.mean(run["clean"][a:b] ** 2))
        assert erle > 20.0, (offset, erle)
        assert guard.stats.aec_errors == 0


@pytest.mark.skipif(not _has_livekit(), reason="livekit (WebRTC APM) not installed")
def test_echo_canceller_lets_the_user_through_while_yuki_talks() -> None:
    """Barge-in on speakers: the user's words over Yuki's still reach the mic path (not silenced)."""
    from yuki.voice.echo import load_apm

    apm, _ = load_apm()
    run = _speaker_room("aec", apm=apm, user_during=True)
    u = int((run["user_at"] - run["t0"]) * RATE)
    n = len(_clip("k_name"))
    user_energy = np.mean(run["clean"][u:u + n] ** 2)
    echo_energy = np.mean(run["clean"][u - 2 * RATE:u - RATE] ** 2)  # a second of Yuki alone, cleaned
    assert run["guard"].converged
    assert 10 * np.log10(user_energy / echo_energy) > 10.0


def test_mode_follows_the_outputs_form_factor() -> None:
    from yuki.voice.echo import choose_mode

    assert choose_mode(True) == ("full", "headphones")
    assert choose_mode(False) == ("aec", "speakers")
    assert choose_mode(False, aec_available=False) == ("half", "speakers_no_aec")
    assert choose_mode(False, preference="half") == ("half", "setting")
    assert choose_mode(False, preference="full") == ("full", "setting")


def test_timeline_reference_is_what_was_played_when_it_is_heard() -> None:
    from yuki.voice.echo import LATENCY_TRUST, PlaybackTimeline

    tl = PlaybackTimeline(latency_s=0.04)
    tone = (np.sin(np.arange(1600) / 5) * 8000).astype("<i2")
    tl.played(tone.tobytes(), RATE, at=10.0)
    heard = 10.0 + 0.04 * LATENCY_TRUST
    ref = tl.reference(heard, 1600)
    assert np.allclose(ref, tone.astype(np.float32) / 32768.0, atol=1e-4)
    assert not tl.reference(heard + 0.2, 160).any()  # nothing after it
    assert tl.audible(heard + 0.05, heard + 0.06) and not tl.audible(heard + 0.5, heard + 0.6, tail_s=0.3)
    assert tl.audible(heard + 0.35, heard + 0.36, tail_s=0.3)


def test_a_failing_canceller_falls_back_to_half_duplex() -> None:
    from yuki.voice.echo import EchoGuard, PlaybackTimeline

    class Broken:
        def process_reverse_stream(self, frame: Any) -> None:
            raise RuntimeError("apm went away")

    tl = PlaybackTimeline(latency_s=0.0)
    tl.played((np.ones(3200) * 3000).astype("<i2").tobytes(), RATE, at=5.0)
    guard = EchoGuard("aec", tl, tail_s=0.2, input_latency_s=0.0)
    guard._aec = types.SimpleNamespace(step=lambda *a: Broken().process_reverse_stream(None))
    samples, gated = guard.process(np.full(320, 0.1, np.float32), 5.05)
    assert gated and not samples.any() and guard.stats.aec_errors == 1 and guard.effective == "half"


def test_guard_outlives_the_conversation_only_for_the_tail() -> None:
    from yuki.voice.audio import SharedMic
    from yuki.voice.echo import EchoGuard, PlaybackTimeline

    tl = PlaybackTimeline(latency_s=0.0)
    tl.played((np.ones(1600) * 3000).astype("<i2").tobytes(), RATE, at=100.0)
    mic = SharedMic(None)
    guard = EchoGuard("half", tl, tail_s=0.3)
    mic.set_echo(guard)
    guard.close()
    mic.feed(np.full(320, 0.05, np.float32), 100.2)
    assert mic.echo is guard and mic.stats.gated_blocks == 1
    mic.feed(np.full(320, 0.05, np.float32), 100.7)
    assert mic.echo is None and mic.stats.gated_blocks == 1
    assert mic.stats.blocks == 2  # gated blocks still count: the device watch reads the counter


def test_backlog_handoff_loses_nothing_and_doubles_nothing() -> None:
    from yuki.voice.audio import BACKLOG_S, SharedMic

    assert BACKLOG_S >= 10.0  # a cold start (Pipecat import + Deepgram connect) fits in it
    mic = SharedMic(None)
    for i in range(50):
        mic.feed(np.full(320, i / 100, np.float32), 1.0 + i * 0.02)
    seen: list[float] = []
    token, kept = mic.subscribe_from(lambda pcm, at: seen.append(at), 1.5)
    for i in range(50, 60):
        mic.feed(np.full(320, i / 100, np.float32), 1.0 + i * 0.02)
    times = [at for _pcm, at in kept] + seen
    assert times == sorted(times) and len(times) == len(set(times))
    assert abs(times[0] - 1.5) < 0.021 and abs(times[-1] - (1.0 + 59 * 0.02)) < 1e-9
    mic.unsubscribe(token)


def test_earcons_are_short_and_quiet() -> None:
    from yuki.voice.echo import tone

    for kind, most_s in (("listening", 0.25), ("error", 0.4)):
        pcm = np.frombuffer(tone(kind), dtype="<i2")
        assert 0.1 < len(pcm) / 24_000 < most_s
        assert np.abs(pcm).max() < 32767 * 10 ** (-14 / 20)


# ---------------------------------------------------------------------------
# Call mode
# ---------------------------------------------------------------------------


class _Memory:
    def __init__(self) -> None:
        self.turns: list[dict[str, Any]] = []

    def log_turn(self, **kw: Any) -> None:
        self.turns.append(kw)


def _observer(memory: Any, call: list[bool]) -> Any:
    from yuki.voice.pipeline import TurnObserver

    return TurnObserver(settings=Settings(), log=None, memory=memory, session="t", speech_end=lambda: None,
                        on_state=lambda *a: None, tts_provider="deepgram", activity=lambda: None,
                        call_mode=lambda: call[0])


def test_call_talk_is_neither_answered_nor_kept_nor_counted() -> None:
    from pipecat.frames.frames import (
        BotStartedSpeakingFrame, BotStoppedSpeakingFrame, FunctionCallInProgressFrame, LLMFullResponseEndFrame,
        LLMFullResponseStartFrame, LLMTextFrame, TranscriptionFrame,
    )

    memory = _Memory()
    obs = _observer(memory, [True])
    obs.on_frame(TranscriptionFrame(text="Yuki, pause the music", user_id="u", timestamp="t"))
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(FunctionCallInProgressFrame(function_name="media_key", tool_call_id="c1", arguments={}))
    obs.on_frame(LLMFullResponseEndFrame())
    assert obs.exchanges == 1 and obs.last_exchange is not None
    first = obs.last_exchange
    for i, words in enumerate(("bro did you see that clutch", "what are we doing after this?")):
        obs.on_frame(TranscriptionFrame(text=words, user_id="u", timestamp="t"))
        obs.on_frame(LLMFullResponseStartFrame())
        obs.on_frame(FunctionCallInProgressFrame(function_name="stay_quiet", tool_call_id=f"q{i}", arguments={}))
        obs.on_frame(LLMFullResponseEndFrame())
    assert obs.exchanges == 1 and obs.last_exchange == first  # talk to the call is no exchange with Yuki
    # A friend line cut short by the next words before the mouth decided anything: not kept either.
    obs.on_frame(TranscriptionFrame(text="haha no way", user_id="u", timestamp="t"))
    obs.on_frame(TranscriptionFrame(text="Yuki, what time is it?", user_id="u", timestamp="t"))
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(LLMTextFrame(text="Twenty past nine."))
    obs.on_frame(BotStartedSpeakingFrame())
    obs.on_frame(BotStoppedSpeakingFrame())
    obs.on_frame(LLMFullResponseEndFrame())
    obs.close()
    # v3.5: every turn is logged, marked with who it was for; memory never extracts the "others" ones.
    kept = [(t["user_text"], t["addressed"]) for t in memory.turns]
    assert kept == [("Yuki, pause the music", "yuki"), ("bro did you see that clutch", "others"),
                    ("what are we doing after this?", "others"), ("haha no way", "others"),
                    ("Yuki, what time is it?", "yuki")]
    assert obs.totals["for_others"] == 3 and obs.exchanges == 2


def test_outside_a_call_a_quiet_turn_is_still_kept() -> None:
    from pipecat.frames.frames import (
        FunctionCallInProgressFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, TranscriptionFrame,
    )

    memory = _Memory()
    obs = _observer(memory, [False])
    obs.on_frame(TranscriptionFrame(text="hmm", user_id="u", timestamp="t"))
    obs.on_frame(LLMFullResponseStartFrame())
    obs.on_frame(FunctionCallInProgressFrame(function_name="stay_quiet", tool_call_id="q", arguments={}))
    obs.on_frame(LLMFullResponseEndFrame())
    obs.close()
    assert [t["user_text"] for t in memory.turns] == ["hmm"] and obs.totals["for_others"] == 0
    assert memory.turns[0]["addressed"] == "others"  # v3.5: a quiet reply to their words = not said to Yuki


def test_the_replay_after_a_wake_keeps_the_words_and_drops_the_waiting() -> None:
    """The kept audio replayed into speech recognition after a cold start: every word, short pauses."""
    from yuki.voice.transport import REPLAY_MAX_PAUSE_BLOCKS, REPLAY_TAIL_BLOCKS, squeeze_replay

    rng = np.random.default_rng(0)

    def block(level_db: float, i: int) -> tuple[bytes, float]:
        x = rng.standard_normal(320) * 10 ** (level_db / 20)
        return (np.clip(x * 32767, -32768, 32767).astype("<i2").tobytes(), 1.0 + i * 0.02)

    # 0.5 s quiet, 1 s speech, 2 s quiet (a pause), 1 s speech, then 4 s quiet until connected
    plan = [-62] * 25 + [-24] * 50 + [-62] * 100 + [-24] * 50 + [-62] * 200
    kept = [block(level, i) for i, level in enumerate(plan)]
    out = squeeze_replay(kept)
    times = [at for _pcm, at in out]
    speech = [kept[i][1] for i, level in enumerate(plan) if level == -24]
    assert set(speech) <= set(times)  # no word lost
    assert times == sorted(times)
    assert len(out) == REPLAY_MAX_PAUSE_BLOCKS + 50 + REPLAY_MAX_PAUSE_BLOCKS + 50 + REPLAY_TAIL_BLOCKS
    assert len(out) * 0.02 < 0.5 * len(kept) * 0.02  # 8.5 s kept, 3.3 s replayed


def test_the_thinker_never_hears_talk_to_the_call() -> None:
    from yuki.voice.bridge import heard_words
    from yuki.voice.pipeline import quiet_answered

    messages = [
        {"role": "user", "content": "bro did you see that clutch"},
        {"role": "assistant", "tool_calls": [{"id": "a", "function": {"name": "stay_quiet", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "a", "content": "{}"},
        {"role": "user", "content": "Yuki, add up my invoices"},
        {"role": "assistant", "content": "On it.", "tool_calls": [{"id": "b", "function": {"name": "tell_yuki"}}]},
    ]
    context = types.SimpleNamespace(get_messages=lambda: messages)
    quiet = quiet_answered(messages)
    assert quiet == {id(messages[0])}
    assert heard_words(context, skip=lambda m: id(m) in quiet) == ["Yuki, add up my invoices"]


def test_the_call_note_is_there_only_in_a_call() -> None:
    from yuki.voice.mouth import CALL_LAYER, STAY_QUIET, MouthMemory, mouth_system_blocks, volatile_block

    memory = MouthMemory(source="memory", standing="- keep it short", personality="Banters a lot.")
    plain = mouth_system_blocks(memory, tail=["tail"])
    call = mouth_system_blocks(memory, tail=["tail"], call="Discord")
    assert [b["text"] for b in plain] == [b["text"] for b in call if not b["text"].startswith("[They are in a")]
    assert call[1]["text"] == CALL_LAYER.format(app="Discord") and "cache_control" not in call[1]
    assert "voice call on Discord" in volatile_block(call="Discord")["text"]
    assert "voice call" not in volatile_block()["text"]
    assert "people on the call" in STAY_QUIET["description"]


def test_call_mode_ends_soon_after_the_last_exchange_however_much_they_talk() -> None:
    from yuki.voice.pipeline import Conversation

    settings = Settings(voice_call_idle_s=1.0, voice_idle_s=20.0)
    bridge = types.SimpleNamespace(open_voice_jobs=lambda: 0, closing=False, busy_tools=0)
    closing: list[str] = []
    conv = Conversation(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=None,
                        log=types.SimpleNamespace(event=lambda *a, **k: None), speech_end=lambda: None,
                        on_state=lambda *a: None, session="t", call_app="Discord", on_closing=closing.append)

    class Runner:
        cancelled = False

        async def cancel(self) -> None:
            self.cancelled = True

    async def go() -> float:
        runner = Runner()
        conv._loop = asyncio.get_running_loop()
        conv.stt_ready_at = time.monotonic()
        started = time.monotonic()
        obs = conv.observer

        async def friends_talk() -> None:  # the user keeps talking to the call, with pauses
            while not runner.cancelled:
                obs.user_speaking = True
                conv._on_activity()
                await asyncio.sleep(0.3)
                obs.user_speaking = False
                conv._on_activity()
                await asyncio.sleep(0.15)

        talk = asyncio.create_task(friends_talk())
        await asyncio.wait_for(conv._watch(runner), timeout=10)
        talk.cancel()
        assert runner.cancelled
        return time.monotonic() - started

    took = asyncio.run(go())
    assert conv.reason == "call_idle" and closing == ["call_idle"]
    assert took < 1.0 + Conversation.CALL_SPEECH_GRACE_S + 0.5, took


def test_a_back_and_forth_earns_a_longer_quiet_window() -> None:
    from yuki.voice.pipeline import Conversation

    settings = Settings(voice_idle_s=20.0, voice_idle_per_exchange_s=8.0, voice_idle_max_s=45.0)
    conv = Conversation(settings=settings, bridge=types.SimpleNamespace(), transport=None, memory=None,
                        mouth_memory=None, log=None, speech_end=lambda: None, on_state=lambda *a: None, session="t")
    for exchanges, expected in ((0, 20.0), (1, 20.0), (2, 28.0), (4, 44.0), (9, 45.0)):
        conv.observer.exchanges = exchanges
        assert conv.idle_limit() == expected


# ---------------------------------------------------------------------------
# The session: calls, notices, a mic that comes back
# ---------------------------------------------------------------------------


class _Runtime(__import__("PySide6.QtCore", fromlist=["QObject"]).QObject):
    pass


def _session(tmp_path: Path, **over: Any) -> Any:
    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp_path / "sessions", requests_csv=tmp_path / "r.csv", **over)
    session = VoiceSession(settings, _Runtime(), None, prefs_path=tmp_path / "voice.json")
    session.play_tone = lambda *a: None  # never the real speakers
    return session


def test_wake_word_stays_on_in_a_call_and_a_running_conversation_switches_mode(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.wake_enabled = False  # no real listener in this test
    calls: list[Any] = []
    session._conversation = types.SimpleNamespace(set_call=calls.append)
    session.on_call_status({"call": {"app": "Discord", "status": "recording"}})
    assert session.call_app == "Discord" and calls == ["Discord"]
    session.wake_enabled = True
    assert session._wake_allowed()  # the default now: the name works during calls
    session.wake_enabled = False
    session.on_call_status({"call": None})
    assert session.call_app is None and calls == ["Discord", None]
    old = _session(tmp_path, voice_wake_during_calls=False)
    old.wake_enabled = True
    old.call_active = True
    assert not old._wake_allowed()


def test_failures_are_told_once_a_minute_with_a_tone_and_always_in_the_pill(tmp_path: Path) -> None:
    session = _session(tmp_path)
    played: list[Any] = []
    session.play_tone = lambda pcm, rate, device: played.append((len(pcm), rate, device))
    states: list[tuple[str, str]] = []
    session.state_changed.connect(lambda s, d: states.append((s, d)))
    assert session.notice("Voice can't reach the speech services right now (network?).")
    assert not session.notice("Voice can't reach the speech services right now (network?).")
    assert session.notice("Can't open the microphone: device busy")
    time.sleep(0.2)
    assert len(played) == 2 and all(rate == 24_000 for _n, rate, _d in played)
    assert [s for s, _d in states] == ["error", "error", "error"]


def test_a_failed_start_after_a_wake_is_heard_and_seen(tmp_path: Path) -> None:
    session = _session(tmp_path)
    played: list[Any] = []
    session.play_tone = lambda *a: played.append(a)
    states: list[str] = []
    session.state_changed.connect(lambda s, d: states.append(s))
    session.wake_enabled = False
    session._on_thread_done("error", "Voice needs DEEPGRAM_API_KEY (a Windows user environment variable).")
    time.sleep(0.2)
    assert states[-1] == "error" and len(played) == 1


class _FakeStream:
    fail_opens = 0

    def __init__(self, **kw: Any) -> None:
        if _FakeStream.fail_opens > 0:
            _FakeStream.fail_opens -= 1
            raise OSError("device invalidated")
        self.kw = kw
        self.latency = 0.012

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def close(self) -> None:
        pass


#: PortAudio's WASAPI list as a fake: the headphones connect after PortAudio started, so they
#: appear only once the list is re-read (live 2026-09-26: the tray's "Headphones (WH-XB900N)").
_EARLY = [{"name": "Line 1/2 (4- M-Audio AIR 192 4)", "max_input_channels": 2, "max_output_channels": 0},
          {"name": "Speakers (4- M-Audio AIR 192 4)", "max_input_channels": 0, "max_output_channels": 2},
          {"name": "VG271U (NVIDIA High Definition Audio)", "max_input_channels": 0, "max_output_channels": 2}]
_LATE = _EARLY + [{"name": "Headphones (WH-XB900N)", "max_input_channels": 0, "max_output_channels": 2}]


def _fake_sounddevice(monkeypatch: pytest.MonkeyPatch, *, stale: bool = False) -> dict[str, Any]:
    state: dict[str, Any] = {"refresh": 0, "devices": _EARLY if stale else _LATE, "opened": []}
    sd = types.ModuleType("sounddevice")

    def query_devices(i: int | None = None) -> Any:
        rows = [{**d, "hostapi": 0, "default_samplerate": 48000.0} for d in state["devices"]]
        return rows if i is None else rows[i]

    sd.query_hostapis = lambda i=None: ({"name": "Windows WASAPI", "default_input_device": 0,
                                         "default_output_device": 1} if i is not None
                                        else [{"name": "Windows WASAPI"}])
    sd.query_devices = query_devices

    class Stream(_FakeStream):
        def __init__(self, **kw: Any) -> None:
            super().__init__(**kw)
            state["opened"].append((kw.get("device"), "out" if "extra_settings" in kw and "callback" not in kw
                                    else "in"))

        def write(self, pcm: bytes) -> None:
            pass

    def terminate() -> None:
        state["refresh"] += 1
        state["devices"] = _LATE

    sd.InputStream = Stream
    sd.RawOutputStream = Stream
    sd.WasapiSettings = lambda **kw: None
    sd.default = types.SimpleNamespace(device=(0, 1))
    sd._terminate = terminate
    sd._initialize = lambda: None
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    monkeypatch.setitem(sys.modules, "soxr", types.SimpleNamespace(ResampleStream=lambda *a, **k: None))
    return state


def test_mic_reopens_on_a_stale_device_list(monkeypatch: pytest.MonkeyPatch) -> None:
    """After a device change or sleep PortAudio's list can be stale: it is re-read and the open retried."""
    from yuki.voice.audio import SharedMic

    state = _fake_sounddevice(monkeypatch)
    _FakeStream.fail_opens = 2  # auto-convert and the resampled fallback both fail on the stale list
    mic = SharedMic("Line 1/2 (4- M-Audio AIR 192 4)")
    facts = mic.open()
    assert mic.is_open and facts.get("devices_refreshed") and state["refresh"] == 1
    assert facts["channels"] == 2 and facts["input_latency_ms"] == 12.0
    mic.close()


def test_speaker_opens_the_chosen_device_by_its_index(monkeypatch: pytest.MonkeyPatch) -> None:
    from yuki.voice.audio import Speaker

    state = _fake_sounddevice(monkeypatch)
    speaker = Speaker(24_000, "Headphones (WH-XB900N)")
    facts = speaker.open()
    assert facts["found"] and facts["device"] == "Headphones (WH-XB900N)" and facts["index"] == 3
    assert state["opened"][-1][0] == 3  # the output stream was opened on that device, not the default
    speaker.close()


def test_a_speaker_connected_after_start_is_opened_after_rereading_the_list(monkeypatch: pytest.MonkeyPatch,
                                                                            tmp_path: Path) -> None:
    """The live bug of 2026-09-26: the tray chose the headphones, PortAudio's list predated them,
    and the voice played on the M-Audio speakers. Now the list is re-read (the mic's stream closes
    for a moment, keeping its audio and its listeners) and the headphones open."""
    from yuki.voice.audio import SharedMic

    state = _fake_sounddevice(monkeypatch, stale=True)
    session = _session(tmp_path)
    session.set_device("render", "Headphones (WH-XB900N)")
    session._resolve = lambda kind: "Headphones (WH-XB900N)" if kind == "render" else None  # Core Audio has them
    mic = SharedMic(None)
    session._mic = mic
    mic.open()
    heard: list[float] = []
    mic.subscribe(lambda pcm, at: heard.append(at))
    mic.feed(np.zeros(320, np.float32), 1.0)
    speaker, facts = session._open_output(session._resolve("render"))
    assert facts["found"] and facts["refreshed"] and facts["device"] == "Headphones (WH-XB900N)"
    assert state["opened"][-1][0] == 3 and state["refresh"] == 1
    assert mic.is_open and mic.backlog(0.0) and heard == [1.0]  # the wake word's audio and listener survived
    mic.feed(np.zeros(320, np.float32), 1.02)
    assert heard == [1.0, 1.02]
    assert session._output_note(facts) == ""
    speaker.close()
    mic.close()


def test_a_missing_speaker_plays_on_the_default_and_says_so(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    state = _fake_sounddevice(monkeypatch, stale=True)
    state["devices"] = _EARLY
    monkeypatch.setattr("yuki.voice.audio.refresh_devices", lambda: True)  # the re-read finds nothing new
    session = _session(tmp_path)
    speaker, facts = session._open_output("Headphones (WH-XB900N)")
    assert not facts["found"] and facts["device"] == "Speakers (4- M-Audio AIR 192 4)"
    assert "Couldn't open 'Headphones (WH-XB900N)'" in session._output_note(facts)
    assert "Speakers (4- M-Audio AIR 192 4)" in session._output_note(facts)
    speaker.close()


@pytest.mark.parametrize("stale, opened", [(False, "Headphones (WH-XB900N)"), (True, "Speakers (4- M-Audio AIR 192 4)")])
def test_echo_mode_is_chosen_for_the_device_that_really_plays(monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
                                                              stale: bool, opened: str) -> None:
    """The headphone check reads the device the audio really plays on, not the tray's wish."""
    import yuki.voice.audio as audio
    import yuki.voice.pipeline as pipeline

    state = _fake_sounddevice(monkeypatch, stale=stale)
    if stale:
        monkeypatch.setattr(audio, "refresh_devices", lambda: True)  # re-read, still not there
    checked: list[Any] = []
    monkeypatch.setattr(audio, "headphones_check",
                        lambda known=(), device=None: (checked.append(device) or (device or "").startswith("Head"),
                                                       {"device": device}))

    def stop_here(*a: Any, **k: Any) -> Any:
        raise pipeline.VoiceUnavailable("stop here")

    monkeypatch.setattr(pipeline, "preflight", stop_here)
    session = _session(tmp_path, voice_wake_chime=False)
    session._resolve = lambda kind: "Headphones (WH-XB900N)" if kind == "render" else None
    session.mic_factory = lambda device: types.SimpleNamespace(
        is_open=True, device_used="Line 1/2 (4- M-Audio AIR 192 4)", input_latency_s=0.01, set_echo=lambda g: None,
        stats=types.SimpleNamespace(levels=lambda: {}), opened_as="x", open=lambda: {},
        close=lambda keep_backlog=False: None)
    session._thread_main("s", "tray", time.monotonic(), time.monotonic(), None)
    assert checked == [opened]
    assert (session._guard is None) == (opened.startswith("Head"))  # headphones: full duplex; speakers: a guard


def test_the_tray_speaker_moves_a_running_conversation(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import yuki.voice.audio as audio
    from yuki.voice.audio import SharedMic
    from yuki.voice.offline import Room, RoomSpeaker

    monkeypatch.setattr(audio, "headphones_check",
                        lambda known=(), device=None: ((device or "").startswith("Head"), {"device": device}))
    session = _session(tmp_path)
    notes: list[tuple[str, str]] = []
    session.state_changed.connect(lambda s, d: notes.append((s, d)))
    session._resolve = lambda kind: "Headphones (WH-XB900N)" if kind == "render" else None
    session._mic = mic = SharedMic(None)
    mic.device_used = "Line 1/2 (4- M-Audio AIR 192 4)"
    speaker = RoomSpeaker(24_000, "Speakers (4- M-Audio AIR 192 4)", room=Room())
    speaker.open()
    session._speaker = speaker
    session._guard = object()
    mic.set_echo(session._guard)
    session._switch_output()
    assert speaker.device == "Headphones (WH-XB900N)" and session.speaker_used == "Headphones (WH-XB900N)"
    assert mic.echo is None and session._guard is None  # headphones now: full duplex
    APP.processEvents()
    assert notes and notes[-1][0] == "note" and "Headphones (WH-XB900N)" in notes[-1][1]
    assert "mic:" in notes[-1][1]  # the mic stays as chosen, and the note says which it is


def test_a_mic_that_would_not_reopen_is_retried_until_it_does(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.wake_enabled = False
    session._thread = types.SimpleNamespace(is_alive=lambda: True)  # a conversation holds the mic

    class Mic:
        def __init__(self, _device: Any) -> None:
            self.device, self.is_open, self.fail, self.opens = None, False, 0, 0
            self.stats = types.SimpleNamespace(blocks=0)
            self.device_used = "Mic"

        def open(self) -> dict[str, Any]:
            if self.fail:
                self.fail -= 1
                raise OSError("device not ready after resume")
            self.is_open = True
            self.opens += 1
            return {}

        def close(self) -> None:
            self.is_open = False

    session.mic_factory = Mic
    mic = session._ensure_mic()
    mic.fail = 2
    session._on_devices_seen({"devices": {"capture": ["Mic"], "render": []}, "stalled": True})
    assert not mic.is_open  # the reopen after the stall failed
    session._watch_devices()
    assert not mic.is_open  # still not ready
    session._watch_devices()
    assert mic.is_open and mic.opens == 2  # back, without anyone touching anything
    session._thread = None
    session._device_timer.stop()


def test_a_wake_during_teardown_starts_the_next_conversation(tmp_path: Path) -> None:
    session = _session(tmp_path)
    session.wake_enabled = True
    session._listener = types.SimpleNamespace(resume=lambda: None, pause=lambda: None, running=True)
    started: list[Any] = []
    session.start_conversation = lambda via, **kw: started.append((via, kw))  # type: ignore[method-assign]
    session._apply_wake = lambda: None  # type: ignore[method-assign]
    session._thread = types.SimpleNamespace(is_alive=lambda: True)
    session._on_wake_heard(5.0, time.monotonic())
    assert session._pending_wake is None  # mid-conversation: the mouth hears the name itself
    session._on_closing("idle")
    session._on_wake_heard(6.0, time.monotonic())
    assert session._pending_wake is not None
    session._thread = None
    session._on_thread_done("idle", "")
    assert started and started[0][0] == "wake" and started[0][1]["wake_at"] == 6.0


def test_pill_keeps_an_error_up_and_wide_enough_to_read() -> None:
    code = (
        "import os; os.environ['QT_QPA_PLATFORM']='offscreen'\n"
        "from PySide6.QtWidgets import QApplication; app = QApplication([])\n"
        "from yuki.voice.indicator import VoicePill, PILL_WIDTH\n"
        "p = VoicePill(); msg = \"Voice can't reach the speech services right now (network?).\"\n"
        "p.show_state('error', msg); w = p.width()\n"
        "p.show_state('wake', ''); app.processEvents()\n"
        "print(p.isVisible() and not p.closing, w > PILL_WIDTH + 40,\n"
        "      msg in p.toolTip() and p.label.text().startswith(\"Voice can't reach\"))\n"
    )
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)
    assert out.stdout.strip().splitlines()[-1] == "True True True", out.stderr[-800:]


# ---------------------------------------------------------------------------
# The deferred transcriber: present runs
# ---------------------------------------------------------------------------


class _Store:
    def __init__(self) -> None:
        self.call_open = False
        self.path = Path("memory.db")

    def call_chunks(self) -> list[dict[str, Any]]:
        return [{"call_status": "ended", "audio_s": 60.0, "call_id": 20, "id": 1}]

    def calls(self, **kw: Any) -> list[Any]:
        return [object()] if self.call_open else []

    def sweep_spool(self, **kw: Any) -> list[Any]:
        return []

    def wake(self) -> None:
        pass


class _Proc:
    pid = 4242

    def __init__(self) -> None:
        self.code: int | None = None

    def poll(self) -> int | None:
        return self.code

    def kill(self) -> None:
        self.code = -9


def _scheduler(**facts: Any) -> Any:
    from yuki.memory.transcribe import DeferConfig, TranscribeScheduler

    state = {"fullscreen": False, "busy": False, "idle": 3.0, "locked": False, "gpu": 8, "free": 4000,
             "cpu": 10.0, "night": False, "procs": {1908: 6.0}, **facts}
    store = _Store()
    logged: list[tuple[str, dict]] = []
    spawned: list[Any] = []

    def spawn(argv: list[str]) -> Any:
        proc = _Proc()
        spawned.append(proc)
        return proc

    sched = TranscribeScheduler(
        store, db_path=Path("memory.db"), log=lambda t, **f: logged.append((t, f)),
        live_fn=lambda: {"fullscreen": state["fullscreen"]}, idle_fn=lambda: state["idle"],
        locked_fn=lambda: state["locked"],
        gpu_fn=lambda: {"gpu_util": state["gpu"], "vram_used_mb": 6144 - state["free"], "vram_total_mb": 6144},
        cpu_fn=lambda: state["cpu"], busy_fn=lambda: state["busy"],
        now_fn=lambda: __import__("datetime").datetime(2026, 9, 26, 3 if state["night"] else 19, 0),
        spawn=spawn, config=DeferConfig(), procs_fn=lambda: state["procs"], family_fn=lambda pid: {pid, pid + 1},
    )
    sched._signal_stop = lambda on: None
    return sched, state, store, logged, spawned


def _observe(sched: Any, minutes: float = 4.0) -> float:
    now = 0.0
    while now < minutes * 60:
        sched.step(now)
        now += 60.0
    return now


def test_transcriber_runs_while_the_user_is_present_when_the_pc_is_free() -> None:
    sched, state, _store, logged, spawned = _scheduler()
    _observe(sched)
    assert len(spawned) == 1
    assert [f["why"] for t, f in logged if t == "call_transcribe_start"] == ["present"]


@pytest.mark.parametrize("blocker, fact, value", [
    ("fullscreen", "fullscreen", True), ("fullscreen", "busy", True), ("gpu_busy", "gpu", 70),
    ("vram_low", "free", 1500), ("cpu_busy", "cpu", 80.0),
])
def test_transcriber_waits_for_a_free_pc(blocker: str, fact: str, value: Any) -> None:
    sched, state, _store, logged, spawned = _scheduler(**{fact: value})
    _observe(sched)
    assert not spawned and ("call_transcribe_waiting", blocker) in [(t, f.get("reason")) for t, f in logged]


def test_transcriber_waits_during_a_call_and_stops_when_one_starts() -> None:
    sched, state, store, logged, spawned = _scheduler()
    store.call_open = True
    _observe(sched)
    assert not spawned
    store.call_open = False
    now = _observe(sched, 8.0)
    assert len(spawned) == 1
    store.call_open = True
    sched.step(now + 5)
    assert sched._stopping == "call_started"


def test_a_game_in_a_window_stops_a_present_run() -> None:
    sched, state, _store, _logged, spawned = _scheduler()
    now = _observe(sched)
    assert spawned
    state["procs"] = {4243: 97.0, 1908: 5.0}  # the worker's child is busy: that is its own work
    for i in range(4):
        sched.step(now + 5 * i)
    assert sched._stopping is None
    state["procs"] = {4243: 90.0, 7777: 85.0}  # something else takes the GPU
    for i in range(4, 12):
        sched.step(now + 5 * i)
    assert sched._stopping == "gpu_contended"


def test_an_away_run_carries_on_when_the_user_comes_back() -> None:
    sched, state, _store, logged, spawned = _scheduler(idle=600.0)
    now = _observe(sched)
    assert [f["why"] for t, f in logged if t == "call_transcribe_start"] == ["away"]
    state["idle"] = 2.0
    sched.step(now + 5)
    assert sched._stopping is None and sched._run_why == "present"
    assert ("call_transcribe_continues", {"was": "away", "now": "present"}) in logged


def test_without_present_runs_the_old_gates_hold() -> None:
    from dataclasses import replace

    from yuki.memory.transcribe import DeferConfig

    sched, state, _store, logged, spawned = _scheduler()
    sched._config = replace(DeferConfig(), present=False)
    _observe(sched)
    assert not spawned and ("call_transcribe_waiting", "user_present") in [(t, f.get("reason")) for t, f in logged]
    state["night"] = True
    _observe(sched, 6.0)
    assert [f["why"] for t, f in logged if t == "call_transcribe_start"] == ["night"]
