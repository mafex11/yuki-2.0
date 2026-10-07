"""capC1b (report 97, left open by report 96's second verification): the trace worker's hung-app watchdog.

A UI Automation or MSAA call into an app that stopped answering could hold the trace worker for as long as UI
Automation's own timeouts allow. Now, by machine facts only (Windows' ``IsHungAppWindow``, a ``WM_NULL`` sent with
``SMTO_ABORTIFHUNG``, or a lookup the worker measured at :data:`yuki.memory.trace_uia.SLOW_CALL_S` or more), lookups
into that app are skipped (the action is kept point-only) while it stays hung, with health counters, and they come
back once it answers again. Fakes only: nothing is sent to any real window.
"""

from __future__ import annotations

import time

from test_trace_worker import APP, HWND, Rig, button  # noqa: F401
from yuki.memory.trace_uia import HungWatch


class Fake:
    def __init__(self) -> None:
        self.hung: set[int] = set()
        self.answering = True
        self.t = 0.0
        self.probes: list[int] = []
        self.asked: list[int] = []

    def is_hung(self, hwnd: int) -> bool:
        self.asked.append(hwnd)
        return hwnd in self.hung

    def answers(self, hwnd: int) -> bool:
        self.probes.append(hwnd)
        return self.answering and hwnd not in self.hung

    def clock(self) -> float:
        return self.t


def watch(f: Fake, **kw) -> HungWatch:
    return HungWatch(hung=f.is_hung, answers=f.answers, pid_of=lambda hwnd: 500 + hwnd, clock=f.clock,
                     exists=lambda hwnd: True, **kw)


def test_a_hung_window_is_skipped_until_it_answers_again():
    f = Fake()
    w = watch(f)
    assert not w.skip(11, 101)
    f.hung.add(11)
    assert w.skip(11, 101) and w.marked() == 1
    asked = len(f.asked)
    f.t += 0.5
    assert w.skip(11, 101) and len(f.asked) == asked              # not asked again before PROBE_EVERY_S
    f.t += 2.0
    assert w.skip(11, 101) and len(f.asked) == asked + 1          # asked: still hung (no message sent to it)
    assert f.probes == []
    f.hung.clear()
    f.t += 2.0
    assert not w.skip(11, 101) and w.marked() == 0                # it answers: lookups as usual again
    assert w.counts == {"hung_marked": 1, "hung_slow": 0, "hung_unanswered": 0, "hung_skipped": 3,
                        "hung_recovered": 1}


def test_a_slow_lookup_marks_its_app_and_another_app_is_not_affected():
    f = Fake()
    w = watch(f)
    w.note(11, 101, 0.05)
    assert w.marked() == 0
    w.note(11, 101, 1.5)
    assert w.marked() == 1 and w.counts["hung_slow"] == 1
    assert w.skip(11, 101) and not w.skip(12, 102)
    f.t += 3.0
    assert not w.skip(11, 101)                                    # not hung by Windows, answers: recovered


def test_the_pid_is_found_from_the_window_when_unknown():
    f = Fake()
    w = watch(f)
    f.hung.add(11)
    assert w.skip(11, 0) and w.skip(11, 511)                      # the same process either way


def test_the_worker_skips_lookups_into_a_hung_app_keeps_the_click_point_only_and_recovers(tmp_path):
    rig = Rig(tmp_path)
    try:
        f = Fake()
        rig.w.hung = watch(f)
        rig.uia.points[(150, 120)] = button()
        f.hung.add(HWND[APP])
        rig.click()
        rig.settle()
        rows = rig.rows()
        assert rows and rows[-1]["kind"] == "click" and rows[-1]["element_key"] is None
        assert ("point", 150, 120) not in rig.uia.calls               # no call went into the hung app
        stats = rig.w.stats()
        assert stats["lookup_skipped_hung"] >= 1 and stats["hung_marked"] == 1 and stats["hung_now"] == 1
        f.hung.clear()
        f.t += 5.0
        rig.click()
        rig.settle()
        assert ("point", 150, 120) in rig.uia.calls and rig.rows()[-1]["element_key"]
        assert rig.w.stats()["hung_recovered"] == 1 and rig.w.stats()["hung_now"] == 0
    finally:
        rig.store.close()


