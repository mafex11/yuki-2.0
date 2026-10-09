"""fLive: VolumeWatch (PLAN.md D1 ``volume_change``, D.1, C.5): Core Audio events coalesced into moments, Yuki's own
changes marked by its event context, no polling, footprint.

Offline: the coalescer and the watch thread are fed synthetic event streams (``register=False``); no audio device is
opened, registered or changed.
"""

from __future__ import annotations

import threading
import time
import tracemalloc

from yuki.core.inputtag import YUKI_AUDIO_CONTEXT, is_yuki_audio_context
from yuki.memory.volumewatch import COALESCE_S, Coalescer, VolumeWatch


def test_a_60hz_drag_then_quiet_is_one_change_from_first_to_last() -> None:
    c = Coalescer()
    c.baseline(("master",), 30.0, False)
    t = 100.0
    for i in range(61):                      # one second of a slider drag at 60 Hz, 30 -> 70
        c.push(("master",), "master", "master", 30.0 + 40.0 * i / 60, False, t + i / 60)
    assert c.due(t + 1.0 + COALESCE_S - 0.01) == []     # still moving (not quiet for 1.5 s)
    out = c.due(t + 1.0 + COALESCE_S + 0.01)
    assert len(out) == 1
    assert out[0]["value_from"] == 30.0 and out[0]["value"] == 70.0 and out[0]["yuki"] is False
    assert c.pending() == 0
    # the next drag starts from where the last ended
    c.push(("master",), "master", "master", 50.0, False, t + 10)
    assert c.due(t + 20)[0]["value_from"] == 70.0


def test_without_a_baseline_the_first_event_is_the_start_and_tiny_moves_are_nothing() -> None:
    c = Coalescer()
    c.push(("session", 11), "spotify", "session", 40.0, False, 0.0)
    c.push(("session", 11), "spotify", "session", 40.2, False, 0.1)
    assert c.due(5.0) == []                  # 0.2 points: no change
    c.push(("session", 11), "spotify", "session", 40.2, True, 6.0)   # muted only: a change
    out = c.due(10.0)
    assert len(out) == 1 and out[0]["muted_from"] is False and out[0]["muted"] is True


def test_yukis_context_marks_the_change_as_yukis() -> None:
    assert is_yuki_audio_context(YUKI_AUDIO_CONTEXT)
    assert is_yuki_audio_context(YUKI_AUDIO_CONTEXT.lower().strip("{}"))
    assert not is_yuki_audio_context("{00000000-0000-0000-0000-000000000000}")
    assert not is_yuki_audio_context("")
    c = Coalescer()
    c.baseline(("master",), 20.0)
    c.push(("master",), "master", "master", 25.0, False, 0.0, yuki=False)
    c.push(("master",), "master", "master", 40.0, False, 0.1, yuki=True)
    assert c.due(5.0)[0]["yuki"] is True


def _watch(**kw):
    moments, logs = [], []
    w = VolumeWatch(moments.append, log=lambda t, **f: logs.append((t, f)), register=False,
                    front_fn=lambda: "arc", **kw)
    return w, moments, logs


def test_the_thread_turns_endpoint_and_session_events_into_moments() -> None:
    w, moments, logs = _watch(quiet_s=0.05)
    w.start()
    try:
        w.feed("endpoint", 35.0, False, "", time.time())
        w.feed("endpoint", 48.0, False, YUKI_AUDIO_CONTEXT, time.time())
        w._sessions[7] = (None, None, None, 4242, "spotify")     # a watched session (registration is COM-only)
        w.feed("session", 7, 80.0, False, "", time.time())
        w.feed("session", 7, 62.0, False, "", time.time())
        w.feed("session", 99, 10.0, False, "", time.time())      # a released session: ignored
        deadline = time.monotonic() + 2.0
        while len(moments) < 2 and time.monotonic() < deadline:
            time.sleep(0.02)
    finally:
        w.stop()
    by_app = {m["app"]: m for m in moments}
    assert set(by_app) == {"master", "spotify"}
    master = by_app["master"]
    assert master["kind"] == "volume_change" and master["means"] == "master" and master["by"] == "yuki"
    assert master["value_from"] == 35.0 and master["value"] == 48.0 and master["evidence"] == "endpoint_event"
    assert master["facts"]["front"] == "arc" and master["source"] == "volume"
    spot = by_app["spotify"]
    assert spot["means"] == "session" and spot["by"] == "user" and spot["value_from"] == 80.0 and spot["value"] == 62.0
    events = [f for t, f in logs if t == "volume_event"]
    assert {e["target"] for e in events} == {"master", "session"} and all("from" in e and "to" in e for e in events)
    assert w.mode == "stopped"


