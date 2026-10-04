"""Voice v3.16 (docs/research/revamp-2026-10-02/04 §6 F1-F7, 07 §6 A-B): calls that never end on an owed answer, the
end cue, the stop record, a stall-proof idle clock, TTS resilience, listening off made visible, device identity, the
owed-output ledger and the marker speech channel. Offline only: fakes and stand-ins, no microphone, no speakers, no
network beyond localhost fakes.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PySide6.QtCore import QCoreApplication

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def _no_real_audio_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 7: no test here may open a real microphone or speaker. Any sounddevice stream fails the test loudly."""
    import sounddevice

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a test tried to open a real audio device")

    for name in ("InputStream", "RawInputStream", "OutputStream", "RawOutputStream", "Stream", "RawStream"):
        monkeypatch.setattr(sounddevice, name, refuse)


class Log:
    """A content-free voice log stand-in that keeps every event."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.log_dir = None

    def event(self, event_type: str, **fields: Any) -> dict[str, Any]:
        self.events.append((event_type, fields))
        return fields

    def turn(self, row: dict[str, Any]) -> None:
        self.events.append(("turn", row))

    def of(self, kind: str, **match: Any) -> list[dict[str, Any]]:
        return [f for k, f in self.events if k == kind and all(f.get(a) == b for a, b in match.items())]


def _conversation(tmp_path: Path, *, call: bool = False, bridge: Any = None, voice_activity: Any = None,
                  **over: Any) -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    settings = Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", voice_priority_boost=False, **over)
    bridge = bridge or SimpleNamespace(has_open_voice_job=lambda: False, not_heard=None, open_voice_jobs=lambda: 0,
                                       closing=False, busy_tools=0)
    log = Log()
    conv = Conversation(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=MouthMemory(),
                        log=log, speech_end=lambda: None, on_state=lambda *a: None, session="t",
                        call_app="Discord" if call else None, voice_activity=voice_activity)
    queued: list[Any] = []

    async def queue_frame(frame: Any) -> None:
        queued.append(frame)

    conv.worker = SimpleNamespace(queue_frame=queue_frame)
    tones: list[str] = []
    conv.on_tone = tones.append
    return conv, queued, tones, log


class Runner:
    cancelled = False

    async def cancel(self) -> None:
        self.cancelled = True


def _turn(**kw: Any) -> Any:
    from yuki.voice.pipeline import _Turn

    now = time.monotonic()
    base = dict(n=3, trigger="user", opened=now - 2, user_text="Open Chrome.", transcript_at=now - 1.5)
    base.update(kw)
    return _Turn(**base)


# -- F1: a call never ends while the user is owed an answer -----------------------------------------------------

def test_a_turn_heard_only_as_a_breath_of_silence_is_unanswered(tmp_path: Path) -> None:
    """Live 2 Oct 21:24:08: the guard dropped "Open Chrome."'s reply, the 20 ms breath set first_audio, the rerun
    never ran. Answered now means words that left the speaker."""
    conv, _queued, _tones, _log = _conversation(tmp_path)
    now = time.monotonic()
    turn = _turn(responses=1, guarded=2, first_audio=now - 1.0, audible=[" "])
    conv.observer.turn = turn
    assert conv._unanswered() is turn
    turn.audible = ["Opening Chrome. "]
    assert conv._unanswered() is None
    for answered in (_turn(responses=1, tools=["open_app"]), _turn(responses=1, tools=["stay_quiet"]),
                     _turn(responses=1, marker="quiet"), _turn(responses=1, gave_up=True)):
        conv.observer.turn = answered
        assert conv._unanswered() is None


def test_the_unheard_turn_is_answered_again_once_then_the_error_tone_and_one_more_window(tmp_path: Path) -> None:
    conv, queued, tones, log = _conversation(tmp_path)
    conv.REPLY_WATCH_AFTER_S, conv.REPLY_WATCH_QUIET_S = 0.05, 0.05
    now = time.monotonic()
    turn = _turn(responses=1, first_audio=now - 1.0, transcript_at=now - 1.0)
    conv.observer.turn = turn
    conv.observer.last_user_at = now - 1.0

    async def main() -> None:
        watch = asyncio.create_task(conv._reply_watch(every_s=0.02))
        await asyncio.sleep(0.3)
        assert [type(f).__name__ for f in queued] == ["LLMRunFrame"] and turn.rerun
        assert conv.observer.rerun_turn is turn and tones == []
        from pipecat.frames.frames import (
            BotStartedSpeakingFrame, BotStoppedSpeakingFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame,
            LLMTextFrame,
        )

        # the rerun's reply stays in the user's turn and is a mark alone again: only the voice's breath plays
        conv.observer.on_frame(LLMFullResponseStartFrame())
        assert conv.observer.turn is turn
        conv.observer.on_frame(LLMTextFrame(text="[smiles]"))
        conv.observer.on_frame(LLMFullResponseEndFrame())
        conv.observer.on_frame(BotStartedSpeakingFrame())
        conv.observer.on_frame(BotStoppedSpeakingFrame())
        assert conv.observer.exchanges == 0   # the breath is no exchange
        await asyncio.sleep(0.3)
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)

    asyncio.run(main())
    assert len(queued) == 1 and turn.gave_up and tones == ["error"]
    assert conv._grace_since is not None and conv._unanswered() is None
    assert log.of("reply_watch", op="gave_up")


def test_a_call_conversation_stays_open_while_a_turn_is_unanswered_or_a_held_hand_waits(tmp_path: Path) -> None:
    for case in ("unanswered", "held"):
        bridge = SimpleNamespace(open_voice_jobs=lambda: 0, closing=False, busy_tools=0, held_call=None, held_at=None)
        conv, _queued, _tones, _log = _conversation(tmp_path, call=True, bridge=bridge, voice_call_idle_s=0.3)
        runner = Runner()

        async def main() -> None:
            conv._loop = asyncio.get_running_loop()
            conv.stt_ready_at = time.monotonic()
            if case == "unanswered":
                conv.observer.turn = _turn(responses=1)
            else:
                bridge.held_call = ("window", {}, None)
                bridge.held_at = time.monotonic()
            watch = asyncio.create_task(conv._watch(runner))
            await asyncio.sleep(1.2)
            assert not runner.cancelled, case   # 4x the idle window: still open
            if case == "unanswered":
                conv.observer.turn.audible = ["Opening Chrome. "]
            else:
                conv.observer.last_user_at = time.monotonic()   # they answered: the next turn decides
            conv._on_activity()
            await asyncio.wait_for(watch, timeout=3.0)
            assert runner.cancelled

        asyncio.run(main())
        assert conv.reason == "call_idle", case


def test_call_idle_never_fires_while_the_mic_hears_them_talking(tmp_path: Path) -> None:
    """Live 2 Oct 21:28:00 the session closed on "Hello?", before Flux reported the start of the turn."""
    voiced = {"since": 0.0, "last": 0.0}
    conv, _q, _t, _log = _conversation(tmp_path, call=True, voice_call_idle_s=0.3,
                                       voice_activity=lambda: (voiced["since"], voiced["last"]))
    runner = Runner()

    async def main() -> float:
        conv._loop = asyncio.get_running_loop()
        conv.stt_ready_at = time.monotonic()
        watch = asyncio.create_task(conv._watch(runner))
        talking_until = time.monotonic() + 1.0
        voiced["since"] = time.monotonic()
        while time.monotonic() < talking_until:   # a sentence with short gaps: the voiced level, measured
            voiced["last"] = time.monotonic()
            if time.monotonic() - voiced["since"] > 1.5:
                voiced["since"] = time.monotonic()
            await asyncio.sleep(0.05)
        assert not runner.cancelled
        await asyncio.wait_for(watch, timeout=3.0)
        return time.monotonic() - talking_until

    after = asyncio.run(main())
    assert runner.cancelled and after < 1.5


# -- F2: the end cue -----------------------------------------------------------------------------------------

def _session(tmp_path: Path, **over: Any) -> Any:
    from PySide6.QtCore import QObject

    from yuki.voice.session import VoiceSession

    settings = Settings(log_dir=tmp_path / "sessions", requests_csv=tmp_path / "r.csv", voice_standby=False, **over)
    session = VoiceSession(settings, QObject(), None, prefs_path=tmp_path / "voice.json")
    played: list[bytes] = []
    session.play_tone = lambda pcm, rate, device: played.append(pcm)
    session.wake_enabled = False   # no real listener
    # Never a real device (rule 7): a pretend mic, no wake listener, no device watch, no standby.
    session.mic_factory = _FakeMic
    session._ensure_listener = lambda: None
    session._stop_listener = lambda: None
    session._arm_standby = lambda delay_ms: None
    session.devices_source = lambda: {"capture": [], "render": [], "default_capture": None, "default_render": None}
    return session, played


class _FakeMic:
    """A microphone that never opens a device."""

    def __init__(self, device: Any = None) -> None:
        self.device, self.is_open, self.device_used = device, False, "Mic"
        self.stats = SimpleNamespace(blocks=0, overflows=0, last_voiced=0.0, voiced_since=0.0, levels=lambda: {})

    def open(self) -> dict[str, Any]:
        self.is_open = True
        return {}

    def close(self, **_kw: Any) -> None:
        self.is_open = False

    def set_echo(self, guard: Any) -> None:
        pass


def _wait_for(cond: Any, s: float = 2.0) -> bool:
    end = time.monotonic() + s
    while time.monotonic() < end:
        APP.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def test_a_conversation_that_ended_by_itself_says_so_with_a_tone_and_a_line(tmp_path: Path) -> None:
    from yuki.voice.echo import tone
    from yuki.voice.session import ENDED_NOT_LISTENING_NOTE, ENDED_NOTE

    session, played = _session(tmp_path)
    states: list[tuple[str, str]] = []
    session.state_changed.connect(lambda s, d: states.append((s, d)))
    session._wake_allowed = lambda: True
    for reason, exchanges, want_tone in (("call_idle", 2, True), ("idle", 1, True), ("hotkey", 3, False),
                                         ("user_asked", 2, False), ("tray", 1, False), ("call_idle", 0, False)):
        played.clear()
        states.clear()
        session._last_end = {"reason": reason, "exchanges": exchanges}
        session._on_thread_done(reason, "")
        assert _wait_for(lambda: bool(played)) == want_tone, (reason, exchanges)
        assert (("ended", ENDED_NOTE) in states) == want_tone, (reason, exchanges)
    assert played[-1] == tone("ended") if played else True
    # the tone is the user's choice; the line shows either way
    session.set_end_tone(False)
    assert json.loads((tmp_path / "voice.json").read_text())["end_tone"] is False
    played.clear()
    states.clear()
    session._last_end = {"reason": "call_idle", "exchanges": 1}
    session._on_thread_done("call_idle", "")
    assert not _wait_for(lambda: bool(played), 0.3) and ("ended", ENDED_NOTE) in states
    session._wake_allowed = lambda: False   # the name is not listened for: the line says that instead
    states.clear()
    session._on_thread_done("call_idle", "")
    assert ("ended", ENDED_NOT_LISTENING_NOTE) in states


def test_the_end_tone_is_soft_falling_and_never_speech() -> None:
    import numpy as np

    from yuki.voice.echo import TONES, tone

    pcm = np.frombuffer(tone("ended"), dtype="<i2").astype(float)
    error = np.frombuffer(tone("error"), dtype="<i2").astype(float)
    notes = TONES["ended"][0]
    assert notes[0][0] > notes[1][0]                     # falling
    assert pcm.max() < error.max() * 0.6                 # softer than the error tone
    assert len(pcm) / 24_000 < 0.5                       # short


# -- F3 + F4: the stop record, and an idle clock that does not count stalls ------------------------------------

def test_the_stop_record_says_why_and_how_it_ended(tmp_path: Path) -> None:
    from yuki.voice.session import _stop_facts

    conv, _q, _t, _log = _conversation(tmp_path, call=True, voice_call_idle_s=0.2)
    runner = Runner()

    async def main() -> None:
        conv._loop = asyncio.get_running_loop()
        conv.stt_ready_at = time.monotonic()
        conv.observer.last_exchange = time.monotonic()
        await asyncio.wait_for(conv._watch(runner), timeout=3.0)

    asyncio.run(main())
    facts = conv.stop_facts
    assert facts["reason"] == "call_idle" and facts["unanswered"] is False and facts["held"] is False
    assert 0.2 <= facts["since_exchange_s"] < 1.5 and facts["stall_ms_in_window"] == 0
    merged = _stop_facts(conv)
    assert "reason" not in merged and {"since_exchange_s", "unanswered", "held", "stall_ms_in_window"} <= set(merged)


@pytest.mark.parametrize("queued_reply", [True, False])
def test_a_stall_with_a_reply_queued_never_ends_the_call(tmp_path: Path, queued_reply: bool) -> None:
    """04 F4: the loop blocks 3 s inside the watch with a reply frame queued just before: no stop. Control: it stops
    once the idle window has passed in loop time (the stall does not count as quiet)."""
    from pipecat.frames.frames import LLMFullResponseStartFrame

    bridge = SimpleNamespace(open_voice_jobs=lambda: 0, closing=False, busy_tools=0)
    conv, _q, _t, _log = _conversation(tmp_path, call=True, bridge=bridge, voice_call_idle_s=1.0)
    runner = Runner()
    blocked = {"done": False}

    def block_once() -> int:
        if not blocked["done"] and time.monotonic() - conv.stt_ready_at > 0.4:
            blocked["done"] = True
            if queued_reply:
                asyncio.get_running_loop().call_soon(conv.observer.on_frame, LLMFullResponseStartFrame())
            time.sleep(3.0)   # the loop stalls (a disk-bound write, a GIL hog)
        return 0

    bridge.open_voice_jobs = block_once

    async def main() -> float:
        conv._loop = asyncio.get_running_loop()
        conv.stt_ready_at = started = time.monotonic()

        async def tick() -> None:   # something wakes the watch now and then, as frames do
            while not runner.cancelled:
                await asyncio.sleep(0.1)
                conv._on_activity()

        ticker = asyncio.create_task(tick())
        watch = asyncio.create_task(conv._watch(runner))
        done, _ = await asyncio.wait({watch}, timeout=6.0)
        ticker.cancel()
        if not done:
            watch.cancel()
        return time.monotonic() - started

    took = asyncio.run(main())
    if queued_reply:
        assert not runner.cancelled   # the reply that waited out the stall keeps it open
    else:
        assert runner.cancelled and conv.reason == "call_idle"
        assert 1.0 + 3.0 - 0.1 <= took < 1.0 + 3.0 + 1.0, took   # on time in loop time, not cut by the stall
        assert conv.stop_facts["stall_ms_in_window"] >= 2900


# -- F5: TTS resilience --------------------------------------------------------------------------------------

class _Servers:
    """The fake Gemini + Aura with v3.16's knobs: Gemini failures by text, an Aura that drops its first connection."""

    def __new__(cls, *a: Any, **k: Any) -> Any:
        from test_voice_gemini import FakeServers

        class Servers(FakeServers):
            def __init__(self) -> None:
                super().__init__()
                self.fail: set[str] = set()
                self.aura_drops = 0

            async def gemini(self, request: Any) -> Any:
                from aiohttp import web

                body = await request.json()
                text = body["contents"][0]["parts"][0]["text"]
                if text in self.fail:
                    self.requests.append(body["contents"][0]["parts"][0])
                    return web.json_response({"error": {"code": 500, "status": "INTERNAL"}}, status=500)
                request._read_bytes = json.dumps(body).encode()
                return await super().gemini(request)

            async def speak(self, request: Any) -> Any:
                if self.aura_drops > 0:
                    self.aura_drops -= 1
                    await request.read()
                    request.transport.close()   # the server closed the pooled connection: no byte comes back
                    return None
                return await super().speak(request)

        return Servers()


