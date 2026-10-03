"""appPT (3 Oct) on the island, offscreen (QT_QPA_PLATFORM=offscreen: nothing on screen, no focus, no hotkey held):
the send countdown opens on its own with who / where / the exact text, Stop and a chip tap go out as signals, the end
closes it; "Working · step 2" with no total anywhere; the brain's own live line in the thinking / working line and on
the run card's current step. And the tray app's wiring: Stop / Esc cancel, a swap goes to the runtime, the end line
says plainly what did not happen. All names and messages are invented."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace
from typing import Any

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    from yuki.ui import lattice as L

    L.REDUCED_OVERRIDE[0] = False
    yield app
    L.REDUCED_OVERRIDE[0] = None


def _island():
    from yuki.ui.island import Island

    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    return isl


def _pump(qapp, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        qapp.processEvents()
        time.sleep(0.005)


SEND = {"state": "counting", "who": "Sam", "where": "Discord DM", "app": "Discord", "text": "omw, 10 min",
        "seconds": 3.0, "who_alts": ["Maya"], "where_alts": ["WhatsApp"], "typed": False, "from_memory": True}


# -- 2. no "of N" anywhere ----------------------------------------------------------------------------------------


def test_the_pill_says_working_step_n_and_nothing_implies_a_total() -> None:
    from yuki.ui.island import IslandInputs, RunInfo, compact_word, spec_for, step_of

    run = RunInfo(5, time.monotonic() - 12, steps=2, line="Opening Discord")
    i = IslandInputs(run=run)
    assert compact_word("working", i) == "Working · step 2"
    assert step_of(run) == (2, 0) and step_of(None) == (1, 0)
    for kw in ({}, {"expanded": True}, {"compact": True}):
        spec = spec_for("working", i, **kw)
        for text in (spec.line, spec.sub, spec.word):
            assert "/" not in text and " of " not in text, text
    assert not hasattr(run, "of")


def test_the_run_card_counts_the_steps_done_so_far_and_never_0_of_5(qapp) -> None:
    from yuki.ui.island import RunInfo

    isl = _island()
    try:
        run = RunInfo(5, time.monotonic(), steps=0, line="", request="tell sam omw")
        isl.update(run=run)
        isl.expanded = True
        isl._fill_card()
        card = isl.window.run
        assert card.count.text() == "" and card.cost.text() == ""      # nothing done yet: no "0/5", no "0"
        steps = [{"kind": "done", "verb": "Opened", "obj": "Discord", "ms": "1.2 s", "note": "", "chips": []},
                 {"kind": "running", "verb": "Checking", "obj": "first", "ms": "now", "note": "", "chips": []}]
        isl._steps = lambda r: [dict(s) for s in steps]
        isl.update(run=RunInfo(5, run.started, steps=2, line="Checking first", request="tell sam omw",
                               thought="Checked Discord first: your last message to Sam was at 6:08 PM"))
        isl._fill_card()
        assert card.count.text() == "1" and card.cost.text() == "done so far"
        visible = [r for r in card.rows if r.isVisibleTo(card)]
        assert len(visible) == 2                                       # the steps so far and the current one
        assert visible[1].note.text().startswith("Checked Discord first")   # the live line under the current step
        assert visible[0].note.text() == ""
    finally:
        isl.stop()


def test_the_face_and_halo_draw_the_steps_so_far_without_pending_segments() -> None:
    from yuki.ui.dots import FACE_W, FaceModel, HaloModel

    for n, cols in ((1, {8}), (3, {6, 8, 10}), (9, {4, 6, 8, 10, 12})):
        face = FaceModel()
        face.set("working", 0.0, (n, 0))
        face.target(0.5)
        row6 = {k % FACE_W for k, v in enumerate(face.g) if k // FACE_W == 6 and v > 0}
        assert row6 == cols, (n, row6)
    for n, half in ((1, 3), (2, 7), (7, 17)):
        halo = HaloModel()
        halo.set_state("working", 0.0, (n, 0))
        lit = [halo._u[k] for k in range(len(halo._u)) if halo.state_val("working", k, 0.3, False)[0] > 0]
        # the steps so far sit centred and grow from the middle; no dim segment stands for a step to come
        # (a pending segment would be lit dim at 0.16 and widen the range)
        assert lit and min(lit) >= -half - 1 and max(lit) <= half, (n, min(lit), max(lit))


# -- 3. the brain's own live line ----------------------------------------------------------------------------------


def test_the_working_line_is_the_brains_own_note_and_the_step_moves_to_the_sub_line() -> None:
    from yuki.ui.island import IslandInputs, RunInfo, spec_for

    run = RunInfo(5, time.monotonic() - 5, steps=3, line="Typing your message",
                  thought="Nothing like it in the chat, so sending it now")
    spec = spec_for("working", IslandInputs(run=run))
    assert spec.line == "Nothing like it in the chat, so sending it now"
    assert spec.sub.startswith("step 3 · Typing your message · 0:0")
    bare = spec_for("working", IslandInputs(run=RunInfo(5, time.monotonic(), steps=3, line="Typing your message")))
    assert bare.line == "Typing your message" and bare.sub.startswith("step 3 · 0:0")
    think = spec_for("thinking", IslandInputs(run=RunInfo(5, time.monotonic(), thought="Checking who Sam is first")))
    assert think.line == "Checking who Sam is first"


def test_a_thought_lasts_until_the_next_step_and_code_never_shows(qapp) -> None:
    from yuki.ui.island import RunInfo

    isl = _island()
    try:
        isl.strip.start_run(5, line="Opening Discord", request="tell sam omw")
        isl.strip.run_step(5, "Opening Discord", 1)
        isl.set_thought(5, "checking the chat first so it isn't sent twice.")
        assert isl.inputs.run.thought == "Checking the chat first so it isn't sent twice"
        isl.set_thought(5, "Stop-Process -Name slack")               # code-looking: never shown
        assert isl.inputs.run.thought == "Checking the chat first so it isn't sent twice"
        isl.set_thought(6, "another run's line")                      # another request: ignored
        assert isl.inputs.run.thought.startswith("Checking")
        isl.strip.run_step(5, "Typing your message", 2)               # the next step starts clean
        assert isl.inputs.run.thought == ""
        isl.set_thought(5, "Sending it now")
        isl.strip.start_run(5, steps=2, line="Typing your message")  # the strip taken over again keeps it
        assert isl.inputs.run.thought == "Sending it now" and isinstance(isl.inputs.run, RunInfo)
    finally:
        isl.stop()


# -- 1. the send countdown on the island -----------------------------------------------------------------------------


def test_the_send_countdown_opens_on_its_own_with_who_where_and_the_text(qapp) -> None:
    from yuki.ui.island import SEND_W

    isl = _island()
    try:
        isl.strip.start_run(5, line="Typing your message", request="tell sam I'm on my way, 10 minutes")
        isl.show_send(5, dict(SEND))
        _pump(qapp, 0.05)
        spec = isl.window.spec
        assert isl.state == "send-countdown" and not isl.hovered
        assert not spec.compact and spec.body == "send" and spec.width == SEND_W and spec.halo is None
        assert spec.line == "Sending to Sam on Discord…"
        assert spec.sub == "resolved from memory · tap a chip to swap"
        card = isl.window.send
        assert card.isVisibleTo(isl.window.panel)
        assert card.who.value == "Sam" and card.who.can_swap and card.where.value == "Discord DM"
        assert card.msg.text() == "“omw, 10 min”" and card.tx.text() == "Sending to Sam on Discord"
        assert card.said.text() == "you said “tell sam I'm on my way, 10 minutes”"
        assert card.drain_t.text() == "3 s to stop" and card.k_stop.kbd == "Esc"
        assert isl.window.panel_height(spec, SEND_W) > 74 + 120
    finally:
        isl.stop()


def test_stop_and_a_chip_tap_go_out_as_signals_and_the_end_closes_it(qapp) -> None:
    isl = _island()
    stops: list[int] = []
    swaps: list[tuple] = []
    isl.send_stop.connect(stops.append)
    isl.send_swap.connect(lambda rid, kind, value: swaps.append((rid, kind, value)))
    try:
        isl.strip.start_run(5, line="Typing your message", request="tell sam omw")
        isl.show_send(5, dict(SEND))
        card = isl.window.send
        card.k_stop.click()
        assert stops == [5]
        card.who.swap.emit()
        card.where.swap.emit()
        assert swaps == [(5, "who", "Maya"), (5, "where", "WhatsApp")]
        # the agent answers the swap: the card stays open, switching, no drain, no more swaps
        isl.show_send(5, {**SEND, "state": "swapped", "who": "Maya", "who_alts": ["Sam"]})
        assert isl.state == "send-countdown" and isl.inputs.send.phase == "switching"
        assert isl.window.spec.line == "Switching to Maya on Discord…"
        assert not card.drain.isVisibleTo(card) and not card.who.can_swap
        card.who.swap.emit()
        assert len(swaps) == 2                                         # nothing to swap while it switches
        # the new countdown, then the time runs out: the card closes and the run goes on
        isl.show_send(5, {**SEND, "who": "Maya"})
        assert isl.inputs.send.phase == "counting" and card.drain.isVisibleTo(card)
        isl.show_send(5, {**SEND, "state": "going"})
        assert isl.inputs.send is None and isl.state != "send-countdown"
    finally:
        isl.stop()


def test_the_countdown_never_outlives_its_run_or_a_question(qapp) -> None:
    isl = _island()
    try:
        isl.strip.start_run(5, line="Typing your message", request="tell sam omw")
        isl.show_send(5, {**SEND, "state": "swapped"})
        isl.strip.ask(5, "Which Maya, the one from work?", voice=False)
        assert isl.inputs.send is None and isl.state == "needs-you"
        isl.strip.answered(5)
        isl.show_send(5, dict(SEND))
        isl.strip.end_run(5)
        assert isl.inputs.send is None
        isl.strip.start_run(6, line="x", request="y")
        isl.show_send(6, dict(SEND))
        isl.strip.show_final("Stopped. Nothing was sent.", outcome="stopped")
        assert isl.inputs.send is None
        isl.show_send(9, {**SEND, "state": "stopped"})                # another request's end changes nothing
        assert isl.inputs.send is None
    finally:
        isl.stop()


def test_the_tray_counts_a_send_countdown_as_working() -> None:
    from yuki.ui.tray import tray_state

    assert tray_state("send-countdown") == "working"


# -- the tray app's wiring (a bare YukiUi: no lanes, no voice, no hotkeys held) --------------------------------------


class FakeEsc:
    def __init__(self) -> None:
        from PySide6.QtCore import QObject, Signal

        class _S(QObject):
            pressed = Signal(str)
            failed = Signal(str, str)

        self._s = _S()
        self.pressed, self.failed = self._s.pressed, self._s.failed
        self.started = self.stopped = 0
        self.threads: list[Any] = []

    def start(self) -> None:
        self.started += 1

    def stop(self) -> None:
        self.stopped += 1


def _bare_ui(qapp) -> Any:
    from PySide6.QtCore import QObject

    from yuki.ui.app import YukiUi

    ui = YukiUi.__new__(YukiUi)
    QObject.__init__(ui)
    ui.events = []
    ui.ui_log = SimpleNamespace(event=lambda name, **f: ui.events.append((name, f)))
    ui.island = _island()
    ui.nudges = SimpleNamespace(make_way=lambda: None, try_show=lambda: None)
    ui.cancels, ui.choices = [], []
    ui.runtime = SimpleNamespace(cancel=lambda rid, reason="cancelled", by="": ui.cancels.append((rid, by)) or "running",
                                 send_choice=lambda rid, kind, value: ui.choices.append((rid, kind, value)) or True)
    ui.voice = SimpleNamespace(owns=lambda rid: False, interrupt=lambda: None)
    ui._stopped, ui._send_stopped, ui._esc = {}, {}, None
    ui.escs = []
    ui.esc_listener = lambda: ui.escs.append(FakeEsc()) or ui.escs[-1]
    ui.island.send_stop.connect(ui._on_send_stop)
    ui.island.send_swap.connect(ui._on_send_swap)
    return ui


def test_esc_is_held_only_while_a_send_counts_down_and_stops_it(qapp) -> None:
    ui = _bare_ui(qapp)
    try:
        ui.island.strip.start_run(5, line="Typing your message", request="tell sam omw")
        ui._on_send_pending("worker", 5, dict(SEND))
        assert len(ui.escs) == 1 and ui.escs[0].started == 1 and ui._esc is ui.escs[0]
        ui.escs[0].pressed.emit("send_stop")                          # Esc
        assert ui.cancels == [(5, "send")] and ui._stopped == {5: "send"}
        assert ui._esc is None and ui.escs[0].stopped == 1             # Esc let go at once
        ui._on_send_pending("worker", 5, {**SEND, "state": "stopped"})
        assert ui._send_stopped == {5: False} and ui.island.inputs.send is None
    finally:
        ui.island.stop()


def test_a_chip_tap_reaches_the_runtime_and_the_going_send_lets_esc_go(qapp) -> None:
    ui = _bare_ui(qapp)
    try:
        ui.island.strip.start_run(5, line="Typing your message", request="tell sam omw")
        ui._on_send_pending("worker", 5, dict(SEND))
        ui.island.window.send.who.swap.emit()
        assert ui.choices == [(5, "who", "Maya")]
        ui._on_send_pending("worker", 5, {**SEND, "state": "swapped", "who": "Maya"})
        assert ui._esc is None and ui.escs[0].stopped == 1             # no Esc while it switches
        ui._on_send_pending("worker", 5, {**SEND, "who": "Maya"})
        assert len(ui.escs) == 2 and ui._esc is ui.escs[1]
        ui._on_send_pending("worker", 5, {**SEND, "state": "going"})
        assert ui._esc is None and ui.escs[1].stopped == 1 and ui.cancels == []
    finally:
        ui.island.stop()


def test_the_partial_now_reaches_the_islands_line(qapp) -> None:
    ui = _bare_ui(qapp)
    try:
        ui.island.strip.start_run(5, line="Opening Discord", request="tell sam omw")
        ui.island.strip.run_step(5, "Opening Discord", 1)
        ui._on_partial("worker", 5, "thinking", "The user wants me to...")   # the raw thinking never shows here
        assert ui.island.inputs.run.thought == ""
        ui._on_partial("worker", 5, "now", "Sam's DM is the one you use most")
        assert ui.island.inputs.run.thought == "Sam's DM is the one you use most"
    finally:
        ui.island.stop()


def test_a_send_stopped_in_its_countdown_ends_saying_nothing_was_sent(qapp) -> None:
    from yuki.ui.app import YukiUi

    finals: list[tuple] = []
    for typed, line in ((False, "Stopped. Nothing was sent."), (True, "Stopped. Your message is in the box, not sent.")):
        ui = SimpleNamespace(_acting=set(), _question=None, _island_answered=lambda rid: None, _runs={},
                             _stopped={5: "send"}, _handed=set(), _send_stopped={5: typed}, _esc=None,
                             _ask_outcome=lambda *a, **k: None, _end_run=lambda rid: True, cards={},
                             overlay=SimpleNamespace(isVisible=lambda: False, closing=False),
                             strip=SimpleNamespace(run_id=None, show_final=lambda text, **k: finals.append((text, k))),
                             nudges=SimpleNamespace(make_way=lambda: None, try_show=lambda: None), _settled={},
                             ui_log=SimpleNamespace(event=lambda *a, **k: None))
        YukiUi._settle(ui, "worker", 5, "Cancelled by the user.", tone="error")
        assert finals[-1][0] == line and finals[-1][1]["outcome"] == "stopped"
        assert ui._send_stopped == {}