def test_idle_means_no_wakeups_and_no_cpu() -> None:
    w, _m, _l = _watch()      # idle_wait 60 s: blocked on the queue the whole time
    w.start()
    time.sleep(1.0)
    wakeups = w.counters["wakeups"]
    w.stop()
    assert wakeups == 0
    assert w.cpu_s < 0.05


def test_a_bad_event_or_a_failing_consumer_never_ends_the_watch() -> None:
    def boom(_m):
        raise RuntimeError("consumer down")

    logs = []
    w = VolumeWatch(boom, log=lambda t, **f: logs.append((t, f)), register=False, quiet_s=0.02)
    w.start()
    try:
        w.feed("session", "not-a-key")                 # malformed
        w.feed("endpoint", 10.0, False, "", 0.0)
        w.feed("endpoint", 30.0, False, "", 0.0)
        time.sleep(0.3)
        assert w.alive
    finally:
        w.stop()
    assert any(t == "volume_error" for t, _f in logs)


def test_one_hour_of_synthetic_events_is_cheap() -> None:
    """Footprint bar (PLAN.md B.1, C.5): 1 h of synthetic events (a drag every 30 s, 90 events each, on 4 targets)
    through the coalescer: CPU and memory."""
    c = Coalescer()
    tracemalloc.start()
    t0 = time.thread_time()
    out = 0
    for drag in range(120):
        base = drag * 30.0
        target = ("session", drag % 4)
        for i in range(90):
            c.push(target, "app", "session", (drag * 7 + i) % 100, False, base + i / 60)
        out += len(c.due(base + 10.0))
    cpu = time.thread_time() - t0
    _cur, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert out >= 100
    assert cpu < 0.5
    assert peak < 3 * 2**20
    print(f"\nvolumewatch bench: 10800 events, {out} changes, cpu {cpu * 1000:.1f} ms, peak {peak / 1024:.0f} KiB")


def test_the_watch_thread_is_one_named_daemon() -> None:
    w, _m, _l = _watch()
    w.start()
    try:
        names = [t.name for t in threading.enumerate() if t.name == "yuki-memory-volume"]
        assert names == ["yuki-memory-volume"]
        assert all(t.daemon for t in threading.enumerate() if t.name == "yuki-memory-volume")
    finally:
        w.stop()


def test_stats_show_the_threads_cpu_while_it_runs() -> None:
    """Review r1 L3: the per-thread CPU is readable from stats() during the watch's life, not only after it ends."""
    def busy(_m) -> None:   # the consumer runs on the watch thread: burn ~80 ms of its CPU
        end = time.thread_time() + 0.08
        while time.thread_time() < end:
            pass

    w = VolumeWatch(busy, register=False, quiet_s=0.02, front_fn=lambda: None)
    w.start()
    try:
        w.feed("endpoint", 10.0, False, "", 0.0)
        w.feed("endpoint", 30.0, False, "", 0.0)
        deadline = time.monotonic() + 3.0
        while w.counters["changes"] < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        w.feed("endpoint", 31.0, False, "", 0.0)   # one more wake-up after the busy consumer
        deadline = time.monotonic() + 3.0
        while w.stats()["cpu_ms"] <= 0 and time.monotonic() < deadline:
            time.sleep(0.02)
        assert w.alive and w.stats()["cpu_ms"] >= 50
    finally:
        w.stop()
