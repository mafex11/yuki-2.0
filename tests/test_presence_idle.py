"""presence (5 Oct): the memory service's idle signal ignores a vibrating mouse and hears real input.

The user's mouse reports ~23 moves a second of 0-1 px with nobody at the PC (report 73, release 6). The old signal
(``_PresenceSampler``: 3 idle-timer resets within 1.5 s is a person) never let the user go idle. These tests feed
:class:`yuki.memory.userpresence.PresenceInput` the way the Raw Input reader does, on a fake clock, and check the
idle seconds, the timeline's away rows, the picking-up gap and the idle-gated jobs that read them. Nothing touches the
desktop or the real input stream; temp databases only.
"""

from __future__ import annotations

import random
import tempfile
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.core.inputtag import YUKI_INPUT_TAG
from yuki.memory.userpresence import MOVE_WINDOW_S, Presence, PresenceInput

T0 = datetime(2026, 10, 1, 13, 0).timestamp()


class Clock:
    def __init__(self, t: float = T0) -> None:
        self.t = float(t)

    def __call__(self) -> float:
        return self.t


class FakeReader:
    """Stands in for the Raw Input reader: the test feeds the facts itself."""

    def __init__(self, ok: bool = True, error: str = "") -> None:
        self.ok, self.error, self.running, self.starts = ok, error, False, 0

    def start(self) -> bool:
        self.starts += 1
        self.running = self.ok
        return self.ok


def jitter(monitor: PresenceInput, start: float, end: float, *, rate: float = 23.0, seed: int = 5) -> int:
    """The vibrating sensor as measured: ~``rate`` relative reports a second, 75% of them 0 px on screen (counts of
    0 or 1), the rest 1, wandering both ways with a slight drift. Returns the reports fed."""
    rnd = random.Random(seed)
    t, n = start, 0
    while t < end:
        dx = rnd.choice((-1, 0, 0, 1, 1)) if rnd.random() < 0.6 else 0
        dy = rnd.choice((-1, 0, 1)) if rnd.random() < 0.4 else 0
        monitor.relative(dx, dy, at=t)
        n += 1
        t += rnd.uniform(0.5, 1.5) / rate
    return n


def presence(clock: Clock, *, raw: float = 0.0, reader: FakeReader | None = None, move_px: int = 40,
             fallback=lambda: -1.0) -> Presence:
    """Windows' own idle time held at ``raw`` (the jitter keeps it at ~0)."""
    p = Presence(raw_idle=lambda: raw, fallback=fallback, reader=reader or FakeReader(), move_px=lambda: move_px,
                 clock=clock)
    p.monitor.now = clock
    return p


# -- the facts --------------------------------------------------------------------------------------------------------


def test_jitter_never_counts_as_the_mouse_moving():
    m = PresenceInput()
    fed = jitter(m, T0, T0 + 600)
    assert fed > 10_000
    assert m.last("mouse") == 0.0 and m.latest() == 0.0
    assert m.counts["moves"] == fed and "mouse" not in m.counts
    assert m.max_travel < 20                       # live, 5 Oct: at most 9.8 counts within 0.3 s


def test_slow_drift_in_one_direction_never_counts():
    m = PresenceInput()
    t = T0
    for _ in range(23 * 60):                       # a count every report, all one way, for a minute
        m.relative(1, 0, at=t)
        t += 1 / 23
    assert m.last("mouse") == 0.0 and m.max_travel < 10


def test_a_hand_on_the_mouse_counts_at_once():
    m = PresenceInput()
    jitter(m, T0, T0 + 10)
    assert m.relative(12, 3, at=T0 + 10.00) is False
    assert m.relative(14, 2, at=T0 + 10.02) is False
    assert m.relative(15, 4, at=T0 + 10.04) is True        # 41 counts net within 0.3 s
    assert m.last("mouse") == T0 + 10.04


def test_turning_in_a_game_counts_though_the_cursor_never_moves():
    # the counts are the mouse's own: a game that holds the cursor still still shows the hand
    m = PresenceInput()
    for i in range(30):
        m.relative(-3, 0, at=T0 + i * 0.008)                # 1000 Hz-ish reports, a flick left
    assert m.last("mouse") > 0.0


