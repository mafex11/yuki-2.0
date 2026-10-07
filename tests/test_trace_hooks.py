"""capC1 (report 96): the trace's hook thread callbacks: the outcome cap per action, titles only from the name-change
stream, nothing while blocked; and presence sharing its one Raw Input reader with the trace's sink.

The callbacks are called directly (no hook is registered, nothing touches the desktop), except one short read-only
start of a real reader, which only listens.
"""

from __future__ import annotations

import time

from yuki.memory import trace_hooks as th
from yuki.memory.trace_hooks import TraceHooks
from yuki.memory.userpresence import Presence


def _hooks():
    items = []
    h = TraceHooks(items.append, pid_of=lambda hwnd: (77,), own_pid=1, recent=None, clock=lambda: 5.0)
    h.blocked = False
    h._fg = 900
    return h, items


def test_at_most_200_outcome_events_per_action_the_rest_only_counted():
    h, items = _hooks()
    h.gen, h._n = 1, 0                     # an action opened the outcome hooks
    for i in range(500):
        h._on_event(None, th.EVENT_OBJECT_VALUECHANGE, 900, -4, i, 0, 0)
    assert len(items) == th.OUTCOME_CAP == 200 and h.counts["outcome_over"] == 300
    h.gen, h._n = 2, 0                     # the next action starts its own count
    h._on_event(None, th.EVENT_OBJECT_STATECHANGE, 900, -4, 1, 0, 0)
    assert items[-1][0] == "out" and items[-1][-1] == 2


def test_never_locationchange_and_outcome_events_only_around_actions():
    ranges = [r for pair in th.OUTCOME + th.LOW_RATE for r in range(pair[0], pair[1] + 1)]
    assert 0x800B not in ranges            # EVENT_OBJECT_LOCATIONCHANGE is never hooked
    assert th.OUTCOME_S == 1.5


def test_only_the_windows_own_title_passes_the_name_change_stream():
    h, items = _hooks()
    for i in range(50):                    # a streaming app renaming its content
        h._on_event(None, th.EVENT_OBJECT_NAMECHANGE, 900, -4, i, 0, 0)
    h._on_event(None, th.EVENT_OBJECT_NAMECHANGE, 900, 0, 0, 0, 0)
    assert [i[2] for i in items] == [th.EVENT_OBJECT_NAMECHANGE] and h.counts["low_dropped"] == 50


def test_nothing_is_queued_while_blocked():
    h, items = _hooks()
    h.blocked = True
    for ev in (th.EVENT_OBJECT_INVOKED, th.EVENT_OBJECT_FOCUS, th.EVENT_OBJECT_VALUECHANGE):
        h._on_event(None, ev, 900, -4, 1, 0, 0)
    h._on_clipboard()
    assert items == []


def test_the_private_clipboard_marks_are_windows_documented_names():
    assert set(th.PRIVATE_FORMATS) == {"ExcludeClipboardContentFromMonitorProcessing",
                                       "CanIncludeInClipboardHistory", "Clipboard Viewer Ignore"}


# -- presence and the trace share one reader --------------------------------------------------------------------------


class FakeReader:
    def __init__(self) -> None:
        self.running = False
        self.error = ""
        self.sink = None
        self.messages = 0
        self.woken = 0

    def start(self, wait_s: float = 1.0) -> bool:
        self.running = True
        return True

    def listen_mouse(self) -> None:
        self.woken += 1


def test_the_trace_attaches_to_presences_reader_and_presence_answers_as_before():
    reader = FakeReader()
    now = {"t": 100.0, "raw": 0.0}
    p = Presence(raw_idle=lambda: now["raw"], fallback=lambda: 0.0, reader=reader, move_px=lambda: 40,
                 clock=lambda: now["t"], front_unheard=lambda: False)
    sink = object()
    assert p.attach_sink(sink) is True and reader.sink is sink and reader.woken == 1
    assert p.source == "raw_input"         # the same reader presence asks
    p.monitor.key_down(at=110.0)
    now.update(t=120.0, raw=5.0)           # jitter since: Windows' idle timer says 5 s, the user's last key was 10 s ago
    assert p.idle_s() == 10.0


def test_no_raw_input_means_no_trace_input():
    class Dead(FakeReader):
        def start(self, wait_s: float = 1.0) -> bool:
            self.error = "OSError: no desktop"
            return False

    p = Presence(raw_idle=lambda: 0.0, fallback=lambda: 7.0, reader=Dead(), move_px=lambda: 40,
                 front_unheard=lambda: False)
    assert p.attach_sink(object()) is False and p.idle_s() == 7.0


def test_a_real_reader_feeds_a_sink_plain_integers_read_only():
    """One second of the real reader in this process, with a sink that only counts (counts and types only; nothing is
    sent to the desktop). Skipped where no desktop answers."""
    import pytest

    from yuki.memory.userpresence import PresenceInput, _RawReader

    seen = {"mouse": 0, "key": 0, "types": set()}

    class CountingSink:
        blocked = False
        wants_mouse = True

        def mouse(self, *args):
            seen["mouse"] += 1
            seen["types"] |= {type(a).__name__ for a in args}

        def key(self, *args):
            seen["key"] += 1
            seen["types"] |= {type(a).__name__ for a in args}

    reader = _RawReader(PresenceInput())
    reader.sink = CountingSink()
    if not reader.start():
        pytest.skip(f"no Raw Input here: {reader.error}")
    try:
        time.sleep(1.0)
    finally:
        reader.stop()
    assert seen["types"] <= {"float", "int"}            # never the RAWINPUT buffer or a structure
    assert reader.messages >= seen["mouse"] + seen["key"]
    print(f"\nreal reader, 1 s: {reader.messages} WM_INPUT, {seen['mouse']} mouse and {seen['key']} key reports to the sink")


def test_the_real_reader_survives_the_trace_waking_the_mouse():
    """Live 7 Oct (release 10): the trace's wake message reached the real message loop, which read ``msg.hwnd``
    (``ctypes.wintypes.MSG`` names it ``hWnd``): the reader died, presence fell back to the idle timer and the trace
    got no input. Fakes never ran that line; this sends the real thread message to a real reader."""
    from yuki.memory.userpresence import PresenceInput, _RawReader

    reader = _RawReader(PresenceInput())
    if not reader.start():
        pytest.skip(f"no Raw Input here: {reader.error}")
    try:
        for _ in range(3):
            reader.listen_mouse()
            time.sleep(0.2)
        assert reader.running and not reader.error, reader.error
        assert reader._thread is not None and reader._thread.is_alive()
    finally:
        reader.stop()