def _tts(fake: Any, events: list, **kw: Any) -> Any:
    from yuki.voice.gemini_tts import service_class
    from yuki.voice.pipeline import _clause_tts

    return _clause_tts(service_class())(
        api_key="fake-key", model="gemini-3.8-flash-lite-tts", voice="Leda", base_url=f"{fake.url}/v1beta",
        fallback_key="fake-dg", fallback_url=f"{fake.url}/v1/speak", log=lambda e, **f: events.append((e, f)),
        pause_frame_processing=False, **kw)


def test_aura_retries_once_on_a_fresh_connection_when_its_pooled_one_was_dropped() -> None:
    from pipecat.frames.frames import ErrorFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import _levels

    async def main() -> None:
        async with _Servers() as fake:
            fake.fail = {"What did I do today?"}   # Gemini fails this piece: Aura says it
            fake.aura_drops = 1                    # and Aura's first try finds its connection closed
            events: list = []
            tts = _tts(fake, events)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("What did I do today?"), LLMFullResponseEndFrame(),
                      SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert _levels(down) == [777]                                   # heard, on the retry
            assert [f["error"] for e, f in events if e == "tts_retry"] == ["ServerDisconnectedError"]
            assert not [f for f in down if isinstance(f, ErrorFrame)] and tts.stats["aura_retries"] == 1

    asyncio.run(main())


