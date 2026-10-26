"""Offline checks of the coach and conversation fixes from the design notes

Temp databases, synthetic timelines and stub models only; nothing touches the desktop
or a real model. The coach chain's own tests (observe, Trigger, the coach's decide and tick, the welcome back, the break
nudge) went with that chain (kProactive, 18 Oct: the card judge writes every card now).
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from yuki.memory import acting
from yuki.memory.conversations import ConversationWorker, own_words
from yuki.memory.nudges import (
    NudgeConfig,
    activity_sessions,
    coach_facts,
    feedback_text,
    rules_text,
)
from yuki.memory.store import NewNudge, NudgeCheckin, NewQuestion, Store, TimelineRow

DAY = datetime(2026, 9, 25)
CFG = NudgeConfig()


def at(hhmm: str, day: int = 0) -> float:
    h, m = (int(x) for x in hhmm.split(":"))
    return (DAY + timedelta(days=day, hours=h, minutes=m)).timestamp()


@pytest.fixture()
def store():
    with tempfile.TemporaryDirectory() as tmp:
        s = Store.open(Path(tmp) / "memory.db")
        try:
            yield s
        finally:
            s.close()


def present(store: Store, start: float, end: float, app: str = "Warp", *, step: float = 300.0, **kw) -> None:
    t = start
    while t < end:
        e = min(end, t + step)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower(), app, active_s=e - t, **kw))
        t = e


def card(store: Store, when: float, text: str, kind: str = "nudge", trigger: str = "transition",
         reaction: str | None = None, reply: str | None = None, inputs: dict | None = None) -> int:
    _cid, nid = store.record_checkin(NudgeCheckin(at=when, trigger=trigger, outcome=kind),
                                     NewNudge(kind=kind, text=text, trigger=trigger, inputs=inputs or {}))
    if reaction:
        store.react_to_nudge(nid, reaction, at=when + 17, reply=reply)
    return nid


# -- item 1: a reply to a card is feedback on that kind of card ---------------------------


def test_own_words_leaves_out_the_card_and_the_notes():
    text = '[Replying to your nudge (kind: nudge), sent 11:07: "Finish that first"]\nstfu\n\n[Yuki\'s coach said ...]'
    assert own_words(text) == "stfu"
    assert own_words("Minimize Discord\n\n[Asked by voice. ...]") == "Minimize Discord"
    assert own_words("plain words") == "plain words"


def test_card_reply_is_feedback_and_never_touches_a_rule(store):
    rule = store.add_conversation_fact("rule", "Don't use em-dashes.", quote="Don't use em-dashes.", at=at("09:05"))
    nid = card(store, at("11:07"), "Finish the harness first.", reaction="replied")
    turn_id = store.add_turn("s1", 1, at("11:07") + 17, '[Replying to your nudge (kind: nudge), sent 11:07: "x"]\nstfu')
    store.flush_turns()
    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids([turn_id])
    cards = worker.cards_for(turns)
    assert cards[turn_id][0].id == nid and cards[turn_id][1] is True
    items = [
        {"op": "ADD", "item": 0, "kind": "preference", "subject": "nudges", "text": "", "quote": "stfu",
         "due": "", "end_as": "", "card": 0, "exchanges": [1], "reason": "r"},
        {"op": "INVALIDATE", "item": rule, "kind": "rule", "subject": "", "text": "", "quote": "stfu",
         "due": "", "end_as": "revoked", "card": 0, "exchanges": [1], "reason": "r"},
    ]
    changes, records = worker.validate(items, turns, store.conversation_facts(), cards)
    assert [c.kind for c in changes] == ["feedback"] and changes[0].nudge_id == nid
    assert changes[0].text == 'The user replied to this card: "stfu"'
    assert records[1].status.startswith("rejected: a reply to a card changes only feedback")


def test_a_quote_from_the_card_itself_is_not_the_users_words(store):
    card(store, at("11:07"), "Finish the harness first.", reaction="replied")
    turn_id = store.add_turn("s1", 1, at("11:07") + 17,
                             '[Replying to your nudge (kind: nudge), sent 11:07: "Finish the harness first."]\nok')
    store.flush_turns()
    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids([turn_id])
    items = [{"op": "ADD", "item": 0, "kind": "feedback", "subject": "call-back", "text": "t",
              "quote": "Finish the harness first", "due": "", "end_as": "", "card": 0, "exchanges": [1], "reason": ""}]
    changes, records = worker.validate(items, turns, [], worker.cards_for(turns))
    assert not changes and "not the user's own words" in records[0].status


def test_the_coach_reads_a_card_reply_as_feedback_not_a_rule(store):
    store.add_conversation_fact("rule", "Call me captain.", quote="call me captain", at=at("09:00"))
    card(store, at("11:07"), "Finish the harness first.", reaction="replied", reply="stfu")
    turn_id = store.add_turn("s1", 1, at("11:07") + 17, "[Replying ...]\nstfu")
    store.flush_turns()
    store.add_conversation_fact("preference", "The user does not want nudges about work focus.", quote="stfu",
                                origin="model", source_turn_ids=[turn_id], at=at("11:07") + 17)
    rules, feedback = coach_facts(store, at("12:00"))
    assert [r.quote for r in rules] == ["call me captain"]
    assert len(feedback) == 1 and feedback[0][1].text == "Finish the harness first."
    plain = lambda v, cap: str(v or "")[:cap]
    assert "stfu" not in rules_text(store, plain, at("12:00"))
    assert '"stfu"' in feedback_text(store, plain, at("12:00"))
    assert coach_facts(store, at("10:00"))[1] == []          # not in force before it was said


def test_standing_context_shows_coach_feedback_with_ids_forget_rule_takes(store):
    from yuki.memory.api import MemoryClient

    card(store, at("11:07"), "Finish the harness first.", reaction="replied")
    turn_id = store.add_turn("s1", 1, at("11:07") + 17, "[Replying ...]\nstfu")
    store.flush_turns()
    pref = store.add_conversation_fact("preference", "No nudges about work focus.", quote="stfu", origin="model",
                                       source_turn_ids=[turn_id], at=at("11:07") + 17)
    fb = store.add_conversation_fact("feedback", "Wants to hear from the coach.", quote="no messages from you",
                                     origin="model", at=at("15:37"))
    client = MemoryClient(store)
    text = client.standing_context()
    coach = text.split("What the user said back to your own cards")[1]   # one Yuki (revamp D): no separate coach
    assert f"[rule:{pref}]" in coach and f"[feedback:{fb}]" in coach
    assert client.revoke_rule(f"feedback:{fb}") == fb and client.revoke_rule(f"rule:{pref}") == pref


def test_an_answer_goes_only_onto_the_question_asked(store):
    ids = store.add_questions([
        NewQuestion(at=at("10:00"), kind="person", subject="Elsa", question="Who is Elsa?", evidence="e", reason="r",
                    importance=6, origin="journal"),
        NewQuestion(at=at("10:00"), kind="person", subject="Rohan", question="Who is Rohan?", evidence="e",
                    reason="r", importance=6, origin="journal"),
    ])
    q_open, q_asked = ids[0][0], ids[1][0]
    _cid, nid = store.record_checkin(NudgeCheckin(at=at("18:16"), trigger="question", outcome="question"),
                                     NewNudge(kind="question", text="Who's Rohan?", trigger="question",
                                              ref=f"question:{q_asked}"), question=(q_asked, "asked"))
    store.react_to_nudge(nid, "shown", at=at("18:16"))
    turn_id = store.add_turn("s1", 1, at("18:18"), '[The user may be answering (kind: question)]\nhes a friend from pax')
    store.flush_turns()
    worker = ConversationWorker(store, log_dir=Path(tempfile.gettempdir()))
    turns = store.turns_by_ids([turn_id])
    items = [{"question": q_open, "outcome": "answered", "quote": "hes a friend from pax", "exchange": 1},
             {"question": q_asked, "outcome": "answered", "quote": "hes a friend from pax", "exchange": 1}]
    out, rejected = worker.answers_from(items, turns, worker.cards_for(turns))
    assert [a[0] for a in out] == [q_asked] and rejected == ["not a question that was asked"]


# -- item 4: time at the PC comes from presence -------------------------------------------


def test_sessions_fold_short_hops_into_the_session(store):
    present(store, at("20:00"), at("20:40"), "VALORANT", fullscreen=True)
    present(store, at("20:40"), at("20:44"), "Discord")
    present(store, at("20:44"), at("21:30"), "VALORANT", fullscreen=True)
    present(store, at("21:30"), at("21:38"), "Google Chrome")          # an interlude, folded back
    present(store, at("21:38"), at("22:10"), "VALORANT", fullscreen=True)
    sessions = activity_sessions(store.timeline_states(at("19:00"), at("22:10")), at("19:00"), at("22:10"), CFG)
    assert len(sessions) == 1 and sessions[0].label == "VALORANT"
    assert round(sessions[0].present_s / 60) == 130 and set(sessions[0].hops) == {"Discord", "Google Chrome"}


# -- item 6: the agent's context -------------------------------------------------------------


def test_the_day_begins_when_they_sat_down(store):
    from yuki.memory.api import MemoryClient

    present(store, at("20:00", -1), at("05:00"))
    present(store, at("08:56"), at("12:31"))
    line = MemoryClient(store).day_context(now=at("12:31"))
    assert "08:56" in line and "not since midnight" in line


def test_kal_at_night_counts_from_the_users_day(store):
    from yuki.memory.journal import user_day_line

    present(store, at("16:00", -1), at("04:40"))
    line = user_day_line(store, [at("04:39")])
    assert "Thursday 2026-09-24" in line and "\"tomorrow\" (\"kal\") is Friday 2026-09-25" in line


# -- item 4: the acting marker never stands ----------------------------------------------------


def test_the_acting_marker_expires_and_its_writes_are_retried(tmp_path, monkeypatch):
    db = tmp_path / "memory.db"
    assert acting.begin(db, "tok", "scroll down", lane="worker")
    assert acting.current(db).token == "tok"
    path = acting.acting_path(db)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["requests"][0]["seen_at"] -= acting.MAX_AGE_S + 5
    path.write_text(json.dumps(data), encoding="utf-8")
    assert acting.current(db) is None and acting.stale(db)[0]["token"] == "tok"
    real, fails = os.replace, [2]

    def flaky(a, b):
        if fails[0]:
            fails[0] -= 1
            raise PermissionError("sharing violation")
        return real(a, b)

    monkeypatch.setattr(acting.os, "replace", flaky)
    assert acting.begin(db, "tok2", "click", lane="worker") and acting.current(db).token == "tok2"
