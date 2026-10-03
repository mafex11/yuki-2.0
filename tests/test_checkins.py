"""Offline checks of Yuki's proactive check-ins (yuki.memory.checkins): the goals table and API (migration 30), each
measured trigger, the writer's checks, the worth-asking vetting (the Arch question dropped), budgets and learning,
the fold-in of answers into goals, expiry, and the one-Yuki wording of the fixed strings Yuki writes.

Temp databases, synthetic timelines and stub models only; nothing touches the desktop or a real model.
"""

from __future__ import annotations

import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from yuki.memory import checkins as C
from yuki.memory import questions as Q
from yuki.memory.api import MemoryClient
from yuki.memory.nudges import NudgeConfig, NudgeWorker, observe
from yuki.memory.store import NewNudge, NewQuestion, NudgeCheckin, Store, TimelineRow, WorkOp

DAY = datetime(2026, 9, 28)
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


class Stub:
    """A model client: write_checkin answers ``card``, vet_questions ``vet``, fold_answer ``fold``."""

    def __init__(self, card: dict | None = None, vet: list | None = None, fold: dict | None = None):
        self.card = card or {}
        self.vet = vet if vet is not None else [{"n": 1, "decision": "keep", "refs": [], "why": "worth it",
                                                 "question": None}]
        self.fold = fold or {}
        self.calls: list[str] = []
        self.requests: list[dict] = []
        self.messages = SimpleNamespace(create=self.create)

    def create(self, **request):
        name = request["tool_choice"]["name"]
        self.calls.append(name)
        self.requests.append(request)
        data = {
            "write_checkin": {"reason": "stub", "say": "none", "text": None, "question": None, "subject": "",
                              "goal": "none", "goal_text": "", "goal_scope": "", "goal_thread": "", "evidence": [],
                              "connects": "", "offer": None, **self.card},
            "vet_questions": {"decisions": self.vet},
            "fold_answer": {"reason": "stub", "tone": "neutral", "ops": [], **self.fold},
            "welcome_back": {"reason": "stub", "say": "none", "detail": "", "text": None, "evidence": [],
                             "mentions": [], "offer": None},
        }.get(name, {})
        return SimpleNamespace(content=[SimpleNamespace(type="tool_use", name=name, input=data)], stop_reason="tool_use",
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20, cache_creation_input_tokens=0,
                                                     cache_read_input_tokens=0))


def worker_for(store: Store, stub: Stub, config: NudgeConfig = CFG) -> NudgeWorker:
    return NudgeWorker(store, config=config, client=stub, log_dir=Path(tempfile.gettempdir()) / "yuki-checkin-tests",
                       locked_fn=lambda: False, media_fn=lambda: [], render_fn=lambda: [],
                       event_name="Local\\YukiNudgeReadyTest", clock=lambda: at("21:00"))


def present(store: Store, start: float, end: float, app: str = "T3 Code") -> None:
    t = start
    while t < end:
        e = min(end, t + 300.0)
        store.save_timeline(TimelineRow(None, t, e, "present", app.lower(), app, active_s=e - t))
        t = e


def fact(store: Store, when: float, text: str, *, thread: int | None = None, milestone: bool = False) -> int:
    fid = store.add_journal(when, None, "Google Chrome", text, 6)
    with store._lock:
        store._conn.execute("UPDATE journal SET created_at=?, milestone=?, work_thread_id=? WHERE id=?",
                            (when + 60.0, int(milestone), thread, fid))
    return fid


def work(store: Store, title: str, start: float, touches=(), open_items=("build the booth link cache",)) -> int:
    op = WorkOp("starts", None, title=title, state="half done", open_items=list(open_items), at=start)
    store.apply_work([op])
    for t in touches:
        store.apply_work([WorkOp("continues", op.result_id, at=t)])
    return op.result_id


def ask(store: Store, when: float, moment: str = "followup", goal: dict | None = None, text: str = "Done yet?",
        trigger: str = "checkin") -> tuple[int, int, int | None]:
    _c, nid, qid, gid = store.record_checkin_question(
        NudgeCheckin(at=when, trigger=f"checkin_{moment}", outcome="question", model="m"),
        NewNudge(kind="question", text=text, trigger=trigger),
        NewQuestion(at=when, kind="fact", subject="booth link cache", question="Did the user finish it?",
                    evidence="e", reason="r", importance=7, origin="checkin"),
        moment=moment, goal=goal)
    return nid, qid, gid