def test_a_slow_first_byte_is_hedged_with_aura_and_the_next_piece_tries_gemini_again() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import _levels

    async def main() -> None:
        async with _Servers() as fake:
            fake.delays = {"Opening Chrome.": 1.2}   # past the hedge (0.3 s), short of Gemini's own timeout (3 s)
            events: list = []
            tts = _tts(fake, events, hedge_s=0.3)
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("Opening Chrome. "), SleepFrame(1.0),
                      LLMTextFrame("It's up."), LLMFullResponseEndFrame(), SleepFrame(1.5)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert fake.aura == ["Opening Chrome."]                          # Aura won the first piece
            assert [p["text"] for p in fake.requests] == ["Opening Chrome.", "It's up."]   # not sticky
            assert _levels(down) == [777, 1000 + 37 * 8]                      # both heard, in order
            assert [f.get("winner") for e, f in events if e == "tts_hedge" and f.get("op") == "winner"] == ["aura"]
            assert fake.cancelled >= 1                                         # the slower Gemini was cancelled

    asyncio.run(main())


def test_two_gemini_failures_open_the_breaker_and_later_pieces_go_straight_to_aura() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with _Servers() as fake:
            fake.fail = {"One.", "Two."}
            events: list = []
            tts = _tts(fake, events, breaker=(2, 60.0, 60.0))
            frames = []
            for text in ("One.", "Two.", "Three, now."):   # three replies, each said once the one before failed
                frames += [LLMFullResponseStartFrame(), LLMTextFrame(text), LLMFullResponseEndFrame(), SleepFrame(0.5)]
            frames.append(SleepFrame(0.8))
            await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert [p["text"] for p in fake.requests] == ["One.", "Two."]    # the third never waited on Gemini
            assert fake.aura == ["One.", "Two.", "Three, now."]
            assert [f["state"] for e, f in events if e == "tts_breaker"] == ["open"]

    asyncio.run(main())


