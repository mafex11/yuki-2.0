"""capC1c (report 99): a drag is the cursor leaving Windows' drag rectangle between press and release.

The first live recording had 9 drags in 8 minutes, several of them plain clicks (an address bar, a search box, a chat
list). The test now compares the cursor (physical pixels) at button-down and at button-up against ``SM_CXDRAG`` x
``SM_CYDRAG`` scaled for the monitor's DPI as Windows does (``GetSystemMetricsForDpi``), never the device's raw counts
(mickeys, which pointer speed and acceleration rescale) and never a wobble that came back. Fed as the reader feeds it
(plain integers, a fake cursor); nothing touches the desktop.
"""

from __future__ import annotations

import random

from yuki.memory.trace_input import Metrics, TraceInput

L_DOWN, L_UP = 0x1, 0x2
HW = 1


class Cursor:
    def __init__(self) -> None:
        self.x, self.y = 500, 400

    def __call__(self):
        return self.x, self.y


def metrics(**kw) -> Metrics:
    return Metrics(hover_w=4, hover_h=4, hover_s=0.4, double_s=0.5, double_w=4, double_h=4, drag_w=4, drag_h=4,
                   read=False, **kw)


def sink(cursor: Cursor, **kw):
    items: list = []
    s = TraceInput(items.append, metrics=metrics(**kw), cursor=cursor, clock=lambda: 0.0)
    s.blocked = False
    return s, items


def ups(items):
    return [i for i in items if i[0] == "up"]


def test_a_click_with_the_resting_jitter_is_never_a_drag():
    """About 23 reports a second of 0-1 px while the button is held for a second, as measured on this PC."""
    rnd = random.Random(7)
    cur = Cursor()
    s, items = sink(cur)
    for press in range(20):
        t0 = press * 2.0
        s.mouse(t0, 0, L_DOWN, 0, 0, 0, 0, HW)
        for k in range(23):
            dx, dy = rnd.choice((-1, 0, 1)), rnd.choice((-1, 0, 1))
            cur.x = 500 + max(-1, min(1, cur.x - 500 + dx))           # the sensor wanders within a pixel
            cur.y = 400 + max(-1, min(1, cur.y - 400 + dy))
            s.mouse(t0 + k / 23, 0, 0, 0, dx, dy, 0, HW)
        s.mouse(t0 + 1.0, 0, L_UP, 0, 0, 0, 0, HW)
    assert len(ups(items)) == 20 and not any(u[6] for u in ups(items))


def test_a_text_selection_is_a_drag_with_its_end_point():
    cur = Cursor()
    s, items = sink(cur)
    s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    for k in range(30):
        cur.x += 4
        s.mouse(0.01 * k, 0, 0, 0, 3, 0, 0, HW)
    s.mouse(0.4, 0, L_UP, 0, 0, 0, 0, HW)
    (up,) = ups(items)
    assert up[6] is True and (up[3], up[4]) == (620, 400)


def test_a_window_moved_by_its_title_bar_is_a_drag():
    cur = Cursor()
    s, items = sink(cur)
    s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    for k in range(50):
        cur.x += 6
        cur.y += 4
        s.mouse(0.01 * k, 0, 0, 0, 6, 4, 0, HW)
    s.mouse(0.6, 0, L_UP, 0, 0, 0, 0, HW)
    (up,) = ups(items)
    assert up[6] is True and (up[3], up[4]) == (800, 600)


def test_a_wobble_that_comes_back_before_the_release_is_a_click():
    cur = Cursor()
    s, items = sink(cur)
    s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    for dx in (3, 2, -2, -3):                                         # out 5 px and back
        cur.x += dx
        s.mouse(0.02, 0, 0, 0, dx, 0, 0, HW)
    s.mouse(0.1, 0, L_UP, 0, 0, 0, 0, HW)
    assert ups(items)[0][6] is False
    s.mouse(0.2, 0, L_DOWN, 0, 0, 0, 0, HW)                           # and it still makes a double click
    assert items[-1][0] == "down" and items[-1][7] is True


def test_raw_counts_never_decide_only_the_cursor_does():
    """A fast flick of raw counts while the cursor stays (pinned at a screen edge, or acceleration off) is no drag;
    a slow move of few counts that the cursor turns into pixels past the rectangle is one."""
    cur = Cursor()
    s, items = sink(cur)
    s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    for _ in range(10):
        s.mouse(0.01, 0, 0, 0, 40, 0, 0, HW)                          # 400 counts, the cursor never moved
    s.mouse(0.2, 0, L_UP, 0, 0, 0, 0, HW)
    assert ups(items)[-1][6] is False
    s.mouse(1.0, 0, L_DOWN, 0, 0, 0, 0, HW)
    cur.x += 3
    s.mouse(1.01, 0, 0, 0, 1, 0, 0, HW)                               # 1 count, 3 px with pointer speed
    s.mouse(1.1, 0, L_UP, 0, 0, 0, 0, HW)
    assert ups(items)[-1][6] is True


def test_the_rectangle_is_windows_own_at_96_dpi_and_scaled_for_a_150_percent_monitor():
    for dpi, moved, drag in ((96, 2, False), (96, 3, True), (144, 3, False), (144, 4, True)):
        cur = Cursor()
        s, items = sink(cur, dpi_at=lambda x, y, d=dpi: d)
        s.mouse(0.0, 0, L_DOWN, 0, 0, 0, 0, HW)
        cur.y += moved
        s.mouse(0.01, 0, 0, 0, 0, 1, 0, HW)
        s.mouse(0.1, 0, L_UP, 0, 0, 0, 0, HW)
        assert ups(items)[0][6] is drag, (dpi, moved)


def test_windows_own_per_dpi_metric_is_used_when_there_is_one_and_per_monitor():
    asked: list = []

    def for_dpi(metric, dpi):
        asked.append((metric, dpi))
        return 10 if dpi == 192 else 4

    m = metrics(dpi_at=lambda x, y: 192 if x >= 1920 else 96, for_dpi=for_dpi)
    assert m.drag_size(2500, 100) == (10, 10) and m.drag_size(100, 100) == (4, 4)
    assert (68, 192) in asked and (69, 192) in asked
    assert m.is_drag(2500, 100, 2504, 100) is False and m.is_drag(2500, 100, 2506, 100) is True
    broken = metrics(dpi_at=lambda x, y: 1 / 0)
    assert broken.drag_size(0, 0) == (4, 4)                           # a failing read: the system's own values


def test_a_drag_row_keeps_how_far_the_cursor_ended_from_the_press(tmp_path):
    import json

    from test_trace_worker import Rig

    from yuki.memory.trace_uia import Element

    rig = Rig(tmp_path)
    try:
        rig.uia.points[(150, 120)] = Element(role="Edit", name="Search", bounds=(100, 100, 600, 140), pid=101,
                                             patterns=("value",))
        t = rig.clock.t
        rig.feed("down", t, "left", 150, 120, "user", 0, False, 11)
        rig.feed("up", t + 0.5, "left", 290, 124, "user", True, dt=0.5)
        rig.settle()
        (row,) = rig.rows()
        assert row["kind"] == "drag" and json.loads(row["facts"])["moved_px"] == 140
    finally:
        rig.store.close()


def test_the_real_metrics_scale_for_the_monitor_under_a_point():
    m = Metrics()                                                     # read-only system calls
    w, h = m.drag_size(0, 0)
    assert w >= 1 and h >= 1 and m.is_drag(0, 0, 0, 0) is False
