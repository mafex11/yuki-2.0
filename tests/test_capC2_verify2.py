"""capC2 second verification (report 98, "Second verification"): the ten findings V1-V10, each as a test.

V1 a family of more than 12 tasks keeps its usual path; V2 forgetting an hour takes back every transition its rows
made, and their times; V3 a far link does not glue the tasks between its ends into one piece; V4 a forget's undo
never lands on a newer family; V5 a file saved and opened in one app is no link; V6 no url link from a stale press;
V7 the families wait when a game began during the pass; V8 a rejected task leaves the usual path at once; V9 is in
test_capC2_tasks; V10 a window refused by its estimate is not prepared again that day.

Offline: temp databases, invented apps, names and goals; the model is a stand-in.
"""

from __future__ import annotations

import json
from typing import Any

from test_c_practice import Words, vec
from test_capC2_families import ATTACH, BUILD, FILE, SEND, task, three_reports
from test_capC2_links import DAY, NOW, act, links, store  # noqa: F401 - the store fixture
from test_capC2_tasks import Worker, episode, good_answer, prepared
from yuki.memory import trace_families as F
from yuki.memory import trace_links as TL
from yuki.memory import trace_store as ts
from yuki.memory import trace_tasks as T


def _hints(store: Any, words: str, now: float) -> dict[str, Any]:
    return F.hints(store, vec(words), ["chatter"], now=now)


# -- V1 ----------------------------------------------------------------------------------------------------------------


def test_a_family_of_more_than_24_tasks_keeps_its_usual_path(store):
    steps = []
    for i in range(26):
        task(store, NOW + i * DAY, "You sent the weekly report to the team", [ATTACH, FILE, SEND],
             outcome="field_submitted")
        F.refresh(store, Words(), now=NOW + i * DAY + 100)
        got = _hints(store, "send the weekly report to the team", NOW + i * DAY + 200)
        steps.append(len(got["path"]["steps"]) if got["path"] else 0)
    assert steps[0] == 0 and all(n == 3 for n in steps[1:])          # 13, 24, 25 and 26 tasks alike
    fam = store._query("SELECT n_tasks, path_n FROM ui_families")[0]
    assert fam["n_tasks"] == 26 and fam["path_n"] == F.PATH_TASKS
    assert _hints(store, "send the weekly report to the team", NOW + 26 * DAY)["path"]["tasks"] == 26


def test_a_step_most_of_the_latest_routes_took_stays_on_the_path_of_a_big_family(store):
    # 20 tasks: the latest 12 take a Reply step 7 times of 12 (most of them); the older 8 never did
    reply = ("chatter", "click", "Button", "Reply", "k-reply", None)
    for i in range(20):
        route = [ATTACH, reply, FILE, SEND] if i >= 8 and (i - 8) % 12 < 7 else [ATTACH, FILE, SEND]
        task(store, NOW + i * DAY, "You sent the weekly report to the team", route, outcome="field_submitted")
    F.refresh(store, Words(), now=NOW + 20 * DAY)
    got = _hints(store, "send the weekly report to the team", NOW + 20 * DAY + 10)
    assert got["path"]["tasks"] == 20
    assert any("Reply" in s for s in got["path"]["steps"])          # 7 of the 12 routes counted, not 7 of 20


# -- V2 ----------------------------------------------------------------------------------------------------------------


def _trans(store: Any) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {(r["process"], r["from_key"], r["to_key"]): dict(r) for r in store._query("SELECT * FROM ui_transitions")}