# -- the goals table and API (migration 30) ------------------------------------------------------------


def test_migration_30_adds_goals_and_the_check_in_columns(store):
    assert store._conn.execute("PRAGMA user_version").fetchone()[0] >= 30
    gid = store.add_goal("day", "finish the booth link cache", source="answer", day="2026-09-28", status="open",
                         words="finish the booth link cache", source_ref="question:4", evidence=["question:4"],
                         at=at("11:00"))
    g = store.goal(gid)
    assert (g.scope, g.status, g.source, g.ref, g.evidence) == ("day", "open", "answer", f"G{gid}", ["question:4"])
    assert store.update_goal(gid, type="answered", status="done", at=at("12:00"), evidence=["J5"], question_id=4)
    g = store.goal(gid)
    assert g.status == "done" and g.ended_at == at("12:00") and g.evidence == ["question:4", "J5"]
    assert [e["type"] for e in store.goal_events(gid)] == ["created", "answered"]
    assert store.goals(("open",)) == [] and len(store.goals(None, scope="day", day="2026-09-28")) == 1
    with pytest.raises(ValueError):
        store.add_goal("week", "x", source="answer")          # a scope that is not one
    assert not store.update_goal(999, type="answered")         # no such goal


def test_the_goals_api_and_todays_goals_in_the_agents_context(store):
    client = MemoryClient(store)
    today = C.user_day(time.time())
    gid = store.add_goal("day", "finish the booth link cache", source="answer", day=today, status="open",
                         words="finishing the booth link cache")
    wid = work(store, "Operator demo", time.time() - 3600)
    store.add_goal("thread", "ship the operator demo for the 9th", source="thread", work_thread_id=wid)
    rows = client.goals(today=True)
    assert [r["id"] for r in rows] == [f"G{gid}"] and rows[0]["today"] and rows[0]["words"]
    assert {r["scope"] for r in client.goals()} == {"day", "thread"}
    parts = client.standing_parts()
    lines = [i["line"] for i in parts["groups"]["goal"]]
    assert any(f"[G{gid}] today" in ln for ln in lines) and any("Operator demo" in ln for ln in lines)
    assert "unknown: inferred" in " ".join(lines)              # an inferred goal says it is not confirmed
    # the agent's commitments block renders every non-rule group: the goals reach it there
    from yuki.memory.api import STANDING_GROUPS, STANDING_RULE_GROUPS, render_standing

    body = render_standing(parts, tuple(g for g in STANDING_GROUPS if g not in STANDING_RULE_GROUPS))
    assert "The user's goals" in body and f"[G{gid}]" in body
    # the one-string context (the voice talker's split_standing) does not carry them
    assert f"[G{gid}]" not in client.standing_context()


# -- one check-in, stored in one transaction; the answer paths ---------------------------------------------


def test_a_check_in_is_a_question_card_with_its_goal_and_never_queued(store):
    wid = work(store, "Operator demo", at("09:00"))
    nid, qid, gid = ask(store, at("10:00"), goal={"new": {"scope": "thread", "text": "ship the operator demo",
                                                          "source": "thread", "work_thread_id": wid,
                                                          "source_ref": f"W{wid}", "evidence": [f"W{wid}"]}})
    q, n, g = store.question(qid), store.nudge(nid), store.goal(gid)
    assert (q.origin, q.status, q.moment, q.goal_id, q.nudge_id) == ("checkin", "asked", "followup", gid, nid)
    assert (n.kind, n.trigger, n.ref) == ("question", "checkin", f"question:{qid}")
    assert n.inputs["question_id"] == qid and n.inputs["goal_id"] == gid
    assert (g.status, g.checkins, g.last_checkin_at, g.work_thread_id) == ("unknown", 1, at("10:00"), wid)
    assert [e["type"] for e in store.goal_events(gid)] == ["created", "checked_in"]
    store.add_questions([NewQuestion(at=at("10:00"), kind="person", subject="Bro", question="Who is Bro?",
                                     evidence="", reason="", importance=6, origin="journal")])
    assert store.next_question().subject == "Bro"              # the getting-to-know-you queue never holds a check-in
    # the same goal is checked on again later: a check-in is never deduped away
    _n2, qid2, gid2 = ask(store, at("11:00"), goal={"id": gid})
    assert qid2 != qid and gid2 == gid and store.goal(gid).checkins == 2


