"""appTakeover (4 Oct): when the user takes over the keyboard or mouse while a request works, it pauses.

The user: "when it's working and I start typing with my keyboard or I suddenly move my mouse then it should think that
I have taken over ... and it should stop temporarily ... not a very small vibration, a big movement, because my mouse
sensor is bad and sometimes vibrates on its own."

Covered here, offline only (stub input sources, a scripted brain, the stub desk, invented names; no hook, no live
desktop, no microphone, no real memory):

1. the watch (:class:`yuki.actions.leases.TakeoverWatch`): jitter never pauses, a real move does, a key and a click do,
   Yuki's own cursor moves and keys never do (also when a look comes late), input that went to Yuki is not a takeover;
2. the watch thread: fires once, stops, waits its grace; the movement setting is kept within bounds;
3. the agent: an action the pause caught is not run, the request waits, a resume reads the screen again (the window
   in front, in full) and goes on, words with the resume reach the brain as the user's, a stop or the long-pause limit
   ends it with "Stopped. Nothing else was done.", and nothing pauses without the watch (CLI, evals) or before acting;
4. the runtime: the pause reaches the UI at once, words to a paused request resume it, Resume by id;
5. the island: "You took over · paused" with Resume and Stop (auto-expanded), the run card's paused line and Resume,
   the tray face;
6. the voice: the talker's line says it is paused (its "go on" is for this task), no "still on it" while paused, an
   unresumed end is quiet;
7. an offline replay of a whole run through the runtime with a takeover injected mid-run.
"""

from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from yuki.actions import leases
from yuki.actions.leases import TakeoverThread, TakeoverWatch, takeover_move_px
from yuki.agent.memory import MemoryAccess
from yuki.config import Settings
from yuki.log.events import ErrorEvent, Final, Paused, SessionLogger, ToolCall, ToolResult
from yuki.voice.offline import ChatPlayerDesk


# -- 1. the watch, on stub input sources ------------------------------------------------------------------------------


class Desk:
    """Stub input sources on a stub clock: the cursor, the keys, ``GetLastInputInfo`` and Yuki's own input span."""

    def __init__(self) -> None:
        self.t = 1000.0
        self.pos: tuple[int, int] | None = (500, 500)
        self.down: frozenset[int] = frozenset()
        self.tapped: frozenset[int] = frozenset()
        self.last_input = 900.0
        self.span: tuple[float, float | None] = (0.0, 0.0)
        self.yuki_windows = False
        self.key_reads = 0

    def watch(self, move_px: int = 40) -> TakeoverWatch:
        return TakeoverWatch(move_px=move_px, age=lambda: self.t - self.last_input, keys=self.keys,
                             cursor=lambda: self.pos, span=lambda: self.span, ours=lambda kind, where: self.yuki_windows,
                             now=lambda: self.t)

    def keys(self) -> tuple[frozenset[int], frozenset[int]]:
        self.key_reads += 1
        tapped, self.tapped = self.tapped, frozenset()   # the low bit is "since the last look"
        return self.down, tapped

    def move(self, x: int, y: int) -> None:
        self.pos = (x, y)
        self.last_input = self.t

    def step(self, w: TakeoverWatch, dt: float = 0.1) -> str | None:
        self.t += dt
        return w.poll()


def test_jitter_never_pauses():
    """A twitching sensor: the cursor shakes a few pixels around one point for ten seconds (and drifts 8 px a second)."""
    d = Desk()
    w = d.watch(40)
    d.step(w)
    import random

    rng = random.Random(4)
    fired = []
    for k in range(100):
        d.move(500 + int(k * 0.8) + rng.randint(-4, 4), 500 + rng.randint(-4, 4))
        fired.append(d.step(w))
    assert fired == [None] * 100
    assert w.max_travel < 20     # what jitter looked like, for the log


def test_a_one_look_jump_that_comes_straight_back_never_pauses():
    d = Desk()
    w = d.watch(40)
    d.step(w)
    d.move(560, 500)              # a glitch: one look far away...
    assert d.step(w) is None
    d.move(501, 500)              # ...and straight back
    assert d.step(w) is None
    assert d.step(w) is None


def test_a_real_move_pauses_within_a_few_looks():
    d = Desk()
    w = d.watch(40)
    d.step(w)
    got = []
    for x in (530, 580, 640, 700):
        d.move(x, 510)
        got.append(d.step(w))
    assert "mouse" in got and got.index("mouse") <= 2