def test_a_marks_only_reply_ends_with_a_breath_and_no_error() -> None:
    """Live 2 Oct 21:27:08/21:27:53: a reply of one mark raised "completed with no audio" in red."""
    from pipecat.frames.frames import (
        ErrorFrame, LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSAudioRawFrame,
    )
    from pipecat.tests.utils import SleepFrame, run_test

    async def main() -> None:
        async with _Servers() as fake:
            events: list = []
            silent: list[int] = []
            tts = _tts(fake, events)
            tts._yuki_on_silent = silent.append
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("[smiles]"), LLMFullResponseEndFrame(),
                      SleepFrame(0.8)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert not [f for f in down if isinstance(f, ErrorFrame)]
            assert [f for f in down if isinstance(f, TTSAudioRawFrame)]        # the breath
            assert silent == [0] and not fake.requests                          # nothing with words was lost

    asyncio.run(main())


def test_the_pill_shows_an_error_only_for_lost_words_and_never_pipecats_text() -> None:
    from pipecat.frames.frames import ErrorFrame, LLMTextFrame, TranscriptionFrame

    from yuki.voice.pipeline import SILENT_REPLY_NOTE, TurnObserver

    states: list[tuple[str, str]] = []
    obs = TurnObserver(settings=Settings(), log=None, memory=None, session="t", speech_end=lambda: None,
                       on_state=lambda s, d: states.append((s, d)), tts_provider="deepgram", activity=lambda: None)
    obs.on_frame(TranscriptionFrame(text="make it a little bit less", user_id="u", timestamp="t"))
    obs.on_frame(LLMTextFrame(text="[sigh]"))
    obs.on_frame(ErrorFrame(error="TTS context e1424606 completed with no audio"))
    assert not [s for s in states if s[0] == "error"]                       # marks alone: nothing to say was lost
    obs.tts_silent(0)
    assert not [s for s in states if s[0] == "error"]
    obs.on_frame(LLMTextFrame(text="Turned it down."))
    obs.on_frame(ErrorFrame(error="TTS context 59f31d04 completed with no audio"))
    errors = [d for s, d in states if s == "error"]
    assert errors == [SILENT_REPLY_NOTE] and "TTS context" not in errors[0]


# -- F6: listening off, visible and scoped ----------------------------------------------------------------------

def test_off_for_this_call_is_never_stored_and_comes_back_when_the_call_ends(tmp_path: Path) -> None:
    session, _played = _session(tmp_path)
    session.wake_enabled = True
    session._ensure_listener = lambda: None   # no real listener or mic
    session._close_mic = lambda: None
    states: list[str] = []
    session.listening_changed.connect(states.append)
    session.set_call_off(True)                          # not in a call: nothing to scope it to
    assert session.call_off is False and session.listening_state == "on"
    session.on_call_status({"call": {"app": "Discord", "evidence": "fresh", "others": 1}})
    session.set_call_off(True)
    assert session.listening_state == "call_off" and not session._wake_allowed() and not session.listening
    prefs = json.loads((tmp_path / "voice.json").read_text()) if (tmp_path / "voice.json").exists() else {}
    assert prefs.get("wake", True) is True and "call_off" not in json.dumps(prefs)   # never written
    session.on_call_status({"call": None})               # the call ended
    assert session.call_off is False and session.listening_state == "on" and session._wake_allowed()
    assert states[-1] == "on" and "call_off" in states
    # "stop listening" said in a call: the talker's this_call -> off for this call
    session.on_call_status({"call": {"app": "Discord", "evidence": "fresh", "others": 1}})
    session._go_quiet(60, True)
    assert session.listening_state == "call_off" and not session.quiet
    session.set_wake(True)                               # turning listening on means now
    assert session.listening_state == "on"
    session.set_wake(False)                              # the permanent off is stored, and shown
    assert json.loads((tmp_path / "voice.json").read_text())["wake"] is False and session.listening_state == "off"


