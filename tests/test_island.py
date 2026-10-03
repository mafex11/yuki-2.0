"""The Lattice native surfaces (phase E6-E10), offscreen (QT_QPA_PLATFORM=offscreen; nothing is shown on screen, no
focus is taken, no native window call is made): which island state wins, the call timer's one clock, the run card's
steer states, the cards' keys reaching memory's offer ops, the ask bar's resolved chips and unsent words, the tray's
states, the monitor choice, and that a hidden or settled island runs no timer. All data is invented."""

from __future__ import annotations

import os
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

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


def _call(minutes: float, status: str = "recording") -> dict:
    start = datetime.now().astimezone() - timedelta(minutes=minutes)
    return {"status": status, "app": "Discord", "session_started_at": start.isoformat(timespec="seconds"),
            "detected_at": (datetime.now().astimezone() - timedelta(minutes=2)).isoformat(timespec="seconds")}


def _island(qapp):
    from yuki.ui.island import Island

    isl = Island(bindings={"voice": "ctrl+alt+y", "cancel": "alt+shift+x"})
    isl.screen_state = lambda: (5, "accepts")
    return isl


# -- the state machine --------------------------------------------------------------------------------------


def test_the_most_urgent_state_wins():
    from yuki.ui.island import FinalInfo, IslandInputs, RunInfo, resolve

    run = RunInfo(1, time.monotonic(), steps=2)
    call = _call(10)
    everything = IslandInputs(voice="speaking", voice_error="mic gone", listening="off", run=run,
                              question="Which file?", call=call, fullscreen=True, final=FinalInfo("Done."))
    assert resolve(everything) == "game-quiet"                     # a full screen hides it, whatever goes on
    i = IslandInputs(**{**everything.__dict__, "fullscreen": False})
    assert resolve(i) == "needs-you"                               # Yuki waiting for you beats the rest
    i.question = ""
    assert resolve(i) == "voice-stopped"                           # then a voice that failed (reason and fix)
    i.voice_error = ""
    assert resolve(i) == "speaking"
    i.voice = "hearing"
    assert resolve(i) == "hearing"
    i.voice = "starting"
    assert resolve(i) == "listening"                               # the wake: listening, eyes opening
    i.voice = "off"
    assert resolve(i) == "done-flash"                              # the end word shows over a run still going
    i.final = None
    assert resolve(i) == "working"
    i.run = RunInfo(2, time.monotonic(), steps=0)
    assert resolve(i) == "thinking"                                # a run before its first step
    i.run = None
    assert resolve(i) == "quiet-in-call"
    i.call = _call(3, "ended")
    assert resolve(i) == "not-listening"                           # v3.16: Yuki isn't listening, at a glance
    i.listening = "on"
    assert resolve(i) == "hidden-armed"


def test_each_state_draws_the_designs_parts():
    from yuki.ui.island import FinalInfo, IslandInputs, RunInfo, spec_for

    call = _call(46)
    run = RunInfo(5, time.monotonic() - 46, steps=2, line="Renaming the exports")
    w = spec_for("working", IslandInputs(run=run, call=call), now=time.monotonic())
    assert w.width == 680 and w.halo == "working" and w.keys == ("stop", "chev") and w.rec
    assert w.sub.startswith("step 2 · 0:4") and w.step == (2, 5)   # no plan: "step n", five segments
    card = spec_for("working", IslandInputs(run=run, call=call), expanded=True)
    assert card.body == "run" and card.width == 600 and card.halo is None and card.exp
    done = spec_for("done-flash", IslandInputs(call=call, final=FinalInfo("Renamed 12 exports.")))
    assert done.chip == "done" and not done.rec and done.halo is None   # the design: its chip only, no halo (item 20)
    stopped = spec_for("done-flash", IslandInputs(final=FinalInfo("Stopped.", "stopped")))
    assert stopped.face == "handed" and stopped.halo is None and stopped.chip == "stopped"
    lis = spec_for("listening", IslandInputs(voice="listening", call=call), voice_key="Ctrl+Alt+Y")
    assert lis.body == "caption" and lis.exp and lis.sub.startswith("Ctrl+Alt+Y to stop") and lis.rec
    assert lis.halo is None                                        # not in the wake before your first word
    vs = spec_for("voice-stopped", IslandInputs(voice_error="The microphone was unplugged."))
    assert vs.keys == ("retry", "settings") and vs.sub == "The microphone was unplugged." and not vs.rec
    q = spec_for("quiet-in-call", IslandInputs(call=call, listening="call_off"))
    assert q.hidden and q.why_hidden == "in a call"                # nothing stays on screen in a call (item 20)
    rest = spec_for("hidden-armed", IslandInputs())
    assert rest.mini and rest.resting and spec_for("hidden-armed", IslandInputs(), resting=False).hidden
    assert spec_for("game-quiet", IslandInputs()).hidden and spec_for("not-listening", IslandInputs()).hidden


