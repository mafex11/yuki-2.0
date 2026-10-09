"""capQ2a (report 103): a hover is kept only when presence says the user moved the mouse there.

The 8 Oct audit: 59 hovers came while the user was away, the mouse drifting over the taskbar about every 20 s. A
rest becomes a hover only when presence's own rule (:mod:`yuki.memory.userpresence`: the takeover distance within its
window, from the mouse's own reports) saw a real movement since that movement began; presence that cannot say keeps
none. Every app and name is invented; nothing touches the desktop.
"""

from __future__ import annotations

import pytest
from test_trace_worker import FakeSink, Rig, button

from yuki.memory.userpresence import PresenceInput


@pytest.fixture
def rig(tmp_path, monkeypatch):
    from yuki.memory import trace, watcher

    r = Rig(tmp_path)
    r.monitor = PresenceInput(move_px=40, window_s=0.3, now=r.clock)

    class Service:                                             # the service's presence, listening by Raw Input
        source = "raw_input"
        monitor = r.monitor

    monkeypatch.setattr(watcher, "_presence", Service, raising=False)
    r.w._present = trace._presence_moved()
    r.sink = FakeSink()
    r.w.sink = r.sink
    r.uia.points[(150, 120)] = button(name="Settings")
    yield r
    r.store.close()


def _rest(rig, *, hand: bool) -> None:
    """The pointer starts moving, moves (a hand: 60 counts at once; a drift: 1 count a report), then rests."""
    seq = rig.sink.anchor_seq + 1
    rig.sink.anchor_at, rig.sink.anchor_seq, rig.sink.moving = rig.clock.t, seq, True
    rig.feed("moving", rig.clock.t)
    for _ in range(3):
        rig.clock.t += 0.05
        rig.monitor.relative(30 if hand else 1, 0)
    rig.sink.anchor_at = rig.clock.t
    rig.clock.t += 0.5
    rig.w.tick()


def test_a_rest_after_the_users_own_movement_is_a_hover(rig):
    _rest(rig, hand=True)
    rig.settle(62)                                             # an open rest is closed after a minute
    (row,) = rig.rows()
    assert row["kind"] == "hover" and rig.store._dec(row["name_ciphertext"]) == "Settings"


def test_a_drifting_mouse_while_the_user_is_away_makes_no_hover(rig):
    _rest(rig, hand=False)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["skip_hover_away"] == 1
    assert rig.uia.calls == []                                 # no lookup for it either


def test_an_old_real_movement_does_not_vouch_for_a_later_drift(rig):
    _rest(rig, hand=True)
    rig.clock.t += 20
    rig.cursor = (400, 300)
    rig.uia.points[(400, 300)] = button(name="Search")
    _rest(rig, hand=False)                                     # 20 s later the mouse drifted on its own
    rig.settle()
    assert [rig.store._dec(r["name_ciphertext"]) for r in rig.rows()] == ["Settings"]


def test_presence_that_cannot_say_keeps_no_hover(rig):
    rig.w._present = lambda since: None
    _rest(rig, hand=True)
    rig.settle()
    assert rig.rows() == [] and rig.w.counters["skip_hover_away"] == 1


def test_clicks_and_keys_are_not_judged_by_it(rig):
    rig.w._present = lambda since: False
    rig.click()
    rig.feed("key", rig.clock.t, "chord", "Ctrl+B", "user", 0, dt=0.2)
    rig.settle()
    assert [r["kind"] for r in rig.rows()] == ["click", "keys"]


def test_the_services_presence_is_asked_by_its_own_rule(monkeypatch):
    from yuki.memory import trace, watcher

    class P:
        source = "raw_input"
        monitor = PresenceInput(move_px=40, window_s=0.3, now=lambda: 10.0)

    P.monitor.relative(50, 0)                                    # a hand's move at t=10
    monkeypatch.setattr(watcher, "_presence", P, raising=False)
    moved = trace._presence_moved()
    assert moved(9.0) is True and moved(11.0) is False
    P.source = "fallback"
    assert moved(9.0) is None


# -- second verify V2-2: a short nudge right after the user's click is theirs ------------------------------------------


def _users_click(rig, x, y):
    rig.monitor.button_down(at=rig.clock.t)                    # presence hears it
    rig.sink.anchor_at, rig.sink.anchor_seq = rig.clock.t, rig.sink.anchor_seq + 1   # a press ends the rest
    rig.click(x, y)
    rig.settle(1)


def test_a_short_nudge_right_after_the_users_click_is_a_hover(rig):
    """The user clicks a toolbar button, nudges the mouse 3 counts to the next one and rests on it to read its
    tooltip: shorter than the takeover distance, but presence heard their click since the pointer last rested."""
    _rest(rig, hand=True)                                      # onto Settings: a hover
    rig.settle(1)
    _users_click(rig, 150, 120)
    rig.cursor = (400, 300)
    rig.uia.points[(400, 300)] = button(name="Search")
    _rest(rig, hand=False)                                     # the nudge
    rig.settle(62)
    kinds = [(r["kind"], rig.store._dec(r["name_ciphertext"])) for r in rig.rows()]
    assert kinds == [("hover", "Settings"), ("click", "Settings"), ("hover", "Search")]
    assert not rig.w.counters["skip_hover_away"]


def test_after_the_user_left_only_the_first_drifting_rest_can_pass(rig):
    """The cost, by the numbers: the user clicked and left; the mouse drifts every 20 s. The first rest after their
    click passes (presence heard the click since the last rest), every later one is dropped."""
    _rest(rig, hand=True)
    rig.settle(1)
    _users_click(rig, 150, 120)
    for i, x in enumerate((400, 600, 800, 1000)):
        rig.clock.t += 20
        rig.cursor = (x, 300)
        rig.uia.points[(x, 300)] = button(name=f"Invented spot {i}")
        _rest(rig, hand=False)
        rig.settle(1)
    hovers = [r for r in rig.rows() if r["kind"] == "hover"]
    assert len(hovers) == 2 and rig.w.counters["skip_hover_away"] == 3


def test_keys_typed_before_the_nudge_count_like_a_click(rig):
    _rest(rig, hand=True)
    rig.settle(1)
    rig.monitor.key_down(at=rig.clock.t)
    rig.cursor = (400, 300)
    rig.uia.points[(400, 300)] = button(name="Search")
    _rest(rig, hand=False)
    rig.settle(62)
    assert [r["kind"] for r in rig.rows()] == ["hover", "hover"]
