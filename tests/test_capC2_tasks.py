"""capC2 (report 78 §5.2-5.3, §9, decision D5; report 98): the task pass on finished episode windows.

Code pre-cuts the window at 2-minute pauses and keeps link chains in one piece; one Haiku call (stubbed here) labels
the pieces; code checks every answer (outcome facts only from those recorded inside the task, never in a detour or
another task; goals of the right size that cite no ids; a piece in one task at most). An unclear task never teaches
a trial or a way. Inside the practice worker's blocked() gate, at most 4 windows a pass, nothing older than 7 days,
$0.40 a day with a refusal when over. The Day page's data and the correction round trip, corrections fed back as
worked examples.

Offline: temp databases, invented apps, names and goals; the model is a stand-in.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

from test_capC2_links import DAY, NOW, act, store  # noqa: F401 - the store fixture
from yuki.memory import trace_links as TL
from yuki.memory import trace_tasks as T


class Worker:
    """A stand-in for the practice model's worker: ``_call`` returns the queued answers (a callable gets the call's
    arguments) and adds ``cost`` to the run's stats."""

    model = "stub"

    def __init__(self, answers: list[Any] | None = None, cost: float = 0.01) -> None:
        self.answers = list(answers or [])
        self.calls: list[dict[str, Any]] = []
        self.cost = cost
        self.settings = SimpleNamespace()
        self.logged: list[tuple[str, dict[str, Any]]] = []
        self.embedder = None

    def _call(self, **kw: Any) -> Any:
        self.calls.append(kw)
        kw["stats"].cost_usd += self.cost
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return answer(kw) if callable(answer) else answer

    def _log(self, type_: str, **fields: Any) -> None:
        self.logged.append((type_, fields))


def episode(store: Any, start: float, end: float, *, final: bool = True, window: tuple[float, float] | None = None
            ) -> int:
    ws, we = window or (start, end)
    return int(store._write(lambda c: c.execute(
        "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext, created_at)"
        " VALUES (?,?,?,?,?,?,?)", (start, end, ws, we, int(final), store._enc("an invented episode"), end)).lastrowid))


def seed(store: Any, t0: float) -> dict[str, int]:
    """Three pieces: a copy in webby pasted and sent in chatter; a note typed and saved in notepadx (with a hover, a
    stale click and one of Yuki's clicks among them); a Warp command that worked."""
    ids: dict[str, int] = {}
    ids["copy"] = act(store, t0, "webby", "copy", resolved="event", clip_seq=9)
    ids["chat"] = act(store, t0 + 10, "chatter", "click", role="ListItem", name="Mira Lane", key="k-chat",
                      position="1/List")
    ids["paste"] = act(store, t0 + 15, "chatter", "paste", resolved="focus", role="Edit", key="k-edit", clip_seq=9)
    ids["send"] = act(store, t0 + 18, "chatter", "keys", resolved="focus", role="Edit", key="k-edit", chord="Enter",
                      facts={"field_submitted": {"typed": 0}})
    t1 = t0 + 360
    ids["type"] = act(store, t1, "notepadx", "type", resolved="focus", role="Document", key="k-doc", typed=40)
    ids["hover"] = act(store, t1 + 5, "notepadx", "hover", role="Button", name="Bold", key="k-bold")
    ids["stale"] = act(store, t1 + 6, "notepadx", "click", role="Button", name="Italic", key="k-it", resolved="stale")
    ids["yuki"] = act(store, t1 + 7, "notepadx", "click", role="Button", name="Format", key="k-fmt", source="yuki")
    ids["save_keys"] = act(store, t1 + 30, "notepadx", "keys", resolved="focus", key="k-doc", role="Document",
                           chord="Ctrl+S")
    ids["save"] = act(store, t1 + 40, "notepadx", "file_save", resolved="event", file_digest="d-note", file_ext=".txt",
                      facts={"file_saved": "d-note"})
    t2 = t0 + 720
    ids["cmd"] = act(store, t2, "warp", "command", resolved="app_data", exit_code=0, facts={"command_ok": True})
    return ids


