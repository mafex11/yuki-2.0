"""The observation cards in the UI (offscreen): a typed offer's Will / Won't lines, its button named for the action,
Not now / Less of these, what each acknowledges, and what a press does: memory runs the offer by code and the card
shows the result in place - never an agent run. Run apart from the other GUI tests (QT_QPA_PLATFORM=offscreen);
nothing is shown on screen, no page is opened, no window is focused."""

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


def _soon(minutes: float = 20.0) -> str:
    return (datetime.now() + timedelta(minutes=minutes)).astimezone().isoformat(timespec="seconds")


TODO = {"do": "todo_add", "label": "Add to list", "steps": ["add 'Build the booth link cache', due Mon, to your list"],
        "refs": ["W72"], "text": "Build the booth link cache", "due": "2026-10-05",
        "wont": "Changes only your Yuki list. Opens, types and sends nothing.", "undo": True,
        "expires_at": _soon()}
OFFER = {"id": 7, "kind": "notice", "trigger": "thread", "at": None,
         "text": "Next on the operator demo: the booth link cache, due Monday. Want it on your list?", "offer": TODO}


def test_a_card_with_a_typed_offer_says_what_it_will_and_wont_do(qapp):
    from yuki.ui.nudges import NudgeCard

    card = NudgeCard(linger_ms=1000)
    card.set_nudge(dict(OFFER))
    assert card.title.text() == "Your work"
    assert not card.do_button.isHidden() and card.do_button.text() == "Add to list"   # named for the action
    assert card.dismiss_button.text() == "Not now" and not card.less_button.isHidden()
    assert card.reply_button.isHidden() and card.never_button.isHidden()
    assert card.detail.text() == ("Will: add 'Build the booth link cache', due Mon, to your list\n"
                                  "Won't: Changes only your Yuki list. Opens, types and sends nothing.")
    card.set_nudge({**OFFER, "offer": None, "trigger": "recap"})   # no offer: Reply as before, still Less of these
    assert card.title.text() == "After your call" and card.do_button.isHidden() and card.detail.isHidden()
    assert not card.reply_button.isHidden() and card.dismiss_button.text() == "Dismiss"
    card.set_nudge({"id": 8, "kind": "resume", "text": "Pick it up?", "offer": dict(TODO), "at": None})
    assert not card.do_button.isHidden() and not card.less_button.isHidden()   # resume cards get Less of these too
    card.set_nudge({"id": 9, "kind": "question", "text": "Who's Bro to you?", "at": None})
    assert card.do_button.isHidden() and not card.never_button.isHidden() and card.less_button.isHidden()
    old = {**OFFER, "id": 10, "offer": "Find yuki-2.0 in T3 Code's project selector, tick it, and run the import."}
    card.set_nudge(old)   # a free-text offer from before 3 Oct: no button runs a sentence
    assert card.do_button.isHidden() and not card.reply_button.isHidden()
    card.set_nudge({**OFFER, "id": 11, "offer": {**TODO, "expires_at": _soon(-1)}})   # its time ran out
    assert card.do_button.isHidden()
    card.close()


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
        answer = self.answers.get(name)
        return answer(*args) if callable(answer) else answer


def _controller(memory):
    from yuki.ui.nudges import NudgeCard, NudgeController

    log = SimpleNamespace(event=lambda *a, **k: None)
    return NudgeController(memory, log, card=NudgeCard(linger_ms=1000), screen_state=lambda: (5, "accepts"))


def _settle(qapp, ctl, rounds: int = 40) -> None:
    for _ in range(rounds):
        ctl.wait_acks(1.0)
        qapp.processEvents()
        time.sleep(0.005)


