"""The "Your usual" routine card in the UI (offscreen; yuki.ui.cards, yuki.ui.nudges; PLAN.md Phase R, Q2/Q3): its
heading, the why line from the counts, its skill's Won't line, today's buttons (Open it, Not now, Less of these);
Open it runs the skill in the tray under the voice run's safety (desk lease, Yuki's marker, the game re-check, never
an agent run), reports the outcome to memory and shows it with Undo, which brings back the window that was in front.
Nothing is shown on screen, no window is focused, nothing reaches a desktop: the skill runner and the focus are
stand-ins.

r30: the engine's real game hold on the hand-written ``meet.open`` (gone since lSplit, 10 Oct; the engine's game holds
are covered by test_sCore_engine on the demo capabilities) and the end-to-end lane test (its coach worker's
``media_fn`` / ``render_fn`` went with kProactive, 18 Oct) were cut.
"""

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

    return QApplication.instance() or QApplication([])


def _soon(minutes: float = 10.0) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).astimezone().isoformat(timespec="seconds")


OFFER = {"do": "run_skill", "label": "Open it", "steps": ["Open the Meet tab in Zen"], "refs": ["R1"],
         "wont": "Won't join, unmute, type or send anything. You click Join.", "undo": True,
         "why": "You've joined it around 08:58 on 7 of the last 9 weekdays.", "expires_at": _soon()}
CARD = {"id": 70, "kind": "notice", "trigger": "routine", "at": None, "offer": OFFER,
        "text": "Your 9:00 meeting is coming up. Open the Meet tab in Zen?"}


class Memory:
    installed = True

    def __init__(self, answers: dict | None = None):
        self.acks: list[tuple] = []
        self.calls: list[tuple] = []
        self.answers = answers or {}

    def ack_nudge(self, nudge_id, reaction, *, reply=None, timeout_s=None):
        self.acks.append((nudge_id, reaction))

    def pending_nudges(self, **kw):
        return []

    def call_method(self, name, *args, timeout_s=None, **kwargs):
        self.calls.append((name, *args))
        if name == "card_due" and name not in self.answers:
            # r30: since jSwitch (10 Oct) the tray asks every queued card's card_due first; a stand-in with no answer
            # is an older memory, as the tray reads it (routine_card_due for the routine card, nothing for the rest)
            from yuki.agent.memory import MemoryUnavailable

            raise MemoryUnavailable("memory has no card_due()")
        answer = self.answers.get(name)
        return answer(*args) if callable(answer) else answer


GRANTED = {"ok": True, "do": "run_skill", "state": "running", "skill": "meet.open", "slots": {}, "routine": 1,
           "undo": True}


def _controller(memory, runner=None, desk_busy=None):
    from yuki.ui.nudges import NudgeCard, NudgeController

    log = SimpleNamespace(event=lambda *a, **k: None)
    return NudgeController(memory, log, card=NudgeCard(linger_ms=1000), screen_state=lambda: (5, "accepts"),
                           skill_runner=runner, desk_busy=desk_busy)


def _settle(qapp, ctl, rounds: int = 60) -> None:
    for _ in range(rounds):
        ctl.wait_acks(1.0)
        qapp.processEvents()
        time.sleep(0.005)


def test_the_routine_card_says_your_usual_with_its_why_and_wont_lines(qapp):
    from yuki.ui.cards import card_title, offer_why, routine_card
    from yuki.ui.nudges import NudgeCard

    assert routine_card(CARD) and card_title(CARD) == "Your usual" and offer_why(CARD) == OFFER["why"]
    card = NudgeCard(linger_ms=1000)
    card.set_nudge(dict(CARD))
    assert card.title.text() == "Your usual"
    assert card.body.text() == CARD["text"] and card.rest.text() == OFFER["why"] and not card.rest.isHidden()
    assert card.detail.text() == ("Will: Open the Meet tab in Zen\n"
                                  "Won't: Won't join, unmute, type or send anything. You click Join.")
    assert not card.do_button.isHidden() and card.do_button.text() == "Open it"
    assert card.dismiss_button.text() == "Not now" and not card.less_button.isHidden()   # today's buttons only
    assert card.reply_button.isHidden() and card.never_button.isHidden()
    card.set_nudge({**CARD, "id": 71, "offer": {**OFFER, "expires_at": _soon(-1)}})   # past its time: no key
    assert card.do_button.isHidden()
    card.set_nudge({**CARD, "id": 72, "trigger": "thread", "offer": None})            # any other card: as before
    assert card.title.text() == "Your work" and card.rest.isHidden()
    card.close()