def window(store: Any, t0: float) -> T.TaskWindow:
    eid = episode(store, t0 - 10, t0 + 800)
    return T.TaskWindow(t0 - 10, t0 + 800, [(eid, t0 - 10, t0 + 800)])


def prepared(store: Any, t0: float = NOW) -> tuple[dict[str, int], T.TaskWindow, list[T.Piece], dict[int, list[T.Fact]]]:
    ids = seed(store, t0)
    TL.update(store, now=t0 + 900)
    w = window(store, t0)
    _rows, _links, pieces, facts = T.TaskPass(store, Worker()).prepare(w)
    return ids, w, pieces, facts


def fid(facts: dict[int, list[T.Fact]], action: int, kind: str) -> str:
    return next(f.fid for f in facts[action] if f.kind == kind)


def good_answer(ids: dict[str, int], facts: dict[int, list[T.Fact]]) -> dict[str, Any]:
    return {"tasks": [
        {"pieces": ["p1"], "goal": "You shared a page from webby in a chat", "outcome": fid(facts, ids["send"],
                                                                                              "paste_sent")},
        {"pieces": ["p2"], "goal": "You wrote and saved a text note", "outcome": fid(facts, ids["save"], "file_saved")},
        {"pieces": ["p3"], "goal": "You ran a command in the terminal", "outcome": fid(facts, ids["cmd"],
                                                                                         "command_ok")}]}


# -- pre-cut and step lines ----------------------------------------------------------------------------------------


def test_precut_at_two_minute_pauses_keeps_link_chains_in_one_piece(store):
    ids, _w, pieces, _f = prepared(store)
    assert [p.pid for p in pieces] == ["p1", "p2", "p3"]
    assert pieces[0].ids == {ids["copy"], ids["chat"], ids["paste"], ids["send"]}
    assert ids["hover"] not in pieces[1].ids and ids["stale"] not in pieces[1].ids and ids["yuki"] not in pieces[1].ids
    rows = [{"id": 1, "at": 0.0, "source": "user", "kind": "click", "resolved": "point"},
            {"id": 2, "at": 119.9, "source": "user", "kind": "click", "resolved": "point"},
            {"id": 3, "at": 230.0, "source": "user", "kind": "click", "resolved": "point"},
            {"id": 4, "at": 600.0, "source": "user", "kind": "copy", "resolved": "event"},
            {"id": 5, "at": 900.0, "source": "user", "kind": "click", "resolved": "point"},
            {"id": 6, "at": 1200.0, "source": "user", "kind": "paste", "resolved": "focus"},
            {"id": 7, "at": 1500.0, "source": "user", "kind": "click", "resolved": "point"}]
    cut = T.precut(rows, [])
    assert [sorted(p.ids) for p in cut] == [[1, 2, 3], [4], [5], [6], [7]]           # a 2-minute pause cuts
    chained = T.precut(rows, [{"from": 4, "to": 6, "kind": "clipboard"}])
    assert [sorted(p.ids) for p in chained] == [[1, 2, 3], [4, 5, 6], [7]]           # the chain keeps its pieces
    # a hover is still the user's input: it keeps two clicks 3 minutes apart in one piece
    with_hover = T.precut([rows[0], {"id": 9, "at": 100.0, "source": "user", "kind": "hover", "resolved": "point"},
                           {"id": 10, "at": 200.0, "source": "user", "kind": "click", "resolved": "point"}], [])
    assert [sorted(p.ids) for p in with_hover] == [[1, 10]]


def test_step_lines_are_code_built_and_content_free(store):
    ids, w, pieces, facts = prepared(store)
    lines = T.step_lines(store, pieces, facts)
    text = "\n".join(line for v in lines.values() for line in v)
    assert f"a{ids['paste']} " in text and "chatter · pasted into an unnamed Edit" in text
    assert "an unnamed ListItem (item 2 in a List)" in text and "Mira" not in text      # content: never a name
    assert "typed 40 characters into an unnamed Document" in text
    assert "keys Ctrl+S" in text and "saved a file (.txt)" in text and "ran a terminal command" in text
    assert "{" + fid(facts, ids["save"], "file_saved") + " file saved (.txt)}" in text
    assert "pasted and sent}" in text
    for gone in ("hover", "stale", "yuki"):
        assert f"a{ids[gone]} " not in text
    msg = T.TaskPass(store, Worker()).message(w, pieces, lines, "N" * 32, now=NOW + 900)
    assert "PIECES:" in msg and "p1 " in msg and "UNTRUSTED" in msg