def test_a_press_runs_the_offer_by_code_once_and_shows_the_result_with_undo(qapp):
    memory = Memory({"offer_act": lambda nid: {"ok": True, "do": "todo_add", "state": "done", "undo": True,
                                               "message": "Added to your list."},
                     "offer_undo": lambda nid: {"ok": True, "state": "undone"}})
    ctl = _controller(memory)
    states: list[str] = []
    ctl.offer_state.connect(lambda n, st: states.append(st))
    ctl.card.show_nudge(ctl.record_delivery(dict(OFFER), "card"))
    ctl.card._on_do()
    assert ctl.card.do_button.text() == "Running…" and not ctl.card.do_button.isEnabled()
    _settle(qapp, ctl)
    assert memory.acks == [(7, "accepted")] and ("offer_act", 7) in memory.calls   # no agent, no request: memory
    assert ctl.card.result.text() == "Added to your list." and not ctl.card.undo_button.isHidden()
    assert states == ["running", "done"]
    ctl.accept_offer(dict(OFFER), via="overlay")   # the same card again from the chat thread: nothing runs twice
    _settle(qapp, ctl)
    assert [c for c in memory.calls if c[0] == "offer_act"] == [("offer_act", 7)]
    ctl.card._on_undo()
    _settle(qapp, ctl)
    assert ("offer_undo", 7) in memory.calls and ctl.card.result.text() == "Undone." and states[-1] == "undone"
    ctl.card.close()


def test_a_refused_offer_says_why_and_nothing_runs(qapp):
    memory = Memory({"offer_act": lambda nid: {"ok": False, "why": "expired", "do": "todo_add"}})
    ctl = _controller(memory)
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 21}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ctl.card.result.text() == "This offer expired. Nothing ran." and ctl.card.undo_button.isHidden()
    ctl.card.close()


def test_a_draft_expands_in_the_card_with_copy_and_is_sent_nowhere(qapp):
    from PySide6.QtGui import QGuiApplication

    draft = {"do": "draft", "label": "Show draft", "steps": ["show a reply to Varun you can copy"], "refs": [],
             "text": "Hey Varun, PR #105 is merged.", "wont": "Sends it nowhere and types it nowhere.",
             "expires_at": _soon()}
    memory = Memory({"offer_act": lambda nid: {"ok": True, "do": "draft", "state": "done",
                                               "text": "Hey Varun, PR #105 is merged."}})
    ctl = _controller(memory)
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 31, "offer": draft}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert ctl.card.result.text() == "Hey Varun, PR #105 is merged." and not ctl.card.copy_button.isHidden()
    ctl.card._on_copy()
    assert QGuiApplication.clipboard().text() == "Hey Varun, PR #105 is merged."
    assert [c[0] for c in memory.calls] == ["offer_act"]
    ctl.card.close()


def test_an_ask_offer_goes_to_the_ask_bar_unsent(qapp):
    ask = {"do": "ask", "label": "Edit in ask bar", "steps": ["put a suggestion in the ask bar, unsent"], "refs": [],
           "text": "Check the T3 Code agent's stream failure and tell me what it was",
           "wont": "Runs nothing until you press Enter.", "expires_at": _soon()}
    memory = Memory({"offer_act": lambda nid: {"ok": True, "do": "ask", "state": "prefilled", "text": ask["text"]}})
    ctl = _controller(memory)
    handed: list[tuple] = []
    ctl.ask_requested.connect(lambda n, text: handed.append((n["id"], text)))
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 41, "offer": ask}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert handed == [(41, ask["text"])]
    ctl.card.close()


def test_bring_back_opens_the_page_and_undo_brings_back_what_was_in_front(qapp, monkeypatch):
    import yuki.ui.nudges as un

    opened: list[str] = []
    focused: list[int] = []
    monkeypatch.setattr(un, "_open_page", lambda url: opened.append(url) or True)
    monkeypatch.setattr(un, "_foreground_hwnd", lambda: 4242)
    import yuki.actions.launch as launch

    monkeypatch.setattr(launch, "focus_window", lambda hwnd, **kw: focused.append(hwnd))
    back = {"do": "bring_back", "label": "Open it again", "steps": ["open the booth link sheet again"], "refs": ["L7"],
            "target": {"url": "https://example.com/booth", "title": "Booth links"}, "wont": "Opens that page only.",
            "undo": True, "expires_at": _soon()}
    memory = Memory({"offer_act": lambda nid: {"ok": True, "do": "bring_back", "state": "done",
                                               "url": "https://example.com/booth", "title": "Booth links",
                                               "message": "Opened Booth links.", "undo": True},
                     "offer_undo": lambda nid: {"ok": True, "state": "undone"}})
    ctl = _controller(memory)
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 51, "offer": back}, "card"))
    ctl.card._on_do()
    _settle(qapp, ctl)
    assert opened == ["https://example.com/booth"] and ctl.card.result.text() == "Opened Booth links."
    ctl.card._on_undo()
    _settle(qapp, ctl)
    assert focused == [4242] and ("offer_undo", 51) in memory.calls
    ctl.card.close()