def test_open_it_runs_the_skill_in_the_tray_reports_it_and_undo_brings_back_the_window(qapp, monkeypatch):
    import yuki.actions.launch as launch
    import yuki.ui.nudges as un

    monkeypatch.setattr(un, "_foreground_hwnd", lambda: 4242)
    focused: list[int] = []
    monkeypatch.setattr(launch, "focus_window", lambda hwnd, **kw: focused.append(hwnd))
    ran: list[dict] = []

    def runner(memory, granted, *, nudge_id=None, log=None):
        ran.append({"skill": granted["skill"], "slots": granted["slots"], "nudge": nudge_id})
        return {"ok": True, "outcome": "done", "verified": True, "route": "a:census:driver",
                "say": "Your Meet tab is up in Zen.", "focus": True, "ms": 930.0}

    memory = Memory({"offer_act": lambda nid: dict(GRANTED), "offer_result": lambda *a: True,
                     "offer_undo": lambda nid: {"ok": True, "do": "run_skill", "state": "undone"}})
    ctl = _controller(memory, runner)
    states: list[str] = []
    ctl.offer_state.connect(lambda n, st: states.append(st))
    ctl.card.show_nudge(ctl.record_delivery(dict(CARD), "card"))
    ctl.card._on_do()
    assert ctl.card.do_button.text() == "Running…"
    _settle(qapp, ctl)
    assert memory.acks == [(70, "accepted")]
    assert ran == [{"skill": "meet.open", "slots": {}, "nudge": 70}]              # the tray ran it, not memory
    result = [c for c in memory.calls if c[0] == "offer_result"]
    assert result == [("offer_result", 70, True, {"outcome": "done", "route": "a:census:driver", "ms": 930.0,
                                                  "verified": True, "focus": True})]
    assert ctl.card.result.text() == "Your Meet tab is up in Zen." and not ctl.card.undo_button.isHidden()
    assert states[0] == "running" and states[-1] == "done"
    ctl.card._on_undo()
    _settle(qapp, ctl)
    assert focused == [4242] and ("offer_undo", 70) in memory.calls and ctl.card.result.text() == "Undone."
    ctl.card.close()


def test_a_run_the_pc_couldnt_confirm_says_so_with_no_undo(qapp, monkeypatch):
    import yuki.ui.nudges as un

    monkeypatch.setattr(un, "_foreground_hwnd", lambda: 4242)

    def runner(memory, granted, **kw):
        return {"ok": False, "outcome": "asked", "say": "I can't read Zen's tabs right now, so I left Zen as it is.",
                "focus": False}

    memory = Memory({"offer_act": lambda nid: dict(GRANTED), "offer_result": lambda *a: True})
    ctl = _controller(memory, runner)
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 73}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ctl.card.result.text() == "I can't read Zen's tabs right now, so I left Zen as it is."
    assert ctl.card.undo_button.isHidden()
    assert [c for c in memory.calls if c[0] == "offer_result"][0][2] is False
    ctl.card.close()


def test_nothing_runs_while_yukis_hands_are_on_the_desktop(qapp):
    ran: list[int] = []
    memory = Memory({"offer_act": lambda nid: dict(GRANTED), "offer_result": lambda *a: True})
    ctl = _controller(memory, lambda *a, **k: ran.append(1) or {"ok": True}, desk_busy=lambda: "worker_busy")
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 74}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ran == [] and ctl.card.result.text() == "I'm busy on your desktop right now. Nothing ran."
    assert [c for c in memory.calls if c[0] == "offer_result"][0][2:] == (False, {"outcome": "busy", "ms": pytest.approx(0, abs=500)})
    ctl.card.close()


def test_the_desk_lease_reads_the_uis_own_busy_reasons_by_default(qapp):
    from yuki.ui.nudges import NudgeCard, NudgeController

    log = SimpleNamespace(event=lambda *a, **k: None)
    reason = {"v": "worker_busy"}
    ctl = NudgeController(Memory(), log, blocked=lambda: reason["v"], card=NudgeCard(linger_ms=1000),
                          screen_state=lambda: (5, "accepts"))
    assert ctl._desk_busy_now() == "worker_busy"
    reason["v"] = "overlay_request"     # the ask bar's request waiting is no hand on the desktop
    assert ctl._desk_busy_now() is None
    ctl.card.close()


def test_a_refused_press_runs_nothing(qapp):
    ran: list[int] = []
    memory = Memory({"offer_act": lambda nid: {"ok": False, "why": "not_ready", "do": "run_skill"}})
    ctl = _controller(memory, lambda *a, **k: ran.append(1) or {"ok": True})
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 75}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ran == [] and ctl.card.result.text() == "I can't do that from a card yet. Nothing ran."
    assert not [c for c in memory.calls if c[0] == "offer_result"]
    ctl.card.close()