def test_facts_are_the_recorded_ones_with_paste_sent_from_a_clipboard_link(store):
    ids, _w, _pieces, facts = prepared(store)
    kinds = {(a, f.kind) for a, fs in facts.items() for f in fs}
    assert (ids["send"], "field_submitted") in kinds and (ids["send"], "paste_sent") in kinds
    assert (ids["save"], "file_saved") in kinds and (ids["cmd"], "command_ok") in kinds
    off = act(store, NOW + 2000, "settingsx", "click", role="CheckBox", name="Transparency", key="k-tr",
              facts={"toggle_state": "off"})
    got = T.facts_of([T.Piece("p9", NOW + 2000, NOW + 2000, [dict(store._query(
        "SELECT * FROM ui_actions WHERE id = ?", (off,))[0])])], [])
    assert got[off][0].kind == "toggle_state" and got[off][0].words == "switched off"   # off is a fact too


# -- the answer, checked ---------------------------------------------------------------------------------------------


def test_outcomes_only_from_facts_recorded_inside_the_task(store):
    ids, _w, pieces, facts = prepared(store)
    save_f = fid(facts, ids["save"], "file_saved")
    checked = T.check({"tasks": [
        {"pieces": ["p1"], "goal": "You shared a page in a chat", "outcome": save_f},          # another task's fact
        {"pieces": ["p2"], "goal": "You saved a text note", "outcome": "f99"},                  # never recorded
        {"pieces": ["p3"], "goal": "You ran a terminal command", "outcome": "none"}]}, pieces, facts)
    assert [d.outcome for d in checked.drafts] == [None, None, None]
    assert any("not in this task" in r for r in checked.rejected)
    assert any("never recorded" in r for r in checked.rejected)
    detour = T.check({"tasks": [{"pieces": ["p1"], "goal": "You shared a page in a chat",
                                 "outcome": fid(facts, ids["send"], "paste_sent"),
                                 "detours": [{"from": f"a{ids['paste']}", "to": f"a{ids['send']}"}]}]}, pieces, facts)
    assert detour.drafts[0].outcome is None and any("in a detour" in r for r in detour.rejected)


def test_goals_pieces_and_detours_are_checked(store):
    ids, _w, pieces, facts = prepared(store)
    checked = T.check({"tasks": [
        {"pieces": ["p1"], "goal": f"You pasted at a{ids['paste']} into the chat", "outcome": "none"},   # an id
        {"pieces": ["p2"], "goal": "Saved", "outcome": "none"},                                          # too short
        {"pieces": ["p3", "p9"], "goal": "You ran your build in the terminal", "outcome": "none"},
        {"pieces": ["p3"], "goal": "You ran it again in the terminal", "outcome": "none"},               # p3 taken
        {"pieces": ["p2"], "goal": "You wrote a note " + "and more " * 20, "outcome": "none"},           # too long
        {"pieces": ["p1"], "goal": "You shared a page in a chat", "outcome": "none",
         "detours": [{"from": f"a{ids['copy']}", "to": f"a{ids['send']}"},                               # all of it
                     {"from": f"a{ids['cmd']}", "to": f"a{ids['cmd']}"}]}],                              # outside
        "extra": 1}, pieces, facts)
    assert [(sorted(p.pid for p in d.pieces), d.goal) for d in checked.drafts] == [
        (["p3"], "You ran your build in the terminal"), (["p1"], "You shared a page in a chat")]
    assert checked.drafts[1].detours == []
    reasons = " | ".join(checked.rejected)
    for words in ("goal is not plain words", "no piece p9", "already in another task", "the whole task",
                  "outside the task"):
        assert words in reasons
    assert T.check("not a dict", pieces, facts).drafts == [] and T.check(None, pieces, facts).drafts == []