def test_moves_spread_wider_than_the_window_do_not_add_up():
    m = PresenceInput()
    for i in range(10):
        m.relative(5, 0, at=T0 + i * MOVE_WINDOW_S)         # 50 counts, but never 40 within one window
    assert m.last("mouse") == 0.0


def test_keys_buttons_and_wheel_count():
    m = PresenceInput()
    assert m.key_down(at=T0 + 1) and m.last("keys") == T0 + 1
    assert m.button_down(at=T0 + 2) and m.last("click") == T0 + 2
    assert m.wheel(at=T0 + 3) and m.last("wheel") == T0 + 3
    assert m.latest() == T0 + 3


def test_yukis_own_stamped_input_never_counts():
    m = PresenceInput()
    assert m.key_down(at=T0, extra=YUKI_INPUT_TAG) is False
    assert m.button_down(at=T0, extra=YUKI_INPUT_TAG) is False
    assert m.wheel(at=T0, extra=YUKI_INPUT_TAG) is False
    for i in range(5):
        assert m.relative(30, 0, at=T0 + i * 0.01, extra=YUKI_INPUT_TAG) is False
    assert m.absolute(900, 500, at=T0 + 0.1, extra=YUKI_INPUT_TAG) is False
    assert m.latest() == 0.0 and m.counts["own"] == 9
    # untagged injected input (dictation, Deskflow) is the user's
    assert m.key_down(at=T0 + 1, extra=0x1234)


def test_an_absolute_device_counts_by_screen_distance():
    m = PresenceInput()
    assert m.absolute(500, 500, at=T0) is False
    assert m.absolute(510, 505, at=T0 + 0.1) is False
    assert m.absolute(545, 500, at=T0 + 0.2) is True         # 45 px from where it was within 0.3 s
    m2 = PresenceInput()
    m2.absolute(500, 500, at=T0)
    assert m2.absolute(545, 500, at=T0 + 1.0) is False       # the old place is out of the window


# -- the idle seconds -------------------------------------------------------------------------------------------------


def test_idle_grows_through_jitter_and_resets_on_a_key():
    clock = Clock()
    p = presence(clock)
    assert p.idle_s() == 0.0 and p.source == "raw_input"
    jitter(p.monitor, T0, T0 + 300)
    clock.t = T0 + 300
    assert p.idle_s() == pytest.approx(300.0)                # the old sampler said ~0 here
    p.monitor.key_down(at=T0 + 300)
    assert p.idle_s() == 0.0
    clock.t = T0 + 330
    assert p.idle_s() == pytest.approx(30.0)


@pytest.mark.parametrize("real", ["click", "wheel", "move"])
def test_idle_resets_on_a_click_the_wheel_or_a_big_move(real):
    clock = Clock()
    p = presence(clock)
    p.idle_s()
    jitter(p.monitor, T0, T0 + 120)
    clock.t = T0 + 120
    assert p.idle_s() == pytest.approx(120.0)
    if real == "click":
        p.monitor.button_down(at=clock.t)
    elif real == "wheel":
        p.monitor.wheel(at=clock.t)
    else:
        for i in range(4):
            p.monitor.relative(15, 0, at=clock.t - 0.05 + i * 0.01)
    assert p.idle_s() == pytest.approx(0.0, abs=0.06)


def test_yukis_own_input_does_not_bring_the_user_back():
    clock = Clock(T0 + 600)
    p = presence(clock)
    p.idle_s()
    clock.t = T0 + 900
    p.monitor.key_down(at=clock.t, extra=YUKI_INPUT_TAG)
    p.monitor.button_down(at=clock.t, extra=YUKI_INPUT_TAG)
    assert p.idle_s() == pytest.approx(300.0)


def test_windows_idle_time_is_the_floor_and_the_start_baseline():
    clock = Clock()
    p = presence(clock, raw=500.0)                          # nothing at all for 500 s when the service started
    assert p.idle_s() == pytest.approx(500.0)
    clock.t = T0 + 60
    assert p.idle_s() == pytest.approx(560.0)               # no real input known since: still growing


