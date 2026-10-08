"""capQ2a (report 103): a taskbar's own redraw while the lookup reads the answer's ancestry is not staleness.

The 8 Oct audit: 15 of 35 correct taskbar clicks were still stale. The shell's own window (it covers the press)
reported shown / reordered objects within the 10-40 ms the lookup took, most of which is reading the chosen element's
ancestors and index; which element it is was settled before that. The rule now counts only changes that came before
the answer was settled (``Element.chosen_at``), or that Windows itself dates before it (the event's own tick, however
late it reached the trace). Both sides: the redraw after the answer is not stale; a list reordering, a dialog
appearing and a menu closing before it still are. Every app and name is invented; nothing touches the desktop.
"""

from __future__ import annotations

import pytest
from test_capC1c_stale import DIALOG, MENU, PRESS, RECTS, SHELL, TASKBAR, _ClosingMenu, pinned
from test_trace_worker import APP, HWND, FakeUia, Rig, button

from yuki.memory.trace_hooks import EVENT_OBJECT_HIDE, EVENT_OBJECT_REORDER, EVENT_OBJECT_SHOW, TraceHooks
from yuki.memory.trace_uia import Element


class Settled(FakeUia):
    """A client whose answer is settled ``settle_s`` after it was asked and whose lookup ends ``slow_s`` later
    (the ancestry and index), as the real client stamps ``chosen_at`` / ``chosen_tick``."""

    def __init__(self, clock, *, settle_s: float, tick0: int = 50_000) -> None:
        super().__init__(clock)
        self.settle_s = settle_s
        self.tick0 = tick0
        self.t0 = clock.t

    def tick_at(self, t: float) -> int:
        return self.tick0 + int((t - self.t0) * 1000)

    def at_point(self, x, y, *, budget_s, index=False, **kw):
        self.calls.append(("point", x, y))
        el = self.points.get((x, y))
        self.clock.t += self.settle_s
        if el is not None:
            el.chosen_at, el.chosen_tick = self.clock.t, self.tick_at(self.clock.t)
        self.clock.t += self.slow_s
        return el


@pytest.fixture
def rig(tmp_path):
    r = Rig(tmp_path)
    r.w._rect = lambda h: RECTS.get(h)
    r.uia = r.w.uia = Settled(r.clock, settle_s=0.005)
    yield r
    r.store.close()


def _press_taskbar(rig, events, *, slow_s=0.03):
    rig.uia.points[PRESS] = pinned()
    rig.uia.slow_s = slow_s
    rig.under = (TASKBAR, SHELL)
    t = rig.clock.t
    rig.feed("down", t, "left", *PRESS, "user", 0, False, TASKBAR)
    for dt, event, hwnd, tick in events:
        rig.feed("out", t + dt, event, hwnd, -4, -77, *((tick,) if tick is not None else ()), 1)
    rig.feed("up", t + 0.09, "left", *PRESS, "user", False)
    rig.feed("fg", t + 0.1, HWND[APP], APP)
    rig.settle()
    return t


def test_the_taskbar_redrawing_itself_while_the_ancestry_is_read_is_not_stale(rig):
    """The live shape: shown and reordered objects of the shell's own window (which covers the press) heard 10-30 ms
    after the press, after the answer was settled at 5 ms, dated by Windows after it too."""
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.010, EVENT_OBJECT_SHOW, TASKBAR, tick(t0 + 0.010)),
                         (0.020, EVENT_OBJECT_REORDER, TASKBAR, tick(t0 + 0.020)),
                         (0.030, EVENT_OBJECT_HIDE, TASKBAR, tick(t0 + 0.030))])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "point" and rig.store._dec(row["name_ciphertext"]) == "Quillpad pinned"
    assert not rig.w.counters["stale_structure"] and not rig.w.counters["resolved_stale"]