# -- the pass ------------------------------------------------------------------------------------------------------


def test_the_pass_writes_tasks_with_checked_outcomes_and_its_cost(store):
    ids, w, pieces, facts = prepared(store)
    worker = Worker([good_answer(ids, facts)], cost=0.02)
    res = T.TaskPass(store, worker).run_window(w, now=NOW + 900)
    assert res.outcome == "ok" and res.tasks == 3 and res.unclear == 0 and res.cost_usd == 0.02
    call = worker.calls[0]
    assert call["tool"]["name"] == "record_tasks" and call["forced"] and call["system"] == T.TASK_PROMPT
    rows = store._query("SELECT * FROM ui_tasks ORDER BY started_at")
    assert [r["outcome_fact"] for r in rows] == ["paste_sent", "file_saved", "command_ok"]
    assert json.loads(rows[0]["apps"]) == ["chatter", "webby"]
    assert rows[1]["outcome_action"] == ids["save"] and rows[1]["outcome_target"] == "d-note"
    assert rows[1]["actions"] == 3                                     # type, Ctrl+S, the save; never the hover,
    assert "wrote" not in (rows[1]["goal_ciphertext"] or "")           # the stale click or Yuki's (and encrypted)
    assert store._dec(rows[1]["goal_ciphertext"]) == "You wrote and saved a text note"
    assert rows[0]["episode_id"] == w.episodes[0][0]
    run = store._query("SELECT * FROM ui_task_runs")[0]
    assert run["ok"] == 1 and run["tasks"] == 3 and run["cost_usd"] == 0.02 and run["window_start"] == w.start
    own = T.task_rows(store, int(rows[1]["id"]))
    assert [r["id"] for r in own] == [ids["type"], ids["save_keys"], ids["save"]]


def test_an_unclear_task_never_becomes_a_trial_or_a_way(store):
    ids, w, pieces, facts = prepared(store)
    ans = good_answer(ids, facts)
    ans["tasks"][2]["outcome"] = "none"
    before = store._query("SELECT count(*) FROM playbooks")[0][0]
    T.TaskPass(store, Worker([ans])).run_window(w, now=NOW + 900)
    rows = {r["outcome_fact"]: dict(r) for r in store._query("SELECT * FROM ui_tasks")}
    assert T.UNCLEAR in rows and not T.learnable(rows[T.UNCLEAR])
    assert T.learnable(rows["file_saved"])
    assert store._query("SELECT count(*) FROM playbooks")[0][0] == before            # the pass writes no way at all
    assert store._query("SELECT count(*) FROM playbook_events")[0][0] == 0
    T.correct_task(store, rows["file_saved"]["id"], "not_one")
    assert not T.learnable(dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (rows["file_saved"]["id"],))[0]))


def test_an_empty_window_costs_no_call_and_a_model_error_is_tried_again_later(store):
    eid = episode(store, NOW, NOW + 600)
    w = T.TaskWindow(NOW, NOW + 600, [(eid, NOW, NOW + 600)])
    worker = Worker([])
    assert T.TaskPass(store, worker).run_window(w, now=NOW + 700).outcome == "empty" and not worker.calls
    assert not T.windows_due(store, NOW + 700)                                       # passed: never again
    ids = seed(store, NOW + 1000)
    episode(store, NOW + 990, NOW + 1800)
    worker = Worker([RuntimeError("the model did not answer")])
    res = T.TaskPass(store, worker).run_window(T.windows_due(store, NOW + 1900)[0], now=NOW + 1900)
    assert res.outcome == "error" and ids
    assert not T.windows_due(store, NOW + 1900 + 60)                                 # waits half an hour
    assert T.windows_due(store, NOW + 1900 + T.RETRY_AFTER_S + 1)


# -- catch-up and the budget -----------------------------------------------------------------------------------------