def test_forgetting_an_hour_takes_back_a_pair_begun_before_it_and_the_times_from_inside_it(store):
    t = NOW
    act(store, t, "chatter", "click", role="Button", name="Attach", key="k-a1")
    for i, key in enumerate(("k-b0", "k-b1", "k-b0", "k-b1"), start=1):
        act(store, t + 50 * i, "webby", "click", role="Button", name=key, key=key)
    act(store, t + 250, "chatter", "click", role="Button", name="Secret", key="k-secret")
    act(store, t + 260, "webby", "click", role="Button", name="k-b0", key="k-b0")
    act(store, t + 320, "chatter", "click", role="Button", name="Again", key="k-a2")
    TL.update(store, now=t + 330)
    before = _trans(store)
    assert ("chatter", "k-a1", "k-secret") in before and before[("webby", "k-b0", "k-b1")]["n"] == 2
    out = ts.forget(store, since=t + 200, until=t + 300, now=t + 400)
    after = _trans(store)
    assert ("chatter", "k-a1", "k-secret") not in after and ("chatter", "k-secret", "k-a2") not in after
    assert ("chatter", "k-a1", "k-a2") not in after                    # as if never: 170 s with no input is a pause
    assert after[("webby", "k-b0", "k-b1")]["n"] == 1 and after[("webby", "k-b0", "k-b1")]["last_at"] == t + 100
    assert after[("webby", "k-b1", "k-b0")]["n"] == 1 and after[("webby", "k-b1", "k-b0")]["last_at"] == t + 150
    assert all(not (t + 200 <= float(r["last_at"]) < t + 300) for r in after.values())
    assert all(not (t + 200 <= at < t + 300) for r in after.values() for _g, at in json.loads(r["gaps"]))
    # the carried state follows: chatter's last element is the one left, so the next click pairs with it
    act(store, t + 330, "chatter", "click", role="Button", name="Third", key="k-a3")
    TL.update(store, now=t + 340)
    assert ("chatter", "k-a2", "k-a3") in _trans(store)
    back = ts.undo_forget(store, out["diary_id"], now=t + 500)
    assert back["ok"]
    restored = _trans(store)
    assert ("chatter", "k-a1", "k-secret") in restored and restored[("webby", "k-b0", "k-b1")]["n"] == 2


def test_forgetting_a_step_between_two_joins_its_neighbours_at_their_own_time(store):
    act(store, NOW, "chatter", "click", role="Button", name="Attach", key="k-a1")
    act(store, NOW + 30, "chatter", "click", role="Button", name="Secret", key="k-secret")
    act(store, NOW + 60, "chatter", "click", role="Button", name="Send", key="k-a2")
    TL.update(store, now=NOW + 70)
    ts.forget(store, since=NOW + 25, until=NOW + 35, now=NOW + 80)
    after = _trans(store)
    assert set(after) == {("chatter", "k-a1", "k-a2")}
    assert after[("chatter", "k-a1", "k-a2")]["last_at"] == NOW + 60
    assert json.loads(after[("chatter", "k-a1", "k-a2")]["gaps"]) == [[60.0, NOW + 60]]


def test_the_replay_reaches_back_to_the_last_two_minute_pause_however_long_the_stretch(store):
    t = NOW
    act(store, t - 1000, "chatter", "click", role="Button", name="Old", key="k-old")       # before a long pause
    act(store, t, "chatter", "click", role="Button", name="Start", key="k-start")
    for i in range(1, 40):                                                                 # 39 minutes of webby
        act(store, t + 60 * i, "webby", "click", role="Button", name="Next", key=f"k-w{i % 2}")
    act(store, t + 60 * 40, "chatter", "click", role="Button", name="Secret", key="k-secret")
    TL.update(store, now=t + 60 * 41)
    assert ("chatter", "k-start", "k-secret") in _trans(store)
    assert ("chatter", "k-old", "k-start") not in _trans(store)
    ts.forget(store, since=t + 60 * 40, until=t + 60 * 40 + 1, now=t + 60 * 42)
    assert ("chatter", "k-start", "k-secret") not in _trans(store)
    assert ("chatter", "k-old", "k-start") not in _trans(store)                            # never made one


# -- V3 ----------------------------------------------------------------------------------------------------------------