def test_unsure_words_are_dimmed_in_the_caption():
    from yuki.ui.island import caption_spans

    text, spans = caption_spans([("rename", 0.98), ("the", 0.95), ("exports", 0.41), ("now", None)], 0.6)
    assert text == "rename the exports now"
    assert [(s.start, s.length, s.dotted) for s in spans] == [(11, 7, True)]   # only the unsure word


# -- the call timer: one clock ------------------------------------------------------------------------------


def test_the_call_timer_counts_from_the_session_and_never_resets_through_a_game(qapp):
    isl = _island(qapp)
    try:
        isl.update(call=_call(95))
        first = isl.call_time()
        assert first.startswith("1:35")                            # the session's 95 minutes, not the row's 2
        isl.screen_state = lambda: (3, "d3d_full_screen")         # a full-screen game: the island goes away
        isl.on_foreground(0)
        assert isl.state == "game-quiet" and (not isl.window.isVisible() or isl.window._leaving)
        during = isl.call_time()
        isl.screen_state = lambda: (5, "accepts")                 # back on the desktop
        isl.on_foreground(0)
        after = isl.call_time()
        assert isl.state in ("call-notice", "quiet-in-call")         # (the start notice may still be up)
        assert first <= during <= after and after.startswith("1:35")   # the same clock, only counting up
    finally:
        isl.stop()


# -- the run card's steer states -----------------------------------------------------------------------------


def test_a_steer_is_added_then_read_at_the_next_step_or_never(qapp):
    isl = _island(qapp)
    try:
        strip = isl.strip
        strip.start_run(9, steps=1, line="Opening the folder")
        strip.set_request(9, "Rename the exports")
        isl.toggle_card()
        assert isl.expanded and isl.current_spec().body == "run"
        isl.window.run.steerbox.input.submitted.emit("only this week's")
        run = isl.inputs.run
        assert [s.state for s in run.steers] == ["added, not yet read"]
        strip.run_step(9, "Renaming the files", 2)                 # the next step came after it: the model read it
        assert isl.inputs.run.steers[0].state == "read at step 2"
        isl._on_steer("skip the drafts")
        isl.steer_unread(9, ["skip the drafts"])                   # arrived after the last step
        assert isl.inputs.run.steers[-1].state == "arrived after the last step"
        isl._on_steer("also the photos")
        isl.steer_result(9, "also the photos", False)              # the runtime: not running any more
        assert isl.inputs.run.steers[-1].state == "it had already finished"
        stops = []
        isl.stop_clicked.connect(stops.append)
        isl.window.run.k_stop.click()
        assert stops == [9] and isl.inputs.run.line == "Stopping…"
        handed = []
        isl.handback_clicked.connect(handed.append)
        isl.window.run.k_hand.click()
        assert handed == [9]
    finally:
        isl.stop()