def test_an_answer_to_a_check_in_waits_for_the_fold_not_the_portrait(store):
    nid, qid, _g = ask(store, at("10:00"))
    client = MemoryClient(store)
    out = client.answer_question(f"question:{qid}", "finishing the booth link cache today", "answered")
    assert out["saved"] and out["correction_id"] is None
    q = store.question(qid)
    assert q.status == "answered" and q.answer_fact_id is None and q.folded_at is None
    assert [x.id for x in store.checkin_answers_pending()] == [qid]
    from yuki.memory.store import CORRECTION_KIND

    assert not [f for f in store.portrait_facts(include_corrections=True) if f.kind == CORRECTION_KIND]


def test_the_fold_turns_an_answer_into_goals_checked_in_code(store):
    wid = work(store, "Story Editor short replies", at("09:00", -1))
    gid = store.add_goal("thread", "short replies instead of paragraphs", source="thread", work_thread_id=wid)
    _n, qid, _g = ask(store, at("10:00"), goal={"id": gid})
    MemoryClient(store).answer_question(f"question:{qid}", "yeah it shipped last night. today im on the booth cache",
                                        "answered")
    stub = Stub(fold={"tone": "welcome", "ops": [
        {"op": "update", "goal": f"G{gid}", "scope": "", "day": "", "thread": "", "text": "",
         "quote": "yeah it shipped last night", "status": "done", "next_checkin": "", "due": ""},
        {"op": "add", "goal": "", "scope": "day", "day": "2026-09-28", "thread": "", "text": "the booth cache",
         "quote": "today im on the booth cache", "status": "open", "next_checkin": "", "due": ""},
        {"op": "add", "goal": "", "scope": "day", "day": "2026-09-28", "thread": "", "text": "invented",
         "quote": "words they never said", "status": "open", "next_checkin": "", "due": ""},
    ]})
    w = worker_for(store, stub)
    out = w.checkins.fold(at("10:05"), CFG)
    assert stub.calls == ["fold_answer"] and out[0]["outcome"] == "folded:2"
    assert "the quote is not the user's words" in " ".join(out[0]["rejected"])
    assert store.goal(gid).status == "done"
    day = [g for g in store.goals(None, scope="day")]
    assert len(day) == 1 and day[0].text == "the booth cache" and day[0].words == "today im on the booth cache"
    assert day[0].source == "answer" and day[0].source_ref == f"question:{qid}" and day[0].status == "open"
    q = store.question(qid)
    assert q.folded_at == at("10:05") and q.reply_tone == "welcome"
    assert store.checkin_answers_pending() == []
    assert "THE USER'S ANSWER" in stub.requests[0]["messages"][0]["content"]


def test_skip_and_dont_ask_need_no_model_and_dont_ask_stops_that_goal(store):
    gid = store.add_goal("thread", "the Arch dual-boot", source="thread")
    _n, q1, _ = ask(store, at("10:00"), goal={"id": gid})
    _n, q2, _ = ask(store, at("11:00"), goal={"id": gid})
    client = MemoryClient(store)
    client.skip_question(f"question:{q1}")
    client.skip_question(f"question:{q2}", never=True)
    stub = Stub()
    w = worker_for(store, stub)
    out = w.checkins.fold(at("11:05"), CFG)
    assert stub.calls == [] and sorted(o["outcome"] for o in out) == ["never", "skipped"]
    assert store.question(q2).reply_tone == "never" and store.goal(gid).next_checkin_at is None
    assert "never" in [e["type"] for e in store.goal_events(gid)]


# -- the measured triggers -------------------------------------------------------------------------------


