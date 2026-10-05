"""hotInput (5 Oct, report 73 release 6): a twitching mouse sensor never holds Yuki's hands; real input still does.

Live, release 6: the user's mouse sent 20-30 tiny moves a second with no key, click or wheel (measured read-only again
5 Oct ~16:00 over 60 s: 11-37 moves a second, median 23, 75% of them 0 px and the rest 1 px, the farthest travel in
0.3 s 5 px). ``GetLastInputInfo`` never got older than ~0.3 s, so the input gate held every ``type_text``, key, scroll
and raw click for its full 12 s (0 characters typed, real clicks 0/3, "you're typing" said), and the takeover watch
never armed (a key tap and a 160 px move both gave 0 pauses).

Offline only: the input monitor (:class:`yuki.actions.userinput.UserInputMonitor`) is fed by the test on a pretend
clock, never by the real Raw Input reader; the dispatcher acts on the stub desk. Nothing reads or sends real input.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

from yuki.actions import userinput
from yuki.actions.leases import INPUT_WAIT_LINE, InputGate, TakeoverWatch
from yuki.actions.userinput import UserInputMonitor
from yuki.core.inputtag import YUKI_INPUT_TAG


class Clock:
    def __init__(self, t: float = 1000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += s


def monitor(clock: Clock, *, move_px: int = 40, own: bool = False) -> UserInputMonitor:
    """A monitor on the pretend clock, listening since 60 s ago (a request started a while before the action)."""
    m = UserInputMonitor(move_px=move_px, now=clock, own_now=lambda t: own, own_span=lambda: (0.0, 0.0))
    m.running, m.started_at = True, clock.t - 60.0
    return m


class Jitter:
    """The live sensor: 20-30 moves a second, 75% of them 0 px and the rest 1 px, never more than a few px from home."""

    def __init__(self, m: UserInputMonitor, clock: Clock, seed: int = 5) -> None:
        self.m, self.clock, self.rng = m, clock, random.Random(seed)
        self.home = (960, 540)
        self.pos = self.home
        self.last_input = clock.t

    def run(self, seconds: float) -> None:
        end = self.clock.t + seconds
        while self.clock.t < end:
            self.clock.t += 1.0 / self.rng.randint(20, 30)
            if self.rng.random() >= 0.75:
                x, y = self.pos
                dx, dy = self.rng.choice([(1, 0), (-1, 0), (0, 1), (0, -1)])
                x, y = x + dx, y + dy
                if abs(x - self.home[0]) > 2 or abs(y - self.home[1]) > 2:
                    x, y = self.home
                self.pos = (x, y)
            self.m.move(self.pos, self.clock.t)
            self.last_input = self.clock.t

    def age(self) -> float:
        return self.clock.t - self.last_input


# -- the monitor ------------------------------------------------------------------------------------------------------


def test_the_measured_jitter_is_no_real_input():
    clock = Clock()
    m = monitor(clock)
    Jitter(m, clock).run(60.0)
    assert m.latest() == (0.0, None)
    assert m.counts["moves"] >= 1100 and "mouse" not in m.counts
    assert m.max_travel < 10


def test_a_hand_moving_the_mouse_is_real_input():
    clock = Clock()
    m = monitor(clock)
    for k in range(1, 9):                         # 160 px in 80 ms
        clock.t += 0.01
        m.move((500 + 20 * k, 500), clock.t)
    assert m.last("mouse") > 0 and m.latest()[1] == "mouse"
    one = monitor(Clock())
    one.move((500, 500), 1000.0)
    assert one.move((660, 500), 1000.01)          # one injected 160 px move (a poke, Deskflow) also counts


def test_the_takeover_distance_is_the_setting():
    clock = Clock()
    m = monitor(clock, move_px=100)
    for k in range(1, 5):                         # 80 px: under a 100 px setting
        clock.t += 0.02
        m.move((500 + 20 * k, 500), clock.t)
    assert m.last("mouse") == 0.0


def test_keys_buttons_and_the_wheel_count_and_yukis_own_tagged_input_never_does():
    clock = Clock()
    m = monitor(clock)
    assert not m.key_down(clock.t, YUKI_INPUT_TAG)
    assert not m.button_down(clock.t, YUKI_INPUT_TAG | (1 << 40))   # the low 32 bits, as Raw Input keeps them
    assert not m.wheel(clock.t, YUKI_INPUT_TAG)
    m.move((500, 500), clock.t)
    assert not m.move((900, 500), clock.t + 0.01, YUKI_INPUT_TAG)
    assert m.latest() == (0.0, None) and m.counts["own"] == 4
    # untagged injected input (Handy's dictation, Deskflow, a macro) is the user's
    assert m.key_down(clock.t + 1, 0) and m.button_down(clock.t + 2, 0x1234) and m.wheel(clock.t + 3)
    assert m.last("keys") == clock.t + 1 and m.last("click") == clock.t + 2 and m.latest() == (clock.t + 3, "wheel")


def test_yukis_own_cursor_jump_never_counts_as_the_user_moving_the_mouse():
    """Yuki moves the cursor with SetCursorPos (no event): the next twitch must not read as a 600 px move."""
    clock = Clock()
    span = {"s": (0.0, 0.0)}
    from yuki.actions import leases

    m = UserInputMonitor(move_px=40, now=clock, own_span=lambda: span["s"],
                         own_now=lambda t: leases.own_input_now(t, span=lambda: span["s"]))
    m.running = True
    m.move((960, 540), clock.t)
    clock.t += 0.05
    span["s"] = (clock.t, clock.t + 0.02)         # Yuki's click: the cursor jumps to (1560, 540)
    clock.t += 0.03
    assert not m.move((1560, 540), clock.t)       # a twitch inside its moment
    clock.t += 0.4
    assert not m.move((1561, 540), clock.t)       # and after it
    assert m.last("mouse") == 0.0


def test_the_monitor_runs_only_while_held():
    m = UserInputMonitor()
    started: list[int] = []
    m._start = lambda: started.append(1)          # type: ignore[method-assign]  (no real reader in a test)
    m._ready.set()
    m.acquire()
    m.acquire()
    m.release()
    assert started == [1] and m._users == 1
    m.release()
    assert m._users == 0 and not m.running
    m.release()                                   # an extra release is harmless
    assert m._users == 0


# -- the input gate ---------------------------------------------------------------------------------------------------


def gate(clock: Clock, m: Any, age: Any, **kw: Any) -> InputGate:
    kw.setdefault("held", lambda: False)
    return InputGate(age=age, sleep=clock.sleep, now=clock, cursor=lambda: (960, 540), signal=m, **kw)


def test_jitter_never_holds_typing_keys_scrolls_or_clicks():
    clock = Clock()
    m = monitor(clock)
    jitter = Jitter(m, clock)
    jitter.run(5.0)
    g = gate(clock, m, jitter.age)
    said: list[str] = []
    for _ in range(4):                            # type_text, press, scroll, click: each asks the gate
        jitter.run(0.3)
        assert jitter.age() < 0.3                 # the input clock is "just now", as live
        assert g.wait(notify=said.append) == {"waited": False, "ok": True, "ms": 0, "real": False}
    assert said == []


def test_a_real_key_holds_the_action_and_the_line_names_typing():
    clock = Clock()
    m = monitor(clock)
    jitter = Jitter(m, clock)
    g = gate(clock, m, jitter.age, notify_after_s=1.0, poll_s=0.05)
    stop_at = clock.t + 2.0
    said: list[str] = []
    real_sleep = g.sleep

    def sleep(s: float) -> None:                  # they type a key every 0.15 s for 2 s, under the jitter
        real_sleep(s)
        jitter.run(0.0)
        if clock.t < stop_at and int(clock.t / 0.15) != int((clock.t - s) / 0.15):
            m.key_down(clock.t)

    g.sleep = sleep
    m.key_down(clock.t - 0.1)
    result = g.wait(notify=said.append)
    assert result["ok"] and result["real"] and result["kind"] == "keys"
    assert 2700 <= result["ms"] <= 3000           # 0.8 s after their last key
    assert said == [INPUT_WAIT_LINE]


def test_a_click_holds_and_a_big_mouse_move_holds_without_the_typing_line():
    clock = Clock()
    m = monitor(clock)
    m.button_down(clock.t - 0.2)
    result = gate(clock, m, lambda: 0.2).wait()
    assert result["ok"] and result["real"] and result["kind"] == "click" and 550 <= result["ms"] <= 700
    clock2 = Clock()
    m2 = monitor(clock2)
    said: list[str] = []
    g = gate(clock2, m2, lambda: 0.0, notify_after_s=0.0, max_s=1.0)
    real_sleep = g.sleep

    def sleep(s: float) -> None:                  # the hand keeps moving the mouse far
        real_sleep(s)
        m2._last["mouse"] = clock2.t

    g.sleep = sleep
    m2._last["mouse"] = clock2.t
    result = g.wait(notify=said.append)
    assert not result["ok"] and result["kind"] == "mouse" and "kept moving the mouse" in result["why"]
    assert said == []                             # never "you're typing" for a mouse


def test_yukis_own_tagged_keys_never_hold_its_next_action():
    clock = Clock()
    m = monitor(clock)
    for k in range(20):                           # Yuki just typed 20 characters
        m.key_down(clock.t - 0.3 + k * 0.01, YUKI_INPUT_TAG)
    assert gate(clock, m, lambda: 0.05).wait()["waited"] is False


def test_no_monitor_and_no_key_pressed_goes_after_a_short_look():
    """Without the monitor (not on the real desktop yet, or Raw Input refused) the input clock still never holds."""
    clock = Clock()
    result = gate(clock, None, lambda: 0.03).wait()
    assert result["ok"] and result["real"] is False and result["ms"] <= 300


def test_the_dispatcher_types_presses_and_scrolls_under_jitter():
    from yuki.voice.offline import StubDesktop
    from yuki.agent.tools import Dispatcher

    clock = Clock()
    m = monitor(clock)
    jitter = Jitter(m, clock)
    jitter.run(2.0)
    desk = StubDesktop(action_delay_s=0)
    d = Dispatcher(desk)
    d.input_gate = gate(clock, m, jitter.age, max_s=12.0)
    for name, args in (("type_text", {"text": "hello"}), ("press", {"key": "enter"}),
                       ("scroll", {"direction": "down", "amount": 3})):
        out = d.dispatch(name, args)
        assert "Not sent" not in out.summary, (name, out.summary)
    assert any(c[0] == "type_text" for c in desk.calls) and any(c[0] == "press" for c in desk.calls)


# -- the takeover watch -----------------------------------------------------------------------------------------------


class Desk:
    """The takeover watch's sources on the pretend clock, the input clock moved by the jitter."""

    def __init__(self) -> None:
        self.clock = Clock()
        self.m = monitor(self.clock)
        self.jitter = Jitter(self.m, self.clock)
        self.span: tuple[float, float | None] = (0.0, 0.0)

    def watch(self) -> TakeoverWatch:
        return TakeoverWatch(age=self.jitter.age, keys=lambda: (frozenset(), frozenset()),
                             cursor=lambda: self.jitter.pos, span=lambda: self.span, ours=lambda kind, where: False,
                             now=self.clock, pressed=lambda: 0.0, signal=self.m)

    def look(self, w: TakeoverWatch, dt: float = 0.1) -> str | None:
        self.jitter.run(dt)
        return w.poll()