def test_the_strip_interface_drives_the_island_and_frees_the_corner_when_done(qapp):
    isl = _island(qapp)
    try:
        strip = isl.strip
        freed = []
        strip.faded_out.connect(lambda: freed.append(True))
        strip.start_run(3, hint="Alt+Shift+X stops it")
        assert strip.isVisible() and strip.run_id == 3 and isl.state == "thinking"
        strip.run_step(3, "Opening the planner", 1)
        assert isl.state == "working"
        strip.show_final("Opened it.", tone="final", outcome="done")
        assert strip.run_id is None and isl.state == "done-flash" and strip.isVisible()
        strip.set_meta("3 s, 1 step, 1¢")
        assert isl.current_spec().word == "Opened it."                # the pill: the result in a few words
        isl.hovered = True                                            # the open island: its sub line
        assert isl.current_spec().sub == "3 s, 1 step, 1¢"
        isl.hovered = False
        strip.dismiss()
        assert not strip.isVisible() and freed and isl.state == "hidden-armed"
        strip.ask(4, "Which planner?", voice=True)
        assert isl.state == "needs-you" and isl.current_spec().body == "choices"
        strip.answered(4)
        assert isl.state == "hidden-armed"
    finally:
        isl.stop()


def test_the_frame_and_playbooks_become_the_run_cards_chips(qapp):
    isl = _island(qapp)
    try:
        isl.strip.start_run(6, steps=0)
        isl.set_frame(6, {"persons": [{"name": "Riya", "routes": [{"label": "WhatsApp chat", "app": "WhatsApp",
                                                                    "mine": 64}]}]})
        assert isl.inputs.run.chips[1][0] == ("memory", "from memory: you message Riya on WhatsApp chat, 64 times")
        isl.strip.run_step(6, "Typing it", 1)
        isl.step_meta(6, "type_text", {"text": "hi", "playbook": "pb:3"})
        assert isl.lesson_refs() == ["pb:3"]
        isl.set_lessons({"pb:3": "type it key by key"})
        assert isl.lesson_refs() == []
    finally:
        isl.stop()


def test_frame_view_reads_the_agents_frame_as_plain_data():
    from yuki.ui.runtime import frame_view

    cand = SimpleNamespace(label="Riya", where="the chat in front", age_s=240.0)
    frame = SimpleNamespace(persons=[{"name": "Riya", "routes": [{"label": "WhatsApp chat", "app": "WhatsApp",
                                                                  "mine": 64, "ref": "route:2"}]}],
                            people=[cand], playbooks=[3, "x"])
    view = frame_view(SimpleNamespace(_frame=frame))
    assert view["persons"][0]["routes"][0]["mine"] == 64 and view["playbooks"] == [3]
    assert view["where"] == "the chat in front 4 min ago"
    assert frame_view(SimpleNamespace()) is None


# -- the cards' keys reach memory's offer ops ----------------------------------------------------------------


class Memory:
    installed = True

    def __init__(self, answers=None):
        self.acks, self.calls, self.answers = [], [], answers or {}

    def ack_nudge(self, nudge_id, reaction, *, reply=None, timeout_s=None):
        self.acks.append((nudge_id, reaction, reply))

    def pending_nudges(self, **kw):
        return []

    def call_method(self, name, *args, timeout_s=None, **kwargs):
        self.calls.append((name, *args))
        a = self.answers.get(name)
        return a(*args) if callable(a) else a


def _settle(qapp, ctl, rounds=40):
    for _ in range(rounds):
        ctl.wait_acks(1.0)
        qapp.processEvents()
        time.sleep(0.005)


def _soon():
    return (datetime.now().astimezone() + timedelta(minutes=30)).isoformat(timespec="seconds")


