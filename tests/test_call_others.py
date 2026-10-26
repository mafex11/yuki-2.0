"""Calls, both sides (owner's decision 3 Oct 2026; the design notes).

The user's microphone plus each consented app's own audio (process loopback) as tracks of one voice session; a
full-screen game's voice chat as a session of its own, asked only when a card can be seen; every line attributed
(``who``, ``app``, ``track_kind``) and the user's lines marked with the apps that were taking the microphone.

Offline only: temp databases, stubbed microphone holders and full-screen facts, :class:`ArraySource` tracks of
synthetic tones (a stub recogniser names each tone by its pitch, so a line's text says which track it came from),
a fake capture-session watch. Nothing reads or touches the live desktop, microphone or speakers.
"""

from __future__ import annotations

import json
import sqlite3
import time

import numpy as np
import pytest

from test_av_signals import DISCORD, PYTHON, Stubs
from yuki.memory import store as store_module
from yuki.memory.calls import (
    RATE, TRACKS_STATE, ArraySource, CallConfig, CallWorker, Chunker, Segment, heard_by,
)
from yuki.memory.loopback import LoopbackUnavailable
from yuki.memory.store import Store
from yuki.memory.transcribe import transcribe_waiting

VALORANT = r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe"
GAME_KEY = "valorant-win64-shipping"
#: Pitches the stub recogniser tells apart: the user, the call's other people, the game's teammates.
USER_HZ, CALL_HZ, GAME_HZ = 300, 800, 1500


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def tones(spec, total_s: float) -> np.ndarray:
    """Silence with tone bursts ``[(start_s, seconds, hz)]``."""
    a = np.zeros(int(total_s * RATE), np.float32)
    for start, dur, hz in spec:
        i, n = int(start * RATE), int(dur * RATE)
        a[i:i + n] += (0.3 * np.sin(2 * np.pi * hz * np.arange(n) / RATE)).astype(np.float32)
    return a


class ToneBackend:
    """Stands in for Whisper: one segment per loud run, its text the run's pitch ("tone 300")."""

    name = "stub:tones"
    device = "cpu"

    def transcribe(self, audio, hint: str = ""):
        frame = 512
        n = len(audio) // frame
        loud = [float(np.sqrt(np.mean(audio[i * frame:(i + 1) * frame] ** 2))) > 0.03 for i in range(n)]
        out, i = [], 0
        while i < n:
            if not loud[i]:
                i += 1
                continue
            j = i
            while j < n and loud[j]:
                j += 1
            seg = audio[i * frame:j * frame]
            hz = int(np.argmax(np.abs(np.fft.rfft(seg))) * RATE / len(seg))
            pitch = min((USER_HZ, CALL_HZ, GAME_HZ), key=lambda p: abs(p - hz))
            out.append(Segment(i * frame / RATE, j * frame / RATE, f"tone {pitch}", "en", avg_logprob=-0.1,
                               no_speech_prob=0.01, compression_ratio=1.0, tokens=3))
            i = j
        return out, {"lang": "en"}

    def close(self) -> None:
        pass


class FakeWatch:
    """Stands in for :class:`yuki.memory.loopback.CaptureWatch`: the test says when an app takes the microphone."""

    instances: list[FakeWatch] = []

    def __init__(self, on_change) -> None:
        self.on_change = on_change
        self.mode = "events"
        self.counters = {"events": 0}
        self.started = self.stopped = False
        FakeWatch.instances.append(self)

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    def emit(self, at: float, pid: int, active: bool) -> None:
        self.counters["events"] += 1
        self.on_change(at, pid, active)


TABLE = [(100, 4, "Discord.exe"), (200, 4, "VALORANT-Win64-Shipping.exe"), (300, 4, "handy.exe")]


