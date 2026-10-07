"""capC6 (report 78 §12 C6; report 101): the Work page and the goal check-ins cite checked outcomes as evidence.

Where the Work page shows what got done (Worked on today) and where a check-in looks at what the user is going for,
a task whose outcome the PC checked may be cited ("you saved a file (.png) at 4 PM"): only learnable tasks (never
unclear, never one the user rejected), the real task and action ids kept internally, plain words to the user with no
ids, never a new card type, never spoken. Without tasks both behave exactly as before.

Offline: temp databases, invented apps, names and goals; stub models.
"""

from __future__ import annotations

from typing import Any

from test_capC2_families import task
from test_capC2_links import act  # noqa: F401
from test_checkins import CFG, Stub, _landed, _moment, at, fact, store, worker_for  # noqa: F401 - the store fixture
from yuki.memory import checkins as C
from yuki.memory import trace_tasks as T
from yuki.memory.api import MemoryClient
from yuki.memory.nudges import observe
from yuki.memory.store import NudgeCheckin
from yuki.ui import web_data as W

SAVE = ("drawer", "click", "Button", "Save", "k-save", None)
NEXT = ("webby", "click", "Button", "Next", "k-next", None)


def _outcome_action(store: Any, tid: int) -> int:
    return int(store._query("SELECT outcome_action FROM ui_tasks WHERE id = ?", (tid,))[0][0])


# -- the Work page: Worked on today ----------------------------------------------------------------------------------


def _work_rows(store: Any) -> dict[str, int]:
    """Two sessions of two threads: in the first a saved plan (checked), then an unclear read and a rejected save
    later in it; in the second a checked save of its own."""
    ids = {"saved": task(store, at("10:05"), "You saved the planting plan", [SAVE], outcome="file_saved"),
           "unclear": task(store, at("10:20"), "You read the seed swap forum", [NEXT]),
           "rejected": task(store, at("10:40"), "You saved a copy of the plan", [SAVE], outcome="file_saved"),
           "other": task(store, at("14:10"), "You saved the October budget", [SAVE], outcome="file_saved")}
    T.correct_task(store, ids["rejected"], "not_mine")
    return ids


SESSIONS = [{"start_at": at("10:00"), "end_at": at("11:00")}, {"start_at": at("14:00"), "end_at": at("14:30")}]
THREADS = [{"ref": "thread:7", "title": "Garden planner"}, {"ref": "thread:9", "title": "October budget"}]


def test_worked_on_today_cites_only_a_learnable_task_in_that_threads_sessions(store):
    ids = _work_rows(store)
    worked = [["Garden planner", 60, "thread:7"], ["October budget", 30, "thread:9"], ["Rust exercises", 20, "thread:11"]]
    W.worked_evidence(MemoryClient(store), worked, SESSIONS, THREADS, at("15:00"))
    g, b, r = worked
    assert g[3]["ref"] == f"task:{ids['saved']}" and g[3]["action"] == _outcome_action(store, ids["saved"])
    assert g[3]["text"] == "you saved a file at 10:05 AM"
    assert g[3]["goal"] == "You saved the planting plan"             # never the unclear one, never the rejected one
    assert b[3]["ref"] == f"task:{ids['other']}" and "2:10 PM" in b[3]["text"]
    assert len(r) == 3                                               # nothing checked in it: unchanged
    for row in worked[:2]:
        assert "task:" not in row[3]["text"] and "#" not in row[3]["text"]   # words only: no ids


def test_worked_on_today_is_unchanged_without_tasks_or_an_older_memory(store):
    worked = [["Garden planner", 60, "thread:7"]]
    W.worked_evidence(MemoryClient(store), worked, SESSIONS, THREADS, at("15:00"))
    assert worked == [["Garden planner", 60, "thread:7"]]

    class Old:   # a memory before capC6: no done_evidence
        pass

    W.worked_evidence(Old(), worked, SESSIONS, THREADS, at("15:00"))
    assert worked == [["Garden planner", 60, "thread:7"]]


def test_work_today_carries_the_evidence_through(store):
    _work_rows(store)
    client = MemoryClient(store)

    class Mem:
        def work_threads_for(self, spans: Any) -> list[Any]:
            return THREADS[: len(spans)]

        def done_evidence(self, since: float, until: float, **kw: Any) -> list[dict]:
            return client.done_evidence(since, until, **kw)

    sessions = [{**s, "apps": [["Drawer", 40]]} for s in SESSIONS]
    today = W.work_today(Mem(), None, sessions, {}, at("15:00"))
    assert [r[:3] for r in today["worked"]] == [["Garden planner", 40, "thread:7"], ["October budget", 40, "thread:9"]]
    assert all(len(r) == 4 and r[3]["text"].startswith("you saved a file at ") for r in today["worked"])


# -- the goal check-ins ----------------------------------------------------------------------------------------------