def test_catch_up_four_windows_a_pass_and_nothing_older_than_7_days(store):
    now = NOW + 10 * DAY
    old = now - 8 * DAY
    episode(store, old, old + 600)                                                   # 8 days ago: skipped
    starts = [now - 3 * DAY + i * 3600 for i in range(6)]
    for s in starts:
        episode(store, s, s + 600)
        act(store, s + 30, "chatter", "click", role="Button", name="Send", key="k-send")
    due = T.windows_due(store, now, limit=None)
    assert [w.start for w in due] == starts
    worker = Worker([{"tasks": [{"pieces": ["p1"], "goal": "You sent a message in the chat", "outcome": "none"}]}
                     for _ in range(8)])
    done = T.TaskPass(store, worker).run_once(now=now)
    assert len(done) == T.WINDOWS_PER_PASS == 4 and len(worker.calls) == 4
    assert [w.start for w in T.windows_due(store, now, limit=None)] == starts[4:]
    T.TaskPass(store, worker).run_once(now=now)
    assert T.windows_due(store, now, limit=None) == []
    assert store._query("SELECT count(*) FROM ui_task_runs WHERE window_start = ?", (old,))[0][0] == 0
    h = T.health(store, now=now)
    assert h["backlog"] == 0 and h["tasks_today"] == 6 and h["unclear_today"] == 6


def test_windows_with_no_action_of_the_user_are_closed_at_once_without_the_gate(store):
    from yuki.memory.practice_learn import PracticeWorker

    now = NOW + 2 * DAY
    for i in range(7):
        episode(store, now - DAY + i * 3600, now - DAY + i * 3600 + 600)          # the trace was off then
    busy = now - DAY + 3 * 3600
    act(store, busy + 60, "chatter", "click", role="Button", name="Send", key="k-send", source="yuki")   # Yuki's only
    with_rows = now - 3600
    episode(store, with_rows, with_rows + 600)
    act(store, with_rows + 60, "chatter", "click", role="Button", name="Send", key="k-send")
    worker = Worker([])
    worker.store = store
    pw = PracticeWorker(store, worker, idle_fn=lambda: 5.0, live_fn=lambda: {}, clock=lambda: now)
    out = pw.task_pass(now)
    assert out.get("tasks_waiting") == "busy" and not worker.calls                  # the gate still holds the model
    assert [w.start for w in T.windows_due(store, now, limit=None)] == [with_rows]  # the 7 empty ones are closed
    assert store._query("SELECT count(*) FROM ui_task_runs WHERE outcome = 'empty' AND ok = 1")[0][0] == 7
    assert json.loads(store.nudge_state(T.HEALTH_KEY))["backlog"] == 1


def test_the_pass_refuses_when_todays_cost_reached_the_cap(store):
    ids, w, pieces, facts = prepared(store)
    store._write(lambda c: c.execute("INSERT INTO ui_task_runs(at, cost_usd, ok) VALUES (?, ?, 1)",
                                     (NOW + 100, T.DAILY_BUDGET_USD)))
    worker = Worker([good_answer(ids, facts)])
    task_pass = T.TaskPass(store, worker)
    res = task_pass.run_window(w, now=NOW + 900)
    assert res.outcome == "refused" and not worker.calls
    assert task_pass.run_window(w, now=NOW + 930).outcome == "refused"
    assert store._query("SELECT count(*) FROM ui_task_runs WHERE refused = 1")[0][0] == 1     # one row a day
    assert T.windows_due(store, NOW + 960)                                                   # it waits, not dropped
    # a call whose estimate would pass the cap is refused too
    store._write(lambda c: c.execute("DELETE FROM ui_task_runs"))
    tight = T.TaskPass(store, worker, budget_usd=0.001)
    assert tight.run_window(w, now=NOW + 990).outcome == "refused" and not worker.calls
    assert T.health(store, now=NOW + 990)["refused_today"] == 1