def test_a_flick_that_stops_far_away_pauses():
    d = Desk()
    w = d.watch(40)
    d.step(w)
    d.move(900, 300)
    assert d.step(w) is None      # seen far once...
    assert d.step(w) == "mouse"   # ...and still far on the next look


def test_the_threshold_is_the_setting():
    d = Desk()
    w = d.watch(100)
    d.step(w)
    for x in (530, 560, 580):     # 80 px: under a 100 px setting
        d.move(x, 500)
        assert d.step(w) is None


def test_a_key_pauses_and_a_click_is_told_apart():
    d = Desk()
    w = d.watch()
    d.step(w)
    d.down, d.last_input = frozenset({0x41}), d.t + 0.05
    assert d.step(w) == "keys"
    d2 = Desk()
    w2 = d2.watch()
    d2.step(w2)
    d2.tapped, d2.last_input = frozenset({1}), d2.t + 0.05     # pressed and let go between two looks
    assert d2.step(w2) == "click"


def test_a_key_held_from_before_the_watch_never_counts():
    d = Desk()
    d.down = frozenset({0x10})    # Shift held as the watch began (the baseline)
    w = d.watch()
    d.step(w)
    for _ in range(10):
        d.last_input = d.t        # autorepeat keeps the input clock moving
        assert d.step(w) is None


def test_keys_are_read_only_when_input_came():
    """An idle desk costs the cursor and the input clock per look; the 250 key reads only when something came."""
    d = Desk()
    w = d.watch()
    d.step(w)
    reads = d.key_reads
    for _ in range(20):
        assert d.step(w) is None
    assert d.key_reads == reads


def test_yukis_own_cursor_moves_and_keys_never_pause():
    d = Desk()
    w = d.watch()
    d.step(w)
    # Yuki clicks far away and types: its span runs, the cursor jumps, keys go down and up
    d.span = (d.t + 0.01, None)
    d.move(1500, 900)
    d.down = frozenset({0x11, 0x56})
    assert d.step(w) is None
    d.down, d.tapped = frozenset(), frozenset({0x11, 0x56})
    d.last_input = d.t
    d.span = (d.span[0], d.t + 0.02)
    assert d.step(w) is None      # within the slack after its end
    for _ in range(10):           # the cursor stays where Yuki put it
        assert d.step(w) is None
    assert w.ignored.get("own", 0) >= 1


def test_a_late_look_after_yukis_input_never_takes_its_keys_for_the_users():
    """The watch thread starved (a busy PC): Yuki's whole input moment came and went between two looks."""
    d = Desk()
    w = d.watch()
    d.step(w)
    start = d.t + 0.02
    d.span = (start, start + 0.05)        # began and ended since the last look
    d.pos = (1400, 200)
    d.tapped = frozenset({0x0D})          # its Enter, still in the "pressed since" bit
    d.last_input = start + 0.05
    d.t += 1.0                            # the next look comes a second late
    d.last_input = d.t - 0.01             # and the twitching sensor moved the clock since
    assert w.poll() is None
    assert d.step(w) is None


def test_the_user_after_yukis_move_does_pause():
    d = Desk()
    w = d.watch()
    d.step(w)
    d.span = (d.t + 0.01, d.t + 0.03)
    d.move(1500, 900)
    assert d.step(w) is None
    d.t += 0.5
    w.poll()
    got = []
    for x in (1440, 1380, 1300):          # then the user's hand
        d.move(x, 880)
        got.append(d.step(w))
    assert "mouse" in got


def test_input_that_went_to_yuki_is_not_a_takeover():
    d = Desk()
    d.yuki_windows = True                 # typing into the ask bar, a click on the island
    w = d.watch()
    d.step(w)
    d.down, d.last_input = frozenset({0x41}), d.t
    assert d.step(w) is None
    assert w.ignored.get("yuki") == 1


# -- 2. the thread and the setting -------------------------------------------------------------------------------------


class _Watch:
    def __init__(self, after: int, kind: str = "mouse") -> None:
        self.after, self.kind, self.polls = after, kind, 0

    def poll(self) -> str | None:
        self.polls += 1
        return self.kind if self.polls >= self.after else None


def test_the_thread_fires_once_then_ends():
    got: list[str] = []
    stub = _Watch(3)
    t = TakeoverThread(got.append, move_px=40, poll_s=0.01, watch=lambda: stub).start()
    t._thread.join(2.0)
    assert got == ["mouse"] and t.fired == "mouse" and not t.running and stub.polls == 3