class Rig:
    """A call worker on stubs: microphone and app tracks are :class:`ArraySource` replays of given audio."""

    def __init__(self, store: Store, t0: float, *, mic=None, apps=None, headphones: bool = True,
                 watch: bool = True, track_error: Exception | None = None, **cfg) -> None:
        self.store, self.t0 = store, t0
        self.stubs = Stubs()
        self.front: dict | None = None
        self.busy = False
        self.headphones = headphones
        self.mic_audio = mic if mic is not None else np.zeros(RATE, np.float32)
        self.app_audio = apps or {}
        self.mic_opened = 0
        self.mic_open_now = 0
        self.max_mic_open = 0
        self.tracks_opened: list = []
        self.track_error = track_error
        #: The processes running (the Toolhelp snapshot): a test closes the game by taking it out.
        self.table = list(TABLE)
        #: What the track factory hands out, when a test wants its own source.
        self.track_source = None
        self.logs: list[tuple[str, dict]] = []
        config = CallConfig(**{"end_grace_s": 30.0, "vad": False, "apps": frozenset({"discord"}), **cfg})
        rig = self

        class Mic(ArraySource):
            def start(self) -> None:
                super().start()
                rig.mic_open_now += 1
                rig.max_mic_open = max(rig.max_mic_open, rig.mic_open_now)

            def stop(self) -> None:
                rig.mic_open_now -= 1

        def mic_factory(c, app):
            self.mic_opened += 1
            audio = self.mic_audio if self.mic_opened == 1 else np.zeros(0, np.float32)   # replayed once
            return Mic(audio, start_at=t0)

        def track_factory(c, spec):
            if self.track_error is not None:
                raise self.track_error
            self.tracks_opened.append(spec)
            if self.track_source is not None:
                return self.track_source(spec)
            audio = self.app_audio.get(spec.process)
            if audio is None or sum(1 for s in self.tracks_opened if s.process == spec.process) > 1:
                audio = np.zeros(0, np.float32)
            return ArraySource(audio, start_at=t0)

        self.w = CallWorker(
            store, signals=self.stubs.signals(frozenset({PYTHON.casefold()})), config=config,
            log=lambda kind, **f: self.logs.append((kind, f)),
            headphones_fn=lambda c: ((True, {"form_factor": "headphones", "device": "Headphones"}) if self.headphones
                                     else (False, {"form_factor": "speakers", "device": "Speakers"})),
            source_factory=mic_factory, track_factory=track_factory,
            pid_fn=lambda holder: 100, foreground_fn=lambda: self.front, gpu_fn=lambda pids: 70.0,
            busy_fn=lambda: self.busy, watch_factory=FakeWatch if watch else None, table_fn=lambda: self.table)
        self.w._tell_ui = lambda: None

    def step(self, dt: float) -> None:
        self.w.step(self.t0 + dt)

    def events(self, kind: str) -> list[dict]:
        return [f for k, f in self.logs if k == kind]

    def game_in_front(self, on: bool = True) -> None:
        self.front = {"hwnd": 7, "pid": 200, "exe": VALORANT} if on else None

    def drain(self) -> None:
        for r in (self.w.active, self.w.game):
            for s in (getattr(r, "session", None), getattr(r, "track", None)):
                if s is not None:
                    assert s.wait_drained(10.0)

    def close(self) -> None:
        self.w.close("test")


def lines_of(store: Store, call_id: int) -> list[dict]:
    caps = store.call_side_captures(call_id)
    return [ln for c in caps for ln in Store.call_lines(c["data"])]


def transcribe(store: Store) -> dict:
    return transcribe_waiting(store, CallConfig(), stop=lambda: False, log=lambda *a, **k: None,
                              backend=ToneBackend())


# -- one call, both sides ------------------------------------------------------------------------------------


def test_a_call_records_both_sides_attributed_in_time_order_with_heard_by(store):
    t0 = time.time() - 3600
    mic = tones([(1, 2, USER_HZ), (10, 2, USER_HZ)], 20)
    others = tones([(5, 2.5, CALL_HZ), (11, 2, CALL_HZ)], 20)       # a friend talks over the user's second line
    rig = Rig(store, t0, mic=mic, apps={"discord": others})
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    store.set_call_pref("discord", "always")          # an "Always" from before 3 Oct: no new card
    try:
        rig.step(0)
        call_id = rig.w.active.call_id
        watch = FakeWatch.instances[-1]
        assert watch.started and rig.w.active.session is not None and rig.w.active.track is not None
        watch.emit(t0 + 0.5, 100, True)                # Discord holds its microphone stream all along
        watch.emit(t0 + 9.8, 300, True)                # Handy (dictation) takes it while the user says line 2
        watch.emit(t0 + 12.3, 300, False)
        rig.drain()
        state = json.loads(store.nudge_state(TRACKS_STATE))
        assert state["mic"]["state"] == "recording" and state["tracks"][0]["state"] == "recording"
        rig.stubs.mic.clear()
        for dt in range(5, 200, 5):
            rig.step(dt)
        assert rig.w.active is None and store.call_record(call_id).status == "ended"
    finally:
        rig.close()
    chunks = store.call_chunks(call_id=call_id)
    sides = sorted((c["side"], c["track_process"] or "") for c in chunks)
    assert sides == [("others", "discord"), ("others", "discord"), ("user", "discord"), ("user", "discord")]
    names = {c["file"].split("-")[1] for c in chunks}
    assert names == {"mic", "app"}                                   # the spool file names carry the track
    assert all(c["track_call_id"] == call_id for c in chunks)
    row = store.call_record(call_id)
    assert row.speech_s == pytest.approx(4.0, abs=0.3)               # the row counts the user's speech only
    transcribe(store)
    lines = lines_of(store, call_id)
    assert [(ln["who"], ln["text"]) for ln in lines] == [
        ("user", "tone 300"), ("others", "tone 800"), ("user", "tone 300"), ("others", "tone 800")]
    assert [round(ln["at"] - t0, 1) for ln in lines] == [1.0, 5.0, 10.0, 11.0]   # the overlap: both kept
    assert {ln["app"] for ln in lines} == {row.app} and {ln["track_kind"] for ln in lines} == {"call"}
    user = [ln for ln in lines if ln["who"] == "user"]
    assert user[0]["heard_by"] == [row.app]                           # only Discord was taking the microphone
    assert len(user[1]["heard_by"]) == 2 and row.app in user[1]["heard_by"]   # Discord and Handy (dictation)
    assert all("heard_by" not in ln for ln in lines if ln["who"] == "others")
    cap = store.call_side_captures(call_id)[0]["data"]
    assert [u["who"] for u in cap["utterances"]] == ["user", "user"]  # what the older readers take as the user's
    assert len(cap["lines"]) == 4 and store.call_record(call_id).utterances == 2