def test_the_talkers_go_quiet_carries_this_call_only_in_a_call() -> None:
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.mouth import CALL_LAYER, GO_QUIET

    assert "this_call" in GO_QUIET["properties"] and "this_call" not in GO_QUIET["required"]
    assert "this_call true" in CALL_LAYER and "{app}" in CALL_LAYER

    async def main() -> list[tuple[float, bool]]:
        asked: list[tuple[float, bool]] = []
        bridge = VoiceBridge(SimpleNamespace(), hands_backend=None)
        bridge.quiet_requested = lambda minutes, this_call=False: asked.append((minutes, this_call))
        bridge.spoke_in_reply = lambda: True
        got: list[Any] = []

        async def result_callback(result: Any, properties: Any = None) -> None:
            got.append(result)

        for in_call in (True, False):
            bridge.call_mode = lambda in_call=in_call: in_call
            await bridge.go_quiet(SimpleNamespace(arguments={"say_first": "Sure.", "minutes": 60, "this_call": True},
                                                  tool_call_id="q", result_callback=result_callback, llm=None))
        return asked

    assert asyncio.run(main()) == [(60.0, True), (60.0, False)]


# -- F7: device identity and recovery -----------------------------------------------------------------------------

def test_a_renamed_endpoint_is_found_by_its_id_and_remembered(tmp_path: Path) -> None:
    from yuki.voice.audio import resolve_device

    devices = {"capture": ["Microphone (AIR 192 4)"], "capture_ids": {"Microphone (AIR 192 4)": "{0.0.1}.{air}"},
               "render": [], "default_capture": "Microphone (AIR 192 4)", "default_render": None}
    assert resolve_device("capture", "Line 1/2 (4- M-Audio AIR 192 4)", devices, wanted_id="{0.0.1}.{air}") == (
        "Microphone (AIR 192 4)", "")
    name, note = resolve_device("capture", "Line 1/2 (4- M-Audio AIR 192 4)", devices)
    assert name is None and "isn't connected" in note
    session, _ = _session(tmp_path)
    session.devices_source = lambda: devices
    (tmp_path / "voice.json").write_text(json.dumps({"mic_device": "Line 1/2 (4- M-Audio AIR 192 4)",
                                                     "mic_device_id": "{0.0.1}.{air}"}))
    assert session._resolve("capture") == "Microphone (AIR 192 4)"
    assert json.loads((tmp_path / "voice.json").read_text())["mic_device"] == "Microphone (AIR 192 4)"
    session.set_device("capture", "Microphone (AIR 192 4)")
    assert json.loads((tmp_path / "voice.json").read_text())["mic_device_id"] == "{0.0.1}.{air}"


def test_the_missing_device_note_shows_once_a_day(tmp_path: Path) -> None:
    session, _ = _session(tmp_path)
    (tmp_path / "voice.json").write_text(json.dumps({"speaker_device": "Airpods (Stereo)"}))
    notes: list[str] = []
    session.state_changed.connect(lambda s, d: notes.append(d) if s == "note" else None)
    for _ in range(3):
        assert session._resolve("render") is None
    assert len(notes) == 1 and "isn't connected" in notes[0]
    again, _ = _session(tmp_path)   # a restart the same day: still once
    again.state_changed.connect(lambda s, d: notes.append(d) if s == "note" else None)
    again._resolve("render")
    assert len(notes) == 1


class _Speaker:
    def __init__(self, fail_switch: bool = False) -> None:
        self.device = "Headphones (WH-XB900N)"
        self.switched: list[Any] = []
        self.writes: list[bytes] = []
        self.fail_switch = fail_switch
        self.timeline = None
        self.latency_s = 0.03

    def switch(self, device: Any, between: Any = None) -> dict[str, Any]:
        self.switched.append(device)
        if self.fail_switch:
            raise OSError("no output device")
        self.device = device or "Speakers (Realtek)"
        return {"device": self.device, "found": True}

    def write(self, pcm: bytes, gate: bool = True) -> None:
        self.writes.append(pcm)


