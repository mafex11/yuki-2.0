"""One call = one session (the design notes §7-8).

The microphone hold is the call: the screen only decides that a hold is a call (the start) and ends a held call
only on a persistent, reliable contradiction; a re-grab of the microphone by the same app is the same call; a
restarted service adopts the row of a hold that goes on and ends any other at its last alive moment.

Temp databases, stubbed microphone holders (the ConsentStore), a stub judge (the real one is a model) and a
simulated clock (``CallEvidence`` throttles on ``time.monotonic``, so the module's clock is replaced the way
``%TEMP%\\yuki-dig\\call-pill\\repro.py`` does). Nothing reads the desktop, the microphone or the speakers.
"""

from __future__ import annotations

import random
import time
import types
from datetime import datetime

import pytest

from test_av_signals import DISCORD, PYTHON, Stubs
from yuki.memory import callevidence as ce
from yuki.memory.callevidence import CallEvidence, Life, Verdict
from yuki.memory.calls import HOLDERS_STATE, CallConfig, CallLife, CallWorker, lingering_call
from yuki.memory.store import Store

ZEN = r"C:\Program Files\Zen Browser\zen.exe"
#: A joined Discord voice panel as the live reads had it (68-80 controls in all 183 in-call reads).
PANEL = ("Voice Connected", "General / Friends", "Disconnect", "Noise Suppression", "Mute", "Deafen") + tuple(
    f"member {i}" for i in range(66))
#: The four shapes of the live frames that ended calls, read mid alt-tab: 0, 18, 21 and 22 controls.
DEGRADED = {n: tuple(f"part {i}" for i in range(n)) for n in (0, 18, 21, 22)}
#: A fully drawn text channel of the same app: a reliable "not in a call" (live: 80 controls).
TEXT = ("#general", "#memes", "Message #memes", "User Settings") + tuple(f"message {i}" for i in range(70))
MEET_JOINED = ("Leave call", "Turn off microphone", "Share screen", "People", "Meeting details") + tuple(
    f"tile {i}" for i in range(20))
MEET_MUTED = ("Leave call", "Turn on microphone", "Share screen", "People", "Meeting details") + tuple(
    f"tile {i}" for i in range(20))
MEET_LEFT = ("You left the meeting", "Rejoin", "Return to home screen", "Submit feedback") + tuple(
    f"link {i}" for i in range(16))


def judge(screen):
    """Stand-in for the model: nothing drawn is unclear; a leave or disconnect control is a joined call;
    anything else (the half-drawn frames included, as the live model judged them) is not in a call."""
    if not screen.controls:
        return Verdict(in_call=False, unclear=True)
    joined = any(c in ("Disconnect", "Leave call") for c in screen.controls)
    return Verdict(in_call=joined, service=("Google Meet" if screen.host == "meet.google.com" else "Discord")
                   if joined else "")


class Sim:
    def __init__(self, t: float) -> None:
        self.t = t


@pytest.fixture()
def sim(monkeypatch):
    clock = Sim(time.time() - 3 * 86400.0)   # in the past: what the call worker writes is history to the API
    monkeypatch.setattr(ce, "time", types.SimpleNamespace(monotonic=lambda: clock.t, time=lambda: clock.t,
                                                          perf_counter=time.perf_counter))
    return clock


@pytest.fixture()
def store(tmp_path):
    s = Store.open(tmp_path / "memory.db")
    yield s
    s.close()


class NullSource:
    device_used = "null"
    opened_as = "null"

    def start(self) -> None:
        pass

    def read(self, timeout_s: float):
        time.sleep(min(timeout_s, 0.01))
        return None

    def stop(self) -> None:
        pass


def make(store: Store, stubs: Stubs, *, record: bool = False, rejudge_s: float = ce.REJUDGE_S, **cfg) -> CallWorker:
    logs: list[tuple[str, dict]] = []

    def log(kind: str, **fields) -> None:
        logs.append((kind, fields))

    config = CallConfig(**{"end_grace_s": 30.0, "vad": False, **cfg})
    worker = CallWorker(
        store, signals=stubs.signals(frozenset({PYTHON.casefold()})), config=config, log=log,
        headphones_fn=(lambda c: (True, {"form_factor": "headphones", "device": "Headphones"})) if record else
        (lambda c: (False, {"form_factor": "speakers", "device": "Speakers"})),
        source_factory=(lambda c, app: NullSource()) if record else
        (lambda c, app: pytest.fail("the microphone must not be opened")),
        evidence=CallEvidence(judge_fn=judge, rejudge_s=rejudge_s, log=log), foreground_fn=lambda: None)
    worker._tell_ui = lambda: None
    worker.sites_fn = lambda key: 8 if key == "zen" else 1
    worker.logs = logs   # type: ignore[attr-defined]
    return worker