def test_the_lattice_cards_keys_run_offers_by_code_and_answer_questions(qapp):
    from yuki.ui.nudges import NudgeCard, NudgeController

    memory = Memory({"offer_act": lambda nid: {"ok": True, "do": "todo_add", "state": "done", "undo": True,
                                               "message": "Added to your list."},
                     "offer_undo": lambda nid: {"ok": True, "state": "undone"},
                     "answer_question": lambda ref, words: {"saved": True}})
    ctl = NudgeController(memory, SimpleNamespace(event=lambda *a, **k: None), card=NudgeCard(linger_ms=5000),
                          screen_state=lambda: (5, "accepts"))
    offer = {"do": "todo_add", "label": "Add to list", "steps": ["add “Sort the exports” to your list"],
             "wont": "Changes only your list.", "undo": True, "expires_at": _soon()}
    card = ctl.card
    card.show_nudge(ctl.record_delivery({"id": 1, "kind": "notice", "trigger": "thread", "at": None,
                                         "text": "The exports are next.", "offer": offer}, "card"))
    assert card.head.text().startswith("Offer · from what I saw") and card.foot.note.text() == "cards never start a run"
    assert card.detail.mode == "grid" and card.detail.will.text() == "add “Sort the exports” to your list"
    card.do_button.click()
    _settle(qapp, ctl)
    assert ("offer_act", 1) in memory.calls and card.result.text() == "Added to your list."
    assert not card.undo_button.isHidden()
    card.undo_button.click()
    _settle(qapp, ctl)
    assert ("offer_undo", 1) in memory.calls and card.result.text() == "Undone."
    card.close_card()
    _settle(qapp, ctl)
    # a break: Took one is acknowledged "accepted" (no offer, nothing runs)
    card.show_nudge(ctl.record_delivery({"id": 2, "kind": "nudge", "at": None, "text": "Stand up?"}, "card"))
    assert card.head.text().startswith("Break") and card.do_button.text() == "Took one"
    card.do_button.click()
    _settle(qapp, ctl)
    assert (2, "accepted", None) in memory.acks and not any(c[0] == "offer_act" and c[1] == 2 for c in memory.calls)
    card.close_card()
    _settle(qapp, ctl)
    # a question answered in the card itself: memory records the user's words
    card.show_nudge(ctl.record_delivery({"id": 3, "kind": "question", "ref": "question:4", "at": None,
                                         "text": "Side project or work? You named it in both."}, "card"))
    assert card.head.text().startswith("Question") and not card.answer_input.isHidden()
    card.answer_input.setText("a side project")
    card.reply_button.click()
    _settle(qapp, ctl)
    assert ("answer_question", "question:4", "a side project") in memory.calls
    assert (3, "replied", "a side project") in memory.acks and card.result.text().startswith("Saved your answer")
    card.close()


def test_a_check_in_is_titled_by_its_moment_and_shows_its_offer(qapp):
    from yuki.ui.cards import card_title
    from yuki.ui.nudges import NudgeCard

    todo = {"do": "todo_add", "label": "Add to list", "steps": ["add “Ship the export cleanup” to your list"],
            "wont": "Changes only your list.", "undo": True, "expires_at": _soon()}
    base = {"id": 11, "kind": "question", "trigger": "checkin", "ref": "question:9", "at": None,
            "text": "Did the export cleanup go out? You said you'd ship it today."}
    # the moment is what Yuki itself wrote on the card (its row, or the card's inputs) - never read from the words
    assert card_title({**base, "moment": "day_start"}) == "Today"
    assert card_title({**base, "inputs": {"moment": "followup"}}) == "Following up"
    assert card_title({**base, "moment": "evening"}) == "Tonight"
    assert card_title(base) == "Checking in"                       # a check-in whose moment is not on its row
    assert card_title({**base, "trigger": None}) == "Getting to know you"   # a plain question is unchanged
    card = NudgeCard(linger_ms=5000)
    card.set_nudge({**base, "moment": "followup", "offer": todo})
    assert card.head.text().startswith("Following up") and card.title.text() == "Following up"
    assert not card.do_button.isHidden() and card.do_button.text() == "Add to list"   # its typed key
    assert card.detail.text() == "Will: add “Ship the export cleanup” to your list\nWon't: Changes only your list."
    assert card.rest.text() == "You said you'd ship it today." and not card.answer_input.isHidden()
    assert not card.reply_button.isHidden() and not card.never_button.isHidden()      # still a question
    card.set_nudge({"id": 12, "kind": "question", "at": None, "text": "Side project or work?", "offer": todo})
    assert card.do_button.isHidden() and card.head.text().startswith("Question")      # not a check-in: no key
    card.close()