def test_a_speaker_that_disconnects_moves_the_conversation_to_the_default_output(tmp_path: Path,
                                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("yuki.voice.audio.headphones_check", lambda *a, **k: (False, {"form_factor": "speakers"}))
    for fails in (False, True):
        session, played = _session(tmp_path, voice_echo="full")
        speaker = _Speaker(fail_switch=fails)
        session._speaker, session.speaker_used = speaker, "Headphones (WH-XB900N)"
        session._thread = SimpleNamespace(is_alive=lambda: True)
        session._mic = SimpleNamespace(is_open=True, device=None, stats=SimpleNamespace(blocks=1),
                                       set_echo=lambda g: None, device_used="Mic")
        stops: list[str] = []
        session.stop_conversation = lambda reason="user", wait_s=0.0: stops.append(reason)
        session._output_target = lambda chosen: chosen   # the default output: None
        session._on_devices_seen({"devices": {"capture": [], "render": ["Speakers (Realtek)"]}, "stalled": False})
        assert _wait_for(lambda: bool(speaker.switched) and not session._rerouting)
        if fails:
            assert stops == ["speaker_gone"]
        else:
            assert stops == [] and session.speaker_used == "Speakers (Realtek)"
            assert _wait_for(lambda: bool(played))   # continued, with a tone
        session._thread = None


def test_three_failed_speaker_writes_reopen_it_once_then_give_up() -> None:
    from pipecat.transports.base_transport import TransportParams

    from yuki.voice.transport import SpeakerOutput

    class Broken:
        def __init__(self) -> None:
            self.ok = False

        def write(self, pcm: bytes) -> None:
            if not self.ok:
                raise OSError("stream invalidated")

    for reopen_works in (True, False):
        speaker = Broken()
        out = SpeakerOutput(speaker, TransportParams(audio_out_enabled=True, audio_out_sample_rate=24_000))
        reopened: list[Any] = []
        failed: list[Any] = []

        def reopen(exc: Any) -> bool:
            reopened.append(exc)
            speaker.ok = reopen_works
            return reopen_works

        out.on_write_failing = reopen
        out.on_write_failed = failed.append
        results = [out._write(speaker, b"\0\0") for _ in range(5)]
        assert len(reopened) == 1
        if reopen_works:
            assert results == [False, False, False, True, True] and failed == []
        else:
            assert results == [False] * 5 and len(failed) == 1


# -- A2-A: the owed-output ledger -----------------------------------------------------------------------------

def test_the_ledger_keeps_an_outcome_owed_until_it_is_heard() -> None:
    from yuki.voice.narrator import OwedLedger

    log = Log()
    ledger = OwedLedger(stale_turns=3, log=log)
    t0 = time.monotonic()
    item = ledger.owe(7, "done", "Pages restored.", user_turns=2, now=t0)
    assert ledger.due(t0 + 0.5, last_response_end=0.0, quiet_for=5.0, busy=False) is None   # its run is coming
    ledger.heard(t0 + 1.0, turn_opened=t0 - 5.0, trigger="user")       # words of an older turn: not its run
    assert item.state == "owed"
    assert ledger.due(t0 + 2.0, last_response_end=t0 + 1.5, quiet_for=0.2, busy=False) is None   # not quiet yet
    due = ledger.due(t0 + 2.0, last_response_end=t0 + 1.5, quiet_for=1.0, busy=False)   # its run ended unheard
    assert due is item and ledger.attempt(item, t0 + 2.0) == "code"
    assert ledger.due(t0 + 3.0, last_response_end=t0 + 1.5, quiet_for=5.0, busy=False) is None  # its retry gap
    ledger.heard(t0 + 3.2, turn_opened=t0 + 0.5, trigger="result")
    assert item.state == "heard" and item.via == "code" and log.of("owed", op="heard", via="code")
    # a talker run that is heard settles it without code
    other = ledger.owe(8, "failed", "That didn't work.", user_turns=2, now=t0 + 10)
    ledger.heard(t0 + 12, turn_opened=t0 + 10.5, trigger="result")
    assert other.state == "heard" and other.via == "talker"
    # said twice by code and never heard: shown with a sound
    third = ledger.owe(9, "done", "Sent it.", user_turns=2, now=t0 + 20)
    ledger.quiet_run()
    assert ledger.due(t0 + 20.1, last_response_end=0.0, quiet_for=2.0, busy=False) is third   # it chose silence
    assert [ledger.attempt(third, t0 + 21 + i * 3) for i in range(3)] == ["code", "code", "pill"]
    assert third.state == "shown"
    # moved on: only the stale rule lets one go
    fourth = ledger.owe(10, "done", "Volume's down.", user_turns=2, now=t0 + 40)
    assert ledger.expire(user_turns=4) == [] and ledger.expire(user_turns=5) == [fourth]
    gone = ledger.owe(11, "ask", "Which window?", user_turns=5, now=t0 + 50)
    ledger.drop(11)
    assert gone.state == "dropped" and not ledger.pending


def test_a_result_runs_stay_quiet_is_not_refused_the_ledger_says_it() -> None:
    from yuki.voice.bridge import VoiceBridge
    from yuki.voice.narrator import OwedLedger

    async def main() -> tuple[list, OwedLedger]:
        bridge = VoiceBridge(SimpleNamespace(), hands_backend=None)
        ledger = OwedLedger(stale_turns=3)
        bridge.ledger = ledger
        bridge.user_turn = lambda: False
        bridge.said_owed_at = time.monotonic()
        ledger.owe(1, "done", "Pages restored.", user_turns=1)
        got: list[Any] = []

        async def result_callback(result: Any, properties: Any = None) -> None:
            got.append((result, getattr(properties, "run_llm", None)))

        await bridge.stay_quiet(SimpleNamespace(arguments={}, tool_call_id="q", result_callback=result_callback))
        return got, ledger

    got, ledger = asyncio.run(main())
    assert got[0][1] is False and "not_quiet" not in got[0][0]   # settled without a second talker run
    assert ledger.pending[0].quiet


def test_the_bridge_owes_every_outcome_it_hands_to_the_talker() -> None:
    from test_voice_v39 import Params, _bridge, _settle

    from yuki.voice.narrator import OwedLedger

    async def main() -> OwedLedger:
        rt, bridge = _bridge()
        ledger = OwedLedger(stale_turns=3)
        bridge.ledger = ledger
        bridge.user_turns = lambda: 1
        params = Params("Restore the pages")
        task = asyncio.create_task(bridge.tell_yuki(params))
        await _settle()
        job = rt.submitted[0][0]
        rt.started.emit("worker", job, "x")
        rt.finished.emit("worker", job, "Pages restored.")
        rt.lane_done.emit("worker", job, "x", False, [])
        await _settle()
        await asyncio.wait_for(task, 1)
        return ledger

    ledger = asyncio.run(main())
    assert [(i.kind, i.text) for i in ledger.items] == [("done", "Pages restored.")]


# -- A2-B: the marker speech channel ------------------------------------------------------------------------------

def _feed(text: str, parts: int = 3) -> tuple[str, Any]:
    from yuki.voice.mouth import ReplyChannel

    channel = ReplyChannel()
    step = max(1, len(text) // parts)
    out = "".join(channel.feed(text[i:i + step]) for i in range(0, len(text), step))
    channel.finish()
    return out, channel


def test_only_text_after_a_say_marker_line_is_speech() -> None:
    assert _feed("say\nIt's ten past four.")[0] == "It's ten past four."
    assert _feed("Say:\n\nSure, one sec.", parts=7)[0] == "Sure, one sec."
    assert _feed("say: Opening it.")[0] == "Opening it."            # the inline form
    spoken, channel = _feed("act\n")
    assert spoken == "" and channel.first == "act" and not channel.unmarked
    spoken, channel = _feed("It's ten past four.")                     # no marker line: never spoken
    assert spoken == "" and channel.unmarked and channel.scratch == len("It's ten past four.")
    spoken, channel = _feed("They asked for the time.\nsay\nTen past four.")   # scratch, then the marker
    assert spoken == "Ten past four." and channel.before == len("They asked for the time.")
    spoken, channel = _feed("quiet\nThose words were for the call.")
    assert spoken == "" and channel.first == "quiet" and channel.scratch > 0
    spoken, channel = _feed("Say, that's cool.")                      # a word, not a marker line
    assert spoken == "" and channel.unmarked
    assert _feed("say\n[style: whispering] Got it.")[0] == "[style: whispering] Got it."


def test_its_history_shows_the_format_it_writes_the_same_bytes_every_call() -> None:
    from yuki.voice.mouth import with_markers

    messages = [{"role": "user", "content": "what time is it?"},
                {"role": "assistant", "content": [{"type": "text", "text": "Ten past four."}]},
                {"role": "user", "content": "pause it"},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "a", "name": "media_key", "input": {}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a", "content": "ok"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "b", "name": "stay_quiet", "input": {}}]},
                {"role": "assistant", "content": "Paused."}]
    out = with_markers(messages)
    assert out[1]["content"][0]["text"] == "say\nTen past four."
    assert out[3]["content"][0] == {"type": "text", "text": "act"}
    assert out[5]["content"][0] == {"type": "text", "text": "quiet"}
    assert out[6]["content"] == "say\nPaused."
    assert with_markers(messages) == out and messages[1]["content"][0]["text"] == "Ten past four."   # a copy
    assert with_markers(out) == out                                   # idempotent


def test_the_prompt_and_the_reminder_exist_only_in_marker_mode() -> None:
    from yuki.voice.mouth import (
        MARKER_REMINDER, MOUTH_PROMPT, RERUN_NOTE, SPEECH_CHANNEL_PROMPT, VOICE_MARKS_PROMPT, MouthMemory,
        mouth_prompt, mouth_system_blocks, volatile_block,
    )

    # vfix4: the voice marks ride with a voice that performs them (the default), never with the marker section's place
    assert mouth_prompt("guard", marks=False) == MOUTH_PROMPT and mouth_prompt(None, marks=False) == MOUTH_PROMPT
    assert mouth_prompt("guard") == MOUTH_PROMPT + "\n\n" + VOICE_MARKS_PROMPT
    assert mouth_prompt("marker").endswith(SPEECH_CHANNEL_PROMPT)
    assert SPEECH_CHANNEL_PROMPT not in mouth_system_blocks(MouthMemory(), tail=["t"])[0]["text"]
    assert SPEECH_CHANNEL_PROMPT in mouth_system_blocks(MouthMemory(), tail=["t"], channel="marker")[0]["text"]
    assert volatile_block(channel="marker")["text"].endswith(MARKER_REMINDER)
    assert MARKER_REMINDER not in volatile_block()["text"]
    assert RERUN_NOTE in volatile_block(rerun=True)["text"] and RERUN_NOTE not in volatile_block()["text"]


def _events(text_parts: list[str], tool: str | None = None, args: str = "") -> list[Any]:
    ev: list[Any] = [SimpleNamespace(type="message_start", message=SimpleNamespace(usage=SimpleNamespace(
        input_tokens=1, output_tokens=0, cache_creation_input_tokens=0, cache_read_input_tokens=0)))]
    if text_parts:
        ev.append(SimpleNamespace(type="content_block_start", index=0, content_block=SimpleNamespace(type="text")))
        ev += [SimpleNamespace(type="content_block_delta", index=0, delta=SimpleNamespace(type="text_delta", text=t))
               for t in text_parts]
        ev.append(SimpleNamespace(type="content_block_stop", index=0))
    if tool:
        ev.append(SimpleNamespace(type="content_block_start", index=1,
                                  content_block=SimpleNamespace(type="tool_use", id="toolu_1", name=tool)))
        ev.append(SimpleNamespace(type="content_block_delta", index=1,
                                  delta=SimpleNamespace(type="input_json_delta", partial_json=args)))
        ev.append(SimpleNamespace(type="content_block_stop", index=1))
    ev.append(SimpleNamespace(type="message_delta", delta=SimpleNamespace(stop_reason="tool_use" if tool else
                                                                          "end_turn"),
                              usage=SimpleNamespace(output_tokens=5)))
    return ev


def test_the_talkers_stream_carries_only_speech_downstream_in_marker_mode() -> None:
    """The marker lines and scratch are taken out of the stream before anything downstream sees them: an act reply's
    say_first is still spoken early (no text counted as said), and the reply is reported to the conversation."""
    from pipecat.services.anthropic.llm import AnthropicLLMService

    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import LlmCall, _layered_service

    reports: list[dict[str, Any]] = []

    class Hooks:
        def channel(self) -> str:
            return "marker"

        def marker_report(self, call: Any, channel: Any) -> None:
            reports.append(channel.facts())

        def fallbacks(self) -> list:
            return []

        def sticky_index(self, size: int) -> int:
            return 0

        def model_ok(self, *a: Any) -> None:
            pass

        def acked(self) -> None:
            pass

        def handoff_began(self, *a: Any) -> None:
            pass

        def early_said(self, *a: Any) -> None:
            pass

    async def run(events: list[Any]) -> tuple[list[str], Any, list[Any]]:
        llm = _layered_service(AnthropicLLMService, MouthMemory(), Hooks())(
            api_key="x", settings=AnthropicLLMService.Settings(model="m"))
        pushed: list[Any] = []

        async def push(frame: Any, direction: Any = None) -> None:
            pushed.append(frame)

        llm.push_frame = push

        async def fake_open(api_call: Any, params: Any) -> tuple[Any, Any]:
            async def gen() -> Any:
                for e in events[1:]:
                    yield e
            return gen(), events[0]

        llm._yuki_open = fake_open
        llm._yuki_call = LlmCall(entered=time.monotonic(), user_turn=True)
        stream = await llm._create_message_stream(None, {})
        texts = [e.delta.text async for e in stream if getattr(e, "type", "") == "content_block_delta"
                 and hasattr(e.delta, "text")]
        return texts, llm._yuki_call, pushed

    texts, call, _ = asyncio.run(run(_events(["sa", "y\nIt's ten", " past four."])))
    assert "".join(texts) == "It's ten past four." and call.first_text is not None
    texts, call, pushed = asyncio.run(run(_events(["act", "\n"], tool="open_app",
                                                  args='{"say_first": "Opening Chrome.", "name": "Chrome"')))
    assert texts == [] and call.first_text is None                    # nothing written was speech
    assert [getattr(f, "text", None) for f in pushed] == ["Opening Chrome."]   # its say_first, said early
    texts, call, _ = asyncio.run(run(_events(["I'll open Chrome ", "for them."])))
    assert texts == [] and call.first_text is None
    assert [r["marker"] for r in reports] == ["say", "act", None] and reports[2]["unmarked"]


def test_in_shadow_mode_the_check_scores_and_logs_but_never_blocks() -> None:
    from yuki.voice.speechguard import SpeechGuard

    log = Log()
    drops: list[int] = []

    async def judge(text: str, said: list[str], user: str) -> str:
        return "DROP: reasoning"

    async def main() -> tuple[Any, Any]:
        guard = SpeechGuard(None, "haiku", judge=judge, timeout_s=0.5, log=log, shadow=True)
        guard.on_drop = lambda: drops.append(1)
        line = "They asked for the time, so I'll read it."
        verdict = await guard.decide(guard.start(line, first=True), line)
        allowed = await guard.allows("Busy day: coding on T3 Code most of it.")
        await asyncio.sleep(0.1)
        return verdict, allowed

    verdict, allowed = asyncio.run(main())
    assert verdict.ok and verdict.by == "shadow" and allowed is True and drops == []
    caught = log.of("speech_guard", op="caught", shadow=True)
    assert len(caught) == 2 and all(c["why"] == "reasoning" for c in caught)


def test_the_gemini_player_never_waits_for_a_shadow_verdict() -> None:
    from pipecat.frames.frames import LLMFullResponseEndFrame, LLMFullResponseStartFrame, LLMTextFrame, TTSTextFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service
    from yuki.voice.speechguard import SpeechGuard

    async def judge(text: str, said: list[str], user: str) -> str:
        await asyncio.sleep(2.0)   # a slow verdict: in shadow mode nothing waits for it
        return "DROP: reasoning"

    async def main() -> None:
        async with FakeServers() as fake:
            tts = _service(fake)
            tts.set_guard(SpeechGuard(None, "haiku", judge=judge, timeout_s=2.5, shadow=True))
            frames = [LLMFullResponseStartFrame(), LLMTextFrame("Busy day: coding on T3 Code, then Valorant."),
                      LLMFullResponseEndFrame(), SleepFrame(1.0)]
            down, _ = await run_test(tts, frames_to_send=frames, start_timeout=10)
            assert _levels(down) and [f for f in down if isinstance(f, TTSTextFrame)]   # heard within 1 s

    asyncio.run(main())


def test_an_owed_line_said_by_code_is_never_held_by_a_blocking_guard() -> None:
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.tests.utils import SleepFrame, run_test

    from test_voice_gemini import FakeServers, _levels, _service
    from yuki.voice.speechguard import SpeechGuard

    seen: list[str] = []

    async def judge(text: str, said: list[str], user: str) -> str:
        seen.append(text)
        return "DROP: reasoning"   # the guard's false catch of 2 Oct 21:25:14 ("Busy day:")

    async def main() -> None:
        async with FakeServers() as fake:
            tts = _service(fake)
            tts.set_guard(SpeechGuard(None, "haiku", judge=judge, timeout_s=1.0))
            text = "Busy day: coding on T3 Code about 1h20m, then Valorant."
            tts.unguard(text)
            down, _ = await run_test(tts, frames_to_send=[TTSSpeakFrame(text=text), SleepFrame(1.2)], start_timeout=10)
            assert _levels(down) and seen == []

    asyncio.run(main())