def _tasks_for_checkin(store: Any, wid: int) -> dict[str, int]:
    ids = {"saved": task(store, at("09:10"), "You saved the rebuild notes", [SAVE], outcome="file_saved"),
           "unclear": task(store, at("09:20"), "You read the seed swap forum", [NEXT]),
           "rejected": task(store, at("09:40"), "You saved a copy of the notes", [SAVE], outcome="file_saved"),
           # two days ago: on the moment's thread (its span's journal entries belong to it), and on none
           "thread_old": task(store, at("11:00", -2), "You saved the rebuild plan", [SAVE], outcome="file_saved"),
           "plain_old": task(store, at("13:00", -2), "You saved a shopping list", [SAVE], outcome="file_saved")}
    fact(store, at("11:00", -2) + 0.5, "The user wrote the Story Editor rebuild plan.", thread=wid)
    T.correct_task(store, ids["rejected"], "not_one")
    return ids


def test_the_check_in_message_offers_only_learnable_tasks_with_t_ids(store):
    wid, _jid = _landed(store)
    ids = _tasks_for_checkin(store, wid)
    w = worker_for(store, Stub())
    m = _moment(store, w)
    user, offered = w.checkins.build_message(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], m)
    assert "WHAT THEY GOT DONE (each one checked by the PC" in user
    assert f"[T{ids['saved']}]" in user and "You saved the rebuild notes: you saved a file" in user
    assert f"[T{ids['thread_old']}]" in user and "on Story Editor rebuild" in user   # its thread, within the week
    for left in ("unclear", "rejected", "plain_old"):
        assert f"[T{ids[left]}]" not in user, left
    assert {f"T{ids['saved']}", f"T{ids['thread_old']}"} <= set(offered)
    assert m.done[f"T{ids['saved']}"] == {"task": ids["saved"], "action": _outcome_action(store, ids["saved"]),
                                          "at": m.done[f"T{ids['saved']}"]["at"]}
    assert len(m.done) <= C.DONE_LINES


def test_without_tasks_the_check_in_message_is_as_before(store):
    _landed(store)
    w = worker_for(store, Stub())
    user, offered = w.checkins.build_message(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w))
    assert "WHAT THEY GOT DONE" not in user and not any(k.startswith("T") for k in offered)


def test_a_check_in_citing_a_checked_task_keeps_its_real_ids_internally_and_none_in_the_text(store):
    wid, jid = _landed(store)
    ids = _tasks_for_checkin(store, wid)
    card = {"say": "ask", "text": "You saved the rebuild notes at 9:10 AM - is the Story Editor rebuild done, or more to go?",
            "question": "Is the user's Story Editor rebuild done?", "subject": "Story Editor rebuild", "goal": "none",
            "evidence": [f"J{jid}", f"T{ids['saved']}"], "connects": "a merge and the notes saved this morning"}
    def decide(card_now: dict) -> str:
        w2 = worker_for(store, Stub(card_now))
        return w2.checkins.decide(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w2),
                                  NudgeCheckin(at=at("10:00"), trigger="checkin_followup", outcome="error"))["outcome"]

    # a T id in the text is refused like any id
    assert decide({**card, "text": f"You saved T{ids['saved']} - rebuild done?"}) == "suppressed:id_in_text"
    # an unclear or rejected task's T id was never offered: citing it is ungrounded
    for left in ("unclear", "rejected"):
        assert decide({**card, "evidence": [f"J{jid}", f"T{ids[left]}"]}) == "suppressed:ungrounded", left
    stub = Stub(card)
    w = worker_for(store, stub)
    result = w.checkins.look(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w))
    assert result["outcome"] == "question" and stub.calls == ["write_checkin", "vet_questions"]
    n = store.nudge(result["nudge_id"])
    assert n.kind == "question" and n.trigger == "checkin"           # the same card as every check-in, no new type
    assert n.inputs["done_tasks"] == [{"task": ids["saved"], "action": _outcome_action(store, ids["saved"])}]
    assert f"T{ids['saved']}" not in n.text and "task:" not in n.text


def test_a_check_in_citing_no_task_has_no_done_tasks(store):
    wid, jid = _landed(store)
    _tasks_for_checkin(store, wid)
    stub = Stub({"say": "ask", "text": "PR #2872 is in - is the rebuild done, or still going?",
                 "question": "Is the user's rebuild done?", "subject": "Story Editor rebuild", "goal": "none",
                 "evidence": [f"J{jid}"], "connects": "a merge"})
    w = worker_for(store, stub)
    result = w.checkins.look(at("10:00"), CFG, observe(store, at("10:00"), CFG), [], _moment(store, w))
    assert result["outcome"] == "question" and "done_tasks" not in store.nudge(result["nudge_id"]).inputs


def test_the_prompt_says_how_to_use_it_in_one_voice():
    p = C.CHECKIN_PROMPT
    assert "WHAT THEY GOT DONE" in p and "Never ask whether that step itself got done" in p
    assert "T7" in str(C.CHECKIN_TOOL["input_schema"]["properties"]["evidence"])
    assert "coach" not in p.split("WHAT THEY GOT DONE")[1][:600].lower()