def test_the_same_redraw_from_an_old_item_without_its_own_time_is_not_stale_either(rig):
    """The worker's older item shape (no event time): heard after the answer was settled, it does not count."""
    _press_taskbar(rig, [(0.015, EVENT_OBJECT_REORDER, TASKBAR, None)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "point"


def test_a_change_heard_before_the_answer_was_settled_is_stale(rig):
    rig.uia.settle_s = 0.05                      # ElementFromPoint answered 50 ms after the press
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.02, EVENT_OBJECT_REORDER, TASKBAR, tick(t0 + 0.02))])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale" and rig.w.counters["stale_structure"] == 1


def test_a_change_windows_dates_before_the_answer_is_stale_however_late_it_was_heard(rig):
    """The hook thread heard it after the answer (it was busy); Windows' own time of it is an earlier tick, after
    the press (2 ms in; the answer was settled at 5 ms)."""
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.02, EVENT_OBJECT_REORDER, TASKBAR, tick(t0) + 2)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale"


def test_the_tick_count_wrapping_round_is_still_ordered(rig):
    rig.uia.tick0 = 0xFFFFFFFF - 2                 # the press is 3 ticks before the wrap, the change at the wrap
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.02, EVENT_OBJECT_REORDER, TASKBAR, (tick(t0) + 3) & 0xFFFFFFFF)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale"


def test_a_change_windows_dates_before_the_press_and_delivered_late_is_not_stale(rig):
    """Review F2: the taskbar's own hover redraw, dated by Windows 500 ms before the press, reached the worker only
    after the answer: it was already on screen when the user pressed, so the press was on what the lookup found."""
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.02, EVENT_OBJECT_REORDER, TASKBAR, tick(t0) - 500)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "point" and not rig.w.counters["stale_structure"]


def test_one_tick_step_before_the_press_still_counts(rig, monkeypatch):
    """A change right after the press (a menu closing on the down) may read the tick count's step before the press:
    one step of it (the clock's interrupt interval, a Windows fact) is allowed, no more."""
    from yuki.memory import trace

    monkeypatch.setattr(trace, "tick_step_ms", lambda: 16)
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.02, EVENT_OBJECT_REORDER, TASKBAR, tick(t0) - 10)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale"


def test_the_before_answer_rule_directly():
    """The reviewer's case, by the numbers: pressed at 100.000, answer settled at 100.020 (tick 50020), a change
    heard at 100.025 and dated tick 49500 (500 ms before the press): not before the answer. Dated 50010: it is."""
    from types import SimpleNamespace

    from yuki.memory.trace import TraceWorker

    el = SimpleNamespace(chosen_at=100.020, chosen_tick=50_020)
    act = SimpleNamespace(t=100.000, lookup_end=100.030, el=el)
    assert TraceWorker._before_answer(act, 100.025, 49_500) is False
    assert TraceWorker._before_answer(act, 100.025, 50_010) is True
    assert TraceWorker._before_answer(act, 100.025, 50_030) is False            # dated after the answer
    assert TraceWorker._before_answer(act, 100.015, None) is True               # heard before the answer
    # second verify V2-3: dated before the press, heard between the press and the answer: not before the answer
    assert TraceWorker._before_answer(act, 100.010, 49_500) is False
    assert TraceWorker._before_answer(act, 100.010, 50_005) is True             # dated after the press: it counts
    act_old = SimpleNamespace(t=100.000, lookup_end=100.030, el=SimpleNamespace(chosen_at=None, chosen_tick=None))
    assert TraceWorker._before_answer(act_old, 100.010, 49_500) is True         # no answer tick: as before


def test_a_redraw_dated_before_the_press_and_heard_before_the_answer_is_not_stale(rig):
    """Second verify V2-3: the shell redrew the button's hover state as the pointer came onto it (dated 200 ms before
    the press); a late hook thread delivered it 12 ms after the press, before the answer was settled at 30 ms. It
    was on screen when the user pressed: the button found is what they pressed."""
    rig.uia.settle_s = 0.03
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.012, EVENT_OBJECT_REORDER, TASKBAR, tick(t0) - 200)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "point" and not rig.w.counters["stale_structure"]