def test_unlocking_the_session_is_the_user():
    clock = Clock()
    p = presence(clock)
    p.idle_s()
    clock.t = T0 + 1800
    assert p.idle_s() == pytest.approx(1800.0)
    p.note_unlock()
    assert p.idle_s() == 0.0


def test_no_raw_input_falls_back_and_says_why():
    clock = Clock()
    p = presence(clock, reader=FakeReader(ok=False, error="OSError: no desktop"), fallback=lambda: 42.0)
    assert p.idle_s() == 42.0
    assert p.facts() == {"source": "fallback", "error": "OSError: no desktop"}
    assert p.last_input_at() is None


def test_a_reader_that_stops_later_falls_back():
    clock = Clock()
    reader = FakeReader()
    p = presence(clock, reader=reader, fallback=lambda: 7.0)
    assert p.idle_s() == 0.0
    reader.running, reader.error = False, "the thread failed"
    assert p.idle_s() == 7.0 and p.facts()["error"] == "the thread failed"
    assert reader.starts == 1                               # never restarted in a loop


def test_the_reader_starts_once():
    reader = FakeReader()
    p = presence(Clock(), reader=reader)
    for _ in range(5):
        p.idle_s()
    assert reader.starts == 1


def test_the_takeover_distance_follows_the_users_setting():
    clock = Clock()
    setting = {"px": 40}
    p = Presence(raw_idle=lambda: 0.0, fallback=lambda: 0.0, reader=FakeReader(), move_px=lambda: setting["px"],
                 clock=clock)
    p.idle_s()
    assert p.monitor.move_px == 40
    setting["px"] = 120
    clock.t = T0 + 30
    p.idle_s()
    assert p.monitor.move_px == 40                          # read again once a minute, not on every question
    clock.t = T0 + 61
    p.idle_s()
    assert p.monitor.move_px == 120


def test_facts_are_counts_only():
    clock = Clock()
    p = presence(clock)
    p.idle_s()
    p.monitor.key_down(at=T0)
    p.monitor.relative(1, 0, at=T0)
    facts = p.facts()
    assert facts["source"] == "raw_input" and facts["counts"] == {"keys": 1, "moves": 1}
    assert set(facts) == {"source", "counts", "max_travel", "move_px"}


def test_watcher_user_idle_s_is_the_jitter_proof_signal(monkeypatch):
    from yuki.memory import watcher

    clock = Clock()
    p = presence(clock)
    monkeypatch.setattr(watcher, "_presence", p)
    watcher.user_idle_s()
    jitter(p.monitor, T0, T0 + 200)
    clock.t = T0 + 200
    assert watcher.user_idle_s() == pytest.approx(200.0)
    assert watcher.presence_facts()["source"] == "raw_input"


def test_the_watchers_unlock_message_brings_the_user_back(monkeypatch):
    from yuki.memory import watcher

    clock = Clock()
    p = presence(clock)
    monkeypatch.setattr(watcher, "_presence", p)
    p.idle_s()
    clock.t = T0 + 900
    seen = []
    stub = SimpleNamespace(_set_locked=seen.append, _log_error=lambda *a: None)
    watcher.Watcher._wndproc(stub, 0, watcher.WM_WTSSESSION_CHANGE, watcher.WTS_SESSION_UNLOCK, 0)
    assert seen == [False] and p.idle_s() == 0.0
    clock.t = T0 + 1000
    watcher.Watcher._wndproc(stub, 0, watcher.WM_WTSSESSION_CHANGE, watcher.WTS_SESSION_LOCK, 0)
    assert seen == [False, True] and p.idle_s() == pytest.approx(100.0)


# -- the timeline's away rows and the picking-up gap -----------------------------------------------------------------