def test_not_now_and_less_are_acknowledged_and_close_the_offer_in_the_thread(qapp):
    memory = Memory()
    ctl = _controller(memory)
    states: list[tuple] = []
    ctl.offer_state.connect(lambda n, st: states.append((n["id"], st)))
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 61}, "card"))
    ctl.card._on_less()
    ctl.card.show_nudge(ctl.record_delivery({**OFFER, "id": 62}, "card"))
    ctl.card._on_dismiss()
    _settle(qapp, ctl)
    assert (61, "less") in memory.acks and (62, "dismissed") in memory.acks
    assert states == [(61, "less"), (62, "dismissed")]
    ctl.card.close()


def test_the_chat_thread_states_are_honest_and_dismissed_cards_lose_the_button(qapp):
    from yuki.ui import chatview
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window.theme import theme

    m = ChatModel()
    m.apply({"type": "nudge", "nudge": dict(OFFER), "at": 100.0})
    m.apply({"type": "nudge", "nudge": {"id": 8, "kind": "notice", "trigger": "recall", "text": "x", "offer": None,
                                        "missed": True}, "at": 101.0})
    m.apply({"type": "nudge", "nudge": {**OFFER, "id": 9, "reaction": "dismissed"}, "at": 102.0})
    m.apply({"type": "nudge", "nudge": {**OFFER, "id": 10, "offer": {**TODO, "expires_at": _soon(-1)}}, "at": 103.0})
    view = chatview.ChatThreadView(m, lambda: theme(), look="overlay")
    w = view._widgets
    assert w["nudge:7"].do is not None and w["nudge:7"].do.text() == "Add to list" and w["nudge:8"].do is None
    assert w["nudge:9"].do.isHidden() and w["nudge:10"].do.isHidden()   # dismissed / expired: no button in history
    got: list[dict] = []
    view.do_nudge.connect(got.append)
    w["nudge:7"].do.click()
    assert [g["id"] for g in got] == [7]
    assert w["nudge:7"].do.text() == "Add to list"                       # never "Done" at the press itself
    m.apply({"type": "nudge_state", "id": 7, "state": "offer:running"})
    assert w["nudge:7"].do.text() == "Running…" and not w["nudge:7"].do.isEnabled()
    m.apply({"type": "nudge_state", "id": 7, "state": "offer:done"})
    assert w["nudge:7"].do.text() == "Done" and not w["nudge:7"].undo.isHidden()
    assert w["nudge:7"].reply.isEnabled()                                # Reply is still there
    m.apply({"type": "nudge_state", "id": 7, "state": "offer:undone"})
    assert w["nudge:7"].do.text() == "Undone" and w["nudge:7"].undo.isHidden()
    m.apply({"type": "nudge_state", "id": 9, "state": "offer:less"})
    assert w["nudge:9"].do.isHidden()
    view.deleteLater()


def test_the_chat_thread_shows_a_draft_once_it_ran(qapp):
    from yuki.ui import chatview
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window.theme import theme

    draft = {"do": "draft", "label": "Show draft", "steps": ["show a reply you can copy"], "refs": [],
             "text": "Hey Varun, merged.", "wont": "Sends it nowhere.", "expires_at": _soon()}
    m = ChatModel()
    m.apply({"type": "nudge", "nudge": {**OFFER, "id": 12, "offer": draft}, "at": 100.0})
    view = chatview.ChatThreadView(m, lambda: theme(), look="overlay")
    w = view._widgets["nudge:12"]
    assert w.offer_result.isHidden()
    m.apply({"type": "nudge_state", "id": 12, "state": "offer:done"})
    assert not w.offer_result.isHidden() and w.offer_result.text() == "Hey Varun, merged."
    view.deleteLater()
