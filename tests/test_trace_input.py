"""capC1 (report 96): the trace's sink on the one Raw Input reader: source labels, rests from a vibrating mouse,
travel, drags, double clicks, the first-step drop while blocked, and the cost of a 1,000 Hz burst.

Everything is fed as the reader feeds it (plain integers, a fake cursor and clock); nothing touches the desktop.
"""

from __future__ import annotations

import random
import time

from yuki.core.inputtag import YUKI_INPUT_TAG
from yuki.memory.trace_input import INJECTED, USER, YUKI, Metrics, TraceInput, source_of
from yuki.memory.userpresence import PresenceInput

L_DOWN, L_UP, R_DOWN, R_UP, WHEEL = 0x1, 0x2, 0x4, 0x8, 0x400
HW = 1          # a hardware device handle (non-zero)


class Cursor:
    def __init__(self) -> None:
        self.x, self.y = 500, 400

    def __call__(self):
        return self.x, self.y


def metrics() -> Metrics:
    # Windows' defaults as the system reports them (the sink is given them; it never invents them)
    return Metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5, double_w=4, double_h=4, drag_w=4, drag_h=4,
                   read=False)


def sink(items=None, cursor=None):
    items = [] if items is None else items
    s = TraceInput(items.append, metrics=metrics(), cursor=cursor or Cursor(), clock=lambda: 0.0)
    s.blocked = False
    return s, items


def test_source_labels_come_from_the_tag_and_the_device():
    assert source_of(YUKI_INPUT_TAG, HW) == YUKI
    assert source_of(YUKI_INPUT_TAG, 0) == YUKI
    assert source_of(0, 0) == INJECTED            # Handy dictation, Deskflow, G Hub macros
    assert source_of(0x1234, 0) == INJECTED
    assert source_of(0, HW) == USER


def test_the_real_system_metrics_are_read_not_invented():
    m = Metrics()
    assert m.hover_w > 0 and m.hover_h > 0 and m.hover_s > 0 and m.double_s > 0 and m.drag_w > 0


def test_a_vibrating_resting_mouse_is_a_rest_and_costs_almost_nothing():
    cur = Cursor()
    s, items = sink(cursor=cur)
    rng = random.Random(7)
    t = 0.0
    for _ in range(23 * 60):                     # a minute of the measured jitter: ~23 reports/s of 0-1 px
        dx, dy = rng.choice((-1, 0, 1)), rng.choice((-1, 0, 1))
        cur.x = 500 + rng.choice((-1, 0, 1))      # the pointer itself wanders at most a pixel
        cur.y = 400 + rng.choice((-1, 0, 1))
        t += 1 / 23
        s.mouse(t, 0, 0, 0, dx, dy, 0, HW)
    assert items == []                            # no rest broken, nothing for the worker
    assert s.counts["moves"] > 1000 and s.counts["rests"] == 0
    assert round(s.travel) == 0                   # jitter adds no travel


def test_a_real_move_ends_the_rest_once_and_adds_its_travel():
    cur = Cursor()
    s, items = sink(cursor=cur)
    for i in range(1, 41):                        # 40 reports moving right, 3 counts and px each
        cur.x += 3
        s.mouse(i * 0.001, 0, 0, 0, 3, 0, 0, HW)
    assert items == [("moving", items[0][1])]     # the worker is told once
    assert s.counts["rests"] >= 20
    cur_travel = s.travel
    assert 100 <= cur_travel + 9 <= 130           # the legs finished (the open leg is added at the next action)
    s.moving = False                              # the worker judged the rest
    s.mouse(1.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    down = items[-1]
    assert down[0] == "down" and down[2] == "left" and down[5] == USER and 115 <= down[6] <= 125
    assert s.travel == 0


def test_moves_stamped_by_yuki_never_make_a_rest():
    cur = Cursor()
    s, items = sink(cursor=cur)
    for i in range(30):
        cur.x += 5
        s.mouse(i * 0.01, 0, 0, 0, 5, 0, YUKI_INPUT_TAG, 0)
    assert items == [] and s.counts["moves"] == 0


def test_click_drag_and_double_click_by_windows_rules():
    cur = Cursor()
    s, items = sink(cursor=cur)
    s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    s.mouse(0.05, 0, L_UP, 0, 0, 0, 0, HW)
    assert items[-1] == ("up", 0.05, "left", 500, 400, USER, False)
    s.mouse(0.2, 0, L_DOWN, 0, 0, 0, 0, HW)       # within the double-click time and rectangle
    assert items[-1][0] == "down" and items[-1][7] is True
    s.mouse(0.25, 0, L_UP, 0, 0, 0, 0, HW)
    s.mouse(2.0, 0, L_DOWN, 0, 0, 0, 0, HW)       # too late: a new click
    assert items[-1][7] is False
    for i in range(10):                           # held and moved past the drag rectangle
        cur.x += 4
        s.mouse(2.0 + i * 0.01, 0, 0, 0, 4, 0, 0, HW)
    s.mouse(2.2, 0, L_UP, 0, 0, 0, 0, HW)
    up = items[-1]
    assert up[0] == "up" and up[6] is True and up[3] == 540


def test_wheel_notches_and_injected_input():
    s, items = sink()
    s.mouse(0.0, 0, WHEEL, (-240) & 0xFFFF, 0, 0, 0, 0)
    assert items[-1][:4] == ("wheel", 0.0, -2, False) and items[-1][6] == INJECTED


def test_everything_is_dropped_at_the_first_step_while_blocked():
    cur = Cursor()
    s, items = sink(cursor=cur)
    s.blocked = True
    calls = []
    s._cursor = lambda: calls.append(1) or (0, 0)
    for i in range(100):
        s.mouse(i * 0.001, 0, L_DOWN if i == 50 else 0, 0, 9, 9, 0, HW)
        s.key(i * 0.001, 0x41, 0, 0, 0x100, 0, HW)
    assert items == [] and calls == [] and s.counts["moves"] == 0 and not s.wants_mouse


def test_a_1000_hz_burst_is_cheap_on_the_readers_thread():
    """60 s of a 1,000 Hz gaming mouse in motion costs the reader presence's work plus the sink's. Bound: the two
    together under 40 us a report (4% of one core while the mouse moves, 0.25% of this PC's 16 threads); measured
    numbers go in report 96."""
    cur = Cursor()
    s, items = sink(cursor=cur)
    presence = PresenceInput(move_px=40)
    n = 20_000                                     # 20 s worth at 1,000 Hz
    rng = random.Random(1)
    t0 = time.perf_counter()
    for i in range(n):
        dx, dy = rng.randint(-6, 6), rng.randint(-6, 6)
        cur.x += dx
        cur.y += dy
        t = i / 1000.0
        presence.relative(dx, dy, t, 0)            # presence's own work, as the reader does it
        s.mouse(t, 0, 0, 0, dx, dy, 0, HW)
    per_report_us = (time.perf_counter() - t0) / n * 1e6
    print(f"\n1000 Hz burst: {per_report_us:.1f} us per report (presence + trace sink), {len(items)} items queued")
    assert per_report_us < 40.0
    assert len(items) <= 2                        # the worker is told the pointer moves, not about every report