def test_heard_by_is_none_where_nothing_was_watched_and_empty_where_nobody_took_the_mic():
    spans = [{"kind": "watch", "start": 100.0, "end": 200.0, "app": None},
             {"kind": "capture", "start": 100.0, "end": 150.0, "app": "Discord"},
             {"kind": "capture", "start": 140.0, "end": 141.0, "app": "VALORANT"},
             {"kind": "capture", "start": 141.0, "end": 142.0, "app": "VALORANT"}]
    assert heard_by(spans, 120, 125) == ["Discord"]
    assert heard_by(spans, 139.5, 142.5) == ["Discord", "VALORANT"]    # push-to-talk spans add up
    assert heard_by(spans, 160, 165) == []                            # watched, nobody: the room, or Yuki
    assert heard_by(spans, 250, 255) is None                          # not watched: unknown
    assert heard_by(spans, 149.9, 155) == []                          # 0.1 s of a 5 s line is not hearing it


# -- the microphone: once per session, headphones only -----------------------------------------------------


def test_without_headphones_the_mic_stays_closed_while_the_app_track_records(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, headphones=False, apps={"discord": tones([(1, 2, CALL_HZ)], 6)})
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    store.set_call_pref("discord", "always")
    try:
        rig.step(0)
        call_id = rig.w.active.call_id
        assert store.call_record(call_id).status == "waiting_headphones"
        assert rig.mic_opened == 0 and rig.w.active.track is not None and rig.w.active.track.listening
        rig.drain()
        rig.headphones = True
        rig.step(5)
        assert rig.mic_opened == 1                                   # back on headphones: the mic opens
        rig.headphones = False
        rig.step(10)
        assert not rig.w.active.session.listening and rig.w.active.track.listening
        assert any(e["reason"] == "not_headphones" for e in rig.events("call_recording_paused"))
    finally:
        rig.close()
    assert {c["side"] for c in store.call_chunks(call_id=call_id)} == {"others"}


def test_a_track_that_cannot_open_is_unavailable_and_the_mic_goes_on(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, mic=tones([(1, 2, USER_HZ)], 6), track_error=LoopbackUnavailable("activate_failed 0x80070005"),
              track_retry_s=60.0)
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    store.set_call_pref("discord", "always")
    try:
        rig.step(0)
        act = rig.w.active
        assert act.track is None and act.track_state == "unavailable" and "activate_failed" in act.track_reason
        assert act.session is not None and act.session.listening      # the microphone exactly as before
        rig.drain()
        rig.step(30)
        assert len(rig.events("call_track_unavailable")) == 1         # not retried every poll
        rig.track_error = None
        rig.step(65)                                                  # after track_retry_s it is tried again
        assert act.track is not None and act.track_state == "recording"
    finally:
        rig.close()
    assert [c["side"] for c in store.call_chunks(call_id=act.call_id)] == ["user"]


def test_record_others_off_records_only_the_microphone(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, mic=tones([(1, 2, USER_HZ)], 6), apps={"discord": tones([(1, 2, CALL_HZ)], 6)},
              record_others=False)
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    store.set_call_pref("discord", "always")
    try:
        rig.step(0)
        assert rig.w.active.track is None and rig.w.active.track_state == "off" and not rig.tracks_opened
    finally:
        rig.close()


# -- a call and a game's voice chat at once -------------------------------------------------------------------


def test_a_call_and_a_game_overlap_with_the_mic_recorded_once(store):
    t0 = time.time() - 3600
    mic = tones([(1, 2, USER_HZ), (20, 2, USER_HZ)], 30)
    rig = Rig(store, t0, mic=mic, apps={"discord": tones([(5, 2, CALL_HZ)], 30),
                                        GAME_KEY: tones([(12, 2, GAME_HZ)], 30)})
    store.set_call_pref("discord", "always")
    store.set_call_pref(GAME_KEY, "always")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    try:
        rig.step(0)
        call = rig.w.active
        rig.stubs.hold("mic", VALORANT, t0 + 2)          # the match starts; its voice chat takes the microphone
        rig.game_in_front()
        rig.step(3)
        game = rig.w.game
        assert game is not None and game.kind == "game" and rig.w.active is call   # a game session, not a call
        assert store.call_record(game.call_id).kind == "game" and store.call_record(game.call_id).via == "game"
        assert game.session is None and call.session is not None    # one microphone, the call's row owns it
        assert game.track is not None and game.track_target == call.call_id
        rig.drain()
        assert rig.mic_opened == 1 and rig.max_mic_open == 1
        rig.stubs.mic[:] = [m for m in rig.stubs.mic if "Discord" not in m["path"]]   # the Discord call ends
        for dt in range(10, 200, 10):
            rig.step(dt)
        assert rig.w.active is None and rig.w.game is game
        assert game.session is not None and game.track_target == game.call_id   # mic and track move to the game
        assert rig.max_mic_open == 1                                  # never two microphones at once
        rig.stubs.mic.clear()
        rig.game_in_front(False)
        for dt in range(200, 400, 10):
            rig.step(dt)
        assert rig.w.game is None
    finally:
        rig.close()
    transcribe(store)
    call_lines = lines_of(store, call.call_id)
    assert [(ln["who"], ln["track_kind"], ln["text"]) for ln in call_lines] == [
        ("user", "call", "tone 300"), ("others", "call", "tone 800"), ("others", "game", "tone 1500"),
        ("user", "call", "tone 300")]
    assert call_lines[2]["app"] == game.app                          # the teammates, labelled with the game
    game_chunks = [c for c in store.call_chunks(statuses=("journaled",)) if c["track_process"] == GAME_KEY]
    assert game_chunks and all(c["call_id"] == call.call_id and c["track_call_id"] == game.call_id for c in game_chunks)
    user_chunks = [c for c in store.call_chunks(statuses=("journaled", "done", "spooled")) if c["side"] == "user"]
    assert len(user_chunks) == 2 and {c["call_id"] for c in user_chunks} == {call.call_id}   # recorded once