def see(w: CallWorker, exe: str, at: float, controls, host: str | None = None, title: str = "") -> None:
    key = exe.rsplit("\\", 1)[-1].rsplit(".", 1)[0].casefold()
    w.evidence.seen({"process": key, "exe": exe, "hwnd": 7, "at": at, "title": title, "controls": list(controls),
                     "host": host if host is not None else ("discord.com" if key == "discord" else None)})


def step(w: CallWorker, sim: Sim, at: float) -> None:
    sim.t = at
    w.step(at)


def session_start(store: Store) -> float | None:
    """What the pill counts from: ``call_status()``'s ``session_started_at`` (epoch s), None without a call."""
    from yuki.memory.api import MemoryClient

    call = MemoryClient(store).call_status()["call"]
    return datetime.fromisoformat(call["session_started_at"]).timestamp() if call else None


def events(w: CallWorker, kind: str) -> list[dict]:
    return [f for k, f in w.logs if k == kind]


def stop_sessions(*workers: CallWorker) -> None:
    for w in workers:
        if w.active is not None and w.active.session is not None:
            w.active.session.stop("test")


def discord_call(store: Store, sim: Sim, **cfg) -> tuple[CallWorker, Stubs, float]:
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", DISCORD, t0 - 20)
    store.set_call_pref("discord", "always")
    w = make(store, stubs, **cfg)
    see(w, DISCORD, t0, PANEL, title="join")
    step(w, sim, t0 + 1)
    assert w.active is not None
    return w, stubs, t0


# -- S1: a degraded frame while the microphone is held ------------------------------------------------------


@pytest.mark.parametrize("n", sorted(DEGRADED))
def test_one_degraded_frame_with_the_microphone_held_never_ends_the_call(store, sim, n):
    """The 2 Oct 20:34 pattern (05 §0): 90 minutes in, one frame read mid alt-tab, the hold never let go."""
    w, stubs, t0 = discord_call(store, sim)
    call_id, start = w.active.call_id, session_start(store)
    for k in range(1, 61):
        step(w, sim, t0 + k * 90)
        if k % 10 == 0:
            see(w, DISCORD, t0 + k * 90, PANEL, title=f"ch{k}")
    now = t0 + 5400
    see(w, DISCORD, now + 2, DEGRADED[n], title="alt-tab frame")
    last = 0.0
    for dt in range(3, 32 * 60, 5):
        step(w, sim, now + dt)
        if dt % 60 == 3:
            assert session_start(store) == start
            elapsed = now + dt - start
            assert elapsed > last   # the pill's time only ever goes up
            last = elapsed
    assert w.active is not None and w.active.call_id == call_id and len(store.calls()) == 1
    assert store.call_record(call_id).detected_at == t0 + 1
    counted = [f for f in events(w, "call_evidence_judged") if f["controls"] == n]
    assert counted and counted[-1]["counted"] == "none"   # neither a call nor no call: unclear or unhealthy
    assert not events(w, "call_ended")


def test_a_half_drawn_frame_counts_as_neither_by_its_measured_size():
    ev = CallEvidence(judge_fn=judge, rejudge_s=0.0)
    at = time.time()
    base = {"process": "discord", "exe": DISCORD, "host": "discord.com"}
    ev.seen({**base, "at": at, "title": "a", "controls": list(PANEL)})
    ev.judge(["discord"])
    ev.seen({**base, "at": at + 5, "title": "b", "controls": list(DEGRADED[22])})   # 22 < 0.5 x 72
    ev.judge(["discord"])
    life = ev.life("discord")
    assert life.positive_at == at and life.negatives == () and life.median_controls == len(PANEL)
    ev.seen({**base, "at": at + 9, "title": "c", "controls": list(TEXT)})            # 74 controls: healthy
    ev.judge(["discord"])
    assert ev.life("discord").negatives == (at + 9,)
    assert ev.state("discord", at - 30, at + 10) == (None, "")   # a contradicted site starts nothing


# -- persistent contradiction ---------------------------------------------------------------------------------


