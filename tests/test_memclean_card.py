"""The call's keep list on its card (report 76 item 5), offscreen: each line with Keep / Not true / It's a joke, the
press answered by memory by code (``recap_keep``: no model, never the agent), the row saying what became of it, Undo,
and the card acknowledged once. Run apart from the other GUI tests (QT_QPA_PLATFORM=offscreen); nothing is shown on
screen. Invented lines."""

from __future__ import annotations

import os
import time
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication

    return QApplication.instance() or QApplication([])


KEEP = {"id": 41, "kind": "notice", "trigger": "recap", "at": None,
        "text": "From your call with Tara: 2 things I'd keep\n- You've been to Japan four times.\n- Someone on the "
                "call said the trek starts at 6 AM on 11 October.",
        "head": "From your call with Tara: 2 things I'd keep",
        "keep": [{"ref": "journal:7", "text": "You've been to Japan four times.", "who": "user"},
                 {"ref": "journal:8", "text": "Someone on the call said the trek starts at 6 AM on 11 October.",
                  "who": "others"}]}


class Memory:
    installed = True

    def __init__(self, answer):
        self.acks: list[tuple] = []
        self.calls: list[tuple] = []
        self.answer = answer

    def ack_nudge(self, nudge_id, reaction, *, reply=None, timeout_s=None):
        self.acks.append((nudge_id, reaction))

    def pending_nudges(self, **kw):
        return []

    def call_method(self, name, *args, timeout_s=None, **kwargs):
        self.calls.append((name, *args))
        return self.answer(*args)


def _settle(qapp, ctl, rounds: int = 40) -> None:
    for _ in range(rounds):
        ctl.wait_acks(1.0)
        qapp.processEvents()
        time.sleep(0.005)


def test_a_keep_list_card_shows_each_line_with_its_three_answers(qapp):
    from yuki.ui.nudges import KEEP_ANSWERS, NudgeCard

    card = NudgeCard(linger_ms=1000)
    card.set_nudge(dict(KEEP))
    assert card.title.text() == "After your call" and card.body.text() == KEEP["head"]
    assert not card.keep_rows.isHidden() and list(card.keep_rows.rows) == ["journal:7", "journal:8"]
    row = card.keep_rows.rows["journal:7"]
    assert [k.text() for k in row["keys"]] == [label for _a, label in KEEP_ANSWERS] == ["Keep", "Not true",
                                                                                        "It's a joke"]
    # review F5: someone else's line is Keep or Not true - a joke is only ever the user's own
    assert [k.text() for k in card.keep_rows.rows["journal:8"]["keys"]] == ["Keep", "Not true"]
    assert card.reply_button.isHidden() and not card.dismiss_button.isHidden()   # Dismiss / Less like this stay
    card.set_nudge({**KEEP, "keep": None, "head": None})   # an ordinary recap: no rows, Reply as before
    assert card.keep_rows.isHidden() and not card.reply_button.isHidden()
    card.close()


def test_a_press_is_answered_by_memory_and_the_row_says_so_with_undo(qapp):
    from yuki.ui.nudges import NudgeCard, NudgeController

    def answer(nudge_id, ref, what):
        return {"ok": True, "ref": ref, "answer": what} if ref == "journal:7" else {"ok": False, "why": "already"}

    memory = Memory(answer)
    ctl = NudgeController(memory, SimpleNamespace(event=lambda *a, **k: None), card=NudgeCard(linger_ms=60_000),
                          screen_state=lambda: (5, "accepts"))
    ctl.card.show_nudge(ctl.record_delivery(dict(KEEP), "card"))
    rows = ctl.card.keep_rows.rows
    rows["journal:7"]["keys"][0].click()           # Keep
    _settle(qapp, ctl)
    assert ("recap_keep", 41, "journal:7", "keep") in memory.calls and memory.acks == [(41, "accepted")]
    assert rows["journal:7"]["state"].text() == "Kept." and not rows["journal:7"]["undo"].isHidden()
    assert all(k.isHidden() for k in rows["journal:7"]["keys"])
    rows["journal:8"]["keys"][1].click()           # Not true, but memory has an answer already
    _settle(qapp, ctl)
    assert rows["journal:8"]["state"].text() == "Already answered." and rows["journal:8"]["undo"].isHidden()
    assert memory.acks == [(41, "accepted")]       # the card is acknowledged once
    rows["journal:7"]["undo"].click()
    _settle(qapp, ctl)
    assert ("recap_keep", 41, "journal:7", "undo") in memory.calls
    assert not any(k.isHidden() for k in rows["journal:7"]["keys"]) and rows["journal:7"]["undo"].isHidden()
    ctl.card._on_expire()                          # fading after an answer records nothing more
    _settle(qapp, ctl)
    assert memory.acks == [(41, "accepted")]
    ctl.card.close()
