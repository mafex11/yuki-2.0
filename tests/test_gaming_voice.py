"""A full-screen game's own voice chat is gaming, not a meeting or a call (the 26 Sept Valorant case).

Synthetic timelines on temp databases: stubbed microphone/camera holders, render sessions, full-screen state
and GPU shares. Nothing here reads the live desktop, the microphone, the speakers or the GPU.
"""

from __future__ import annotations

import time

import pytest

from test_av_signals import DISCORD, Stubs, _facts, _recorder, _rows, _worker, shows  # noqa: F401 - shared stubs

from yuki.memory.avsignals import GAME_GPU_SHARE, Holder, gaming_with_voice

VALORANT = r"C:\Riot Games\VALORANT\live\ShooterGame\Binaries\Win64\VALORANT-Win64-Shipping.exe"
ZOOM = r"C:\Apps\Zoom\Zoom.exe"
MEETINGS = "[meetings]\nhold_s = 20\nend_grace_s = 10\n"


@pytest.fixture()
def store(tmp_path):
    from yuki.memory.store import Store

    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def _fullscreen(rec, hwnds: set[int], gpu: float | None) -> None:
    rec._w.is_fullscreen_front = lambda h: h in hwnds
    rec._gpu_fn = lambda pids: gpu


def _play_for(rec, t0: float, seconds: float) -> None:
    rec._poll_av(t0 + seconds)
    rec._check_meeting(t0 + seconds)


def test_valorant_voice_chat_in_full_screen_is_not_a_meeting(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, VALORANT, "VALORANT"), "VALORANT  ")}, MEETINGS)
    _fullscreen(rec, {1}, gpu=72.0)
    rec._input_at = t0
    stubs.hold("mic", VALORANT, t0 + 1)
    stubs.play(10, VALORANT)
    rec._open(1, t0)
    for s in (15, 30, 60, 120):
        _play_for(rec, t0, s)
        assert rec._current.meeting is None, s
    assert rec.current_meeting() is None
    assert rec.counters["gaming_voice"] == 1 and not rec._meeting_sites
    rec._close(t0 + 130)
    assert all(meeting is None for meeting, _title, _host in _rows(store))


def test_without_a_gpu_reading_full_screen_mic_no_camera_no_page_is_gaming(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, VALORANT, "VALORANT"), "VALORANT")}, MEETINGS)
    _fullscreen(rec, {1}, gpu=None)
    rec._input_at = t0
    stubs.hold("mic", VALORANT, t0 - 60)
    rec._open(1, t0)
    _play_for(rec, t0, 30)
    assert rec._current.meeting is None


def test_a_full_screen_meeting_that_barely_uses_the_gpu_is_still_a_meeting(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, ZOOM, "Zoom"), "Zoom Meeting")}, MEETINGS)
    _fullscreen(rec, {1}, gpu=3.0)
    rec._input_at = t0
    stubs.hold("mic", ZOOM, t0 + 1)
    rec._open(1, t0)
    _play_for(rec, t0, 30)
    assert rec._current.meeting == "Zoom"


def test_a_full_screen_app_holding_the_camera_is_a_meeting(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, ZOOM, "Zoom"), "Zoom")}, MEETINGS)
    _fullscreen(rec, {1}, gpu=80.0)
    rec._input_at = t0
    stubs.hold("mic", ZOOM, t0 - 60)
    stubs.hold("camera", ZOOM, t0 - 60)
    rec._open(1, t0)
    _play_for(rec, t0, 1)
    assert rec._current.meeting == "Zoom"


def test_the_same_game_windowed_keeps_the_old_measured_rule(store, tmp_path):
    t0 = time.time()
    stubs = Stubs()
    rec = _recorder(store, tmp_path, stubs, {1: (10, _facts(10, VALORANT, "VALORANT"), "VALORANT")}, MEETINGS)
    _fullscreen(rec, set(), gpu=72.0)
    rec._input_at = t0
    stubs.hold("mic", VALORANT, t0 - 60)
    rec._open(1, t0)
    _play_for(rec, t0, 1)
    assert rec._current.meeting == "VALORANT"   # not full screen: the measured meeting rule, as before


# -- calls ---------------------------------------------------------------------------------------------------


def _front(pid: int, exe: str):
    return lambda: {"hwnd": 1, "pid": pid, "exe": exe}


def test_valorant_voice_is_not_a_call_but_discord_in_the_background_still_is(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", VALORANT, now - 60)
    stubs.play(10, VALORANT)
    w = _worker(store, stubs)
    w.foreground_fn = _front(10, VALORANT)
    w.gpu_fn = lambda pids: 65.0
    w.step(now)
    assert w.active is None                                  # gaming with voice: no call, no consent card
    stubs.hold("mic", DISCORD, now - 30)                     # the squad joins a Discord call
    stubs.play(4242, DISCORD)
    shows(w, DISCORD, now - 20)                              # its voice-connected panel was seen when joining
    shows(w, VALORANT, now + 1)                              # (a game screen showing call controls stays gaming)
    w.step(now + 5)
    assert w.active is not None and w.active.key == "discord"


def test_a_full_screen_call_app_with_low_gpu_stays_a_call(store):
    now = time.time()
    stubs = Stubs()
    stubs.hold("mic", ZOOM, now - 60)
    stubs.play(20, ZOOM)
    w = _worker(store, stubs)
    w.foreground_fn = _front(20, ZOOM)
    w.gpu_fn = lambda pids: 2.0
    shows(w, ZOOM, now - 30)
    w.step(now)
    assert w.active is not None and w.active.key == "zoom"


def test_gaming_with_voice_signal_itself():
    game = Holder("VALORANT-Win64-Shipping.exe", False, VALORANT, 0.0)
    assert gaming_with_voice(game, fullscreen=True, shows_page=False, gpu_fn=lambda p: GAME_GPU_SHARE)[0]
    assert not gaming_with_voice(game, fullscreen=True, shows_page=False, gpu_fn=lambda p: 1.0)[0]
    assert not gaming_with_voice(game, fullscreen=False, shows_page=False, gpu_fn=lambda p: 90.0)[0]
    assert not gaming_with_voice(game, fullscreen=True, shows_page=True, gpu_fn=lambda p: 90.0)[0]
    gaming, facts = gaming_with_voice(game, fullscreen=True, shows_page=False, camera=[game], gpu_fn=lambda p: 90.0)
    assert not gaming and facts["camera"]