# -- game consent: only when a card can be seen ------------------------------------------------------------------


def _status(store: Store) -> dict:
    from yuki.memory.api import MemoryClient

    return MemoryClient(store).call_status()


def test_a_game_asks_only_when_its_card_can_be_seen_and_records_from_the_answer(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, apps={GAME_KEY: tones([(1, 1, GAME_HZ)], 3)}, ask_timeout_s=60.0)
    rig.stubs.hold("mic", VALORANT, t0 - 10)
    rig.game_in_front()
    try:
        rig.step(0)
        game = rig.w.game
        assert store.call_record(game.call_id).status == "ask_later"
        st = _status(store)
        assert st["call"] is None and st["sessions"][0]["status"] == "ask_later"   # no card over the game, no call
        for dt in range(5, 200, 5):                                  # an hour in full screen asks nothing
            rig.step(dt)
        assert store.call_record(game.call_id).status == "ask_later" and not rig.tracks_opened
        rig.game_in_front(False)                                     # alt-tab to Discord: a card can be seen
        rig.step(200)
        assert store.call_record(game.call_id).status == "asking" and _status(store)["call"]["asking"] is True
        rig.game_in_front()                                          # back in the game before answering
        rig.step(230)
        assert store.call_record(game.call_id).status == "ask_later" and game.asked_s == pytest.approx(30.0)
        rig.busy = True                                              # not in front, but Windows says busy
        rig.game_in_front(False)
        rig.step(240)
        assert store.call_record(game.call_id).status == "ask_later"
        rig.busy = False
        rig.step(250)
        assert store.call_record(game.call_id).status == "asking"
        assert store.answer_call(game.call_id, "yes")
        rig.step(255)
        assert store.call_record(game.call_id).status == "recording" and rig.tracks_opened
        assert rig.mic_opened == 1                                   # the user's side too, from the answer on
    finally:
        rig.close()


def test_a_game_card_counts_only_the_time_it_was_up(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, ask_timeout_s=60.0)
    rig.stubs.hold("mic", VALORANT, t0 - 10)
    rig.game_in_front()
    try:
        rig.step(0)
        game = rig.w.game
        rig.game_in_front(False)
        rig.step(5)                                                  # up 5..45 (40 s)
        rig.game_in_front()
        rig.step(45)
        rig.game_in_front(False)
        rig.step(100)                                                # up again
        rig.step(115)
        assert store.call_record(game.call_id).consent is None       # 40 + 15 < 60
        rig.step(125)
        assert store.call_record(game.call_id).status == "unanswered"
        assert not rig.tracks_opened and rig.mic_opened == 0
    finally:
        rig.close()