def test_a_lookup_measured_slow_by_the_worker_marks_the_app(tmp_path):
    rig = Rig(tmp_path)
    try:
        f = Fake()
        rig.w.hung = watch(f, slow_s=0.02)
        real = rig.uia.at_point

        def slow(x, y, *, budget_s, index=False):
            time.sleep(0.04)
            return real(x, y, budget_s=budget_s, index=index)

        rig.uia.at_point = slow
        rig.uia.points[(150, 120)] = button()
        rig.click()
        rig.settle()
        assert rig.w.stats()["hung_slow"] >= 1 and rig.w.hung.marked() == 1
        calls = len(rig.uia.calls)
        rig.click()
        rig.settle()
        assert len([c for c in rig.uia.calls[calls:] if c[0] == "point"]) == 0   # skipped while marked
    finally:
        rig.store.close()


def test_the_trace_health_carries_the_watchdogs_counts_for_the_doctor(tmp_path):
    rig = Rig(tmp_path)
    try:
        f = Fake()
        rig.w.hung = watch(f)
        f.hung.add(HWND[APP])
        rig.click()
        rig.settle()
        from yuki.core.doctor import _trace_learn_words

        words = _trace_learn_words(None, rig.w.stats())
        assert "skipped 1 look into apps that stopped answering" in words
    finally:
        rig.store.close()


def test_the_re_probe_never_changes_the_shared_user32_prototype():
    """Review F6: ``window_answers`` uses its own ``SendMessageTimeoutW`` prototype; the shared ``windll`` one keeps
    whatever argument types other code gave it (``yuki.perception.tree`` passes a ``DWORD`` result). Window 0: no
    window gets the message."""
    import ctypes
    from ctypes import wintypes

    from yuki.memory.trace_uia import window_answers

    shared = ctypes.windll.user32.SendMessageTimeoutW
    before = (shared.argtypes, shared.restype)
    assert window_answers(0) is False
    assert (shared.argtypes, shared.restype) == before
    result = wintypes.DWORD(0)
    shared(None, 0, 0, 0, 0x0002, 1, ctypes.byref(result))      # a DWORD result still passes (no ArgumentError)


# -- second verification V2: an app frozen for less than 5 s, before an MSAA call --------------------------------------


def test_v2_an_msaa_lookup_probes_first_and_skips_an_app_frozen_for_less_than_five_seconds():
    """Windows calls a window hung only after 5 s without messages; an MSAA call has no timeout. Before one
    (``probe=True``) the window must take a no-op message: one that does not is marked at once and skipped; UI
    Automation lookups (their own timeouts) are not probed."""
    f = Fake()
    w = watch(f)
    f.answering = False                                           # frozen, but not yet "hung" by Windows' verdict
    assert not w.skip(11, 101) and f.probes == []                 # a UI Automation lookup: no probe
    assert w.skip(11, 101, probe=True) and f.probes == [11]       # the MSAA lookup: probed, skipped, marked
    assert w.marked() == 1 and w.counts["hung_unanswered"] == 1
    assert w.skip(11, 101)                                        # then every lookup into it, until it answers
    f.answering = True
    f.t += 3.0
    assert not w.skip(11, 101, probe=True) and w.marked() == 0 and w.counts["hung_recovered"] == 1
    assert not w.skip(11, 101, probe=True)                        # answering: the call goes ahead


def test_v2_a_gone_window_is_never_probed():
    f = Fake()
    w = HungWatch(hung=f.is_hung, answers=f.answers, pid_of=lambda hwnd: 0, clock=f.clock, exists=lambda hwnd: False)
    f.answering = False
    assert not w.skip(11, 101, probe=True) and f.probes == []


def test_v2_the_worker_never_makes_the_outcome_msaa_call_into_a_window_that_does_not_answer(tmp_path):
    """The user clicks; the app freezes at once (not yet hung by Windows' verdict). The click's outcome event is
    looked up through MSAA: the worker probes first, skips the call, marks the app; the row is still written."""
    rig = Rig(tmp_path)
    try:
        f = Fake()
        rig.w.hung = watch(f)
        rig.uia.points[(150, 120)] = button()
        rig.click()
        f.answering = False                                       # frozen right after the click
        rig.feed("out", rig.clock.t + 0.05, 0x800A, HWND[APP], -4, 3, 1)   # a state change in the window
        rig.settle()
        rows = rig.rows()
        assert rows and rows[-1]["kind"] == "click" and rows[-1]["element_key"]   # the press itself was looked up
        assert not [c for c in rig.uia.calls if c[0] == "event"]                  # no MSAA call into the frozen app
        stats = rig.w.stats()
        assert stats["hung_unanswered"] == 1 and stats["lookup_skipped_hung"] >= 1 and f.probes
    finally:
        rig.store.close()