def test_day_start_after_their_night_settles_and_waits_after_the_welcome_back(store):
    present(store, at("22:00", -1), at("23:30", -1))
    present(store, at("10:00"), at("10:20"))
    w = worker_for(store, Stub())
    view = observe(store, at("10:05"), CFG)
    assert w.checkins._day_start(at("10:05"), CFG, view, []) is None            # not settled 10 minutes yet
    m = w.checkins._day_start(at("10:20"), CFG, observe(store, at("10:20"), CFG), [])
    assert m is not None and m.kind == "day_start" and m.frame == "present" and "DAY" in m.required
    assert "after 10h30m away" in m.lines[0]
    # a welcome back card just went out: the check-in waits a little and then reads it
    store.record_checkin(NudgeCheckin(at=at("10:12"), trigger="resume", outcome="resume", model="m"),
                         NewNudge(kind="resume", text="You were on the booth cache last night.", trigger="resume"))
    assert w.checkins._day_start(at("10:20"), CFG, observe(store, at("10:20"), CFG), []) is None
    present(store, at("10:20"), at("10:40"))
    m = w.checkins._day_start(at("10:40"), CFG, observe(store, at("10:40"), CFG), [])
    assert m is not None and any("THE WELCOME BACK" in ln for ln in m.lines)
    # three hours after sitting down it is no longer the first sit-down
    present(store, at("10:40"), at("13:30"))
    assert w.checkins._day_start(at("13:30"), CFG, observe(store, at("13:30"), CFG), []) is None


def test_followup_when_something_landed_on_their_work(store):
    wid = work(store, "Story Editor rebuild", at("09:00", -1), touches=[at(f"{h}:00", -1) for h in range(10, 16)])
    other = fact(store, at("15:00", -1), "The user tested the reply streaming.", thread=wid)
    merged = fact(store, at("09:30"), "The user merged PR #2872 into dev.", thread=wid, milestone=True)
    w = worker_for(store, Stub())
    m = w.checkins._followup(at("10:00"), CFG, None, [])
    assert m is not None and m.kind == "followup" and m.meta["because"] == "landed" and m.thread_id == wid
    assert {f"W{wid}", f"J{merged}"} <= m.required and f"J{other}" in m.ids
    # after a check-in about that thread, the same landing is not asked about again
    ask(store, at("10:00"))
    with store._lock:   # the card names the thread it was about (as decide() writes it)
        import json

        n = store.nudges()[-1]
        store._conn.execute("UPDATE nudges SET inputs_ciphertext=? WHERE id=?",
                            (store.cipher.encrypt(json.dumps({**n.inputs, "thread_id": wid})), n.id))
    assert w.checkins._followup(at("10:30"), CFG, None, []) is None


def test_followup_when_a_burst_of_work_went_quiet(store):
    burst = [at("10:00", -4) + i * 3 * 3600.0 for i in range(9)]
    wid = work(store, "Kite chart extension", burst[0], touches=burst[1:])
    w = worker_for(store, Stub())
    t = store.work_thread(wid)
    quiet_for = at("12:00") - t.last_seen
    assert quiet_for >= C.QUIET_MIN_S
    m = w.checkins._followup(at("12:00"), CFG, None, [])
    assert m is not None and m.meta["because"] == "quiet" and "9 touches" in m.trigger
    # quiet for less than QUIET_MIN_S: not yet
    assert w.checkins._followup(t.last_seen + C.QUIET_MIN_S - 3600.0, CFG, None, []) is None


def test_followup_when_the_check_in_time_they_gave_came_and_when_a_named_day_passed(store):
    gid = store.add_goal("project", "finish the GRE prep", source="answer", status="open",
                         next_checkin_at=at("09:00"))
    w = worker_for(store, Stub())
    m = w.checkins._followup(at("10:00"), CFG, None, [])
    assert m is not None and m.meta["because"] == "next" and m.goal_id == gid and f"G{gid}" in m.required
    store.update_goal(gid, type="answered", next_checkin_at=None)
    tid = store.add_todo("send Varun the deck", due_at=at("08:00"))
    m = w.checkins._followup(at("10:00"), CFG, None, [])
    assert m is not None and m.meta["because"] == "due" and f"todo:{tid}" in m.required


def test_evening_window_and_moment_from_measured_hours(store):
    night_owl = 23 * 3600.0   # their days end at 03:00 (23 h after a 04:00 day start)
    opens, closes = C.evening_window(at("20:00"), CFG, night_owl)
    assert (opens, closes) == (at("20:30"), at("00:30", 1))   # before quiet hours, never inside them
    early = 16 * 3600.0       # their days end at 20:00
    opens, closes = C.evening_window(at("18:00"), CFG, early)
    assert (opens, closes) == (at("19:00"), at("21:00"))
    present(store, at("12:00"), at("16:00"))
    present(store, at("21:00"), at("22:00"))
    w = worker_for(store, Stub())
    m = w.checkins._evening(at("22:00"), CFG, None, [])
    assert m is not None and m.kind == "evening" and m.frame == "future" and "DAY" in m.required
    assert w.checkins._evening(at("18:00"), CFG, None, []) is None            # too early
    w.checkins._totals = None
    store2_cfg = NudgeConfig.from_dict({})
    assert C.typical_end([], "2026-09-28", store2_cfg.resume_day_start) is None