def test_the_watch_arms_under_jitter_and_jitter_never_pauses():
    d = Desk()
    d.jitter.run(3.0)
    w = d.watch()
    got = [d.look(w) for _ in range(100)]         # 10 s of jitter
    assert got == [None] * 100
    assert w.armed                                # live it never armed: the input clock never got 0.8 s old


def test_a_50_ms_key_tap_under_jitter_takes_over():
    d = Desk()
    w = d.watch()
    for _ in range(10):
        assert d.look(w) is None
    d.m.key_down(d.clock.t + 0.03)                # down and up between two looks, the input clock moving anyway
    assert d.look(w) == "keys"


def test_a_click_and_the_wheel_under_jitter_take_over():
    for feed, kind in ((UserInputMonitor.button_down, "click"), (UserInputMonitor.wheel, "wheel")):
        d = Desk()
        w = d.watch()
        for _ in range(10):
            d.look(w)
        feed(d.m, d.clock.t + 0.02)
        assert d.look(w) == kind


def test_a_160_px_mouse_move_under_jitter_takes_over():
    d = Desk()
    w = d.watch()
    for _ in range(10):
        d.look(w)
    start = d.jitter.pos
    for k in range(1, 5):                         # 160 px in 40 ms, then the hand rests there
        d.clock.t += 0.01
        d.jitter.pos = d.jitter.home = (start[0] + 40 * k, start[1])
        d.m.move(d.jitter.pos, d.clock.t)
    assert d.look(w) == "mouse"