@pytest.fixture()
def store():
    from yuki.memory.store import Store

    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def _recorder(store, tmp_path, idle_fn):
    from yuki.memory.avsignals import AvSignals
    from yuki.memory.privacy import PrivacyConfig
    from yuki.memory.timeline import TimelineRecorder
    from yuki.memory.watcher import ProcessFacts

    signals = AvSignals(frozenset(), mic_fn=list, camera_fn=list, render_fn=list, identity_fn=lambda pid: ("", ""),
                        watch=False, backstop_s=0.0, render_max_age_s=0.0)
    rec = TimelineRecorder(store, privacy=PrivacyConfig(tmp_path / "privacy.toml"), signals=signals)
    facts = ProcessFacts(10, "Code.exe", r"C:\Apps\Code\Code.exe", "Visual Studio Code", False)
    rec._w = SimpleNamespace(_window_pid=lambda h: 10, _window_title=lambda h: "notes.md - Visual Studio Code",
                             is_fullscreen_front=lambda h: False, user_idle_s=idle_fn, session_locked=lambda: False)
    rec._procs = SimpleNamespace(get=lambda pid: facts)
    rec._windows = SimpleNamespace(is_user_window=lambda h: True, process_package_ids=lambda pid: ("", ""),
                                   start_menu_index=lambda: None)
    rec._hook_names = lambda hwnd: None
    rec._unhook_names = lambda: None
    return rec


def _run(rec, p: Presence, clock: Clock, until: float, *, real_every: float | None, tick: float = 30.0) -> None:
    """Steps the recorder to ``until``: jitter all the time, and a key every ``real_every`` seconds when given."""
    while clock.t < until:
        nxt = min(until, clock.t + tick)
        jitter(p.monitor, clock.t, nxt, seed=int(clock.t))
        if real_every:
            k = clock.t + real_every
            while k <= nxt:
                p.monitor.key_down(at=k)
                k += real_every
        clock.t = nxt
        rec._advance(nxt)


def test_jitter_alone_now_writes_away_and_the_return_is_a_picking_up_gap(store, tmp_path):
    from yuki.memory.nudges import NudgeConfig, resume_gap

    clock = Clock()
    p = presence(clock)
    rec = _recorder(store, tmp_path, p.idle_s)
    rec._acc_at, rec._input_at = T0, T0 - p.idle_s()
    rec._open(1, T0)
    _run(rec, p, clock, T0 + 600, real_every=7.0)                       # ten minutes of work
    _run(rec, p, clock, T0 + 600 + 4 * 3600, real_every=None)           # four hours of nobody, the mouse vibrating
    _run(rec, p, clock, T0 + 600 + 4 * 3600 + 420, real_every=5.0)      # back for seven minutes
    rec._close(clock.t)
    rows = store.timeline_between(T0 - 1, clock.t + 1)
    states = [r.state for r in rows]
    assert states == ["present", "away", "present"]
    away = rows[1]
    assert away.started_at == pytest.approx(T0 + 600 + 60, abs=8)             # AWAY_AFTER_S after the last key
    assert away.ended_at == pytest.approx(T0 + 600 + 4 * 3600, abs=30)        # back within one tick of the first key
    assert sum(r.active_s for r in rows) == pytest.approx(600 + 60 + 390, abs=15)    # work, the away grace, back
    gap = resume_gap(store, clock.t, NudgeConfig())
    assert gap is not None and gap.qualifies and gap.away_s >= 3.9 * 3600


def test_with_the_old_rule_jitter_kept_the_user_present(store, tmp_path):
    # what the timeline wrote before: an idle signal that the jitter holds at ~0 (the measured old sampler)
    clock = Clock()
    rec = _recorder(store, tmp_path, lambda: 0.1)
    rec._acc_at, rec._input_at = T0, T0
    rec._open(1, T0)
    for i in range(1, 4 * 120 + 1):
        rec._advance(T0 + i * 30)
    rec._close(T0 + 4 * 3600)
    assert {r.state for r in store.timeline_between(T0 - 1, T0 + 4 * 3600 + 1)} == {"present"}


# -- the idle-gated jobs ----------------------------------------------------------------------------------------------


def _idle_after_jitter(seconds: float, *, back: bool = False):
    """An idle function after ``seconds`` of jitter only (then a key, when ``back``)."""
    clock = Clock()
    p = presence(clock)
    p.idle_s()
    jitter(p.monitor, T0, T0 + seconds)
    clock.t = T0 + seconds
    if back:
        p.monitor.key_down(at=clock.t)
    return p.idle_s