def test_care_from_long_days_and_late_nights(store):
    totals = [C.DayTotals(f"2026-09-{d}", present_s=11 * 3600.0, last=C.day_boundary(f"2026-09-{d}") + 22 * 3600.0)
              for d in (25, 26, 27)]
    totals.append(C.DayTotals("2026-09-28", present_s=4 * 3600.0, last=at("20:00")))
    sig = C.care_signal(totals, "2026-09-28", "04:00")
    assert sig is not None and sig["streak"] == 3 and "3 days in a row" in sig["why"][0]
    late = [C.DayTotals(f"2026-09-{d}", present_s=6 * 3600.0, last=C.day_boundary(f"2026-09-{d}") + 23.5 * 3600.0)
            for d in (24, 25, 26, 27)]
    sig = C.care_signal(late + [C.DayTotals("2026-09-28", present_s=3600.0)], "2026-09-28", "04:00")
    assert sig is not None and sig["late_nights"] == 4
    calm = [C.DayTotals(f"2026-09-{d}", present_s=6 * 3600.0, last=C.day_boundary(f"2026-09-{d}") + 18 * 3600.0)
            for d in (25, 26, 27)]
    assert C.care_signal(calm, "2026-09-28", "04:00") is None
    # end to end on the timeline: three 10.5-hour days, then the evening of the fourth
    for d in (-3, -2, -1):
        present(store, at("10:00", d), at("20:30", d))
    present(store, at("12:00"), at("16:00"))
    present(store, at("21:30"), at("22:00"))
    m = worker_for(store, Stub()).checkins._care(at("22:00"), CFG, None, [])
    assert m is not None and m.kind == "care" and "HOURS" in m.required and m.meta["care"]["streak"] == 3


def test_new_thread_needs_real_work_and_no_goal(store):
    start = at("09:00")
    wid = work(store, "Arch dual-boot", start, touches=[start + i * 900.0 for i in range(1, 6)])
    w = worker_for(store, Stub())
    m = w.checkins._new_thread(at("11:00"), CFG, None, [])
    assert m is not None and m.thread_id == wid and f"W{wid}" in m.required
    small = work(store, "Wordle streak", at("10:00"))
    assert all(x.thread_id != small for x in [w.checkins._new_thread(at("11:00"), CFG, None, [])])
    store.add_goal("thread", "daily driver Arch", source="answer", work_thread_id=wid, status="open")
    assert w.checkins._new_thread(at("11:00"), CFG, None, []) is None


# -- the writer's checks and the vetting ------------------------------------------------------------------


def _landed(store: Store) -> tuple[int, int]:
    wid = work(store, "Story Editor rebuild", at("09:00", -1), touches=[at(f"{h}:00", -1) for h in range(10, 16)])
    jid = fact(store, at("09:30"), "The user merged PR #2872 into dev.", thread=wid, milestone=True)
    present(store, at("09:00"), at("10:00"))
    return wid, jid


def _moment(store: Store, w: NudgeWorker) -> C.Moment:
    m = w.checkins._followup(at("10:00"), CFG, None, [])
    assert m is not None
    return m


def test_the_writers_answer_is_checked_on_ids_never_words(store):
    wid, jid = _landed(store)
    base = {"say": "ask", "question": "Did the user's Story Editor rebuild land?", "subject": "Story Editor rebuild",
            "connects": "PR #2872 merged after a day of work"}
    cases = [
        ({**base, "text": "Rebuild done?", "evidence": ["J999999"]}, "suppressed:ungrounded"),
        ({**base, "text": "Rebuild done?", "evidence": ["NOW"]}, "suppressed:no_content"),
        ({**base, "text": f"Is W{wid} done now?", "evidence": [f"W{wid}"]}, "suppressed:id_in_text"),
        ({**base, "text": "Rebuild done?", "evidence": [f"J{jid}"], "goal": "G77"}, "suppressed:unknown_goal"),
        ({**base, "text": "x" * 400, "evidence": [f"J{jid}"]}, "suppressed:too_long"),
        ({"say": "none"}, "none"),
    ]
    for card, outcome in cases:
        stub = Stub(card)
        w = worker_for(store, stub)
        view = observe(store, at("10:00"), CFG)
        out = w.checkins.decide(at("10:00"), CFG, view, [], _moment(store, w),
                                NudgeCheckin(at=at("10:00"), trigger="checkin_followup", outcome="error"))
        assert out["outcome"] == outcome and stub.calls == ["write_checkin"], (card, out["outcome"])