def test_a_takeover_not_taken_now_keeps_the_thread_watching():
    answers = [False, False, True]
    got: list[str] = []

    def on(kind: str) -> bool:
        got.append(kind)
        return answers.pop(0)

    t = TakeoverThread(on, move_px=40, poll_s=0.01, watch=lambda: _Watch(1, "keys")).start()
    t._thread.join(2.0)
    assert got == ["keys"] * 3 and t.refused == 2 and t.fired == "keys"


def test_no_pause_while_yuki_asks_or_a_send_counts_down(tmp_path: Path):
    agent = _agent(Brain([]), tmp_path, _desk(), watches=Watches())
    agent._acting_token = "acting"
    agent._awaiting_answer = True                 # the hand goes to Answer on the island
    assert agent.pause("mouse") is False
    agent._awaiting_answer = False
    agent._send_open = {"who": "Sam"}             # the hand goes to Stop or a chip on the send card
    assert agent.pause("click") is False
    agent._send_open = None
    agent._pause_quiet_until = time.monotonic() + 5.0   # and for a moment after, while it moves on
    assert agent.pause("mouse") is False
    agent._pause_quiet_until = 0.0
    assert agent.pause("mouse") is True and agent.paused


def test_a_stopped_thread_never_fires_and_the_grace_delays_the_first_look():
    got: list[str] = []
    made: list[float] = []

    def make() -> _Watch:
        made.append(time.monotonic())
        return _Watch(1)

    t = TakeoverThread(got.append, move_px=40, poll_s=0.01, grace_s=5.0, watch=make).start()
    time.sleep(0.05)
    t.stop()
    t._thread.join(2.0)
    assert got == [] and made == []       # stopped inside its grace: never looked
    started = time.monotonic()
    t2 = TakeoverThread(got.append, move_px=40, poll_s=0.01, grace_s=0.2, watch=make).start()
    t2._thread.join(2.0)
    assert got == ["mouse"] and made[0] - started >= 0.18