def test_less_of_these_on_a_routine_card_steps_its_routine(qapp):
    memory = Memory({"routine_feedback": lambda nid, kind: {"ok": True, "mode_from": "early", "mode_to": "late"}})
    ctl = _controller(memory)
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 76}, "card"))
    ctl.card._on_less()
    _settle(qapp, ctl)
    assert (76, "less") in memory.acks and ("routine_feedback", 76, "less") in memory.calls
    other = _controller(Memory())
    other.card.show_nudge(other.record_delivery({**CARD, "id": 77, "trigger": "thread"}, "card"))
    other.card._on_less()
    _settle(qapp, other)
    assert not [c for c in other.memory.calls if c[0] == "routine_feedback"]
    ctl.card.close()
    other.card.close()


# -- review round 2: the queue's real guarantee, the desk read, Undo after an unconfirmed run -------------------------


OTHER = {"id": 90, "kind": "notice", "trigger": "thread", "at": None, "offer": None, "text": "Another card."}


def test_a_routine_card_waits_behind_a_card_showing_and_asks_memory_before_it_shows(qapp):
    """L2: the tray's queue is what keeps a routine card from jumping a card on screen; once that card is gone,
    memory is asked "still due?" (off the GUI thread): due -> shown; over -> dropped, the next card shows; held (a
    call, a game in front) -> it waits; past its own expiry -> dropped without asking."""
    answers = {80: {"due": True}, 81: {"due": False, "why": "done", "final": True},
               82: {"due": False, "why": "call", "final": False}}
    memory = Memory({"routine_card_due": lambda nid: answers[nid]})
    ctl = _controller(memory)
    ctl.offer([dict(OTHER)])
    assert ctl.showing is not None and ctl.showing["id"] == 90
    ctl.offer([{**CARD, "id": 80}])
    _settle(qapp, ctl, rounds=5)
    assert ctl.showing["id"] == 90 and [n["id"] for n in ctl.queue] == [80]   # never over the card showing
    assert not [c for c in memory.calls if c[0] == "routine_card_due"]
    ctl.card.withdraw()   # gone at once (the fade is a 240 ms animation)
    ctl.try_show()
    _settle(qapp, ctl)
    assert ("routine_card_due", 80) in memory.calls and ctl.showing["id"] == 80 and ctl.queue == []
    assert "_due" not in ctl.showing
    ctl.card.withdraw()   # gone at once (the fade is a 240 ms animation)
    # the user joined while it waited: dropped, and the card behind it shows
    ctl.offer([{**CARD, "id": 81}, {**OTHER, "id": 91}])
    _settle(qapp, ctl)
    assert ("routine_card_due", 81) in memory.calls and ctl.showing["id"] == 91 and ctl.queue == []
    ctl.card.withdraw()   # gone at once (the fade is a 240 ms animation)
    # held by an open call: it stays queued, nothing shows, a later look asks again
    ctl.offer([{**CARD, "id": 82}])
    _settle(qapp, ctl)
    assert ctl.showing is None and [n["id"] for n in ctl.queue] == [82] and ctl._retry.isActive()
    answers[82] = {"due": True}
    ctl.queue[0]["_due"] = (0.0, {"due": False})   # the answer went stale (ROUTINE_DUE_FRESH_S)
    ctl.try_show()
    _settle(qapp, ctl)
    assert ctl.showing["id"] == 82
    ctl.card.withdraw()   # gone at once (the fade is a 240 ms animation)
    # past its own expiry: dropped at once, memory not asked
    asked = len(memory.calls)
    ctl.offer([{**CARD, "id": 83, "offer": {**OFFER, "expires_at": _soon(-1)}}])
    _settle(qapp, ctl, rounds=5)
    assert ctl.showing is None and ctl.queue == [] and len(memory.calls) == asked
    ctl.card.close()


def test_a_routine_card_memory_cannot_clear_never_shows(qapp):
    """Memory unreachable or without an answer: the card waits (fail closed) and never shows."""
    memory = Memory({})   # routine_card_due answers None
    ctl = _controller(memory)
    ctl.offer([{**CARD, "id": 84}])
    _settle(qapp, ctl)
    assert ctl.showing is None and [n["id"] for n in ctl.queue] == [84]
    ctl.card.close()


def test_the_desk_busy_reason_is_read_on_the_gui_thread(qapp):
    """L8: the UI's widgets are read on the GUI thread before the run's thread starts."""
    import threading

    seen: list[bool] = []
    memory = Memory({"offer_act": lambda nid: dict(GRANTED), "offer_result": lambda *a: True})
    ctl = _controller(memory, lambda *a, **k: {"ok": True, "outcome": "done"},
                      desk_busy=lambda: seen.append(threading.current_thread() is threading.main_thread()) or None)
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 85}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert seen == [True]
    ctl.card.close()