def test_a_grounded_check_in_passes_the_vetting_and_is_stored_with_a_new_goal(store):
    wid, jid = _landed(store)
    offer = {"do": "todo_add", "label": "Add to list", "steps": ["add 'Test reply streaming on beta' to your list"],
             "refs": [f"W{wid}"], "text": "Test reply streaming on beta", "due": "2026-09-29"}
    stub = Stub({"say": "ask", "text": "PR #2872 is in - is the Story Editor rebuild done, or still going?",
                 "question": "Is the user's Story Editor rebuild done?", "subject": "Story Editor rebuild",
                 "goal": "new", "goal_text": "the Story Editor rebuild", "goal_scope": "thread",
                 "goal_thread": f"W{wid}", "evidence": [f"W{wid}", f"J{jid}"],
                 "connects": "a merge after a day of rebuild work", "offer": offer})
    w = worker_for(store, stub)
    result = w.checkins.look(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w))
    assert result["outcome"] == "question" and stub.calls == ["write_checkin", "vet_questions"]
    n, q, g = store.nudge(result["nudge_id"]), store.question(result["question_id"]), store.goal(result["goal_id"])
    assert n.kind == "question" and n.trigger == "checkin" and n.inputs["moment"] == "followup"
    assert n.inputs["offer"]["do"] == "todo_add" and "run by code, never an agent" in n.reason
    assert n.reason.startswith("Your own check-in") and "coach" not in n.reason
    assert q.origin == "checkin" and q.moment == "followup" and q.goal_id == g.id
    assert (g.scope, g.status, g.source, g.work_thread_id) == ("thread", "unknown", "thread", wid)
    vet_msg = stub.requests[1]["messages"][0]["content"]
    assert "FROM: a check-in you want to ask now - past" in vet_msg
    assert w.checkins._looked(result["key"], at("10:01"))


def test_the_arch_question_is_dropped_as_not_worth_asking(store):
    wid, jid = _landed(store)
    arch = fact(store, at("09:40"), "T3 Code asked the user where on C: Arch should go before planning the dual-boot.")
    with store._lock:
        store._conn.execute("UPDATE journal SET tainted=1 WHERE id=?", (arch,))
    stub = Stub({"say": "ask", "text": "Where on the C: drive do you want Arch installed?",
                 "question": "Where on the C: drive does the user want Arch installed?", "subject": "Arch install",
                 "evidence": [f"J{jid}"], "connects": "T3 Code asked it"},
                vet=[{"n": 1, "decision": "not_worth", "refs": [],
                      "why": "relays T3 Code's question; operational trivia", "question": None}])
    w = worker_for(store, stub)
    result = w.checkins.look(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w))
    assert result["outcome"] == "suppressed:vet_not_worth" and result["nudge_id"] is None
    assert store.checkin_questions() == []
    # the vetting's own prompt carries the Arch question as its worked example of not_worth (model judgement)
    assert "Where on the C: drive does the user want Arch Linux installed?" in Q.VET_SYSTEM_PROMPT
    assert "not_worth" in Q.VET_TOOL["input_schema"]["properties"]["decisions"]["items"]["properties"]["decision"][
        "enum"]
    # and the same test runs on the getting-to-know-you queue: not_worth needs no memory item, and drops it
    out = Q.parse_vetting({"decisions": [{"n": 1, "decision": "not_worth", "refs": [], "why": "relay"}]}, 1, {})
    assert out[0].decision == "not_worth"
    # the worth test is its own field, answered first: a "not worth it" never ends in keep (seen once on the copy)
    out = Q.parse_vetting({"decisions": [{"n": 1, "worth_asking": False, "decision": "keep", "refs": [],
                                          "why": "fails test 3"},
                                         {"n": 2, "worth_asking": True, "decision": "keep", "refs": [], "why": "ok"}]},
                          2, {})
    assert [v.decision for v in out] == ["not_worth", "keep"]
    (qid, _o), = store.add_questions([NewQuestion(
        at=at("09:40"), kind="fact", subject="Arch install location", origin="journal", importance=7,
        question="Where on the C: drive does the user want Arch Linux installed?", source_ids=[arch],
        evidence="T3 Code asked the user", reason="")])
    revet_stub = Stub(vet=[{"n": 1, "decision": "not_worth", "refs": [], "why": "relays T3 Code", "question": None}])
    summary = Q.revet(store, client=revet_stub)
    assert summary["not_worth"] == 1 and store.question(qid).status == "dropped"
    sent = revet_stub.requests[0]["messages"][0]["content"]
    assert "FROM: what your memory read on the user's screen" in sent
    assert "text another AI tool wrote or was given" in sent and f"[J{arch}]" in sent