def test_a_far_link_joins_two_pieces_by_a_line_and_the_tasks_between_stay_their_own(store):
    t = NOW
    copy = act(store, t, "webby", "copy", resolved="event", clip_seq=41)
    for k in range(1, 6):
        s = t + 1200 * k
        act(store, s, "notepadx", "click", role="Button", name="New", key="k-new")
        act(store, s + 5, "notepadx", "keys", resolved="focus", role="Document", key="k-doc", chord="Ctrl+S")
        act(store, s + 10, "notepadx", "file_save", resolved="event", file_digest=f"d-n{k}", file_ext=".txt",
            facts={"file_saved": f"d-n{k}"})
    paste = act(store, t + 7200, "chatter", "paste", resolved="focus", role="Edit", key="k-edit", clip_seq=41)
    TL.update(store, now=t + 7300)
    assert (copy, paste, "clipboard") in links(store)
    eid = episode(store, t - 10, t + 7300)
    w = T.TaskWindow(t - 10, t + 7300, [(eid, t - 10, t + 7300)])
    tp = T.TaskPass(store, Worker())
    _rows, lk, pieces, facts = tp.prepare(w)
    assert len(pieces) == 7 and max(p.end - p.start for p in pieces) < 60          # five notes stay five pieces
    msg = tp.message(w, pieces, T.step_lines(store, pieces, facts), "N" * 32, now=t + 7400, links=lk)
    assert f"p1 a{copy} -> p7 a{paste}: copied, then pasted" in msg and "LINKS" in T.TASK_PROMPT
    # the model may join the two linked pieces into one task while the notes between are tasks of their own
    saved = [next(f.fid for f in facts[int(p.rows[-1]["id"])] if f.kind == "file_saved") for p in pieces[1:6]]
    answer = {"tasks": [{"pieces": ["p1", "p7"], "goal": "You copied an address into a chat", "outcome": "none"}]
              + [{"pieces": [f"p{k + 2}"], "goal": "You saved a new note", "outcome": saved[k]} for k in range(5)]}
    checked = T.check(answer, pieces, facts)
    assert len(checked.drafts) == 6 and checked.rejected == []
    assert sum(1 for d in checked.drafts if d.outcome is not None and d.outcome.kind == "file_saved") == 5


# -- V4 ----------------------------------------------------------------------------------------------------------------


def test_undoing_a_forget_puts_a_family_back_under_its_own_id_never_over_a_newer_one(store):
    first = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND])
    F.refresh(store, Words(), now=NOW + 100)
    old_fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (first,))[0][0]
    out = ts.forget(store, process="chatter", now=NOW + 200)
    assert store._query("SELECT count(*) FROM ui_families")[0][0] == 0
    build = task(store, NOW + 300, "You built the planner in the terminal", [BUILD], outcome="command_ok")
    F.refresh(store, Words(), now=NOW + 400)
    new_fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (build,))[0][0]
    assert new_fid != old_fid                                                           # an id is never used again
    assert ts.undo_forget(store, out["diary_id"], now=NOW + 500)["ok"]
    fams = {r["id"]: dict(r) for r in store._query("SELECT * FROM ui_families")}
    assert set(fams) == {old_fid, new_fid}
    assert json.loads(fams[old_fid]["apps"]) == ["chatter"] and json.loads(fams[new_fid]["apps"]) == ["warp"]
    members = {r[0]: r[1] for r in store._query("SELECT task_id, family_id FROM ui_family_members")}
    assert members == {first: old_fid, build: new_fid}
    for fid, fam in fams.items():
        assert fam["n_tasks"] == sum(1 for f in members.values() if f == fid)


# -- V5, V6 ------------------------------------------------------------------------------------------------------------


def test_a_file_saved_and_opened_again_in_one_app_is_no_link(store):
    act(store, NOW, "notepadx", "file_save", resolved="event", file_digest="d-same", file_ext=".txt")
    act(store, NOW + 600, "notepadx", "file_open", resolved="event", file_digest="d-same", file_ext=".txt")
    TL.update(store, now=NOW + 700)
    assert links(store) == []