def test_the_practice_worker_runs_the_pass_only_inside_its_gate(store):
    from yuki.memory.practice_learn import PracticeWorker

    ids, w, pieces, facts = prepared(store)
    worker = Worker([good_answer(ids, facts)])
    worker.store = store
    state = {"idle": 30.0, "full": False}
    pw = PracticeWorker(store, worker, idle_fn=lambda: state["idle"], live_fn=lambda: {"fullscreen": state["full"]},
                        clock=lambda: NOW + 900)
    out = pw.task_pass(NOW + 900)
    assert out.get("tasks_waiting") == "busy" and not worker.calls                   # the user is at the PC
    state.update(idle=4000.0, full=True)
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "fullscreen" and not worker.calls   # a game
    cid = store.start_call(app="Mumble", process="mumble", via="mic", status="recording", at=NOW + 800)
    state["full"] = False
    assert pw.task_pass(NOW + 900).get("tasks_waiting") == "call" and not worker.calls
    store.update_call(cid, status="ended", ended_at=NOW - 3600)
    out = pw.task_pass(NOW + 900)
    assert out["tasks"][0]["tasks"] == 3 and len(worker.calls) == 1
    health = json.loads(store.nudge_state(T.HEALTH_KEY))
    assert health["tasks_today"] == 3 and health["backlog"] == 0


# -- the Day page and corrections ------------------------------------------------------------------------------------


def test_day_tasks_and_the_correction_round_trip(store):
    from yuki.memory.api import MemoryClient

    ids, w, pieces, facts = prepared(store)
    T.TaskPass(store, Worker([good_answer(ids, facts)])).run_window(w, now=NOW + 900)
    m = MemoryClient(store)
    got = m.day_tasks(NOW - 100, NOW + DAY)
    assert [t["outcome"] for t in got] == ["paste_sent", "file_saved", "command_ok"]
    assert got[1]["goal"] == "You wrote and saved a text note" and got[1]["outcome_words"] == "you saved a file (.txt)"
    assert got[0]["ref"].startswith("task:") and got[0]["verdict"] is None
    ref = got[1]["ref"]
    assert m.task_correct(ref, "renamed", "")["ok"] is False                         # say it in words
    assert m.task_correct(ref, "wrong-verdict")["ok"] is False
    r = m.task_correct(ref, "renamed", "  You wrote the shopping list  ")
    assert r == {"ok": True, "verdict": "renamed", "goal": "You wrote the shopping list"}
    after = m.day_tasks(NOW - 100, NOW + DAY)[1]
    assert after["goal"] == "You wrote the shopping list" and after["verdict"] == "renamed"
    row = store._query("SELECT * FROM ui_tasks WHERE id = ?", (got[1]["id"],))[0]
    assert store._dec(row["model_goal_ciphertext"]) == "You wrote and saved a text note" and row["vec_ciphertext"] is None
    assert m.task_correct(got[0]["ref"], "not_mine")["ok"] and m.task_correct(got[2]["ref"], "not_one")["ok"]
    undo = m.task_correct_undo(ref)
    assert undo["ok"] and m.day_tasks(NOW - 100, NOW + DAY)[1]["goal"] == "You wrote and saved a text note"
    m.task_correct(ref, "renamed", "You wrote the shopping list")
    # the corrections go to the next pass as worked examples
    fixes = T.corrections(store, now=NOW + 1000)
    assert {c["verdict"] for c in fixes} == {"renamed", "not_mine", "not_one"}
    t0 = NOW + 5000
    ids2 = seed(store, t0)
    TL.update(store, now=t0 + 900)
    w2 = window(store, t0)
    worker = Worker([{"tasks": []}])
    T.TaskPass(store, worker).run_window(w2, now=t0 + 900)
    user = worker.calls[0]["user"]
    assert "CORRECTIONS" in user and "the user renamed it \"You wrote the shopping list\"" in user
    assert "not one task" in user and "not what they did" in user and ids2