def test_the_journals_question_kinds_leave_out_fact():
    schema = Q.question_item_schema("source", Q.SCREEN_QUESTION_KINDS)
    assert "fact" not in schema["properties"]["kind"]["enum"]
    assert "fact" in Q.question_item_schema("exchange")["properties"]["kind"]["enum"]   # conversations keep it


# -- budgets and learning --------------------------------------------------------------------------------


def test_one_question_card_waits_at_a_time_for_check_ins_and_questions(store):
    present(store, at("09:00"), at("12:00"))
    nid, qid, _g = ask(store, at("10:00"))
    store.react_to_nudge(nid, "shown", at=at("10:00"))
    w = worker_for(store, Stub())
    view = observe(store, at("12:00"), CFG)
    assert C.open_question(store, at("12:00")).id == qid
    assert w.checkins.held(at("12:00"), CFG, view) == "waiting"
    assert w._question_budget(at("12:00"), CFG, view) == "waiting"
    store.react_to_nudge(nid, "dismissed", at=at("12:01"))     # Not now: it no longer waits
    assert C.open_question(store, at("12:02")) is None
    # an unanswered check-in expires after CHECKIN_TTL_S
    _n2, q2, _ = ask(store, at("12:30"))
    assert store.expire_checkins(at("12:30") + C.CHECKIN_TTL_S + 1) == 2 and store.question(q2).status == "expired"


def test_day_cap_and_one_check_in_per_time_frame(store):
    present(store, at("09:00"), at("21:00"))
    for h, m in (("10:00", "day_start"), ("13:00", "followup"), ("17:00", "evening")):
        nid, qid, _ = ask(store, at(h), moment=m)
        MemoryClient(store).answer_question(f"question:{qid}", "ok", "answered")
    w = worker_for(store, Stub())
    assert w.checkins.held(at("21:00"), CFG, observe(store, at("21:00"), CFG)) == "day"
    more = NudgeConfig.from_dict({"checkins_per_day": 5})
    w5 = worker_for(store, Stub(), more)
    assert w5.checkins.held(at("21:00"), more, observe(store, at("21:00"), more)) is None
    # every frame was used today: nothing is even looked at
    assert w5.checkins.tick(at("21:00"), more, observe(store, at("21:00"), more), []) is None
    assert w5.client.calls == []


def test_learning_a_why_are_you_asking_lowers_that_kinds_budget(store):
    assert C.spacing_hours("day_start", 0.0) == 18.0
    _n, q1, _ = ask(store, at("10:00", -2), moment="day_start")
    MemoryClient(store).answer_question(f"question:{q1}", "why are you even asking", "answered")
    store.mark_checkin_folded(q1, "unwelcome")
    scores = C.checkin_scores(store, at("12:00"))
    assert scores == {"day_start": -2.0} and C.spacing_hours("day_start", -2.0) == 72.0
    _n, q2, _ = ask(store, at("10:00", -1), moment="day_start")
    MemoryClient(store).skip_question(f"question:{q2}", never=True)
    assert C.checkin_scores(store, at("12:00"))["day_start"] == -4.0 and C.spacing_hours("day_start", -4.0) is None
    _n, q3, _ = ask(store, at("10:00", -1), moment="evening")
    MemoryClient(store).answer_question(f"question:{q3}", "tomorrow the deck", "answered")
    store.mark_checkin_folded(q3, "welcome")
    assert C.checkin_scores(store, at("12:00"))["evening"] == 1.5     # only that kind moves
    lines = C.checkin_feedback_lines(store, at("12:00"))
    assert any("first sit-down" in ln and "rests" in ln for ln in lines)
    assert any("end of their day" in ln and "fine to ask" in ln for ln in lines)
    # a resting kind is never looked at
    present(store, at("10:00"), at("10:30"))
    present(store, at("22:00", -1), at("23:00", -1))
    w = worker_for(store, Stub())
    view = observe(store, at("10:30"), CFG)
    assert w.checkins.tick(at("10:30"), CFG, view, []) is None and w.client.calls == []