def test_yukis_own_tagged_keys_and_clicks_never_take_over():
    d = Desk()
    w = d.watch()
    for _ in range(10):
        d.look(w)
    d.m.key_down(d.clock.t + 0.02, YUKI_INPUT_TAG)
    d.m.button_down(d.clock.t + 0.03, YUKI_INPUT_TAG)
    assert d.look(w) is None and d.look(w) is None


def test_typing_before_the_watch_is_the_baseline_under_jitter():
    d = Desk()
    for _ in range(5):                            # they were typing as the request reached its first action
        d.m.key_down(d.clock.t)
        d.jitter.run(0.1)
    w = d.watch()
    assert d.look(w) is None and not w.armed
    d.m.key_down(d.clock.t + 0.02)                # their next keystroke is the baseline, not a takeover
    assert d.look(w) is None and w.ignored.get("busy")
    for _ in range(9):                            # still (only the jitter) for 0.9 s
        assert d.look(w) is None
    assert w.armed
    d.m.key_down(d.clock.t + 0.02)
    assert d.look(w) == "keys"


# -- the agent holds the monitor on the real desktop only ---------------------------------------------------------------


class _Log:
    def __init__(self) -> None:
        self.records: list[tuple[str, dict]] = []

    def log(self, kind: str, **fields: Any) -> None:
        self.records.append((kind, fields))