def test_the_consent_card_answers_always_when_the_check_is_on(qapp):
    from yuki.ui.calls import CallConsentCard

    card = CallConsentCard()
    got = []
    card.answered.connect(lambda call, answer: got.append(answer))
    card.set_call({"id": 1, "app": "Discord", "can_remember_choice": True})
    assert card.question.text() == "You’re on a Discord call." and card.always_button.text() == "Always for Discord"
    card.always_button.setChecked(True)
    card.call = {"id": 1}
    card.yes_button.click()
    card.set_call({"id": 2, "app": "Discord"})
    card.no_button.click()
    card.set_call({"id": 3, "app": "Discord"})
    card.always_button.setChecked(True)
    card.no_button.click()
    assert got == ["always", "no", "never"]
    card.close()


# -- the ask bar ---------------------------------------------------------------------------------------------


def test_the_ask_bar_shows_resolved_chips_and_unsent_words(qapp):
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.overlay import Overlay, PANEL_WIDTH

    ov = Overlay()
    ov.attach_thread(ChatModel())
    assert PANEL_WIDTH == 780 and ov.thread.look == "lattice"
    ov.show_resolved("Message Riya", {"persons": [{"name": "Riya", "routes": [{"label": "WhatsApp chat", "mine": 64}]}],
                                      "where": "the chat in front 4 min ago"})
    chips = [(c.key, c.value) for c in ov.intent._chips]
    assert chips == [("who", "Riya"), ("where", "WhatsApp chat")]
    assert ov.intent.because.text() == "because the chat in front 4 min ago, and you sent 64 messages there"
    assert ov.intent.mode == "resolved" and not ov.intent_box.isHidden()
    ov.show_resolved("Open it", {"persons": []})                    # a frame that names nobody: no chips
    assert ov.intent.mode == "resolved" and [c.value for c in ov.intent._chips] == ["Riya", "WhatsApp chat"]
    ov.clear_intent()
    sent = []
    ov.submitted.connect(sent.append)
    ov.input.setPlainText("open the planner")
    ov.show_unsent("from an offer · 21:31")
    assert ov.intent.mode == "unsent" and ov.intent.source.text() == "from an offer · 21:31"
    assert sent == []                                              # nothing goes until Enter (or Send)
    ov.intent.k_send.click()
    assert sent == ["open the planner"] and ov.intent.mode == ""
    ov.close()


# -- the tray ------------------------------------------------------------------------------------------------


def test_the_tray_says_one_state_with_the_same_priorities():
    from yuki.ui.tray import tray_state

    assert tray_state("working", recording=True, listening="off") == "working"
    assert tray_state("listening", recording=True) == "listening"
    assert tray_state("hearing") == "hearing" and tray_state("speaking") == "speaking"
    assert tray_state("needs-you", recording=True) == "needs-you"
    assert tray_state("voice-stopped") == "stopped"
    assert tray_state("quiet-in-call", recording=True) == "call"
    assert tray_state("quiet-in-call", asking=True) == "call-ask"
    assert tray_state("not-listening", listening="quiet") == "off"
    assert tray_state("game-quiet", recording=True) == "game"     # the island is away: the tray's game face (+ rec)
    assert tray_state("hidden-armed") == "resting" and tray_state("done-flash") == "resting"


def test_each_tray_state_has_its_own_face(qapp):
    from yuki.ui.tray import TRAY_STATES, tray_face_image

    for size in (16, 20, 24):
        images = {s: tray_face_image(s, size) for s in TRAY_STATES if s != "call-ask"}
        assert len({bytes(img.constBits()) for img in images.values()}) == len(images)


def test_the_tray_carries_the_call_while_a_game_hides_the_island(qapp):
    from yuki.ui.tray import TrayWatch

    isl = _island(qapp)
    tray = SimpleNamespace(icon=None, tip="", setIcon=lambda i: setattr(tray, "icon", i),
                           setToolTip=lambda t: setattr(tray, "tip", t), geometry=lambda: __import__(
                               "PySide6.QtCore", fromlist=["QRect"]).QRect())
    try:
        watch = TrayWatch(tray, isl, waiting=lambda: 0, extra=lambda: "sonnet · effort high")
        isl.update(call=_call(47))
        watch.refresh()
        assert watch.state == "call" and watch.badge == "rec" and "On Discord · 47:" in tray.tip
        isl.screen_state = lambda: (3, "d3d_full_screen")
        isl.on_foreground(0)
        assert isl.state == "game-quiet" and watch.state == "game" and watch.badge == "rec"
        assert "Quiet while you play" in tray.tip and "47:" in tray.tip
        isl.screen_state = lambda: (5, "accepts")
        isl.on_foreground(0)
        assert watch.state == "call"
    finally:
        isl.stop()