def test_the_attention_framework_knows_the_checkin_channel():
    from yuki.core.attention import DEFAULT_RULES

    rule = DEFAULT_RULES["checkin"]
    assert rule.card and rule.budget == 3 and rule.window_s == 86400.0 and rule.quiet and rule.fullscreen


def test_check_in_triggers_are_not_looks_at_the_session():
    from yuki.memory.nudges import CHECKIN_TRIGGERS

    assert set(CHECKIN_TRIGGERS) == set(C.CHECKIN_TRIGGERS)


# -- end to end through the card worker's tick ------------------------------------------------------------


def test_a_tick_asks_at_the_first_sit_down_and_never_during_a_call(store):
    present(store, at("22:00", -1), at("23:30", -1))
    present(store, at("10:00"), at("10:25"))
    stub = Stub({"say": "ask", "text": "What are you going for today?",
                 "question": "What is the user going for today?", "subject": "today's plan", "goal": "none",
                 "evidence": ["DAY"], "connects": "first sit-down after the night, no plan for today yet"})
    w = worker_for(store, stub)
    w.tick(at("10:20"))                                          # the welcome back is decided first (says none)
    done = w.tick(at("10:25"))
    asked = [d for d in done if d.get("moment") == "day_start"]
    assert asked and asked[0]["outcome"] == "question" and asked[0]["frame"] == "present"
    n = store.nudge(asked[0]["nudge_id"])
    assert n.kind == "question" and n.text == "What are you going for today?"
    assert "write_checkin" in stub.calls and "vet_questions" in stub.calls
    # the same moment is never asked twice
    present(store, at("10:25"), at("10:40"))
    assert not [d for d in w.tick(at("10:40")) if d.get("moment")]


def test_no_check_in_while_a_call_is_open(store):
    present(store, at("22:00", -1), at("23:30", -1))
    present(store, at("10:00"), at("10:25"))
    store.start_call(app="Discord", process="discord", via="mic", at=at("10:10"), status="recording")
    stub = Stub({"say": "ask", "text": "What are you going for today?", "question": "q", "evidence": ["DAY"]})
    w = worker_for(store, stub)
    assert w.checkins.held(at("10:25"), CFG, observe(store, at("10:25"), CFG)) == "call"


# -- one Yuki: the fixed strings Yuki writes --------------------------------------------------------------


def test_one_yuki_wording_in_the_prompts_and_the_card_context():
    from yuki.memory import nudges as N
    from yuki.memory import presence as P
    from yuki.ui.nudges import coach_note

    prompts = {"coach": N.SYSTEM_PROMPT, "writer": N.WRITER_PROMPT, "reminder": N.REMINDER_SYSTEM_PROMPT,
               "resume": N.RESUME_SYSTEM_PROMPT, "observe": P.OBSERVE_PROMPT, "notice": P.NOTICE_WRITER_PROMPT,
               "events": P.EVENTS_PROMPT, "events_vet": P.EVENTS_VET_PROMPT, "ask": Q.ASK_SYSTEM_PROMPT,
               "vet": Q.VET_SYSTEM_PROMPT, "checkin": C.CHECKIN_PROMPT, "fold": C.FOLD_PROMPT}
    for name, text in prompts.items():
        assert text.startswith("You are Yuki"), name
        assert "Yuki's coach" not in text and "judgement half" not in text and "COACH FEEDBACK" not in text, name
    line = coach_note([{"text": "go stretch", "at": None, "kind": "nudge", "reason": "90 min in"}])
    assert line.startswith("[You said on a card (nudge): \"go stretch\"") and "coach" not in line


def test_card_feedback_and_the_agents_state_lines_speak_as_yuki(store):
    from yuki.memory.nudges import feedback_text

    text = feedback_text(store, lambda v, n: str(v)[:n])
    assert text.startswith("CARD FEEDBACK - what the user said back to your cards")
    client = MemoryClient(store)
    store.set_nudge_state("quiet_until", str(time.time() + 3600))
    assert "Your cards are snoozed" in client.coach_context() and "coach" not in client.coach_context().casefold()