def test_three_reliable_negatives_over_two_minutes_end_a_held_call_once(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    see(w, DISCORD, t0 + 100, TEXT, title="text")        # left the voice channel; the hold is stuck on
    step(w, sim, t0 + 101)
    see(w, DISCORD, t0 + 165, TEXT, title="text")        # the same screen read again (65 s later)
    step(w, sim, t0 + 166)
    assert w.active is not None                         # two sights: one is never enough, two not persistent
    see(w, DISCORD, t0 + 231, TEXT, title="text")        # three sights spanning 131 s
    step(w, sim, t0 + 232)
    assert w.active is None
    ended = events(w, "call_ended")
    assert len(ended) == 1 and ended[0]["reason"] == "screen_contradicts" and ended[0]["neg_frames"] == 3
    assert ended[0]["held"] is True and ended[0]["hold_gap_s"] == 0.0
    for dt in range(240, 900, 5):                         # the stuck hold goes on: no second row
        step(w, sim, t0 + dt)
    assert len(store.calls()) == 1 and store.call_record(call_id).stop_reason == "screen_contradicts"


def test_you_left_the_meeting_with_the_microphone_let_go_ends_within_end_grace(store, sim):
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", ZEN, t0 - 30)
    w = make(store, stubs)
    see(w, ZEN, t0, MEET_JOINED, host="meet.google.com")
    step(w, sim, t0 + 1)
    call_id = w.active.call_id
    step(w, sim, t0 + 60)
    stubs.mic.clear()
    see(w, ZEN, t0 + 62, MEET_LEFT, host="meet.google.com")
    for dt in range(65, 130, 5):
        step(w, sim, t0 + dt)
        if w.active is None:
            break
    row = store.call_record(call_id)
    assert row.status == "ended" and row.ended_at <= t0 + 62 + 30 + 5 and row.stop_reason == "screen_contradicts"


# -- re-grabs and releases ---------------------------------------------------------------------------------------


def test_a_regrab_with_a_new_since_under_a_busy_screen_is_the_same_call(store, sim):
    """S3 (05 §4 M3): Discord re-opens its microphone stream at +30 s while the panel changes every 3 s and the
    re-judge throttle holds the newest frame back."""
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(3, 200, 3):
        if dt == 30:
            stubs.mic.clear()
            stubs.hold("mic", DISCORD, t0 + 30)
        see(w, DISCORD, t0 + dt, PANEL, title=f"speaking {dt}")
        step(w, sim, t0 + dt)
        assert w.active is not None and w.active.call_id == call_id
    assert len(store.calls()) == 1
    regrab = events(w, "call_regrab")
    assert len(regrab) == 1 and regrab[0]["new_since"] == t0 + 30 and regrab[0]["old_since"] == t0 - 20
    assert store.call_record(call_id).hold_since == t0 + 30   # what a restart would match


def test_a_short_release_is_the_same_call_a_long_one_is_a_new_call(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    step(w, sim, t0 + 60)
    stubs.mic.clear()
    for dt in range(65, 85, 5):
        step(w, sim, t0 + dt)
    stubs.hold("mic", DISCORD, t0 + 82)                 # back after ~20 s: a re-grab
    for dt in range(85, 300, 5):
        step(w, sim, t0 + dt)
    assert w.active.call_id == call_id and len(store.calls()) == 1
    assert events(w, "call_regrab")[0]["gap_s"] == pytest.approx(22.0, abs=0.1)
    stubs.mic.clear()                                    # left for 5 minutes
    for dt in range(300, 600, 5):
        step(w, sim, t0 + dt)
    assert w.active is None and store.call_record(call_id).stop_reason == "mic_released"
    stubs.hold("mic", DISCORD, t0 + 600)
    step(w, sim, t0 + 602)
    assert w.active is None                              # a new hold needs a new in-call screen to start
    see(w, DISCORD, t0 + 603, PANEL, title="rejoined")
    step(w, sim, t0 + 604)
    assert w.active is not None and w.active.call_id != call_id and len(store.calls()) == 2


def test_meet_mute_lets_go_of_the_microphone_and_the_screen_keeps_the_call(store, sim):
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", ZEN, t0 - 30)
    w = make(store, stubs)
    see(w, ZEN, t0, MEET_JOINED, host="meet.google.com")
    step(w, sim, t0 + 1)
    call_id = w.active.call_id
    step(w, sim, t0 + 60)
    stubs.mic.clear()                                    # muted: Meet lets go of the microphone
    see(w, ZEN, t0 + 61, MEET_MUTED, host="meet.google.com")   # the page shows the call, muted
    for dt in range(65, 500, 5):
        step(w, sim, t0 + dt)
    assert w.active is not None and w.active.call_id == call_id and w.active.alive_by == "screen"
    stubs.hold("mic", ZEN, t0 + 500)                     # unmuted after 7 minutes: the same call
    for dt in range(500, 600, 5):
        step(w, sim, t0 + dt)
    assert w.active.call_id == call_id and len(store.calls()) == 1 and events(w, "call_regrab")
    assert [e["by"] for e in events(w, "call_alive_by")][:2] == ["release_grace", "screen"]


def test_without_the_microphone_an_old_screen_does_not_keep_a_call(store, sim):
    """The in-call frame must come after the microphone was let go: one from before is no proof of anything."""
    w, stubs, t0 = discord_call(store, sim)
    step(w, sim, t0 + 30)
    stubs.mic.clear()
    for dt in range(35, 200, 5):
        step(w, sim, t0 + dt)
    assert w.active is None and events(w, "call_ended")[0]["reason"] == "mic_released"


def test_recording_pauses_while_the_app_lets_go_and_resumes_on_the_regrab(store, sim):
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", DISCORD, t0 - 20)
    store.set_call_pref("discord", "always")
    w = make(store, stubs, record=True)
    try:
        see(w, DISCORD, t0, PANEL)
        step(w, sim, t0 + 1)
        assert w.active.session is not None and w.active.session.listening
        stubs.mic.clear()
        step(w, sim, t0 + 6)
        assert not w.active.session.listening and store.call_record(w.active.call_id).status == "recording"
        assert any(f.get("reason") == "mic_released" for f in events(w, "call_recording_paused"))
        stubs.hold("mic", DISCORD, t0 + 9)
        step(w, sim, t0 + 11)
        assert w.active.session.listening and events(w, "call_recording_resumed")
    finally:
        stop_sessions(w)


# -- restarts ------------------------------------------------------------------------------------------------------


def test_a_restart_mid_call_with_the_hold_unchanged_adopts_the_same_row(store, sim):
    """S2 (05 §4 M2): the service restarts an hour in; the hold goes on; Discord is not read again for 5 min."""
    w, stubs, t0 = discord_call(store, sim)
    call_id, start = w.active.call_id, session_start(store)
    for dt in range(5, 3600, 5):
        step(w, sim, t0 + dt)
    # the old process dies (no clean end); a new one starts on the same database 60 s later
    w2 = make(store, stubs)
    sim.t = t0 + 3660
    assert w2.recover(t0 + 3660) == 0
    assert w2.active is not None and w2.active.call_id == call_id
    resumed = events(w2, "call_resumed")
    assert resumed and resumed[0]["held"] and resumed[0]["down_s"] <= 75
    for dt in range(3665, 3960, 5):
        step(w2, sim, t0 + dt)
    assert w2.active.call_id == call_id and len(store.calls()) == 1 and session_start(store) == start
    assert store.call_record(call_id).status not in ("ended",)


def test_a_restart_after_the_hold_ended_ends_the_row_at_its_last_alive_moment(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(5, 600, 5):
        step(w, sim, t0 + dt)
    alive = store.call_record(call_id).last_alive_at
    assert t0 + 580 <= alive <= t0 + 600                 # written at most every 15 s
    stubs.mic.clear()                                    # the call ended while the service was down
    w2 = make(store, stubs)
    assert w2.recover(t0 + 1800) == 1 and w2.active is None
    row = store.call_record(call_id)
    assert row.status == "ended" and row.ended_at == alive and row.stop_reason == "service_restart"


def test_a_restart_across_the_pc_being_off_leaves_the_off_time_out(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(5, 1800, 5):
        step(w, sim, t0 + dt)
    stubs.mic.clear()                                    # after a reboot no hold from before it is read
    w2 = make(store, stubs)
    w2.recover(t0 + 1800 + 8 * 3600)
    row = store.call_record(call_id)
    assert row.ended_at - row.detected_at < 1800         # not 9.5 hours (row 41 read "505 min")


def test_a_restart_within_the_release_grace_is_the_same_call(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    call_id = w.active.call_id
    for dt in range(5, 300, 5):
        step(w, sim, t0 + dt)
    stubs.mic.clear()
    stubs.hold("mic", DISCORD, t0 + 310)                 # re-grabbed while the service was restarting
    w2 = make(store, stubs)
    sim.t = t0 + 320
    w2.recover(t0 + 320)
    assert w2.active is not None and w2.active.call_id == call_id
    step(w2, sim, t0 + 325)
    assert events(w2, "call_regrab") and len(store.calls()) == 1


def test_a_row_from_before_the_upgrade_is_adopted_only_when_its_hold_began_before_it(store, sim):
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", DISCORD, t0 - 600)
    old = store.start_call(app="Discord", process="discord", via="screen", at=t0 - 500, status="waiting_headphones",
                           consent="always", consent_by="pref")
    with store._lock:   # as migration 26 left an older row: no hold, no alive mark
        store._conn.execute("UPDATE calls SET hold_since=NULL, last_alive_at=NULL WHERE id=?", (old,))
    w = make(store, stubs)
    w.recover(t0)
    assert w.active is not None and w.active.call_id == old and events(w, "call_resumed")[0]["legacy"]
    other = Store.open(store.path.parent / "other.db")
    try:
        stubs2 = Stubs()
        stubs2.hold("mic", DISCORD, t0 - 10)            # a hold that began after the row: another call
        row = other.start_call(app="Discord", process="discord", via="screen", at=t0 - 500)
        with other._lock:
            other._conn.execute("UPDATE calls SET hold_since=NULL, last_alive_at=NULL WHERE id=?", (row,))
        w2 = make(other, stubs2)
        assert w2.recover(t0) == 1 and w2.active is None
    finally:
        other.close()


def test_an_adopted_call_goes_on_numbering_its_chunks_and_counting_its_speech(store, sim):
    stubs = Stubs()
    t0 = sim.t
    stubs.hold("mic", DISCORD, t0 - 600)
    store.set_call_pref("discord", "always")
    call_id = store.start_call(app="Discord", process="discord", via="screen", at=t0 - 500, status="recording",
                               consent="always", consent_by="pref", hold_since=t0 - 600)
    for seq in (1, 2, 3):
        store.write_spool(call_id, seq, start_at=t0 - 400 + seq, audio_s=6.0, speech_s=5.0, payload=b"x", codec="opus")
    store.update_call(call_id, chunks=3, speech_s=15.0, audio_s=18.0, recording_started_at=t0 - 499,
                      last_alive_at=t0 - 20)
    w = make(store, stubs, record=True)
    try:
        sim.t = t0
        w.recover(t0)
        step(w, sim, t0 + 1)
        session = w.active.session
        assert session is not None and session._seq == 3 and session.stats.chunks == 3
        assert session.stats.speech_s == 15.0
        assert store.call_record(call_id).recording_started_at == t0 - 499   # its first start is kept
    finally:
        stop_sessions(w)


# -- what the pill and the Today chart read ---------------------------------------------------------------------


def test_the_pill_counts_from_the_session_and_the_chart_merges_one_hold(store, sim):
    from yuki.memory.callspans import call_spans

    t0 = sim.t
    first = store.start_call(app="Discord", process="discord", via="screen", at=t0, status="recording",
                             consent="always", consent_by="pref", hold_since=t0 - 20)
    store.update_call(first, status="ended", ended_at=t0 + 5684)
    second = store.start_call(app="Discord", process="discord", via="screen", at=t0 + 5684 + 1915,
                              status="recording", consent="always", consent_by="pref", hold_since=t0 - 18)
    assert session_start(store) == pytest.approx(t0, abs=1.0)   # one hold: the pill does not restart at 00:00
    spans = call_spans(store, t0 - 60, t0 + 9000, now=t0 + 9000)
    assert len(spans) == 1 and spans[0]["call_ids"] == [first, second]   # a 32-minute gap inside one hold


# -- the gates of heavy work ---------------------------------------------------------------------------------


def test_heavy_work_waits_while_an_ended_rows_app_still_holds_the_microphone(store):
    from yuki.memory.calldigest import CallDigestWorker
    from yuki.memory.transcribe import DeferConfig, TranscribeScheduler

    now = time.time()
    row = store.start_call(app="Discord", process="discord", via="screen", at=now - 3600, hold_since=now - 3700)
    store.update_call(row, status="ended", ended_at=now - 60)
    assert lingering_call(store, now) is None            # nobody holds the microphone (no reading yet)
    store.set_nudge_state(HOLDERS_STATE, '{"keys": ["discord"], "at": 0}')
    assert lingering_call(store, now).id == row
    sched = TranscribeScheduler(store, config=DeferConfig(), live_fn=lambda: {}, idle_fn=lambda: 0.0,
                                locked_fn=lambda: False, gpu_fn=lambda: None, cpu_fn=lambda: 0.0,
                                busy_fn=lambda: False, spawn=lambda argv: None, procs_fn=lambda: {},
                                family_fn=lambda pid: set())
    assert sched.blocked(time.monotonic(), DeferConfig())[0] == "call_lingering"   # even with during_calls on
    digest = CallDigestWorker(store, live_fn=lambda: {}, log=lambda *a, **k: None)
    assert digest.blocked() == "call"
    assert lingering_call(store, now + 601) is None       # ten minutes on, it is over
    store.set_nudge_state(HOLDERS_STATE, '{"keys": [], "at": 0}')
    assert lingering_call(store, now) is None and digest.blocked() is None


def test_the_call_worker_keeps_the_holders_for_the_gates(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    assert '"discord"' in (store.nudge_state(HOLDERS_STATE) or "")
    stubs.mic.clear()
    step(w, sim, t0 + 5)
    assert '"keys": []' in (store.nudge_state(HOLDERS_STATE) or "")


# -- telemetry ------------------------------------------------------------------------------------------------


def test_call_ended_says_what_the_signals_were(store, sim):
    w, stubs, t0 = discord_call(store, sim)
    step(w, sim, t0 + 30)
    stubs.mic.clear()
    for dt in range(35, 200, 5):
        step(w, sim, t0 + dt)
    ended = events(w, "call_ended")[0]
    for key in ("held", "hold_since", "hold_gap_s", "positive_age_s", "neg_frames", "last_controls",
                "median_controls", "front_app_key", "fullscreen"):
        assert key in ended, key
    assert ended["held"] is False and ended["hold_since"] == t0 - 20 and ended["hold_gap_s"] > 90
    assert ended["last_controls"] == len(PANEL) and ended["median_controls"] == len(PANEL)
    assert ended["front_app_key"] == "discord" and ended["fullscreen"] is False


def test_a_cached_verdict_is_logged_when_it_is_applied():
    logs: list[tuple[str, dict]] = []
    ev = CallEvidence(judge_fn=judge, rejudge_s=0.0, log=lambda k, **f: logs.append((k, f)))
    at = time.time()
    base = {"process": "discord", "exe": DISCORD, "host": "discord.com"}
    for i, controls in enumerate((PANEL, TEXT, PANEL)):   # back to a screen judged before: from the cache
        ev.seen({**base, "at": at + i, "title": "", "controls": list(controls)})
        ev.judge(["discord"])
    assert [k for k, _ in logs] == ["call_evidence_judged", "call_evidence_judged", "call_evidence_applied"]
    assert logs[-1][1]["cached"] is True and logs[-1][1]["state"] == "in_call" and logs[-1][1]["counted"] == "pos"


def test_the_judge_has_a_third_answer_and_reads_it_from_the_model():
    from types import SimpleNamespace

    assert ce.JUDGE_TOOL["input_schema"]["properties"]["state"]["enum"] == ["in_call", "not_in_call", "unclear"]
    assert "-> unclear" in ce.JUDGE_PROMPT                # a worked example, how this project steers models

    def client_saying(data):
        usage = SimpleNamespace(input_tokens=10, output_tokens=5, cache_creation_input_tokens=0,
                                cache_read_input_tokens=0)
        block = SimpleNamespace(type="tool_use", input=data)
        return SimpleNamespace(messages=SimpleNamespace(create=lambda **k: SimpleNamespace(content=[block],
                                                                                           usage=usage)))

    shot = ce.Screen(key="discord", host="discord.com", hwnd=1, exe=DISCORD, at=0.0, title="Discord",
                     controls=("Minimize", "Maximize", "Close"), digest="d")
    settings = SimpleNamespace(estimate_cost=lambda model, usage: 0.0)
    for data, state in (({"state": "unclear", "service": "", "others": -1}, "unclear"),
                        ({"state": "in_call", "service": "Discord", "others": 2}, "in_call"),
                        ({"in_call": False, "service": ""}, "not_in_call"),   # the old shape still reads
                        ({}, "unclear")):
        ev = CallEvidence(client=client_saying(data), settings=settings, model="m")
        verdict, _cost = ev._model_verdict(shot)
        assert verdict.state == state, data


# -- the state machine alone ------------------------------------------------------------------------------------


def test_call_life_rules_in_isolation():
    cfg = CallConfig()
    life = CallLife(cfg, now=0.0, hold_since=-20.0)
    assert life.observe(5.0, held=True, hold_since=-20.0, positive_at=0.0, negatives=[1.0, 2.0, 3.0]).alive
    assert life.observe(10.0, held=True, hold_since=-20.0, positive_at=0.0, negatives=[1.0, 60.0, 119.0]).alive
    out = life.observe(15.0, held=True, hold_since=-20.0, positive_at=0.0, negatives=[1.0, 60.0, 121.0])
    assert not out.alive and out.end == "screen_contradicts"
    life = CallLife(cfg, now=0.0, hold_since=-20.0)
    assert life.observe(80.0, held=False, hold_since=None, positive_at=None).by == "release_grace"
    assert life.observe(100.0, held=False, hold_since=None, positive_at=None).by == "end_grace"
    out = life.observe(121.0, held=False, hold_since=None, positive_at=None)
    assert not out.alive and out.end == "mic_released"
    life = CallLife(cfg, now=0.0, hold_since=-20.0)
    step = life.observe(40.0, held=True, hold_since=35.0, positive_at=None)
    assert step.alive and step.regrab == {"gap_s": 35.0, "old_since": -20.0, "new_since": 35.0}
    assert life.observe(41.0, held=True, hold_since=37.0, positive_at=None).regrab is None   # within 5 s: same


# -- the property: one hold epoch is one row --------------------------------------------------------------------


@pytest.mark.parametrize("seed", range(30))
def test_property_releases_regrabs_degraded_frames_and_restarts_never_split_a_call(tmp_path, sim, seed):
    """Random interleavings of: the hold going on, releases shorter than the grace followed by a re-grab, a
    re-grab without a visible release, degraded frames (0/18/21/22 controls), in-call frames and service
    restarts (the old process dies, a new one adopts). A call that never lets go of the microphone for longer
    than ``release_grace_s`` stays one row whose start never moves."""
    rng = random.Random(seed)
    store = Store.open(tmp_path / f"m{seed}.db")
    workers: list[CallWorker] = []
    try:
        stubs = Stubs()
        t = sim.t
        stubs.hold("mic", DISCORD, t - 20)
        store.set_call_pref("discord", "always")
        w = make(store, stubs)
        workers.append(w)
        see(w, DISCORD, t, PANEL, title="join")
        t += 1
        step(w, sim, t)
        call_id = w.active.call_id
        first_at = store.call_record(call_id).detected_at
        back_at: float | None = None
        for i in range(150):
            t += 5
            r = rng.random()
            if back_at is not None:
                if t >= back_at:                                     # re-grab: a new hold of the same app
                    stubs.hold("mic", DISCORD, t - rng.uniform(0.0, 3.0))
                    back_at = None
            elif r < 0.07:                                           # let go for 5-35 s
                stubs.mic.clear()
                back_at = t + rng.choice((5, 10, 20, 30, 35))
            elif r < 0.12:                                           # a stream re-opened between two polls
                stubs.mic.clear()
                stubs.hold("mic", DISCORD, t - 1.0)
            if r >= 0.12 and r < 0.40:
                see(w, DISCORD, t - 1, DEGRADED[rng.choice(sorted(DEGRADED))], title=f"tab {i}")
            elif 0.40 <= r < 0.52:
                see(w, DISCORD, t - 1, PANEL, title=f"panel {i}")
            elif 0.52 <= r < 0.57:                                   # the service restarts
                t += rng.choice((5, 10, 20, 25))
                w = make(store, stubs)
                workers.append(w)
                sim.t = t
                w.recover(t)
                assert w.active is not None and w.active.call_id == call_id, (seed, i)
            step(w, sim, t)
            assert w.active is not None and w.active.call_id == call_id, (seed, i, store.calls()[0].stop_reason)
        assert len(store.calls()) == 1 and store.call_record(call_id).detected_at == first_at
    finally:
        store.close()


def test_life_is_empty_without_evidence():
    assert CallEvidence(judge_fn=judge).life("discord") == Life()
