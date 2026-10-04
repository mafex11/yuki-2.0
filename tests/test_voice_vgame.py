"""vGame (5 Oct 2026, docs/research/revamp-2026-10-02/79-build-vGame.md): "go away" ends at once with no model call,
a request said by name is acknowledged at once, the name alone is answered and never loses the command after it, and in
a game or a call Yuki stays out of the way. Offline only: stand-ins, no microphone, no speakers, no network.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication

from yuki.config import Settings

APP = QCoreApplication.instance() or QCoreApplication([])


@pytest.fixture(autouse=True)
def _no_real_audio_devices(monkeypatch: pytest.MonkeyPatch) -> None:
    """Rule 7: no test here may open a real microphone or speaker."""
    import sounddevice

    def refuse(*_a: Any, **_k: Any) -> None:
        raise AssertionError("a test tried to open a real audio device")

    for name in ("InputStream", "RawInputStream", "OutputStream", "RawOutputStream", "Stream", "RawStream"):
        monkeypatch.setattr(sounddevice, name, refuse)


class Log:
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


class Ack:
    """The acknowledgement lines, as rendered (a stand-in: a short clip per kind)."""

    def pcm(self, kind: str) -> bytes:
        return b"\x10\x00" * (2400 if kind == "request" else 3600)


def _conversation(tmp_path: Path, *, call: bool = False, jobs: int = 0, **over: Any) -> Any:
    from yuki.voice.mouth import MouthMemory
    from yuki.voice.pipeline import Conversation

    settings = Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv", voice_priority_boost=False, **over)
    bridge = SimpleNamespace(has_open_voice_job=lambda: bool(jobs), not_heard=None, open_voice_jobs=lambda: jobs,
                             closing=False, busy_tools=0)
    log = Log()
    conv = Conversation(settings=settings, bridge=bridge, transport=None, memory=None, mouth_memory=MouthMemory(),
                        log=log, speech_end=lambda: None, on_state=lambda *a: None, session="t",
                        call_app="Discord" if call else None, call_evidence="fresh" if call else None)
    sounds: list[tuple[str, int]] = []
    tones: list[str] = []
    dismissed: list[bool] = []
    conv.on_sound = lambda pcm, kind: sounds.append((kind, len(pcm)))
    conv.on_tone = tones.append
    conv.on_dismissed = lambda: dismissed.append(True)
    conv.ack = Ack()
    return conv, log, sounds, tones, dismissed


def _open_turn(conv: Any, text: str, *, started_ago: float = 1.5) -> Any:
    """What the turn observer does with a final transcript (it sees the frame before the fast path does)."""
    obs = conv.observer
    now = time.monotonic()
    obs._user_started_at = now - started_ago
    turn = obs._open("user")
    turn.user_text = text
    turn.transcript_at = now
    turn.speech_end = now - 0.6
    obs.user_turns += 1
    return turn


# -- the dismiss vocabulary ---------------------------------------------------------------------------------------

@pytest.mark.parametrize("words", [
    "Go away.", "Okay okay, go away, go away.", "Nothing. Go away.", "Shut up.", "Yuki, shut up.", "Bye bye.",
    "Go away for now, man.", "Okay. That's all for now. Bye.", "Not you.", "Go away bro", "Dude, I just said go away.",
    "You can go back to sleep.", "Turn off.", "Thank you, bye.", "Stop listening.", "Stop.", "Never mind.",
])
def test_the_users_dismissals_are_recognised(words: str) -> None:
    from yuki.voice.fastpath import dismissal

    assert dismissal(words) is not None


@pytest.mark.parametrize("words", [
    "Tell him to go away.", "Go silent for an hour.", "Stop scrolling.", "What's up?", "No.", "Yeah.", "Okay.",
    "Turn off the lights.", "Oh, never mind. Wrong moment. Sorry.", "Can you stop the music?", "Yuki.",
    "Bro did you see that clutch?", "", "go away " * 8,
])
def test_anything_else_goes_to_the_talker(words: str) -> None:
    from yuki.voice.fastpath import dismissal

    assert dismissal(words) is None


def test_stop_and_cancel_are_the_talkers_while_something_runs() -> None:
    """"Stop" while a page scrolls or a task runs means stop that (the talker stops it); "go away" still ends."""
    from yuki.voice.fastpath import dismissal

    for words in ("Stop.", "Yuki, stop it.", "Cancel.", "Never mind.", "No, leave it."):
        assert dismissal(words) is not None and dismissal(words, busy=True) is None
    assert dismissal("Shut up.", busy=True) == "shut up"
    assert dismissal("Go away.", busy=True) == "go away"


def test_the_name_alone() -> None:
    from yuki.voice.fastpath import bare_name

    assert bare_name("Yuki.") and bare_name("Hey Yuki?") and bare_name("Yuki, Yuki")
    assert not bare_name("Yuki, open Spotify.") and not bare_name("Okay.") and not bare_name("")


def test_the_vocabulary_stays_small() -> None:
    """A command vocabulary, not a classifier: growing it needs a reason (report 76's own words)."""
    from yuki.voice.fastpath import DISMISS_PHRASES, FILLER_WORDS

    assert len(DISMISS_PHRASES) <= 45 and len(FILLER_WORDS) <= 40


def test_trim_and_scale() -> None:
    from yuki.voice.fastpath import scale, trim

    quiet = np.zeros(2400, "<i2")
    loud = (np.sin(np.arange(4800) / 5) * 8000).astype("<i2")
    pcm = np.concatenate([quiet, loud, quiet]).tobytes()
    out = trim(pcm)
    assert 4800 * 2 <= len(out) <= (4800 + 720) * 2
    half = np.frombuffer(scale(loud.tobytes(), -6.0), "<i2")
    assert abs(int(np.abs(half).max()) - int(8000 * 10 ** (-6 / 20))) < 60
    assert trim(quiet.tobytes()) == b""


def test_ack_voice_renders_once_then_reads_the_cache(tmp_path: Path) -> None:
    from yuki.voice.fastpath import AckVoice
    from yuki.voice.ttscache import TTSCache

    calls: list[str] = []
    tone = (np.sin(np.arange(9600) / 4) * 9000).astype("<i2").tobytes()

    def render(text: str) -> bytes:
        calls.append(text)
        return tone

    cache = TTSCache(tmp_path / "c")
    first = AckVoice(provider="deepgram", voice="aura-2-x", cache=cache, render=render)
    first.load()
    assert first.pcm("request") and first.pcm("name") and calls == ["Mm-hm.", "Yeah?"]
    again = AckVoice(provider="deepgram", voice="aura-2-x", cache=TTSCache(tmp_path / "c"), render=render)
    again.load()
    assert again.pcm("request") == first.pcm("request") and len(calls) == 2
    failing = AckVoice(provider="gemini", voice="Leda", cache=TTSCache(tmp_path / "d"),
                       render=lambda _t: (_ for _ in ()).throw(OSError("offline")))
    failing.load()
    assert failing.pcm("request") is None and failing.loaded.is_set()


# -- the fast path in a conversation -------------------------------------------------------------------------------

def test_go_away_ends_on_the_transcript_with_no_model_call_and_is_never_for_others(tmp_path: Path) -> None:
    conv, log, sounds, _tones, dismissed = _conversation(tmp_path, call=True)
    turn = _open_turn(conv, "Go away, dude.")
    assert conv._fast_turn("Go away, dude.", final=True) is True
    assert conv.reason == "user_asked" and conv._stop.is_set() and dismissed == [True]
    assert turn.fast == "dismiss" and not conv.observer.for_others(turn, "")
    assert log.of("fast_path", op="dismiss")[0]["phrase"] == "go away"
    assert log.of("session", state="end_asked", fast=True)
    assert sounds == []   # no acknowledgement on a dismissal


def test_an_eager_dismissal_ends_at_once_and_the_final_words_make_no_second_turn(tmp_path: Path) -> None:
    """Flux's eager end of turn comes ~0.15 s before the final words: a dismissal ends there (closing when they were
    going on costs nothing). Its turn is opened then, and the final transcript only updates its words."""
    from pipecat.frames.frames import TranscriptionFrame

    conv, log, _sounds, _tones, dismissed = _conversation(tmp_path)
    obs = conv.observer
    obs._user_started_at = time.monotonic() - 1.0
    assert conv._fast_turn("Shut up", final=False) is True
    assert conv._stop.is_set() and conv.reason == "user_asked" and dismissed == [True]
    turn = obs.turn
    assert turn.fast == "dismiss" and turn.user_text == "Shut up" and obs.user_turns == 1
    assert log.of("fast_path", op="dismiss")[0]["eager"] is True
    obs.on_frame(TranscriptionFrame(text="Shut up.", user_id="u", timestamp="0"))
    assert obs.turn is turn and turn.user_text == "Shut up." and obs.user_turns == 1
    assert conv._fast_turn("Shut up.", final=True) is True   # swallowed: nothing reaches the talker after it


def test_a_request_said_by_name_is_acknowledged_at_once_and_still_goes_to_the_talker(tmp_path: Path) -> None:
    conv, log, sounds, _tones, _dismissed = _conversation(tmp_path)
    conv.woken_by_name = True
    turn = _open_turn(conv, "Open Spotify.")
    assert conv._fast_turn("Open Spotify.", final=True) is False   # the talker still answers it
    assert sounds == [("request", 4800)] and turn.ack_at is not None and turn.named
    assert log.of("fast_path", op="ack")[0]["by"] == "wake"


def test_the_acknowledgement_plays_on_the_eager_end_of_turn_once(tmp_path: Path) -> None:
    """~0.15 s sooner than the final words; the final transcript neither plays it again nor loses its time."""
    conv, log, sounds, _tones, _dismissed = _conversation(tmp_path)
    conv.woken_by_name = True
    conv.observer._user_started_at = time.monotonic() - 1.0
    assert conv._fast_turn("Open Spotify", final=False) is False
    assert sounds == [("request", 4800)] and log.of("fast_path", op="ack", eager=True)
    turn = _open_turn(conv, "Open Spotify.")
    assert conv._fast_turn("Open Spotify.", final=True) is False
    assert len(sounds) == 1 and turn.ack_at is not None and turn.named
    # the second utterance of the conversation is not "the first words after the name"
    conv.observer._user_started_at = time.monotonic()
    conv._fast_turn("and turn it up", final=False)
    assert len(sounds) == 1


def test_the_name_alone_is_never_acknowledged_on_its_eager_words(tmp_path: Path) -> None:
    conv, _log, sounds, _tones, _dismissed = _conversation(tmp_path)
    conv.woken_by_name = True
    conv._fast_turn("Yuki", final=False)
    assert sounds == []


def test_a_turn_without_the_name_gets_no_acknowledgement(tmp_path: Path) -> None:
    conv, _log, sounds, _tones, _dismissed = _conversation(tmp_path)
    _open_turn(conv, "Open Spotify.")   # not woken by the name, no name heard in it
    assert conv._fast_turn("Open Spotify.", final=True) is False
    assert sounds == []


def test_the_name_heard_in_a_turn_by_the_wake_word_makes_it_said_to_yuki(tmp_path: Path) -> None:
    conv, log, sounds, _tones, _dismissed = _conversation(tmp_path, call=True)
    _open_turn(conv, "Yuki, turn it up a bit.", started_ago=1.5)
    conv._names.append(time.monotonic() - 1.0)   # the spotter heard the name half a second into the turn
    assert conv._fast_turn("Yuki, turn it up a bit.", final=True) is False
    assert sounds and sounds[0][0] == "request" and log.of("fast_path", op="ack")[0]["by"] == "spotter"
    assert conv.observer.turn.named
    # a name heard long before this turn is not this turn's
    conv2, _l, sounds2, _t, _d = _conversation(tmp_path, call=True)
    _open_turn(conv2, "turn it up", started_ago=1.0)
    conv2._names.append(time.monotonic() - 5.0)
    conv2._fast_turn("turn it up", final=True)
    assert sounds2 == []


def test_the_name_alone_is_answered_and_the_next_turn_is_never_dropped_as_for_others(tmp_path: Path) -> None:
    """Report 76: a bare "Yuki" judged as said to someone else 6 times, dropping the command after it."""
    conv, _log, sounds, _tones, _dismissed = _conversation(tmp_path, call=True)
    _open_turn(conv, "Yuki?")
    conv._names.append(time.monotonic() - 0.5)
    assert conv._fast_turn("Yuki?", final=True) is True   # answered here: the talker never judges it
    first = conv.observer.turn
    assert sounds == [("name", 7200)] and first.fast == "name" and not conv.observer.for_others(first, "")
    # the command after it, with no name in it, is said to Yuki: named for the talker, acknowledged, routed
    turn = _open_turn(conv, "Pause the music.")
    assert conv._fast_turn("Pause the music.", final=True) is False
    assert turn.named and sounds[-1][0] == "request"
    from yuki.voice.pipeline import _Hooks

    assert _Hooks(conv).named() is True
    # only the one turn right after it (seconds later: the name the wake word heard is not in this one)
    conv._names.clear()
    third = _open_turn(conv, "lol nice")
    conv._fast_turn("lol nice", final=True)
    assert not third.named


def test_no_acknowledgement_over_yukis_own_voice(tmp_path: Path) -> None:
    conv, _log, sounds, _tones, _dismissed = _conversation(tmp_path)
    conv.woken_by_name = True
    _open_turn(conv, "Open Chrome.")
    conv.observer.bot_speaking = True
    conv._fast_turn("Open Chrome.", final=True)
    assert sounds == []


def test_a_voice_without_rendered_lines_ticks(tmp_path: Path) -> None:
    conv, _log, sounds, tones, _dismissed = _conversation(tmp_path)
    conv.ack = None
    conv.woken_by_name = True
    _open_turn(conv, "Open Chrome.")
    conv._fast_turn("Open Chrome.", final=True)
    assert sounds == [] and tones == ["tick"]
    from yuki.voice.echo import tone

    assert 0 < len(tone("tick")) < 24_000


def test_the_fast_path_can_be_turned_off(tmp_path: Path) -> None:
    conv, _log, sounds, _tones, dismissed = _conversation(tmp_path, voice_fast_dismiss=False, voice_fast_ack=False)
    conv.woken_by_name = True
    _open_turn(conv, "Go away.")
    assert conv._fast_turn("Go away.", final=True) is False and dismissed == [] and sounds == []


def test_the_turn_row_says_named_fast_and_the_ack_time(tmp_path: Path) -> None:
    conv, log, _sounds, _tones, _dismissed = _conversation(tmp_path)
    conv.woken_by_name = True
    _open_turn(conv, "Open Chrome.")
    conv._fast_turn("Open Chrome.", final=True)
    conv.observer.close()
    row = log.of("turn")[0]
    assert row["named"] is True and row["fast"] is None and 0 <= row["eos_to_ack_ms"] < 1000


# -- focus mode: a game or a call ----------------------------------------------------------------------------------

def test_in_a_call_a_turn_judged_not_for_yuki_ends_the_conversation_silently(tmp_path: Path) -> None:
    conv, log, sounds, tones, _dismissed = _conversation(tmp_path, call=True)
    turn = _open_turn(conv, "Bro did you see that clutch?")
    conv._fast_turn(turn.user_text, final=True)
    turn.tools.append("stay_quiet")
    conv.observer._quiet_turn()
    assert conv.reason == "not_for_yuki" and log.of("session", state="focus_quiet_end")
    assert sounds == [] and tones == []


def test_a_game_in_front_is_focus_mode_too_and_a_stale_call_is_not(tmp_path: Path) -> None:
    conv, _log, _s, _t, _d = _conversation(tmp_path)
    assert not conv.focus
    conv.set_game("VALORANT")
    assert conv.focus and conv._prompt_state()["focus"] == {"game": "VALORANT", "call": False}
    stale, *_ = _conversation(tmp_path, call=True)
    stale.call_evidence = "stale"
    assert not stale.focus
    off, *_ = _conversation(tmp_path, call=True, voice_focus_mode=False)
    assert not off.focus


def test_focus_quiet_keeps_the_conversation_while_something_is_owed(tmp_path: Path) -> None:
    conv, log, _s, _t, _d = _conversation(tmp_path, call=True, jobs=1)
    turn = _open_turn(conv, "lol")
    turn.marker = "quiet"
    conv.observer._quiet_turn()
    assert not conv._stop.is_set() and log.of("session", state="focus_quiet_kept")


def test_outside_focus_a_quiet_turn_changes_nothing(tmp_path: Path) -> None:
    conv, _log, _s, _t, _d = _conversation(tmp_path)
    turn = _open_turn(conv, "hmm")
    turn.tools.append("stay_quiet")
    conv.observer._quiet_turn()
    assert not conv._stop.is_set()


def test_the_quiet_hook_fires_from_the_frames_once(tmp_path: Path) -> None:
    from pipecat.frames.frames import FunctionCallInProgressFrame, LLMFullResponseEndFrame

    conv, _log, _s, _t, _d = _conversation(tmp_path, call=True)
    seen: list[int] = []
    conv.observer.on_quiet_turn = lambda turn: seen.append(turn.n)
    turn = _open_turn(conv, "gg")
    conv.observer.on_frame(LLMFullResponseEndFrame())
    conv.observer.on_frame(FunctionCallInProgressFrame(function_name="stay_quiet", tool_call_id="q1", arguments={}))
    conv.observer.on_frame(LLMFullResponseEndFrame())
    assert seen == [turn.n]


def test_focus_shortens_replies_and_quiets_the_voice(tmp_path: Path) -> None:
    from yuki.voice.pipeline import _Hooks

    conv, _log, sounds, _t, _d = _conversation(tmp_path, call=True)
    assert _Hooks(conv).reply_cap() == 18
    plain, *_ = _conversation(tmp_path)
    assert _Hooks(plain).reply_cap() == 45
    out = SimpleNamespace(gain_db=0.0)
    conv.transport = SimpleNamespace(output=lambda: out)
    conv._apply_gain()
    assert out.gain_db == conv.FOCUS_GAIN_DB < 0
    conv.woken_by_name = True
    _open_turn(conv, "Pause the music.")
    conv._fast_turn("Pause the music.", final=True)
    assert sounds and sounds[0][1] == 4800   # the same length, quieter samples


def test_focus_turns_off_step_lines_and_the_router_needs_the_name(tmp_path: Path) -> None:
    conv, log, _s, _t, _d = _conversation(tmp_path, voice_router=True)
    conv.set_game("fullscreen")
    conv.observer._user_started_at = time.monotonic()
    assert conv._step_state()["call_mode"] is True
    turn = _open_turn(conv, "what did I do today")
    call = SimpleNamespace(user_turn=True)
    conv._route_turn(SimpleNamespace(get_messages=lambda: []), call)
    assert getattr(conv, "_router_task", None) is None   # not named: no early thinker in a game
    del turn


def test_the_focus_guide_rides_only_on_focus_turns_and_never_in_the_cached_prompt() -> None:
    from yuki.voice.mouth import CALL_LAYER, FOCUS_GUIDE, MOUTH_PROMPT, volatile_block

    head = FOCUS_GUIDE.split("\n", 1)[1][:60]
    assert head not in volatile_block()["text"]
    text = volatile_block(state={"focus": {"game": "VALORANT", "call": True}})["text"]
    assert head in text and "in a game (VALORANT) and on a voice call" in text
    assert "on a voice call right now" in volatile_block(state={"focus": {"game": None, "call": True}})["text"]
    assert head not in MOUTH_PROMPT and head not in CALL_LAYER
    assert len(FOCUS_GUIDE) < 1600


# -- the wake word ------------------------------------------------------------------------------------------------

def test_a_name_end_that_cannot_be_the_names_is_reported_unknown() -> None:
    """Measured 5 Oct: the model's token times count from its last internal restart, not the stream's start, so on a
    long stream the name's end came out seconds early. Only an end that fits the hit's lag is kept."""
    from yuki.voice.wake import NAME_END_OFFSET_S, RATE, WakeSpotter

    spotter = WakeSpotter.__new__(WakeSpotter)
    at, size = 100.0, RATE // 10
    batch_end = at + size / RATE
    good = batch_end - 0.48 + NAME_END_OFFSET_S
    assert spotter._plausible(good, at, size) == good
    assert spotter._plausible(batch_end - 3.5, at, size) is None
    assert spotter._plausible(batch_end + 0.2, at, size) is None
    assert spotter.retimed == 2
    assert spotter._plausible(None, at, size) is None and spotter._plausible(good, None, size) == good


def _fake_session(**over: Any) -> Any:
    from yuki.voice.session import VoiceSession

    s = SimpleNamespace(settings=Settings(), log=Log(), _listener=SimpleNamespace(rescued=False), closing=False,
                        in_conversation=False, _conversation=None, call_app=None, call_evidence=None,
                        _pending_wake=None, started=[])
    s._wake_allowed = lambda: True
    s._game_now = lambda: None
    s._call_focus = lambda: VoiceSession._call_focus(s)
    s.start_conversation = lambda *a, **k: s.started.append(k)
    for k, v in over.items():
        setattr(s, k, v)
    return s


def test_the_name_said_during_a_conversation_is_passed_to_it() -> None:
    from yuki.voice.session import VoiceSession

    heard: list[float] = []
    s = _fake_session(in_conversation=True, _conversation=SimpleNamespace(name_heard=heard.append))
    VoiceSession._on_wake_heard(s, 1.0, 2.0, 0.0)
    assert heard == [2.0] and s.started == [] and s.log.of("wake", state="name_in_conversation")


def test_in_a_game_or_call_a_rescued_sound_alike_never_wakes() -> None:
    from yuki.voice.session import VoiceSession

    s = _fake_session(_listener=SimpleNamespace(rescued=True), call_app="Discord", call_evidence="fresh")
    VoiceSession._on_wake_heard(s, 1.0, 2.0, 0.0)
    assert s.started == [] and s.log.of("wake", state="ignored", why="rescued_in_focus")
    s2 = _fake_session(_listener=SimpleNamespace(rescued=True))   # not in a game or call: as before
    VoiceSession._on_wake_heard(s2, 1.0, 2.0, 0.0)
    assert len(s2.started) == 1
    s3 = _fake_session(_listener=SimpleNamespace(rescued=False), call_app="Discord", call_evidence="fresh")
    VoiceSession._on_wake_heard(s3, 1.0, 2.0, 0.0)   # the main spotter's own hit wakes it in a call
    assert len(s3.started) == 1


def test_a_standby_warms_speech_recognition_with_half_a_second_of_silence(tmp_path: Path) -> None:
    import asyncio

    conv, log, _s, _t, _d = _conversation(tmp_path)
    sent: list[int] = []

    async def send(audio: bytes) -> None:
        sent.append(len(audio))

    conv._stt = SimpleNamespace(_transport_send_audio=send)
    asyncio.run(conv._warm_stt())
    assert sent == [16_000] and log.of("stt_warm")
    off, _l, *_ = _conversation(tmp_path, voice_stt_warm=False)
    off._stt = SimpleNamespace(_transport_send_audio=send)
    asyncio.run(off._warm_stt())
    assert sent == [16_000]
