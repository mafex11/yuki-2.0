"""The observation cards in the UI (offscreen): Do it / Not now / Less of these, what each acknowledges, and the
request Do it becomes. Run apart from the other GUI tests (QT_QPA_PLATFORM=offscreen); nothing is shown on screen."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


OFFER = {"id": 7, "kind": "notice", "trigger": "stuck", "at": None,
         "text": "Deploy-beta failed three times since 19:52. Want me to pull the log?",
         "offer": "Open the latest failed Deploy Corvana Agent run and tell me why deploy-beta failed."}


def test_a_card_with_an_offer_has_do_it_not_now_and_less_of_these(qapp):
    from yuki.ui.nudges import NudgeCard

    card = NudgeCard(linger_ms=1000)
    card.set_nudge(dict(OFFER))
    assert card.title.text() == "Noticed"
    assert not card.do_button.isHidden() and card.do_button.text() == "Do it"
    assert card.dismiss_button.text() == "Not now" and not card.less_button.isHidden()
    assert card.reply_button.isHidden() and card.never_button.isHidden()
    assert card.detail.text() == "Do it: " + OFFER["offer"] and not card.detail.isHidden()   # what it will run
    card.set_nudge({**OFFER, "offer": None, "trigger": "recap"})   # no offer: Reply as before, still Less of these
    assert card.title.text() == "After your call" and card.do_button.isHidden() and card.detail.isHidden()
    assert not card.reply_button.isHidden() and card.dismiss_button.text() == "Dismiss"
    card.set_nudge({"id": 8, "kind": "resume", "text": "Pick it up?", "offer": "Reopen the PR.", "at": None})
    assert not card.do_button.isHidden() and card.less_button.isHidden()   # Less of these is for observations
    card.set_nudge({"id": 9, "kind": "question", "text": "Who's Bro to you?", "at": None})
    assert card.do_button.isHidden() and not card.never_button.isHidden() and card.less_button.isHidden()
    card.close()


class Memory:
    installed = True

    def __init__(self):
        self.acks: list[tuple] = []

    def ack_nudge(self, nudge_id, reaction, *, reply=None, timeout_s=None):
        self.acks.append((nudge_id, reaction))

    def pending_nudges(self, **kw):
        return []


def test_do_it_acknowledges_accepted_once_and_hands_the_offer_over(qapp):
    from yuki.ui.nudges import NudgeCard, NudgeController, offer_request

    memory = Memory()
    log = SimpleNamespace(event=lambda *a, **k: None)
    ctl = NudgeController(memory, log, card=NudgeCard(linger_ms=1000), screen_state=lambda: (5, "accepts"))
    handed: list[dict] = []
    ctl.do_requested.connect(handed.append)
    ctl.card.set_nudge(dict(OFFER))
    ctl.card._on_do()
    ctl.wait_acks()
    assert memory.acks == [(7, "accepted")] and handed and handed[0]["offer"] == OFFER["offer"]
    ctl.accept_offer(dict(OFFER))   # the same card again from the chat thread (its own dict): nothing runs twice
    ctl.wait_acks()
    assert memory.acks == [(7, "accepted")] and len(handed) == 1
    ctl.card.set_nudge({**OFFER, "id": 11})
    ctl.card._on_less()
    ctl.wait_acks()
    assert memory.acks[-1] == (11, "less")
    request = offer_request(OFFER)
    head, _, instruction = request.partition("\n")
    assert instruction == OFFER["offer"] and "Do it" in head and OFFER["text"] in head
    ctl.card.close()


def test_the_chat_thread_has_do_it_on_a_card_with_an_offer_and_done_after(qapp):
    from yuki.ui import chatview
    from yuki.ui.chat_model import ChatModel
    from yuki.ui.window.theme import theme

    m = ChatModel()
    m.apply({"type": "nudge", "nudge": dict(OFFER), "at": 100.0})
    m.apply({"type": "nudge", "nudge": {"id": 8, "kind": "notice", "trigger": "recall", "text": "x", "offer": None,
                                        "missed": True}, "at": 101.0})
    view = chatview.ChatThreadView(m, lambda: theme(), look="overlay")
    w = view._widgets
    assert w["nudge:7"].do is not None and w["nudge:8"].do is None
    got: list[dict] = []
    view.do_nudge.connect(got.append)
    w["nudge:7"].do.click()
    assert [g["id"] for g in got] == [7]
    m.apply({"type": "nudge_state", "id": 7, "state": "accepted"})
    assert w["nudge:7"].do.text() == "Done" and not w["nudge:7"].do.isEnabled()
    view.deleteLater()