def test_the_movement_setting_is_kept_within_bounds(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    assert takeover_move_px(5) == leases.TAKEOVER_MOVE_MIN_PX
    assert takeover_move_px(9999) == leases.TAKEOVER_MOVE_MAX_PX
    assert takeover_move_px("x") == leases.TAKEOVER_MOVE_PX
    from yuki.ui import ui_settings

    path = tmp_path / "ui.json"
    saved = ui_settings.update(path, takeover_move_px=100)
    assert saved.takeover_move_px == 100 and ui_settings.load(path).takeover_move_px == 100
    assert ui_settings.update(path, takeover_move_px=3).takeover_move_px == 12
    monkeypatch.setattr(ui_settings, "load", lambda path=None: ui_settings.UiSettings(takeover_move_px=64))
    assert takeover_move_px(None) == 64
    assert ui_settings.TAKEOVER_MOVE_LABEL and "Yuki" in ui_settings.TAKEOVER_MOVE_HINT


# -- 3. the agent ---------------------------------------------------------------------------------------------------------

PLAYER = ChatPlayerDesk.PLAYER


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """No live Now picture, and no real window lookups for the stub desk's invented window numbers."""
    import yuki.agent.tools as tools
    import yuki.memory.now as now

    monkeypatch.setattr(now, "shared_client", lambda *a, **k: None)
    monkeypatch.setattr(tools, "_is_own_window", lambda hwnd: False)


def _block(kind: str, **kw: Any) -> SimpleNamespace:
    return SimpleNamespace(type=kind, **kw)


def _use(tool: str, i: int = 0, /, **inp: Any) -> SimpleNamespace:
    return _block("tool_use", id=f"t{tool}{i}", name=tool, input=inp)


def _usage() -> SimpleNamespace:
    return SimpleNamespace(input_tokens=10, output_tokens=5, cache_read_input_tokens=0, cache_creation_input_tokens=0)


def _response(*blocks: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(content=list(blocks), stop_reason="tool_use", usage=_usage(), id="msg", model="m",
                           role="assistant", model_dump=lambda **_: {})


class Brain:
    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **params: Any) -> SimpleNamespace:
        self.calls.append(params)
        step = self.script.pop(0)
        return step(params) if callable(step) else step


def _texts(params: dict) -> str:
    out = []
    for m in params.get("messages") or []:
        content = m.get("content")
        for b in content if isinstance(content, list) else [content]:
            if isinstance(b, dict):
                if b.get("type") == "text":
                    out.append(str(b.get("text") or ""))
                elif b.get("type") == "tool_result":
                    out += [str(c.get("text") or "") for c in b.get("content") or [] if isinstance(c, dict)]
            elif isinstance(b, str):
                out.append(b)
    return "\n".join(out)


class Watches:
    """The agent's takeover factory on stubs: records each watch made, and lets a test fire the user's takeover."""

    def __init__(self) -> None:
        self.made: list[dict[str, Any]] = []

    def __call__(self, on_takeover: Any, *, grace_s: float = 0.0) -> Any:
        w = SimpleNamespace(on=on_takeover, grace_s=grace_s, started=False, stopped=False)
        w.start = lambda: setattr(w, "started", True)
        w.stop = lambda: setattr(w, "stopped", True)
        self.made.append(w)
        return w

    def fire(self, kind: str = "mouse") -> None:
        live = [w for w in self.made if w.started and not w.stopped]
        assert live, "no watch is running"
        live[-1].on(kind)


def _desk() -> ChatPlayerDesk:
    return ChatPlayerDesk(friend="Sam", handle="sam_99", history=[])


def _agent(brain: Brain, tmp: Path, desk: Any, *, watches: Watches | None = None, limit: float = 600.0):
    from yuki.agent.loop import Agent

    settings = Settings(log_dir=tmp / "sessions", stream=False, requests_csv=tmp / "r.csv", judge=False,
                        consult=False, takeover_idle_stop_s=limit)
    agent = Agent(settings, SessionLogger(settings.sessions_dir, console=None), client=brain, backend=desk,
                  prewarm=False, record_requests=False, memory=MemoryAccess(open_client=lambda: None), lane="worker")
    agent.takeover = watches
    return agent


def _press(i: int, key: str) -> SimpleNamespace:
    return _use("press", i, key=key, expect_hwnd=PLAYER)


def _done(i: int = 0, message: str = "Done: playing the next song.") -> SimpleNamespace:
    return _use("done", i, message=message, about=[], states=[])


def _presses(desk: ChatPlayerDesk) -> list[str]:
    return [args[0] for name, args, _kw in desk.calls if name == "press"]


def _drive(agent: Any, words: str, on_event: Any = None) -> list[Any]:
    events = []
    for ev in agent.run(words):
        events.append(ev)
        if on_event is not None:
            on_event(agent, ev)
    return events


def test_the_pause_catches_the_next_action_reads_the_screen_again_and_goes_on(tmp_path: Path):
    from yuki.agent.loop import PAUSED_SKIP

    desk = _desk()
    watches = Watches()
    seen: dict[str, str] = {}
    told: list[tuple] = []

    def second_call(params: dict) -> SimpleNamespace:
        watches.fire("mouse")                       # the user grabs the mouse while the brain decides its next step
        return _response(_press(1, "nexttrack"))

    def after_resume(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_press(2, "nexttrack"), _done())

    brain = Brain([_response(_press(0, "playpause")), second_call, after_resume, _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, watches=watches)
    agent.on_pause = lambda state, why, s: told.append((state, why))

    def on_event(a: Any, ev: Any) -> None:
        if isinstance(ev, Paused) and ev.state == "paused":
            assert _presses(desk) == ["playpause"]  # nothing new started
            threading.Timer(0.1, lambda: a.resume(by="island")).start()

    events = _drive(agent, "play and skip to the next song", on_event)
    paused = [e for e in events if isinstance(e, Paused)]
    assert [(p.state, p.why) for p in paused] == [("paused", "mouse"), ("resumed", "mouse")]
    assert told == [("paused", "mouse"), ("resumed", "mouse")]
    # caught before its step began (review F1): never shown, never run; the brain is told it was not run
    assert not any(isinstance(e, ToolResult) and e.summary == PAUSED_SKIP for e in events)
    assert [e.name for e in events if isinstance(e, ToolCall)].count("press") == 2
    assert PAUSED_SKIP in seen["text"]
    assert _presses(desk) == ["playpause", "nexttrack"]          # once, after the resume
    assert "you were paused" in seen["text"] and "read again" in seen["text"]
    assert "The window in front now, read again after the pause" in seen["text"]   # the full re-read
    assert isinstance(events[-1], Final)
    assert [w.grace_s for w in watches.made] == [0.0, pytest.approx(1.5)]          # watched again after a grace
    assert all(w.stopped for w in watches.made)                                    # nothing watches after the run
    assert not agent.paused


def test_a_takeover_during_an_action_waits_after_it_and_the_breaker_does_not_count_it(tmp_path: Path):
    desk = _desk()
    watches = Watches()
    seen: dict[str, str] = {}

    def after(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_done(1))

    brain = Brain([_response(_press(0, "playpause"), _press(1, "nexttrack"), _done()), after])
    agent = _agent(brain, tmp_path, desk, watches=watches)

    def on_event(a: Any, ev: Any) -> None:
        if isinstance(ev, ToolResult) and ev.name == "press" and ev.ok:
            watches.fire("keys")                    # typing starts right after the first key went out
        if isinstance(ev, Paused) and ev.state == "paused":
            threading.Timer(0.05, a.resume).start()

    events = _drive(agent, "play, then next", on_event)
    assert _presses(desk) == ["playpause"]        # the rest of that response (a key and its done) never ran
    assert any(isinstance(e, Paused) and e.state == "resumed" for e in events)
    assert agent._breaker.trips == [] and isinstance(events[-1], Final)
    assert "earlier tool in this turn failed" not in seen["text"] and seen["text"].count("so you were paused") >= 2


def test_words_given_with_the_resume_reach_the_brain_as_the_users(tmp_path: Path):
    from yuki.agent.loop import STEER_LABEL

    desk = _desk()
    watches = Watches()
    seen: dict[str, str] = {}

    def after(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_done())

    brain = Brain([_response(_press(0, "playpause")), after])
    agent = _agent(brain, tmp_path, desk, watches=watches)

    def on_event(a: Any, ev: Any) -> None:
        if isinstance(ev, ToolResult) and ev.ok:
            watches.fire("click")
        if isinstance(ev, Paused) and ev.state == "paused":
            threading.Timer(0.05, lambda: a.resume("skip this one too", by="typed")).start()

    _drive(agent, "play it", on_event)
    assert f"{STEER_LABEL}: skip this one too" in seen["text"] and "you were paused" in seen["text"]


def test_stop_while_paused_ends_plainly_and_nothing_else_runs(tmp_path: Path):
    from yuki.agent.loop import PAUSE_STOP_LINE

    desk = _desk()
    watches = Watches()
    brain = Brain([_response(_press(0, "playpause")), _response(_press(1, "nexttrack"))])
    agent = _agent(brain, tmp_path, desk, watches=watches)

    def on_event(a: Any, ev: Any) -> None:
        if isinstance(ev, ToolResult) and ev.ok:
            watches.fire("mouse")
        if isinstance(ev, Paused) and ev.state == "paused":
            threading.Timer(0.05, a.cancel).start()     # Stop on the island

    events = _drive(agent, "play it", on_event)
    assert isinstance(events[-1], ErrorEvent) and events[-1].text == PAUSE_STOP_LINE
    assert _presses(desk) == ["playpause"] and len(brain.calls) == 1


def test_a_pause_nobody_resumes_ends_quietly_after_the_limit(tmp_path: Path):
    from yuki.agent.loop import PAUSE_STOP_LINE

    desk = _desk()
    watches = Watches()
    brain = Brain([_response(_press(0, "playpause")), _response(_press(1, "nexttrack"))])
    agent = _agent(brain, tmp_path, desk, watches=watches, limit=0.3)
    told: list[str] = []
    agent.on_pause = lambda state, why, s: told.append(state)
    started = time.monotonic()
    events = _drive(agent, "play it", lambda a, ev: watches.fire() if isinstance(ev, ToolResult) and ev.ok else None)
    assert 0.25 <= time.monotonic() - started < 5.0
    assert [e.state for e in events if isinstance(e, Paused)] == ["paused", "ended"]
    assert told == ["paused", "ended"]
    assert isinstance(events[-1], ErrorEvent) and events[-1].text == PAUSE_STOP_LINE
    assert _presses(desk) == ["playpause"] and len(brain.calls) == 1


def test_nothing_pauses_without_the_watch_or_before_the_first_action(tmp_path: Path):
    desk = _desk()
    agent = _agent(Brain([_response(_done())]), tmp_path, desk, watches=None)
    assert agent.pause("mouse") is False                    # nothing is acting
    events = _drive(agent, "what's playing?")
    assert isinstance(events[-1], Final) and not any(isinstance(e, Paused) for e in events)
    watches = Watches()
    agent2 = _agent(Brain([_response(_use("look_at_desktop"), _done())]), tmp_path, _desk(), watches=watches)
    _drive(agent2, "what's open?")
    assert watches.made == []                               # a request that only reads is never watched


def test_a_pause_also_ends_a_wait_to_send_input(tmp_path: Path):
    """The dispatcher's wait for the user to stop typing (vfix2) ends at once on a pause: nothing is sent."""
    agent = _agent(Brain([]), tmp_path, _desk(), watches=Watches())
    agent._setup_input_gate()
    assert agent.dispatcher.cancel_check() is False
    agent._paused.set()
    assert agent.dispatcher.cancel_check() is True


# -- 4 and 7. the runtime, and a replay of a whole run with a takeover injected -------------------------------------


def test_replay_through_the_runtime_with_a_takeover_mid_run(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    QApplication.instance() or QApplication([])
    desk = _desk()
    watches = Watches()
    seen: dict[str, str] = {}

    def second(params: dict) -> SimpleNamespace:
        watches.fire("mouse")
        return _response(_press(1, "nexttrack"))

    def third(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_press(2, "nexttrack"), _done())

    brain = Brain([_response(_press(0, "playpause")), second, third, _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, watches=watches)
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=_agent(Brain([]), tmp_path, _desk()), memory=SimpleNamespace(session_id="t"))
    states: list[tuple] = []
    finals: list[str] = []
    rt.paused_changed.connect(lambda lane, job, state, why: states.append((job, state, why)))
    rt.finished.connect(lambda lane, job, text: finals.append(text))

    def resume_when_paused() -> None:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and rt.paused_id() is None:
            time.sleep(0.01)
        assert rt.paused_id() == 7
        assert rt.steer(7, "go on") is True               # the user's words to a paused request resume it

    helper = threading.Thread(target=resume_when_paused)
    helper.start()
    rt.worker.enqueue(Job(7, "play and skip to the next song", None, "voice"))
    rt.worker._jobs.put(None)
    rt.worker.run()
    helper.join(5)
    assert states == [(7, "paused", "mouse"), (7, "resumed", "mouse")]
    assert _presses(desk) == ["playpause", "nexttrack"] and finals
    assert "go on" in seen["text"] and "read again" in seen["text"]
    assert rt.paused_id() is None and rt.resume(7) is False


def test_an_unresumed_pause_ends_as_a_stop_through_the_runtime(tmp_path: Path):
    from PySide6.QtWidgets import QApplication

    from yuki.ui.runtime import AgentRuntime, Job
    from yuki.ui.uilog import UiLog

    QApplication.instance() or QApplication([])
    watches = Watches()
    agent = _agent(Brain([_response(_press(0, "playpause")), _response(_press(1, "nexttrack"))]), tmp_path, _desk(),
                   watches=watches, limit=0.3)
    rt = AgentRuntime(Settings(log_dir=tmp_path / "s", requests_csv=tmp_path / "r.csv"),
                      UiLog(sessions_dir=tmp_path / "s", session_id=None), worker_agent=agent,
                      front_desk_agent=_agent(Brain([]), tmp_path, _desk()), memory=SimpleNamespace(session_id="t"))
    stops: list[tuple] = []
    failed: list[str] = []
    rt.cancelled.connect(lambda job, by: stops.append((job, by)))
    rt.failed.connect(lambda lane, job, text: failed.append(text))
    rt.worker.enqueue(Job(3, "play it", None, "text"))
    orig_run = agent.run

    def run(*a: Any, **k: Any) -> Any:
        for ev in orig_run(*a, **k):
            if isinstance(ev, ToolResult) and ev.ok:
                watches.fire()
            yield ev

    agent.run = run
    rt.worker._jobs.put(None)
    rt.worker.run()
    assert stops == [(3, "paused")] and failed == ["Stopped. Nothing else was done."]


# -- 5. the island ------------------------------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yuki.ui import lattice as L

    L.REDUCED_OVERRIDE[0] = False
    yield app
    L.REDUCED_OVERRIDE[0] = None


def test_the_paused_state_wins_over_working_and_shows_resume_and_stop():
    from yuki.ui.island import PAUSED_LINE, IslandInputs, RunInfo, SendInfo, resolve, spec_for

    run = RunInfo(1, time.monotonic(), steps=3, paused=True, paused_why="mouse")
    i = IslandInputs(run=run, voice="speaking")
    assert resolve(i) == "paused"                                   # over Yuki speaking: it waits for you
    spec = spec_for("paused", i, compact=True)
    assert spec.line == PAUSED_LINE and spec.body == "paused" and spec.exp and not spec.compact
    assert spec.face == "handed" and spec.halo is None and "chev" in spec.keys
    assert resolve(IslandInputs(run=run, question="Which file?")) == "needs-you"
    assert resolve(IslandInputs(run=run, send=SendInfo(1))) == "send-countdown"
    from dataclasses import replace

    assert resolve(IslandInputs(run=replace(run, stopping=True))) == "working"   # Stop pressed: "Stopping…"


def test_the_island_opens_paused_and_resume_goes_out(qapp):
    from yuki.ui.island import PAUSED_NOTE, Island

    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    resumed: list[int] = []
    stopped: list[int] = []
    isl.resume_clicked.connect(resumed.append)
    isl.stop_clicked.connect(stopped.append)
    try:
        isl.strip.start_run(5, steps=2, line="Pressing play")
        isl.strip.run_step(5, "Pressing play", 2)
        assert isl.state == "working"
        isl.set_paused(5, True, "keys")
        assert isl.state == "paused" and isl.current_spec().body == "paused"
        assert not isl.window.k_resume.isHidden() and not isl.window.k_pstop.isHidden()
        assert isl.window.ln1.text() == "You took over · paused"
        isl.window.k_resume.clicked.emit()
        assert resumed == [5]
        isl.toggle_card()                                            # the chevron: the run card says it is paused
        assert isl.expanded and isl.current_spec().body == "run"
        assert isl.window.run.touch.text() == PAUSED_NOTE and isl.window.run.paused
        assert not isl.window.run.k_resume.isHidden()
        isl.window.run.k_resume.clicked.emit()
        assert resumed == [5, 5]
        isl.set_paused(5, False)
        assert isl.state == "working" and not isl.window.run.paused and isl.window.k_resume.isHidden()
        isl.set_paused(5, True)
        isl.window.k_pstop.clicked.emit()
        assert stopped == [5] and isl.state == "working"              # "Stopping…" until it ends
    finally:
        isl.stop()


def test_the_tray_face_for_paused_is_waiting_for_you():
    from yuki.ui.tray import tray_state

    assert tray_state("paused") == "needs-you"


# -- 6. the voice -------------------------------------------------------------------------------------------------------


def _bridge_job(state: str = "running") -> tuple[Any, Any]:
    from yuki.voice.bridge import Job, VoiceBridge

    bridge = VoiceBridge(SimpleNamespace(), hands_backend=None)
    job = Job(job_id=9, request="skip to the next song", source="voice", lane="worker", state=state)
    with bridge._lock:
        bridge._jobs[9] = job
    return bridge, job


def test_the_talkers_line_says_it_is_paused_and_its_go_on_is_for_this_task(qapp):
    bridge, job = _bridge_job()
    bridge._on_paused_changed("worker", 9, "paused", "mouse")
    line = bridge.work_line() or ""
    assert job.paused and "paused" in line and "took over" in line and "tell_yuki" in line
    assert job.status(time.monotonic())["paused"]
    bridge._on_paused_changed("worker", 9, "resumed", "mouse")
    assert not job.paused and "paused" not in (bridge.work_line() or "")


def test_no_still_on_it_line_while_paused_and_an_unresumed_end_is_quiet(qapp):
    bridge, job = _bridge_job()
    job.events = SimpleNamespace()
    bridge._loop = SimpleNamespace(is_closed=lambda: False)
    posted: list[str] = []
    armed: list[float] = []
    bridge._post = lambda j, kind, payload: posted.append(kind)
    bridge._arm_wait = lambda j, s: armed.append(s)
    bridge._on_paused_changed("worker", 9, "paused", "keys")
    assert posted == ["moved", "step_hold"]                       # lines waiting to be said are dropped
    posted.clear()
    bridge._wait_check(job)
    assert posted == [] and armed                                  # nothing said, checked again later
    bridge._on_paused_changed("worker", 9, "ended", "keys")
    assert job.superseded and job.paused_out and not job.paused   # its end is never spoken


# -- the tray app's wiring (a bare YukiUi: no lanes, no voice, no window) ----------------------------------------------


def _bare_ui(qapp) -> Any:
    from PySide6.QtCore import QObject

    from yuki.ui.app import YukiUi
    from yuki.ui.island import Island

    ui = YukiUi.__new__(YukiUi)
    QObject.__init__(ui)
    ui.events, ui.emitted, ui.resumes = [], [], []
    ui.ui_log = SimpleNamespace(event=lambda name, **f: ui.events.append((name, f)))
    ui.island = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    ui.island.screen_state = lambda: (5, "accepts")
    ui.window_host = SimpleNamespace(emit=ui.emitted.append, user_answer=lambda job, text: ui.emitted.append(
        {"type": "answer", "job": job, "text": text}))
    ui.runtime = SimpleNamespace(resume=lambda rid, words=None, by="": ui.resumes.append((rid, words, by)) or True,
                                 paused_id=lambda: 5)
    ui._paused_runs = set()
    ui.island.resume_clicked.connect(ui._on_island_resume)
    return ui


def test_the_app_shows_the_pause_on_the_island_and_the_thread_and_resume_goes_out(qapp):
    from yuki.ui.island import PAUSED_NOTE, RESUMED_NOTE

    ui = _bare_ui(qapp)
    try:
        ui.island.strip.start_run(5, line="Pressing play", request="play and skip")
        ui.island.strip.run_step(5, "Pressing play", 1)
        ui._on_paused_changed("worker", 5, "paused", "mouse")
        assert ui.island.state == "paused" and ui._paused_runs == {5}
        assert ui.emitted[-1] == {"type": "said", "job": 5, "text": PAUSED_NOTE}
        ui.island.window.k_resume.clicked.emit()
        assert ui.resumes == [(5, None, "island")]
        ui._on_paused_changed("worker", 5, "resumed", "mouse")
        assert ui.island.state == "working" and ui._paused_runs == set()
        assert ui.emitted[-1]["text"] == RESUMED_NOTE
        ui._on_paused_changed("worker", 5, "paused", "keys")
        ui._on_paused_changed("worker", 5, "ended", "keys")
        assert ui.island.state == "working" and ui._paused_runs == {5}   # kept: its end line says nothing else ran
    finally:
        ui.island.stop()


def test_typing_to_yuki_while_paused_resumes_with_the_words(qapp):
    from yuki.ui.app import YukiUi

    ui = _bare_ui(qapp)
    cards: list[str] = []
    ui._question, ui._pending_ask, ui._nudge_reply = None, None, None
    ui.overlay = SimpleNamespace(add_card=lambda text, *a, **k: cards.append(text), expand_input=lambda: None)
    try:
        YukiUi._on_submitted(ui, "use the other playlist")
        assert ui.resumes == [(5, "use the other playlist", "typed")]
        assert ui.emitted[-1] == {"type": "answer", "job": 5, "text": "use the other playlist"} and cards
    finally:
        ui.island.stop()


def test_a_paused_run_that_is_stopped_ends_saying_nothing_else_was_done(qapp):
    from yuki.ui.app import YukiUi

    finals: list[tuple] = []
    ui = SimpleNamespace(_acting=set(), _question=None, _island_answered=lambda rid: None, _runs={},
                         _stopped={5: "paused"}, _handed=set(), _send_stopped={}, _esc=None, _paused_runs={5},
                         _ask_outcome=lambda *a, **k: None, _end_run=lambda rid: True, cards={},
                         overlay=SimpleNamespace(isVisible=lambda: False, closing=False),
                         strip=SimpleNamespace(run_id=None, show_final=lambda text, **k: finals.append((text, k))),
                         nudges=SimpleNamespace(make_way=lambda: None, try_show=lambda: None), _settled={},
                         ui_log=SimpleNamespace(event=lambda *a, **k: None))
    YukiUi._settle(ui, "worker", 5, "Cancelled by the user.", tone="error")
    assert finals[-1][0] == "Stopped. Nothing else was done." and finals[-1][1]["outcome"] == "stopped"
    assert ui._paused_runs == set()


def test_resume_pressed_before_the_request_reached_its_pause_still_reads_the_screen_again(tmp_path: Path):
    """Resume came while the model call was still out (the island showed the pause at once): the caught action is not
    run, the screen is read again, and the island is told "resumed" (never left saying paused)."""
    from yuki.agent.loop import PAUSED_SKIP

    desk = _desk()
    watches = Watches()
    told: list[str] = []
    seen: dict[str, str] = {}

    def second_call(params: dict) -> SimpleNamespace:
        watches.fire("mouse")
        assert agent.paused and agent.resume(by="island") is True
        assert not agent.paused and agent.resume() is False      # asked once; it is going on
        return _response(_press(1, "nexttrack"))

    def third(params: dict) -> SimpleNamespace:
        seen["text"] = _texts(params)
        return _response(_press(2, "nexttrack"), _done())

    brain = Brain([_response(_press(0, "playpause")), second_call, third, _response(_done(1))])
    agent = _agent(brain, tmp_path, desk, watches=watches)
    agent.on_pause = lambda state, why, s: told.append(state)
    events = _drive(agent, "play and skip")
    assert told == ["paused", "resumed"] and isinstance(events[-1], Final)
    assert PAUSED_SKIP in seen["text"]
    assert _presses(desk) == ["playpause", "nexttrack"] and "read again" in seen["text"]