def test_a_game_never_seen_asks_right_after_its_session_and_always_sticks(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    rig.stubs.hold("mic", VALORANT, t0 - 10)
    rig.game_in_front()
    try:
        rig.step(0)
        game_id = rig.w.game.call_id
        rig.stubs.mic.clear()                                        # the game closes
        rig.front = {"hwnd": 9, "pid": 999, "exe": r"C:\Apps\Other\movie.exe"}   # something else full screen
        for dt in range(5, 300, 5):
            rig.step(dt)
        assert rig.w.game is None and rig.w.after is not None
        row = store.call_record(game_id)
        assert row.ended_at is not None and row.status == "ask_later"   # its time ended; the card waits
        rig.front = None
        rig.step(305)
        st = _status(store)
        assert store.call_record(game_id).status == "asking"
        assert st["call"] is None and st["pending_card"]["id"] == game_id   # a card, never a live call for voice
        assert store.answer_call(game_id, "always")
        rig.step(310)
        assert store.call_record(game_id).status == "ended" and store.call_pref(GAME_KEY) == "always"
        assert rig.w.after is None and not rig.tracks_opened         # nothing recorded for the ended session
        rig.stubs.hold("mic", VALORANT, t0 + 400)                    # next match: recorded without a card
        rig.game_in_front()
        rig.step(401)
        assert rig.w.game.status == "recording" and rig.tracks_opened
    finally:
        rig.close()


def test_never_for_a_game_and_game_voice_off(store):
    t0 = time.time() - 3600
    store.set_call_pref(GAME_KEY, "never")
    rig = Rig(store, t0)
    rig.stubs.hold("mic", VALORANT, t0 - 10)
    rig.game_in_front()
    try:
        rig.step(0)
        assert store.call_record(rig.w.game.call_id).status == "declined" and not rig.tracks_opened
    finally:
        rig.close()
    other = Store.open(store.path.parent / "off.db")
    try:
        rig = Rig(other, t0, game_voice=False)
        rig.stubs.hold("mic", VALORANT, t0 - 10)
        rig.game_in_front()
        rig.step(0)
        assert rig.w.game is None and rig.w.active is None and not other.calls()   # as before: nothing at all
        assert rig.events("call_skipped_gaming")
        rig.close()
    finally:
        other.close()


def test_straight_into_another_game_ends_the_first_session_at_once(store, monkeypatch):
    t0 = time.time() - 3600
    other = r"C:\Games\Other\other-game.exe"
    # capQ2b: a game by a machine fact (Windows' own game detection names it), never by its share of the GPU
    from yuki.memory import gamegate

    monkeypatch.setattr(gamegate, "windows_games", lambda: frozenset({gamegate._norm_path(other)}))
    rig = Rig(store, t0)
    rig.stubs.hold("mic", VALORANT, t0 - 10)
    rig.game_in_front()
    try:
        rig.step(0)
        first = rig.w.game.call_id
        rig.stubs.mic.clear()
        rig.stubs.hold("mic", other, t0 + 20)
        rig.front = {"hwnd": 8, "pid": 300, "exe": other}
        rig.step(21)
        assert rig.w.game is not None and rig.w.game.call_id != first and rig.w.game.key == "other-game"
        assert store.call_record(first).stop_reason == "other_game"
    finally:
        rig.close()


def test_push_to_talk_releases_keep_one_game_session(store):
    """A game that holds the microphone only while push-to-talk is down: one session while it stays in front."""
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    rig.game_in_front()
    try:
        for i in range(60):                                          # 10 minutes, the key pressed now and then
            rig.stubs.mic.clear()
            if i % 7 == 0:
                rig.stubs.hold("mic", VALORANT, t0 + i * 10)
            rig.step(i * 10 + 1)
        assert len(store.calls()) == 1 and rig.w.game is not None
    finally:
        rig.close()


# -- stop, pause, erase, restart: every track ----------------------------------------------------------------


def _both(store: Store, t0: float, rig: Rig) -> None:
    store.set_call_pref("discord", "always")
    store.set_call_pref(GAME_KEY, "always")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    rig.step(0)
    assert rig.w.active is not None and rig.w.game is not None


def test_stop_ends_every_track_of_every_row(store):
    from yuki.memory.api import MemoryClient

    t0 = time.time() - 3600
    rig = Rig(store, t0)
    try:
        _both(store, t0, rig)
        call, game = rig.w.active, rig.w.game
        assert call.session and call.track and game.track
        MemoryClient(store).stop_call()
        rig.step(5)
        assert store.call_record(call.call_id).status == "stopped" and store.call_record(game.call_id).status == "stopped"
        assert call.session is None and call.track is None and game.track is None and game.session is None
        rig.step(10)
        assert call.session is None and game.track is None           # no second card, nothing reopened
    finally:
        rig.close()


def test_pause_ends_every_row_and_track(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    try:
        _both(store, t0, rig)
        call, game = rig.w.active, rig.w.game
        rig.w.paused = True
        rig.step(5)
        assert rig.w.active is None and rig.w.game is None and rig.w.after is None
        assert call.session is None and call.track is None and game.track is None
        assert {store.call_record(call.call_id).status, store.call_record(game.call_id).status} == {"ended"}
        assert FakeWatch.instances[-1].stopped
        state = json.loads(store.nudge_state(TRACKS_STATE))
        assert state["mic"]["state"] == "off" and state["rows"] == []
    finally:
        rig.close()


def test_erase_removes_waiting_audio_of_every_track(store):
    from yuki.memory.api import MemoryClient

    t0 = time.time() - 3600
    rig = Rig(store, t0, mic=tones([(1, 1, USER_HZ)], 4), apps={"discord": tones([(1, 1, CALL_HZ)], 4),
                                                                GAME_KEY: tones([(1, 1, GAME_HZ)], 4)})
    try:
        _both(store, t0, rig)
        rig.drain()
    finally:
        rig.close()
    spooled = store.call_chunks()
    assert {(c["side"], c["track_process"] or "") for c in spooled} == {("user", "discord"), ("others", "discord"),
                                                                        ("others", GAME_KEY)}
    assert MemoryClient(store).erase_call_audio() == len(spooled)
    assert store.call_chunks() == [] and not any(store.spool_dir.iterdir())


def test_a_restart_adopts_the_call_and_the_game_and_their_tracks_go_on(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    _both(store, t0, rig)
    call_id, game_id = rig.w.active.call_id, rig.w.game.call_id
    for dt in range(5, 60, 5):
        rig.step(dt)
    rig.w.active.session.stop("crash")                               # the old process dies: nothing ends
    rig.w.active.track.stop("crash")
    rig.w.game.track.stop("crash")
    rig2 = Rig(store, t0)
    rig2.stubs = rig.stubs
    rig2.w.signals = rig.stubs.signals(frozenset({PYTHON.casefold()}))
    rig2.game_in_front()
    try:
        rig2.w.recover(t0 + 70)
        assert rig2.w.active.call_id == call_id and rig2.w.game.call_id == game_id
        rig2.step(71)
        assert rig2.w.active.session is not None and rig2.w.active.track is not None
        assert rig2.w.game.track is not None and rig2.w.game.track_target == call_id
        assert len(store.calls()) == 2
    finally:
        rig2.close()


# -- what the UI reads ------------------------------------------------------------------------------------------


def test_call_status_shows_the_card_first_and_every_track(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    store.set_call_pref("discord", "always")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        st = _status(store)
        assert st["call"]["kind"] == "call" and st["call"]["status"] == "recording" and st["recording"]
        assert [s["kind"] for s in st["sessions"]] == ["game", "call"]
        assert st["mic"]["state"] == "recording" and st["mic"]["call_id"] == rig.w.active.call_id
        tracks = {t["track_kind"]: t for t in st["tracks"]}
        assert tracks["call"]["state"] == "recording" and tracks["game"]["state"] == "asking"
        rig.game_in_front(False)
        rig.step(5)
        st = _status(store)
        assert st["call"]["kind"] == "game" and st["call"]["asking"]   # the game's card goes up now
    finally:
        rig.close()


# -- the speech gate on an app track -----------------------------------------------------------------------------


def test_a_break_in_an_app_track_cuts_the_chunk_where_the_sound_stopped():
    cfg = CallConfig(vad=False)
    c = Chunker(cfg, breaks=True)
    out = c.feed(100.0, tones([(0, 1, CALL_HZ)], 1.0))
    out += c.feed(110.0, tones([(0, 1, CALL_HZ)], 1.0))              # 9 s later: Windows sent nothing between
    out += c.flush()
    assert [round(ch.start_at, 1) for ch in out] == [100.0, 110.0] and c.breaks == 1
    mic = Chunker(cfg)                                                # a microphone never breaks on its stamps
    out = mic.feed(100.0, tones([(0, 1, USER_HZ)], 1.0)) + mic.feed(110.0, tones([(0, 1, USER_HZ)], 1.0)) + mic.flush()
    assert len(out) == 1 and mic.breaks == 0


# -- storage --------------------------------------------------------------------------------------------------------


def test_migration_reads_old_rows_as_the_users(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    before = store_module.MIGRATIONS[:-1]
    monkeypatch.setattr(store_module, "MIGRATIONS", before)
    old = Store.open(path)
    with old._lock:   # a row as the call worker wrote it before migration 34 (no kind)
        call_id = old._conn.execute("INSERT INTO calls(app, process, via, detected_at, status, updated_at)"
                                    " VALUES ('Discord', 'discord', 'screen', 1000, 'recording', 1000)").lastrowid
        old._conn.execute("INSERT INTO call_chunks(call_id, seq, start_at, audio_s, speech_s, file, bytes, codec,"
                          " status, text_ciphertext, created_at) VALUES (?,1,1000,3,2,NULL,0,'opus','done',?,1000)",
                          (call_id, old.cipher.encrypt(json.dumps([{"at": 1000.5, "end": 1002.0, "text": "hello",
                                                                     "lang": "en"}]))))
        old._conn.commit()
    assert old._conn.execute("PRAGMA user_version").fetchone()[0] == len(before)
    old.close()
    monkeypatch.undo()
    s = Store.open(path)
    try:
        assert s._conn.execute("PRAGMA user_version").fetchone()[0] == len(store_module.MIGRATIONS)
        assert s.call_record(call_id).kind == "call"
        chunk = s.call_chunks(statuses=("done",))[0]
        assert chunk["side"] == "user" and chunk["track_process"] is None
        s.update_call(call_id, status="ended", ended_at=1010.0)
        assert s.journal_call_chunks(call_id) is not None
        data = s.call_side_captures(call_id)[0]["data"]
        assert data["utterances"][0]["who"] == "user" and data["lines"][0]["app"] == "Discord"
        # a capture written before the change (no lines, no who) reads as the user's
        assert Store.call_lines({"app": "Zoom", "utterances": [{"at": 1, "end": 2, "text": "hi"}]}) == [
            {"at": 1, "end": 2, "text": "hi", "who": "user", "app": "Zoom", "track_kind": "call"}]
        assert s.mic_spans_between(0, 1e12) == []
    finally:
        s.close()


def test_spool_names_carry_the_track_and_spans_are_pruned_with_the_spool(store):
    a = store.write_spool(1, 1, start_at=1.0, audio_s=1, speech_s=1, payload=b"x", codec="opus")
    b = store.write_spool(1, 2, start_at=1.0, audio_s=1, speech_s=1, payload=b"x", codec="opus", side="others",
                          track_app="VALORANT", track_process="VALORANT-Win64-Shipping.exe", track_kind="game",
                          track_call_id=2)
    rows = {r["id"]: r for r in store.call_chunks()}
    assert rows[a]["file"].startswith("call1-mic-00001-") and rows[b]["file"].startswith("call1-app-valorant_win64_shipping-00002-")
    assert rows[b]["track_process"] == GAME_KEY and rows[b]["track_kind"] == "game" and rows[b]["track_call_id"] == 2
    with pytest.raises(ValueError):
        store.write_spool(1, 3, start_at=1.0, audio_s=1, speech_s=1, payload=b"x", codec="opus", side="them")
    old = store.open_mic_span(kind="capture", at=time.time() - 30 * 86400, process="discord", app="Discord", pid=1)
    store.close_mic_span(old, time.time() - 30 * 86400 + 5)
    new = store.open_mic_span(kind="watch", at=time.time())
    store.sweep_spool(max_age_s=7 * 86400, max_bytes=10**9)
    left = store.mic_spans_between(0, time.time() + 10)
    assert [s["kind"] for s in left] == ["watch"] and new
    with sqlite3.connect(store.path) as conn:
        assert conn.execute("SELECT count(*) FROM mic_spans").fetchone()[0] == 1


# -- review fixes (report 60, "Review fixes") ------------------------------------------------------------------


def _mic(store: Store) -> dict:
    return json.loads(store.nudge_state(TRACKS_STATE))["mic"]


@pytest.mark.parametrize("call_state", ["asking", "never", "no", "not_asked", "stopped"])
def test_r1_a_call_not_said_yes_to_is_never_recorded_through_the_games_microphone(store, call_state):
    """Review finding 1: VALORANT "Always", the Discord call not consented. What the user says while Discord holds
    the microphone is the call's: the game's session must not record it. The game's own audio goes on."""
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    store.set_call_pref(GAME_KEY, "always")
    if call_state in ("never", "stopped"):
        store.set_call_pref("discord", "never" if call_state == "never" else "always")
    if call_state == "not_asked":
        store.set_nudge_state("quiet_until", str(time.time() + 10_000))
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        call, game = rig.w.active, rig.w.game
        if call_state == "no":
            assert store.answer_call(call.call_id, "no")
        if call_state == "stopped":
            store.request_call_stop(call.call_id)
        rig.step(5)
        assert store.call_record(game.call_id).status == "recording"
        assert store.call_record(call.call_id).status not in ("recording", "waiting_headphones")
        assert rig.mic_open_now == 0                                  # nothing the user says is kept
        assert _mic(store) == {"state": "paused", "call_id": game.call_id, "reason": "call_not_consented"}
        assert game.track is not None and game.track.listening       # the game's own audio: consented, goes on
        rig.stubs.mic[:] = [m for m in rig.stubs.mic if "Discord" not in m["path"]]   # Discord lets go
        rig.step(10)
        assert rig.mic_open_now == 1 and _mic(store)["state"] == "recording"   # now what is said is the game's
    finally:
        rig.close()


def test_r1_a_recording_call_never_records_a_declined_games_teammates(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, apps={"discord": tones([(1, 1, CALL_HZ)], 3), GAME_KEY: tones([(1, 1, GAME_HZ)], 3)})
    store.set_call_pref("discord", "always")
    store.set_call_pref(GAME_KEY, "never")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        assert store.call_record(rig.w.game.call_id).status == "declined"
        assert [s.process for s in rig.tracks_opened] == ["discord"]   # never the teammates
        assert rig.w.active.session is not None and rig.w.game.session is None   # the mic is the call's alone
        assert rig.w.game.track_state == "off"
        rig.drain()
    finally:
        rig.close()
    assert not [c for c in store.call_chunks() if c["track_process"] == GAME_KEY]


def test_r2_a_closed_game_stops_the_microphone_though_its_session_lingers(store):
    """Review finding 2: the game's row may live on for its grace, but the microphone stops when the game is gone."""
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    store.set_call_pref(GAME_KEY, "always")
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        assert rig.mic_open_now == 1
        rig.stubs.mic.clear()                                        # the game closes: no hold, no window,
        rig.game_in_front(False)
        rig.table = [p for p in rig.table if "VALORANT" not in p[2]]   # no process
        rig.step(2)
        assert rig.w.game is not None                                # the row's own grace goes on
        assert rig.mic_open_now == 0 and _mic(store)["reason"] == "game_closed"
        assert rig.w.game.track_state == "paused" and rig.w.game.track_reason == "game_closed"
    finally:
        rig.close()


def test_r2_a_push_to_talk_game_keeps_the_microphone_only_while_it_is_used(store):
    t0 = time.time() - 3600
    rig = Rig(store, t0, game_mic_grace_s=3.0)
    store.set_call_pref(GAME_KEY, "always")
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        watch = FakeWatch.instances[-1]
        rig.stubs.mic.clear()                                        # push-to-talk up: no hold
        rig.step(5)
        assert rig.mic_open_now == 1                                 # in front, drawing like a game: in use
        rig.game_in_front(False)                                     # alt-tab: not held, not in front
        rig.step(10)
        assert rig.mic_open_now == 0 and _mic(store)["reason"] == "game_not_in_use"
        watch.emit(t0 + 12, 200, True)                               # its capture session opens (key down)
        rig.step(12.5)
        assert rig.mic_open_now == 1
        watch.emit(t0 + 14, 200, False)                              # key up
        rig.step(16)
        assert rig.mic_open_now == 1                                 # within game_mic_grace_s
        rig.step(18)
        assert rig.mic_open_now == 0
        assert rig.w.game is not None and len(store.calls()) == 1   # one session all along
    finally:
        rig.close()


def test_r3_call_status_follows_the_recording_row_and_any_recording_track(store):
    """Review finding 3: a declined call open beside a recording game - the indicator and Stop follow the game."""
    t0 = time.time() - 3600
    rig = Rig(store, t0)
    store.set_call_pref(GAME_KEY, "always")
    store.set_call_pref("discord", "never")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        st = _status(store)
        assert st["call"]["id"] == rig.w.game.call_id and st["call"]["status"] == "recording" and st["recording"]
    finally:
        rig.close()
    other = Store.open(store.path.parent / "hp.db")
    try:
        rig = Rig(other, t0, headphones=False)                      # no headphones: the mic is closed, the
        other.set_call_pref(GAME_KEY, "always")                    # game's own audio is still kept
        rig.stubs.hold("mic", VALORANT, t0 - 5)
        rig.game_in_front()
        rig.step(0)
        st = _status(other)
        assert st["call"]["status"] == "waiting_headphones" and st["mic"]["state"] == "paused"
        assert st["recording"] is True                               # Stop stays reachable
        rig.close()
    finally:
        other.close()


def test_r6_a_game_that_does_not_record_is_never_a_call_for_voice(store):
    """Review finding 6: voice treats any ``call`` as call mode; a never / waiting game must not be one."""
    t0 = time.time() - 3600
    store.set_call_pref(GAME_KEY, "never")
    rig = Rig(store, t0)
    rig.stubs.hold("mic", VALORANT, t0 - 5)
    rig.game_in_front()
    try:
        rig.step(0)
        st = _status(store)
        assert st["call"] is None and st["recording"] is False and st["pending_card"] is None
        assert [(s["kind"], s["status"]) for s in st["sessions"]] == [("game", "declined")]
    finally:
        rig.close()


class FakeLoopback:
    """Stands in for :class:`yuki.memory.loopback.LoopbackSource` opening on its own thread."""

    def __init__(self, ready: bool = False) -> None:
        self.ready, self.alive, self.error = ready, True, None
        self.device_used, self.opened_as, self.tried = "process loopback pid 100", "process_loopback fake", ["ok"]
        self.started = 0

    def start(self) -> None:
        self.started += 1

    def read(self, timeout_s: float):
        time.sleep(min(timeout_s, 0.01))
        return None

    def stop(self) -> None:
        self.alive = False


def test_r5_r8_a_track_opens_in_the_background_and_a_dead_capture_is_logged_and_retried(store):
    """Review findings 5 and 8: the poll never waits for a track to open; a capture thread that dies is a failed
    track (logged, retried after track_retry_s), never one shown "recording" while it captures nothing."""
    t0 = time.time() - 3600
    rig = Rig(store, t0, track_retry_s=60.0)
    first = FakeLoopback()
    rig.track_source = lambda spec: first
    store.set_call_pref("discord", "always")
    rig.stubs.hold("mic", DISCORD, t0 - 30)
    try:
        rig.step(0)
        act = rig.w.active
        assert first.started == 1 and act.track_state == "starting"
        first.ready = True
        rig.step(5)
        assert act.track_state == "recording" and rig.events("call_track_ready")
        first.alive, first.error = False, "OSError: GetBuffer 0x88890004"   # the device went away
        rig.step(10)
        assert act.track is None and act.track_state == "unavailable" and "GetBuffer" in act.track_reason
        assert "GetBuffer" in rig.events("call_track_unavailable")[-1]["reason"]
        rig.step(30)
        assert len(rig.tracks_opened) == 1                           # not before track_retry_s
        second = FakeLoopback(ready=True)
        rig.track_source = lambda spec: second
        rig.step(75)
        assert len(rig.tracks_opened) == 2 and act.track_state == "recording"
    finally:
        rig.close()


def test_r7_others_words_reach_no_generic_capture_reader(store):
    """Review finding 7: a call transcript's ``lines`` (the others' words) never reach the generic readers that feed
    prompts; the call readers still get everything."""
    t = time.time() - 600
    cid = store.start_call(app="Discord", process="discord", via="screen", at=t)
    mine = {"at": t + 1, "end": t + 2, "text": "I'll bring the charger", "who": "user"}
    theirs = {"at": t + 3, "end": t + 4, "text": "my bank pin is in the drawer", "who": "others"}
    capture_id = store.add_call_side(cid, app="Discord", process="discord", started_at=t, ended_at=t + 5,
                                     utterances=[mine], lines=[mine, theirs], final=True)
    with store._lock:
        store._conn.execute("UPDATE captures SET journal_batch_id = 9 WHERE id = ?", (capture_id,))
    thread_id = store.call_record(cid).thread_id
    assert store.captures_between(t - 1, t + 10, limit=10) == []
    texts = [store.first_capture_text(thread_id)[1],   # captures_by_ids went with kClean (18 Oct, nothing called it)
             store.batch_captures([9])[9][0]["text"],
             store.recent_captures(t - 1, t + 10, skip_kinds=())[0]["text"]]
    for text in texts:
        assert "charger" in text and "drawer" not in text
    data = store.call_side_captures(cid)[0]["data"]
    assert [ln["who"] for ln in Store.call_lines(data)] == ["user", "others"]