def test_undo_after_a_run_that_took_focus_but_couldnt_confirm(qapp, monkeypatch):
    """L9: Zen came forward but the Meet tab couldn't be confirmed: the card says so, with Undo back to the window
    that was in front."""
    import yuki.actions.launch as launch
    import yuki.ui.nudges as un

    monkeypatch.setattr(un, "_foreground_hwnd", lambda: 4343)
    focused: list[int] = []
    monkeypatch.setattr(launch, "focus_window", lambda hwnd, **kw: focused.append(hwnd))

    def runner(memory, granted, **kw):
        return {"ok": False, "outcome": "unverified", "say": "Zen is up, but I couldn't see the Meet tab.",
                "focus": True}

    memory = Memory({"offer_act": lambda nid: dict(GRANTED), "offer_result": lambda *a: True,
                     "offer_undo": lambda nid: {"ok": True, "do": "run_skill", "state": "undone"}})
    ctl = _controller(memory, runner)
    ctl.card.show_nudge(ctl.record_delivery({**CARD, "id": 86}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ctl.card.result.text() == "Zen is up, but I couldn't see the Meet tab."
    assert not ctl.card.undo_button.isHidden()
    ctl.card._on_undo()
    _settle(qapp, ctl)
    assert focused == [4343] and ("offer_undo", 86) in memory.calls and ctl.card.result.text() == "Undone."
    ctl.card.close()


# -- run_card_skill: the voice run's safety ---------------------------------------------------------------------------


class Marker:
    def __init__(self):
        self.begun: list[tuple] = []
        self.ended: list[str] = []

    def acting_begin(self, token, request, *, lane=""):
        self.begun.append((token, request, lane))
        return True

    def acting_end(self, token):
        self.ended.append(token)
        return True


def test_run_card_skill_takes_the_lease_publishes_the_marker_and_never_hands_over(monkeypatch):
    import yuki.skills.engine as engine
    from yuki.memory.acting import request_id_of
    from yuki.ui.nudges import run_card_skill

    seen: dict = {}

    def fake(skill, slots, words, ctx, **kw):
        from yuki.actions import leases

        seen.update(skill=skill, slots=slots, words=words, origin=ctx.origin, request_id=ctx.request_id,
                    lease=leases._INPUT._is_owned())
        ctx.on_focus()
        ctx.on_focus()   # published once
        return engine.SkillResult(skill=skill, outcome="done", verified=True, done_line="Your Meet tab is up in Zen.",
                                  route="A:census:driver", focus_taken=True, ms=812.0)

    monkeypatch.setattr(engine, "resolve_and_run", fake)
    marker = Marker()
    got = run_card_skill(marker, dict(GRANTED), nudge_id=70)
    assert seen["skill"] == "meet.open" and seen["origin"] == "card" and seen["words"] == [] and seen["lease"]
    assert len(marker.begun) == 1 and marker.begun[0][2] == "card"
    token = marker.begun[0][0]
    assert request_id_of(token, "card") == seen["request_id"] and marker.ended == [token]   # D18: one request id
    assert "meet.open" in marker.begun[0][1]
    assert got == {"ok": True, "outcome": "done", "verified": True, "route": "A:census:driver",
                   "say": "Your Meet tab is up in Zen.", "focus": True, "ms": 812.0}

    def handed(skill, slots, words, ctx, **kw):
        return engine.SkillResult(skill=skill, outcome="handed_over", handoff={"goal": "x"})

    monkeypatch.setattr(engine, "resolve_and_run", handed)
    got = run_card_skill(Marker(), dict(GRANTED), nudge_id=71)
    assert got["ok"] is False and got["say"] == "I couldn't do that from the card. Nothing else ran."


def test_run_card_skill_with_the_desk_lease_held_elsewhere_runs_nothing(monkeypatch):
    import threading

    import yuki.skills.engine as engine
    import yuki.ui.nudges as un
    from yuki.actions import leases

    monkeypatch.setattr(un, "CARD_LEASE_S", 0.2)
    monkeypatch.setattr(engine, "resolve_and_run", lambda *a, **k: pytest.fail("ran without the lease"))
    held, release = threading.Event(), threading.Event()

    def other_hand():
        with leases.input_lease():
            held.set()
            release.wait(5)

    t = threading.Thread(target=other_hand)
    t.start()
    held.wait(5)
    try:
        got = un.run_card_skill(Marker(), dict(GRANTED), nudge_id=72)
    finally:
        release.set()
        t.join(5)
    assert got["ok"] is False and got["outcome"] == "busy"