def test_a_change_after_the_press_heard_before_the_answer_is_still_stale(rig):
    rig.uia.settle_s = 0.03
    tick = rig.uia.tick_at
    t0 = rig.clock.t
    _press_taskbar(rig, [(0.012, EVENT_OBJECT_REORDER, TASKBAR, tick(t0) + 10)])
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale"


def test_a_list_reordering_under_the_point_before_the_answer_is_still_stale(rig):
    rig.uia.points[(150, 120)] = Element(role="ListItem", name="Weekly notes", bounds=(100, 110, 400, 130), pid=APP,
                                         parent_role="List", ancestors=[("List", "")])
    rig.uia.settle_s, rig.uia.slow_s = 0.06, 0.02
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("out", t + 0.03, EVENT_OBJECT_REORDER, HWND[APP], -4, 0, rig.uia.tick_at(t + 0.03), 1)
    rig.feed("up", t + 0.1, "left", 150, 120, "user", False, dt=0.02)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


def test_a_dialog_appearing_over_the_point_before_the_answer_is_still_stale(rig):
    rig.uia.points[(150, 120)] = button()
    rig.uia.settle_s = 0.06
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, HWND[APP])
    rig.feed("out", t + 0.02, EVENT_OBJECT_SHOW, DIALOG, 0, 0, rig.uia.tick_at(t + 0.02), 1)
    rig.feed("up", t + 0.1, "left", 150, 120, "user", False, dt=0.02)
    rig.settle()
    assert rig.rows()[0]["resolved"] == "stale"


def test_a_menu_closing_while_the_lookup_answered_is_still_stale(rig):
    rig.uia = rig.w.uia = _ClosingMenu(rig.clock, rig)
    rig.uia.points[(150, 120)] = Element(role="MenuItem", name="Rename", bounds=(140, 110, 340, 130), pid=APP)
    rig.under = (MENU, APP)
    t = rig.clock.t
    rig.feed("down", t, "left", 150, 120, "user", 0, False, MENU)
    rig.feed("up", t + 0.08, "left", 150, 120, "user", False, dt=0.08)
    rig.settle()
    (row,) = rig.rows()
    assert row["resolved"] == "stale"


def test_a_change_queued_before_the_press_was_handled_is_judged_from_the_history_too(rig):
    """The event reached the queue before the press item (the worker was behind): the short history is judged by
    the same rule when the action finishes."""
    rig.uia.points[PRESS] = pinned()
    rig.under = (TASKBAR, SHELL)
    rig.uia.settle_s = 0.05
    t = rig.clock.t
    rig.feed("out", t + 0.01, EVENT_OBJECT_REORDER, TASKBAR, -4, -77, rig.uia.tick_at(t + 0.01), 1)
    rig.feed("down", t, "left", *PRESS, "user", 0, False, TASKBAR)
    rig.feed("up", t + 0.09, "left", *PRESS, "user", False)
    rig.settle()
    (row,) = [r for r in rig.rows() if r["kind"] == "click"]
    assert row["resolved"] == "stale"


def test_the_hook_thread_passes_the_events_own_time_and_keeps_the_generation_last():
    items: list = []
    h = TraceHooks(items.append, pid_of=lambda hwnd: (77,), own_pid=1, recent=None, clock=lambda: 5.0)
    h.blocked, h._fg, h.gen, h._n = False, 900, 3, 0
    h._on_event(None, EVENT_OBJECT_SHOW, 900, -4, 5, 0, 123456)
    assert items[-1] == ("out", 5.0, EVENT_OBJECT_SHOW, 900, -4, 5, 123456, 3)
    from yuki.memory.trace_hooks import EVENT_OBJECT_FOCUS

    h._on_event(None, EVENT_OBJECT_FOCUS, 900, -4, 5, 0, 654321)
    assert items[-1] == ("ev", 5.0, EVENT_OBJECT_FOCUS, 900, -4, 5, 654321)