def _fake_agent(backend: Any) -> Any:
    """Just what ``Agent._input_on`` / ``_input_off`` read (no real agent, desk or memory is made)."""
    from types import SimpleNamespace

    return SimpleNamespace(dispatcher=SimpleNamespace(backend=backend), _input_held=False, logger=_Log())


def test_a_stub_desk_never_starts_the_monitor(monkeypatch):
    from yuki.agent.loop import Agent
    from yuki.voice.offline import StubDesktop

    calls: list[str] = []
    monkeypatch.setattr(userinput, "acquire", lambda: calls.append("acquire") or True)
    monkeypatch.setattr(userinput, "release", lambda: calls.append("release"))
    agent = _fake_agent(StubDesktop(action_delay_s=0))
    Agent._input_on(agent)
    Agent._input_off(agent)
    assert calls == []


def test_the_agent_holds_the_monitor_for_a_real_desktop_request(monkeypatch):
    from yuki.agent import tools
    from yuki.agent.loop import Agent

    calls: list[str] = []
    monkeypatch.setattr(userinput, "acquire", lambda: calls.append("acquire") or True)
    monkeypatch.setattr(userinput, "release", lambda: calls.append("release"))
    agent = _fake_agent(tools._LazyRealBackend.__new__(tools._LazyRealBackend))
    Agent._input_on(agent)
    Agent._input_on(agent)                        # once per request
    Agent._input_off(agent)
    Agent._input_off(agent)
    assert calls == ["acquire", "release"]
    assert [r[1]["state"] for r in agent.logger.records] == ["released"]


def test_the_run_takes_and_gives_back_the_monitor():
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "yuki" / "agent" / "loop.py").read_text(encoding="utf-8")
    lines = [line.strip() for line in src.splitlines()]
    start = lines.index("self._input_on()")
    assert lines[start - 1] == "self._watch_off()" and lines[start - 3] == "self._cancelled.clear()"
    end = lines.index("self._input_off()")
    assert lines[end - 1] == "self._acting_done()"


if __name__ == "__main__":   # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