def test_the_day_page_puts_tasks_inside_their_sessions():
    from yuki.ui import web_data as W

    sessions = [{"start_at": NOW, "end_at": NOW + 3600}, {"start_at": NOW + 4000, "end_at": NOW + 8000}]

    class Mem:
        def day_tasks(self, since: float, until: float) -> list[dict[str, Any]]:
            return [{"ref": "task:1", "start_at": NOW + 100, "end_at": NOW + 900, "goal": "You sent  the  plan",
                     "apps": ["chatter", "webby"], "outcome": "paste_sent", "outcome_words": "you pasted it in and sent it",
                     "verdict": None},
                    {"ref": "task:2", "start_at": NOW + 3500, "end_at": NOW + 4700, "goal": "You read a forum",
                     "apps": ["webby"], "outcome": "unclear", "outcome_words": "not sure it got done",
                     "verdict": "not_one"},
                    {"ref": "task:3", "start_at": NOW + 9000, "end_at": NOW + 9100, "goal": "Outside every session",
                     "apps": [], "outcome": "unclear", "outcome_words": "not sure it got done", "verdict": None}]

    W.session_tasks(Mem(), sessions, NOW - 100, {"chatter": "Chatter", "webby": "Webby"})
    assert [t["ref"] for t in sessions[0]["tasks"]] == ["task:1"]
    assert sessions[0]["tasks"][0]["goal"] == "You sent the plan" and sessions[0]["tasks"][0]["apps"] == ["Chatter",
                                                                                                         "Webby"]
    assert sessions[0]["tasks"][0]["checked"] is True
    assert [t["ref"] for t in sessions[1]["tasks"]] == ["task:2"]                    # its middle is in the second
    assert sessions[1]["tasks"][0]["verdict"] == "not_one" and sessions[1]["tasks"][0]["checked"] is False
    W.session_tasks(object(), sessions, NOW, {})                                     # an older memory: unchanged


def test_the_window_ops_correct_and_undo_through_memory(tmp_path):
    from yuki.ui import web_window

    calls = []

    class Mem:
        def task_correct(self, ref: str, verdict: str, words: str = "") -> dict[str, Any]:
            calls.append(("correct", ref, verdict, words))
            return {"ok": True, "verdict": verdict, "goal": words or "You sent the plan"}

        def task_correct_undo(self, ref: str) -> dict[str, Any]:
            calls.append(("undo", ref))
            return {"ok": True, "verdict": None, "goal": "You sent the plan"}

    win = web_window.WebWindow.__new__(web_window.WebWindow)
    win.memory = Mem()
    refreshed = []
    win.refresh = lambda name, *a, **k: refreshed.append(name)
    assert win.a_task_correct({"ref": "task:4", "verdict": "renamed", "words": " You  paid the bill "}) == {
        "ref": "task:4", "verdict": "renamed", "goal": "You paid the bill"}
    assert win.a_task_correct_undo({"ref": "task:4"})["goal"] == "You sent the plan"
    assert calls == [("correct", "task:4", "renamed", "You paid the bill"), ("undo", "task:4")]
    assert refreshed == ["day", "day"]
    assert {"task_correct", "task_correct_undo"} <= set(web_window.WebWindow.ACTIONS)


def test_the_fixture_shows_tasks_in_sessions_with_invented_words():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1] / "yuki" / "ui" / "web" / "fixtures" / "window.js").read_text(
        encoding="utf-8")
    data = json.loads(text[text.index("= ") + 2:].rstrip().rstrip(";"))
    day = data["pages"]["day"]
    tasks = [t for s in day["sessions"] for t in s.get("tasks", [])]
    assert len(tasks) >= 4 and day["can"]["correct_task"] is True
    assert {"not_one", "renamed", None} <= {t["verdict"] for t in tasks}
    assert data["replies"]["task_correct"] == {"ok": True}


def test_the_doctor_says_it_in_plain_words():
    from yuki.core import doctor

    raw = json.dumps({"at": NOW, "tasks_today": 7, "unclear_today": 2, "cost_today": 0.12, "budget": 0.4,
                      "backlog": 3, "refused_today": 0})
    words = doctor._trace_tasks_words(raw, NOW + 60)
    assert words == ("; found 7 tasks in your sessions today (2 of them with no clear end); that cost about $0.12 of "
                     "the $0.40 a day it may use; 3 finished sessions still to look at, when you're away")
    capped = json.loads(raw) | {"refused_today": 1, "backlog": 0}
    assert "the day's limit is reached" in doctor._trace_tasks_words(json.dumps(capped), NOW + 60)
    assert doctor._trace_tasks_words(raw, NOW + 2 * DAY) == "" and doctor._trace_tasks_words(None, NOW) == ""