# -- the monitor in use ---------------------------------------------------------------------------------------


def test_surfaces_go_to_the_monitor_in_use_not_the_primary():
    from yuki.ui.island import pick_screen

    class S:
        def __init__(self, name):
            self._n = name

        def name(self):
            return self._n

    left, right = S("\\\\.\\DISPLAY1"), S("\\\\.\\DISPLAY2")
    assert pick_screen("\\\\.\\DISPLAY2", [left, right], left) is right
    assert pick_screen("", [left, right], left) is left               # unknown: the primary
    assert pick_screen("\\\\.\\DISPLAY9", [left, right], left) is left


# -- no timer while hidden, nothing while idle -----------------------------------------------------------------


def test_a_hidden_or_settled_island_runs_no_timer(qapp):
    from yuki.ui import lattice as L

    isl = _island(qapp)
    loop = L.Loop.shared()
    try:
        from yuki.ui.island import RunInfo

        isl.update(voice="listening", call=_call(5))
        qapp.processEvents()
        assert isl.window.isVisible() and loop.running and not isl._clock.isActive()   # the pill: no clock
        isl.update(voice="off", run=RunInfo(8, time.monotonic(), steps=1, line="Opening it"))
        isl.pointer(True, now=True)
        assert isl._clock.isActive()                               # the open bar's run time ticks
        isl.update(run=None, voice="listening")
        isl.hovered = False
        isl.screen_state = lambda: (3, "d3d_full_screen")
        isl.on_foreground(0)                                       # a full screen: the island leaves
        deadline = time.monotonic() + 2.0
        while isl.window.isVisible() and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        for _ in range(5):
            qapp.processEvents()
        assert not isl.window.isVisible() and not isl.window.halo_win.isVisible()
        assert not isl._clock.isActive() and not L.Blinker.of(1600).active and not L.Blinker.of(1000).active
        assert isl.window.face._frame_fn not in loop.subs.values()
        assert not loop.running
        # back, idle: the mini island settles and then nothing runs
        isl.screen_state = lambda: (5, "accepts")
        isl.update(voice="off", call=None, run=None)
        isl.on_foreground(0)
        deadline = time.monotonic() + 3.0
        while loop.running and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert isl.state == "hidden-armed" and not loop.running and not isl._clock.isActive()
        draws = loop.draws
        for _ in range(20):
            qapp.processEvents()
            time.sleep(0.01)
        assert loop.draws == draws                                  # idle: no repaint at all
    finally:
        isl.stop()


def test_the_face_and_halo_follow_the_designs_formulas():
    from yuki.ui.dots import FACE_H, FACE_W, FaceModel, HaloModel

    face = FaceModel()
    face.set("working", 0.0, (2, 5))
    face.target(1.0)
    lit = {(n % FACE_W, n // FACE_W) for n, v in enumerate(face.g) if v > 0}
    assert (4, 2) in lit and (11, 3) in lit and (6, 6) in lit     # half-lidded eyes, the step pips under the bar
    assert len(face.g) == FACE_W * FACE_H == 17 * 7
    halo = HaloModel()
    assert (halo.cols, halo.rows, halo.ai, halo.aj) == (56, 17, 28, 8)
    halo.set_state("quiet-in-call", 0.0)
    for k in range(80):
        more = halo.step(k / 30)
    assert not more                                               # a static figure settles: no more frames
    lit = {(n % halo.cols - halo.ai, n // halo.cols - halo.aj) for n in halo.lit}
    assert (16, 0) in lit and all(v == 0 for _u, v in lit)        # the dotted line with its one live dot
