"""Report 76 quick win 2: a call that shows "recording" but keeps nothing is flagged (worker, island, doctor), and a
call row survives a memory-service stop (the next start adopts it).

Temp databases, stubbed microphone holders and a simulated clock (the helpers of ``test_call_session``); nothing reads
the desktop, the microphone or the speakers, and the real database is never opened.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import pytest

from test_call_session import discord_call, events, make, sim, step, store  # noqa: F401 - fixtures
from yuki.memory.calls import TRACKS_STATE, CallConfig, _Active
from yuki.memory.store import Store

# -- a service stop is not the call ending ---------------------------------------------------------------------------


def test_a_service_stop_leaves_the_call_open_and_the_next_start_adopts_it(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(5, 600, 5):
        step(w, sim, t0 + dt)
    w.close("service_stop", now=t0 + 602)                       # an install stops the service mid-call
    row = store.call_record(call_id)
    assert row.status != "ended" and row.ended_at is None and row.last_alive_at == t0 + 602
    assert events(w, "call_suspended") and not events(w, "call_ended")
    w2 = make(store, stubs)                                     # the new build starts a minute later
    sim.t = t0 + 660
    assert w2.recover(t0 + 660) == 0
    assert w2.active is not None and w2.active.call_id == call_id and len(store.calls()) == 1
    assert events(w2, "call_resumed")[0]["down_s"] <= 60


def test_a_call_that_ended_while_the_service_was_stopped_ends_at_the_stop(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(5, 300, 5):
        step(w, sim, t0 + dt)
    w.close("service_stop", now=t0 + 301)
    stubs.mic.clear()                                           # the user hung up meanwhile
    w2 = make(store, stubs)
    assert w2.recover(t0 + 3600) == 1 and w2.active is None
    row = store.call_record(call_id)
    assert row.status == "ended" and row.ended_at == t0 + 301 and row.stop_reason == "service_restart"


def test_other_reasons_still_end_the_row(store, sim):
    w, _stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    w.close("disabled", now=t0 + 30)
    assert store.call_record(call_id).status == "ended" and not events(w, "call_suspended")


# -- the stall flag --------------------------------------------------------------------------------------------------


def _session(*, listening_since, last_audio_at=None, last_kept_at=None):
    return SimpleNamespace(listening_since=listening_since, last_audio_at=last_audio_at, last_kept_at=last_kept_at)


def _row(call_id=7, app="Discord", **fields):
    act = _Active(call_id, "discord", app, "screen", None, 0.0, 0.0, status="recording")
    for k, v in fields.items():
        setattr(act, k, v)
    return act


def _stall_worker(store):
    from test_av_signals import Stubs

    w = make(store, Stubs())
    return w


def test_no_new_chunk_for_the_stall_time_is_flagged_once_then_cleared(store):
    w = _stall_worker(store)
    cfg = CallConfig(stall_min=10.0)
    now = 10_000.0
    mic = _session(listening_since=now - 3600, last_audio_at=now - 1, last_kept_at=now - 11 * 60)
    w.active = _row(session=mic)
    w._check_stall(cfg, now)
    assert w._stall["reason"] == "no_speech" and w._stall["minutes"] == 11
    w._check_stall(cfg, now + 60)
    assert len(events(w, "call_recording_stalled")) == 1        # once per stall, not every poll
    assert w.track_states()["stall"]["call_id"] == 7
    mic.last_kept_at = now + 100                                # a chunk was kept again
    w._check_stall(cfg, now + 120)
    assert w._stall is None and events(w, "call_recording_flowing")


def test_a_microphone_that_brings_no_audio_at_all_is_a_dead_capture(store):
    w = _stall_worker(store)
    now = 10_000.0
    w.active = _row(session=_session(listening_since=now - 15 * 60))   # opened, never read a sample
    w._check_stall(CallConfig(stall_min=10.0), now)
    assert w._stall["reason"] == "no_audio"


def test_the_other_sides_chunks_keep_the_row_flowing_while_the_user_is_muted(store):
    w = _stall_worker(store)
    now = 10_000.0
    mic = _session(listening_since=now - 3600, last_audio_at=now - 1, last_kept_at=None)
    track = _session(listening_since=now - 3600, last_kept_at=now - 30)
    w.active = _row(session=mic, track=track, track_target=7)
    w._check_stall(CallConfig(stall_min=10.0), now)
    assert w._stall is None


def test_nothing_listening_is_never_a_stall(store):
    w = _stall_worker(store)
    now = 10_000.0
    w.active = _row(session=_session(listening_since=None, last_kept_at=now - 3600))   # paused: already explained
    w._check_stall(CallConfig(stall_min=10.0), now)
    assert w._stall is None


def test_a_fresh_recording_is_not_flagged_before_the_stall_time(store):
    w = _stall_worker(store)
    now = 10_000.0
    w.active = _row(session=_session(listening_since=now - 9 * 60, last_audio_at=now - 1))
    w._check_stall(CallConfig(stall_min=10.0), now)
    assert w._stall is None


def test_the_recording_session_measures_audio_but_keeps_no_silence(store):
    import numpy as np

    from yuki.memory.calls import ArraySource, RecordingSession

    cfg = CallConfig(vad=False)
    s = RecordingSession(store, call_id=store.start_call(app="Discord", process="discord", via="test"), app="Discord",
                         process="discord", cfg=cfg, source_factory=lambda: ArraySource(np.zeros(16000 * 3)),
                         log=lambda *a, **k: None)
    s.start()
    try:
        assert s.listening_since is not None
        assert s.wait_drained(5.0)
        assert s.last_audio_at is not None and s.last_kept_at is None   # three seconds of silence: audio, no chunk
    finally:
        s.stop("test")
    assert s.listening_since is None


# -- what the island and the doctor say ------------------------------------------------------------------------------


def test_call_status_carries_the_stall_and_the_island_says_it(store):
    from yuki.memory.api import MemoryClient
    from yuki.ui.call_words import live_view

    now = time.time()
    call_id = store.start_call(app="Discord", process="discord", via="screen", at=now - 1800, status="recording",
                               consent="always", consent_by="pref", hold_since=now - 1810)
    stall = {"call_id": call_id, "app": "Discord", "kind": "call", "since": now - 720, "minutes": 12,
             "reason": "no_audio"}
    state = {"rows": [call_id], "mic": {"state": "recording", "call_id": call_id, "reason": None},
             "tracks": [{"call_id": call_id, "app": "Discord", "process": "discord", "track_kind": "call",
                         "state": "recording", "reason": None}], "stall": stall}
    store.set_nudge_state(TRACKS_STATE, json.dumps(state))
    status = MemoryClient(store).call_status()
    assert status["stall"]["minutes"] == 12
    view = live_view(status)
    assert view["line"].endswith("no sound from your mic for 12 min")
    assert view["short_words"] == "no sound from your mic for 12 min"
    assert "No sound from your mic for 12 min." in view["sentence"]
    # review F4: a quiet voice channel (audio arrives, nobody speaks) is normal: the island says nothing about it
    state["stall"] = dict(stall, reason="no_speech")
    store.set_nudge_state(TRACKS_STATE, json.dumps(state))
    quiet = live_view(MemoryClient(store).call_status())
    assert "min" not in quiet["line"] and "mic" not in quiet["sentence"].replace("Your mic", "")
    store.set_nudge_state(TRACKS_STATE, json.dumps({"rows": [call_id], "stall": None}))
    assert "no sound" not in live_view(MemoryClient(store).call_status())["line"]


def test_a_stall_for_another_row_is_not_shown(store):
    from yuki.memory.api import MemoryClient

    now = time.time()
    call_id = store.start_call(app="Discord", process="discord", via="screen", at=now - 60, status="recording")
    store.set_nudge_state(TRACKS_STATE, json.dumps({"rows": [call_id], "stall": {"call_id": call_id + 99,
                                                                                  "minutes": 30}}))
    assert MemoryClient(store).call_status()["stall"] is None


@pytest.fixture()
def doctor_db(tmp_path, monkeypatch):
    import yuki.memory.api as api

    monkeypatch.setattr(api, "service_running", lambda db: True)
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


def test_doctor_is_ok_when_every_recording_kept_audio(doctor_db):
    from yuki.core.doctor import OK, check_call_recording

    now = time.time()
    c = doctor_db.start_call(app="Discord", process="discord", via="screen", at=now - 7200, status="recording",
                             consent="yes")
    doctor_db.update_call(c, recording_started_at=now - 7100, status="ended", ended_at=now - 3600)
    doctor_db.write_spool(c, 1, start_at=now - 7000, audio_s=6.0, speech_s=5.0, payload=b"x", codec="opus")
    assert check_call_recording(doctor_db.path, now=now).status == OK


def test_doctor_flags_a_silent_loss_a_live_stall_and_a_stuck_row(doctor_db):
    from yuki.core.doctor import WARN, check_call_recording

    now = time.time()
    lost = doctor_db.start_call(app="Discord", process="discord", via="screen", at=now - 2 * 86400, status="recording",
                                consent="always")
    doctor_db.update_call(lost, recording_started_at=now - 2 * 86400 + 10, status="ended",
                          ended_at=now - 2 * 86400 + 3600)
    short = doctor_db.start_call(app="Zoom", process="zoom", via="screen", at=now - 86400, status="recording")
    doctor_db.update_call(short, recording_started_at=now - 86400, status="ended", ended_at=now - 86400 + 120)
    live = doctor_db.start_call(app="Discord", process="discord", via="screen", at=now - 1200, status="recording")
    doctor_db.update_call(live, last_alive_at=now - 5)
    doctor_db.set_nudge_state(TRACKS_STATE, json.dumps({"stall": {"call_id": live, "app": "Discord", "minutes": 14,
                                                                  "reason": "no_audio"}}))
    stuck = doctor_db.start_call(app="VALORANT", process="valorant", via="game", at=now - 5 * 3600,
                                 status="recording", kind="game")
    doctor_db.update_call(stuck, last_alive_at=now - 4 * 3600)
    c = check_call_recording(doctor_db.path, now=now)
    assert c.status == WARN
    assert "the microphone sends no sound for 14 min" in c.detail
    assert "VALORANT voice chat from" in c.detail and "still marked open" in c.detail
    assert "1 recorded call in the last 7 days kept nothing" in c.detail and "Discord, 59 min" in c.detail
    assert "Zoom" not in c.detail                                # two minutes is under the stall time


def test_doctor_leaves_a_quiet_voice_channel_alone(doctor_db):
    # review F4: "no_speech" (silence arrives, nothing kept) is a quiet channel, not a broken microphone
    from yuki.core.doctor import OK, check_call_recording

    now = time.time()
    live = doctor_db.start_call(app="Discord", process="discord", via="screen", at=now - 3000, status="recording")
    doctor_db.update_call(live, last_alive_at=now - 5)
    doctor_db.set_nudge_state(TRACKS_STATE, json.dumps({"stall": {"call_id": live, "app": "Discord", "minutes": 47,
                                                                  "reason": "no_speech"}}))
    c = check_call_recording(doctor_db.path, now=now)
    assert c.status == OK and "47" not in c.detail


def test_doctor_reads_no_live_stall_while_the_service_is_stopped(doctor_db, monkeypatch):
    # review F5: a stopped service leaves its rows open on purpose; the last flag is not a live stall
    import yuki.memory.api as api
    from yuki.core.doctor import OK, check_call_recording

    monkeypatch.setattr(api, "service_running", lambda db: False)
    now = time.time()
    live = doctor_db.start_call(app="Discord", process="discord", via="screen", at=now - 3000, status="recording")
    doctor_db.update_call(live, last_alive_at=now - 900)
    doctor_db.set_nudge_state(TRACKS_STATE, json.dumps({"stall": {"call_id": live, "app": "Discord", "minutes": 12,
                                                                  "reason": "no_audio"}}))
    assert check_call_recording(doctor_db.path, now=now).status == OK


def test_a_service_stop_clears_the_stall_it_leaves_behind(store, sim):
    # review F5: close("service_stop") suspends the rows and writes "no stall" for the UI and the doctor
    w, _stubs, t0 = discord_call(store, sim)
    w._stall = {"call_id": w.active.call_id, "app": "Discord", "kind": "call", "since": t0, "minutes": 12,
                "reason": "no_audio"}
    w._note_tracks()
    assert json.loads(store.nudge_state(TRACKS_STATE))["stall"] is not None
    w.close("service_stop", now=t0 + 30)
    assert w._stall is None and json.loads(store.nudge_state(TRACKS_STATE))["stall"] is None


def test_doctor_dates_are_natural():
    from datetime import datetime

    from yuki.core.doctor import _when

    assert _when(datetime(2026, 10, 3, 20, 0).timestamp()) == "3 October, 8 PM"
    assert _when(datetime(2026, 10, 3, 0, 15).timestamp()) == "3 October, 12:15 AM"
