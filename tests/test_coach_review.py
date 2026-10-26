"""Offline checks of the coach and conversation fixes from the design notes

Temp databases, synthetic timelines and stub models only; nothing touches the desktop
or a real model.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory import acting
from yuki.memory.conversations import ConversationWorker, own_words
from yuki.memory.nudges import (
    NudgeConfig,
    NudgeWorker,
    Trigger,
    activity_sessions,
    build_coach_message,
    coach_facts,
    feedback_text,
    observe,
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


def test_a_glance_after_a_long_absence_is_not_being_back(store):
    present(store, at("10:00"), at("02:40", 1) - 86400 + 86400)
    # 15.5 h away, a 20-second glance, then nothing: not "at the PC for 4h"
    store.save_timeline(TimelineRow(None, at("18:12", 1), at("18:12", 1) + 20, "present", "warp", "Warp", active_s=20))
    view = observe(store, at("18:12", 1) + 103, CFG)
    assert view.gate == "settling" and view.at_pc_since == at("18:12", 1)
    assert view.last_break["seconds"] > 15 * 3600


def test_sessions_fold_short_hops_into_the_session(store):
    present(store, at("20:00"), at("20:40"), "VALORANT", fullscreen=True)
    present(store, at("20:40"), at("20:44"), "Discord")
    present(store, at("20:44"), at("21:30"), "VALORANT", fullscreen=True)
    present(store, at("21:30"), at("21:38"), "Google Chrome")          # an interlude, folded back
    present(store, at("21:38"), at("22:10"), "VALORANT", fullscreen=True)
    sessions = activity_sessions(store.timeline_states(at("19:00"), at("22:10")), at("19:00"), at("22:10"), CFG)
    assert len(sessions) == 1 and sessions[0].label == "VALORANT"
    assert round(sessions[0].present_s / 60) == 130 and set(sessions[0].hops) == {"Discord", "Google Chrome"}


# -- item 2: every card rests on evidence; a break nudge once per session ------------------


class Stub:
    """coach_decision / write_card / welcome_back / write_reminder with canned answers."""

    def __init__(self, decision: dict | None = None, card: dict | None = None, reminder: dict | None = None):
        self.calls: list[str] = []
        self.decision = decision or {"say": "none"}
        self.card = card or {"say": "none"}
        self.reminder = reminder or {"say": "remind", "text": "Call Asha about the flat now."}
        self.messages = SimpleNamespace(create=self.create)

    @staticmethod
    def _due(request: dict) -> list[str]:
        """The to-do the request folds into the welcome back ("[todo:3]"), as a real answer would name it."""
        text = str(request["messages"][0]["content"])
        start = text.find("[todo:")
        end = text.find("]", start)
        return [text[start + 1:end]] if start >= 0 and end > start else []

    def create(self, **request):
        name = request["tool_choice"]["name"]
        self.calls.append(name)
        base = {"reason": "stub", "mentions": [], "evidence": []}
        data = {"coach_decision": {**base, "is_break_suggestion": False, "detail": "", **self.decision},
                "write_card": {**base, "text": None, **self.card},
                "write_reminder": {"reason": "stub", "text": "", **self.reminder},
                "welcome_back": {**base, "say": "resume", "detail": "the nudges fix", "evidence": ["NOW"],
                                 "mentions": self._due(request), "text": "The deck for Varun is due."}}.get(name, {})
        block = SimpleNamespace(type="tool_use", name=name, input=data)
        return SimpleNamespace(content=[block], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20,
                                                     cache_creation_input_tokens=0, cache_read_input_tokens=0))


def worker_for(store: Store, client: Stub, **kw) -> NudgeWorker:
    return NudgeWorker(store, config=CFG, client=client, log_dir=Path(tempfile.gettempdir()) / "yuki-coach-tests",
                       locked_fn=lambda: False, media_fn=lambda: [], event_name="Local\\YukiNudgeReadyTest",
                       **{"render_fn": lambda: [], "clock": lambda: at("00:00", -1), **kw})


def ticks(worker: NudgeWorker, start: float, end: float, step: float = 60.0) -> list[dict]:
    out, t = [], start
    while t <= end:
        out += worker.tick(t)
        t += step
    return out


def test_an_ungrounded_decision_never_reaches_the_writer(store):
    present(store, at("09:00"), at("11:30"))
    stub = Stub(decision={"say": "nudge", "is_break_suggestion": True, "detail": "Warp since 9",
                          "evidence": ["J999999"]})
    view = observe(store, at("11:30"), CFG)
    result = worker_for(store, stub).decide(at("11:30"), CFG, view, Trigger("periodic"), [])
    assert result["outcome"] == "suppressed:ungrounded" and stub.calls == ["coach_decision"]


def test_a_call_back_needs_a_drifted_session_and_its_own_content(store):
    present(store, at("09:00"), at("11:30"))
    stub = Stub(decision={"say": "nudge", "is_break_suggestion": False, "detail": "left Warp", "evidence": ["NOW"]})
    view = observe(store, at("11:30"), CFG)
    result = worker_for(store, stub).decide(at("11:30"), CFG, view, Trigger("periodic"), [])
    assert result["outcome"] == "suppressed:no_content" and "write_card" not in stub.calls


def test_one_break_nudge_per_session_written_by_the_writer(store):
    present(store, at("09:00"), at("12:30"))
    stub = Stub(decision={"say": "nudge", "is_break_suggestion": True, "detail": "Warp since 09:00",
                          "evidence": ["BREAK"]},
                card={"say": "nudge", "text": "Warp since 9, it's 11, go stretch.", "evidence": ["BREAK"]})
    worker = worker_for(store, stub)
    done = ticks(worker, at("09:00"), at("12:30"))
    nudges = [d for d in done if d["outcome"] == "nudge"]
    assert len(nudges) == 1 and nudges[0]["text"] == "Warp since 9, it's 11, go stretch."
    sent = store.nudges(kinds=("nudge",))
    assert sent[0].inputs["is_break_suggestion"] and sent[0].inputs["evidence"] == ["BREAK"]
    assert stub.calls.count("write_card") == 1
    assert not any(d["outcome"] == "nudge" for d in done if d is not nudges[0])


# -- items 4 and 5: one card on return; plans from calls are asked, never scheduled -------


def test_the_welcome_back_carries_what_fell_due(store):
    present(store, at("20:00", -1), at("01:00"))
    present(store, at("09:00"), at("09:30"))
    todo = store.add_todo("Send the deck to Varun", due_at=at("08:30"))
    worker = worker_for(store, Stub())
    done = ticks(worker, at("09:00"), at("09:30"))
    kinds = [d["trigger"] for d in done if d.get("nudge_id")]
    assert kinds == ["resume"]                                   # no reminder card of its own
    assert store.reminded(f"todo:{todo}", at("08:30"))


def test_a_plan_said_on_a_call_is_asked_once_and_a_vague_one_never(store):
    present(store, at("08:00"), at("12:00"))
    store.add_conversation_fact("commitment", "Help Elina finish her Claude CLI setup.", quote="kal karte hain",
                                origin="call", due_at=at("15:00"), at=at("04:39"))
    worker = worker_for(store, Stub(reminder={"say": "remind", "text": "You told Elina you'd sort it at 3, "
                                                                         "want a reminder?"}))
    worker.config = lambda: CFG
    store.set_nudge_state("resume_gap", "x")                    # no welcome back in this test
    done = ticks(worker, at("10:00"), at("10:30"))
    asked = [d for d in done if d["trigger"] == "confirm"]
    assert len(asked) == 1 and asked[0]["outcome"] == "reminder"
    n = store.nudges(kinds=("reminder",))[0]
    assert n.trigger == "confirm" and "todo tool" in n.reason
    vague = store.add_conversation_fact("commitment", "Try to do something, possibly Japanese.", origin="call",
                                        due_at=at("18:00"), at=at("09:00"))
    worker2 = worker_for(store, Stub(reminder={"say": "none", "text": ""}))
    done2 = ticks(worker2, at("10:40"), at("11:00"))
    assert [d["outcome"] for d in done2 if d["trigger"] == "confirm"] == ["suppressed:vague"]
    assert store.reminded(f"commit:{vague}", at("18:00"))


def test_a_meeting_is_held_while_its_app_still_plays(store):
    me = os.getpid()
    import psutil

    process = psutil.Process(me).name().lower().removesuffix(".exe")
    store.save_timeline(TimelineRow(None, at("09:00"), at("09:10"), "present", process, "Meet", meeting="Google Meet",
                                    active_s=600))
    present(store, at("09:10"), at("09:25"))
    view = observe(store, at("09:25"), CFG)
    assert view.gate is None
    playing = worker_for(store, Stub(), render_fn=lambda: [{"pid": me, "state": "active"}])
    silent = worker_for(store, Stub(), render_fn=lambda: [])
    assert playing._meeting_held(at("09:25"), view, CFG) and not silent._meeting_held(at("09:25"), view, CFG)


def test_questions_wait_for_real_time_at_the_pc(store):
    present(store, at("09:00"), at("09:12"))
    worker = worker_for(store, Stub())
    view = observe(store, at("09:12"), CFG)
    assert worker._question_budget(at("09:12"), CFG, view) == "pace"
    present(store, at("09:12"), at("09:40"))
    view = observe(store, at("09:40"), CFG)
    assert worker._question_budget(at("09:40"), CFG, view) is None


def test_a_page_where_nothing_changed_is_not_named(store):
    present(store, at("16:40"), at("18:30"), "Google Chrome", step=7000, title="Settings | ElevenLabs",
            host="elevenlabs.io")
    view = observe(store, at("18:29"), CFG)
    text = build_coach_message(store, view, Trigger("periodic"), [], CFG)
    assert "input was going elsewhere" in text and "ElevenLabs" not in text


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


# -- a break nudge only once one is due (break_after_min at the PC), once per session (2026-09-29) -----------


def test_a_break_nudge_waits_for_ninety_minutes_hops_included(store):
    present(store, at("09:00"), at("09:40"), "VALORANT")
    present(store, at("09:40"), at("09:43"), "Discord")          # a hop: part of the gaming session
    present(store, at("09:43"), at("10:40"), "VALORANT")
    stub = Stub(decision={"say": "nudge", "is_break_suggestion": True, "detail": "VALORANT since 09:00",
                          "evidence": ["BREAK"]},
                card={"say": "nudge", "text": "VALORANT since 9, go stretch.", "evidence": ["BREAK"]})
    worker = worker_for(store, stub)
    early = worker.decide(at("10:00"), CFG, observe(store, at("10:00"), CFG), Trigger("periodic"), [])
    assert early["outcome"] == "suppressed:break_too_soon" and "write_card" not in stub.calls
    assert "no break nudge before 10:30" in stub_request_text(early)
    late = worker.decide(at("10:35"), CFG, observe(store, at("10:35"), CFG), Trigger("periodic"), [])
    assert late["outcome"] == "nudge"                             # 95 min with the Discord hop folded in


def stub_request_text(result: dict) -> str:
    return str(result["requests"][0]["messages"][0]["content"])


def test_no_look_for_a_break_before_one_is_due_and_one_per_session(store):
    present(store, at("09:00"), at("12:00"))
    stub = Stub(decision={"say": "nudge", "is_break_suggestion": True, "detail": "Warp since 09:00",
                          "evidence": ["BREAK"]},
                card={"say": "nudge", "text": "Warp since 9, go stretch.", "evidence": ["BREAK"]})
    worker = worker_for(store, stub)
    done = ticks(worker, at("09:00"), at("10:29"))
    assert "coach_decision" not in stub.calls                     # nothing to say before 90 min: no coach look
    done = ticks(worker, at("10:30"), at("12:00"))
    assert [d["outcome"] for d in done if d["outcome"] == "nudge"] == ["nudge"]
    first = store.nudges(kinds=("nudge",))[0]
    assert first.at >= at("10:30")
    # a drift look in the same session never brings a second break nudge
    view = observe(store, at("11:50"), CFG)
    session = activity_sessions(store.timeline_states(at("09:00"), at("11:50")), at("09:00"), at("11:50"), CFG)[-1]
    again = worker.decide(at("11:50"), CFG, view, Trigger("drift", session=session), [])
    assert again["outcome"] == "suppressed:break_again"