def test_a_stale_hyperlink_press_starts_no_url_link(store):
    act(store, NOW, "chatter", "click", role="Hyperlink", key="k-link", resolved="stale")
    act(store, NOW + 1, "webby", "switch", resolved="event", page_key="pg-1", facts={"page_reached": True})
    TL.update(store, now=NOW + 10)
    assert links(store) == []
    h = act(store, NOW + 100, "chatter", "click", role="Hyperlink", key="k-link")       # the same, its identity taken
    s = act(store, NOW + 101, "webby", "switch", resolved="event", page_key="pg-2", facts={"page_reached": True})
    TL.update(store, now=NOW + 110)
    assert links(store) == [(h, s, "url")]


# -- V7 ----------------------------------------------------------------------------------------------------------------


def test_the_families_wait_when_a_game_began_during_the_pass(store, monkeypatch):
    from yuki.memory.practice_learn import PracticeWorker

    ids, _w, _pieces, facts = prepared(store)
    worker = Worker([good_answer(ids, facts)])
    worker.store = store
    state = {"full": False}
    real_call = worker._call

    def call_then_a_game(**kw: Any) -> Any:
        out = real_call(**kw)
        state["full"] = True
        return out

    worker._call = call_then_a_game
    refreshed: list[bool] = []
    monkeypatch.setattr(F, "refresh", lambda *a, **kw: refreshed.append(True) or {"tasks": 1})
    pw = PracticeWorker(store, worker, idle_fn=lambda: 4000.0, live_fn=lambda: {"fullscreen": state["full"]},
                        clock=lambda: NOW + 900)
    out = pw.task_pass(NOW + 900)
    assert out["tasks"][0]["tasks"] == 3 and refreshed == [] and out["families_waiting"] == "fullscreen"
    state["full"] = False
    out = pw.task_pass(NOW + 960)
    assert refreshed == [True] and "families_waiting" not in out


# -- V8 ----------------------------------------------------------------------------------------------------------------


def test_a_rejected_task_leaves_the_usual_path_at_once(store):
    reports = three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    got = _hints(store, "send the weekly report to the team", NOW + 3 * DAY + 10)
    assert got["path"]["tasks"] == 3
    assert T.correct_task(store, reports[0], "not_mine", now=NOW + 3 * DAY + 20)["ok"]
    assert T.correct_task(store, reports[1], "not_one", now=NOW + 3 * DAY + 30)["ok"]
    got = _hints(store, "send the weekly report to the team", NOW + 3 * DAY + 40)       # no refresh in between
    assert got["path"] is None
    assert store._query("SELECT n_tasks FROM ui_families")[0][0] == 1
    assert sorted(r[0] for r in store._query("SELECT task_id FROM ui_family_members")) == [reports[2]]
    T.undo_correction(store, reports[0], now=NOW + 3 * DAY + 50)                       # taken back: placed again
    F.refresh(store, Words(), now=NOW + 3 * DAY + 60)
    assert _hints(store, "send the weekly report to the team", NOW + 3 * DAY + 70)["path"]["tasks"] == 2


# -- V10 ---------------------------------------------------------------------------------------------------------------


def test_a_window_refused_by_its_estimate_is_not_prepared_again_that_day(store, monkeypatch):
    ids, w, _pieces, facts = prepared(store)
    store._write(lambda c: c.execute("INSERT INTO ui_task_runs(at, cost_usd, ok) VALUES (?, ?, 1)",
                                     (NOW + 100, T.DAILY_BUDGET_USD - 0.001)))
    built: list[int] = []
    real = T.step_lines
    monkeypatch.setattr(T, "step_lines", lambda *a, **kw: built.append(1) or real(*a, **kw))
    worker = Worker([good_answer(ids, facts)])
    tp = T.TaskPass(store, worker)
    outcomes = [tp.run_window(w, now=NOW + 900 + 30 * k).outcome for k in range(5)]
    assert outcomes == ["refused"] * 5 and len(built) == 1 and not worker.calls
    assert store._query("SELECT count(*) FROM ui_task_runs WHERE refused = 1")[0][0] == 1
    assert tp.run_window(w, now=NOW + DAY + 900).outcome == "ok" and len(built) == 2      # the next day: tried