class _PortraitWorker:
    def __init__(self):
        self.runs = []

    def run(self, kind):
        from yuki.memory.portrait import RunResult
        from yuki.memory.store import PortraitRunStats

        self.runs.append(kind)
        return RunResult(1, kind, True, stats=PortraitRunStats(kind=kind, model="stub"))

    def rerender(self, reason=""):
        return None

    def audit(self, *, full=False, reason=""):
        return None

    def loop_check_pending(self):
        return False


def test_the_portraits_missed_slot_runs_once_jitter_alone_has_gone_on_ten_minutes(store):
    from yuki.memory.portrait import PortraitScheduler

    store.save_portrait("Work\n- x")
    now = datetime(2026, 9, 25, 4, 0).timestamp()
    for seconds, want in ((300, None), (660, "weekly")):
        sched = PortraitScheduler(store, _PortraitWorker(), db_path=store.path, idle_fn=_idle_after_jitter(seconds),
                                  clock=lambda: now)
        kind, reason = sched.due(now)
        assert kind == want, reason
    sched = PortraitScheduler(store, _PortraitWorker(), db_path=store.path,
                              idle_fn=_idle_after_jitter(3600, back=True), clock=lambda: now)
    assert sched.due(now)[0] is None                                      # the user is back: it waits


def test_the_weekly_reviews_missed_slot_runs_after_jitter_alone(store):
    from yuki.memory.nudges import NudgeConfig
    from yuki.memory.review import ReviewConfig, ReviewScheduler

    now = datetime(2026, 9, 28, 9, 0).timestamp()
    config = ReviewConfig()

    def due(idle_fn):
        return ReviewScheduler(store, SimpleNamespace(), db_path=store.path, config=config,
                               nudge_config=NudgeConfig(), idle_fn=idle_fn, clock=lambda: now,
                               signal=lambda: None).due(now)

    assert due(_idle_after_jitter(config.idle_min * 60 + 30))[0] is True
    assert due(_idle_after_jitter(config.idle_min * 60 + 30, back=True))[0] is False


def test_call_transcription_sees_the_user_away_and_back(store):
    from yuki.memory.transcribe import DeferConfig, TranscribeScheduler

    cfg = DeferConfig(present=False)

    def sched(idle_fn):
        s = TranscribeScheduler(store, config=cfg, live_fn=lambda: {}, idle_fn=idle_fn, locked_fn=lambda: False,
                                gpu_fn=lambda: None, cpu_fn=lambda: 0.0, busy_fn=lambda: False,
                                spawn=lambda argv: None, procs_fn=lambda: {}, family_fn=lambda pid: set(),
                                now_fn=lambda: datetime(2026, 10, 1, 15, 0))
        t = 0.0
        while t <= cfg.gpu_quiet_min * 60 + cfg.check_s:
            s._sample(t, cfg)
            t += cfg.check_s
        return s, t

    s, t = sched(_idle_after_jitter(cfg.idle_min * 60 + 30))
    assert s.blocked(t, cfg) == (None, "away")
    s, t = sched(_idle_after_jitter(cfg.idle_min * 60 + 30, back=True))
    assert s.blocked(t, cfg) == ("user_present", "")
    s._run_why = "away"
    assert s.interrupt(cfg, t) == "user_back"


def test_the_memory_service_never_loads_the_hands_for_presence():
    import subprocess
    import sys

    code = ("import sys, yuki.memory.watcher as w, yuki.memory.userpresence; "
            "print('yuki.actions' in sys.modules, 'pyautogui' in sys.modules)")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120,
                         cwd=str(Path(__file__).resolve().parents[1]))
    assert out.stdout.split() == ["False", "False"], out.stderr


def test_time_is_not_read_from_the_wall_clock_by_the_facts():
    # the reader stamps with time.monotonic(); the facts' default clock is the same one
    m = PresenceInput()
    before = time.monotonic()
    m.key_down()
    assert before <= m.last("keys") <= time.monotonic()
