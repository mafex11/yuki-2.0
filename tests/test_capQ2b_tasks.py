"""capQ2b (report 104): the task pass's input and its checks, after the first full day of real data.

Twenty minutes of music in a music program came back as "you worked on code in bl64"; a single app switch and seven
seconds of taskbar clicks came back as tasks; a window coming to the front was taken as the end of a browsing session.
Now: apps are named by their program name from the executable's version info, a project file's facts are given
(its name only after 3 distinct days), the model may mark noise pieces (counted, never stored as tasks), and a window
coming to the front is no outcome when nothing else was done in that app or the work went on in it.

Offline: temp databases, invented programs, names, projects and goals; the model and Windows are stand-ins.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from test_appnames import FakeProbe
from test_capC2_links import NOW, act, store  # noqa: F401 - the store fixture
from test_capC2_tasks import Worker, episode
from yuki.core import appnames
from yuki.memory import trace_links as TL
from yuki.memory import trace_store
from yuki.memory import trace_tasks as T

BL_EXE = r"C:\Program Files\Beatline Audio\Beatline 24\BL64.exe"
WEBBY_EXE = r"C:\Program Files\Webby\webby.exe"
DAY = 86400.0


@pytest.fixture()
def names(monkeypatch):
    """Windows' version info, faked: bl64 is "Beatline Studio", webby is "Webby Browser"; chatter is unknown."""
    probe = FakeProbe()
    probe.versions[BL_EXE] = ("Beatline Studio", "Beatline Studio")
    probe.versions[WEBBY_EXE] = ("Webby Browser", "")
    probe.processes[501] = (BL_EXE, "", "")
    probe.processes[502] = (WEBBY_EXE, "", "")
    resolver = appnames.Resolver(probe, names_file=None)
    monkeypatch.setattr(appnames, "_default", resolver)
    return resolver


def _save_with_project(store: Any, at: float, digest: str, *, project: str = "Night Drive") -> int:
    row = {"at": at, "process": "bl64", "kind": "file_save", "source": "user", "resolved": "event",
           "file_digest": digest, "file_ext": ".blp", "outcome": "changed",
           "facts": json.dumps({"file_saved": digest})}
    data = {"project": project, "tempo": 128.0, "generators": ["Bass Synth", "Drum Kit", "Bass Synth"],
            "effects": ["Room Verb"], "channels": ["Kick"], "patterns": ["Verse"], "pattern_count": 6,
            "channel_count": 9}
    return trace_store.write_batch(store, [row], [], [{"row": 0, "kind": "flp", "outcome": "ok", "data": data,
                                                       "file_digest": digest, "at": at}])[0]


def seed(store: Any, t0: float) -> dict[str, int]:
    """p1 music in bl64 ending in a save; p2 seven seconds of taskbar clicks; p3 one switch to an app; p4 a taskbar
    click bringing webby up, then reading in it."""
    ids: dict[str, int] = {}
    ids["drag"] = act(store, t0, "bl64", "drag", resolved="point")
    ids["rclick"] = act(store, t0 + 30, "bl64", "rclick", resolved="point")
    ids["keys"] = act(store, t0 + 60, "bl64", "keys", resolved="focus", chord="Ctrl+S")
    ids["save"] = _save_with_project(store, t0 + 61, "d-proj")
    t1 = t0 + 400
    ids["tb1"] = act(store, t1, "explorer", "click", role="Button", name="Chatter - 1 running window", key="k-tb1",
                     facts={"window_verified": "chatter"})
    ids["tb2"] = act(store, t1 + 4, "explorer", "click", role="Button", name="Webby - 1 running window", key="k-tb2",
                     facts={"window_verified": "webby"})
    ids["tb3"] = act(store, t1 + 7, "explorer", "click", role="Button", name="Beatline - 1 running window",
                     key="k-tb3", facts={"window_verified": "bl64"})
    t2 = t0 + 800
    ids["switch"] = act(store, t2, "zenny", "switch", resolved="event", facts={"window_verified": "zenny"})
    t3 = t0 + 1200
    ids["front"] = act(store, t3, "explorer", "click", role="Button", name="Webby", key="k-tb2",
                       facts={"window_verified": "webby"})
    ids["link"] = act(store, t3 + 8, "webby", "click", role="Hyperlink", key="k-link", position="2/List",
                      facts={"page_reached": "pg-1"}, page_key="pg-1")
    ids["scroll"] = act(store, t3 + 20, "webby", "scroll", resolved="point")
    return ids


def _window(store: Any, t0: float) -> T.TaskWindow:
    eid = episode(store, t0 - 10, t0 + 1300)
    return T.TaskWindow(t0 - 10, t0 + 1300, [(eid, t0 - 10, t0 + 1300)])


def _fid(facts: dict[int, list[T.Fact]], action: int, kind: str) -> str:
    return next(f.fid for f in facts[action] if f.kind == kind)


# -- program names --------------------------------------------------------------------------------------------------


def test_program_names_come_from_version_info_and_unknown_apps_keep_their_key(names):
    T.prime_program_names(["bl64", "webby", "chatter"])              # one scan of the running programs for all
    assert T.program_name("bl64") == "Beatline Studio"
    assert T.program_name("webby") == "Webby Browser"
    assert T.program_name("chatter") == "chatter"                    # Windows has no name for it: as recorded
    assert T.program_name("") == ""


def test_step_lines_and_the_model_input_name_each_app_by_its_program(store, names):
    ids = seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    seen: list[str] = []
    worker = Worker([lambda kw: seen.append(kw["user"]) or {"tasks": []}])
    T.TaskPass(store, worker).run_window(w, now=NOW + 1400)
    msg = seen[0]
    assert f"a{ids['drag']} " in msg and "Beatline Studio · drag a point in the window" in msg
    assert " bl64 · " not in msg and "[Beatline Studio]" in msg
    assert "a window of Webby Browser in front" in msg and "a window of chatter in front" in msg
    # the stored task keeps the process keys (other modules key by them)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, facts = tp.prepare(w)
    assert pieces[0].apps == ["bl64"]


def test_project_facts_are_given_and_the_name_only_after_three_days(store, names):
    ids = seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, _facts = tp.prepare(w)
    lines = T.project_lines(store, pieces)
    assert lines == [f"a{ids['save']}: Beatline Studio project file; tempo 128 BPM; instruments: Bass Synth, Drum Kit;"
                     " effects: Room Verb; 6 patterns; 9 channels"]                 # no name, channels or patterns
    for back in (1, 2):                                                             # the same file on 2 more days
        trace_store.write_batch(store, [], [], [{"kind": "flp", "outcome": "ok", "data": {"project": "x"},
                                                 "file_digest": "d-proj", "at": NOW - back * DAY}])
    assert T.project_lines(store, pieces)[0].endswith('; named "Night Drive"')
    seen: list[str] = []
    T.TaskPass(store, Worker([lambda kw: seen.append(kw["user"]) or {"tasks": []}])).run_window(w, now=NOW + 1400)
    assert "PROJECTS (facts read from project files" in seen[0] and "Verse" not in seen[0] and "Kick" not in seen[0]


# -- noise, never a task -------------------------------------------------------------------------------------------


def test_noise_pieces_are_counted_and_never_stored_as_tasks(store, names):
    ids = seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, facts = tp.prepare(w)
    assert [p.pid for p in pieces] == ["p1", "p2", "p3", "p4"]
    answer = {"tasks": [
        {"pieces": ["p1"], "goal": "You worked on a beat in Beatline Studio", "outcome": _fid(facts, ids["save"],
                                                                                              "file_saved")},
        {"pieces": ["p4"], "goal": "You read a page in Webby", "outcome": "none"}],
        "noise": ["p2", "p3", "p9", "p1"]}
    log: list[tuple[str, dict]] = []
    res = T.TaskPass(store, Worker([answer]), log=lambda t, **f: log.append((t, f))).run_window(w, now=NOW + 1400)
    assert res.tasks == 2 and res.noise == 2 and res.left_out == 0
    assert any("noise: no piece p9" in r for r in res.rejected) and any("p1 is in a task" in r for r in res.rejected)
    spans = [(r[0], r[1]) for r in store._query("SELECT started_at, ended_at FROM ui_tasks ORDER BY id")]
    assert len(spans) == 2 and all(not (NOW + 400 <= s <= NOW + 900) for s, _e in spans)   # p2, p3: no task row
    assert json.loads(store.nudge_state(T.NOISE_KEY))["today"] == 2
    assert T.health(store, now=NOW + 1400)["noise_today"] == 2
    assert T.health(store, now=NOW + 1400 + DAY)["noise_today"] == 0                       # a new day
    entry = next(f for t, f in log if t == "trace_tasks")
    assert entry["noise"] == 2 and entry["tasks"] == 2


def test_the_prompt_teaches_noise_and_program_names_with_worked_examples():
    assert '"noise"' in T.TASK_PROMPT and "noise" in T.TASK_TOOL["input_schema"]["properties"]
    assert "Beatbox Studio is a music program" in T.TASK_PROMPT
    assert "A window coming to the front is the START of the work done in that window" in T.TASK_PROMPT


# -- a window coming to the front --------------------------------------------------------------------------------------


def test_a_window_coming_to_the_front_alone_is_no_learnable_outcome(store, names):
    ids = seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, facts = tp.prepare(w)
    switch_f = _fid(facts, ids["switch"], "window_verified")
    front_f = _fid(facts, ids["front"], "window_verified")
    tb_f = _fid(facts, ids["tb2"], "window_verified")
    checked = T.check({"tasks": [
        {"pieces": ["p3"], "goal": "You opened Zenny", "outcome": switch_f},               # one switch, nothing done
        {"pieces": ["p2"], "goal": "You switched between your apps", "outcome": tb_f},     # taskbar clicks only
        {"pieces": ["p4"], "goal": "You browsed pages in Webby", "outcome": front_f}]},    # the start of the reading
        pieces, facts)
    assert [d.outcome for d in checked.drafts] == [None, None, None]
    assert sum("nothing_else_there" in r for r in checked.rejected) == 2
    assert any("work_went_on" in r for r in checked.rejected)
    tid = tp.write(w, checked.drafts[:1], None, NOW + 1400)[0]
    row = dict(store._query("SELECT outcome_fact, user_verdict FROM ui_tasks WHERE id = ?", (tid,))[0])
    assert row["outcome_fact"] == T.UNCLEAR and not T.learnable(row)


def test_tasks_stored_before_the_window_rule_are_not_learnable_when_read_back(store, names):
    """Review F3: tasks written with a window-only outcome (before the rule) are re-checked on their own rows when read
    for learning; the stored rows are never changed."""
    from yuki.memory import trace_demos as TD

    ids = seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, facts = tp.prepare(w)
    by = {p.pid: p for p in pieces}
    every = {f.fid: f for fs in facts.values() for f in fs}

    def draft(pids: list[str], goal: str, fid: str) -> T.Draft:
        mine = [by[p] for p in pids]
        rows = sorted((r for p in mine for r in p.rows), key=lambda r: (float(r["at"]), int(r["id"])))
        return T.Draft(mine, goal, every[fid], [], rows)

    old = [draft(["p3"], "You opened Zenny", _fid(facts, ids["switch"], "window_verified")),
           draft(["p4"], "You browsed pages in Webby", _fid(facts, ids["front"], "window_verified")),
           draft(["p1", "p2"], "You made a beat and came back to it", _fid(facts, ids["tb3"], "window_verified"))]
    tids = tp.write(w, old, None, NOW + 1400)
    rows = {tid: dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (tid,))[0]) for tid in tids}
    assert all(r["outcome_fact"] == "window_verified" for r in rows.values())
    assert all(T.learnable(r) for r in rows.values())                           # without the store: as before
    assert [T.learnable(rows[t], store) for t in tids] == [False, False, True]
    assert [T.stored_window_only(store, rows[t]) for t in tids] == ["nothing_else_there", "work_went_on", None]
    due = {d["id"]: d["learnable"] for d in TD.due_tasks(store, NOW + 1400, limit=10)}
    assert [due[t] for t in tids] == [False, False, True]                       # the demo learner's list too
    assert [r[0] for r in store._query("SELECT outcome_fact FROM ui_tasks ORDER BY id")] == ["window_verified"] * 3


def test_corrections_name_apps_by_their_program_as_the_step_lines_do(store, names):
    """Review F2: the CORRECTIONS lines name apps as the step lines do, never by the process key."""
    seed(store, NOW)
    TL.update(store, now=NOW + 1400)
    w = _window(store, NOW)
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, _facts = tp.prepare(w)
    mine = [pieces[0]]                                                          # the beat in bl64
    tid = tp.write(w, [T.Draft(mine, "You wrote some code", None, [], list(mine[0].rows))], None, NOW + 1400)[0]
    assert T.correct_task(store, tid, "renamed", "You made a beat", now=NOW + 1500)["ok"]
    seen: list[str] = []
    T.TaskPass(store, Worker([lambda kw: seen.append(kw["user"]) or {"tasks": []}])).run_window(w, now=NOW + 1600)
    fixes = seen[0].split("CORRECTIONS")[1]
    assert "You called a task in Beatline Studio" in fixes and "bl64" not in fixes


def test_a_window_brought_back_at_the_end_of_work_in_it_may_still_be_the_outcome():
    rows = [{"id": 1, "at": 0.0, "process": "notesy", "kind": "type", "source": "user"},
            {"id": 2, "at": 5.0, "process": "chatter", "kind": "click", "source": "user"},
            {"id": 3, "at": 9.0, "process": "explorer", "kind": "click", "source": "user"}]
    back = T.Fact("f1", "window_verified", 3, "notesy", "a window of notesy in front")
    assert T.window_only(back, rows) is None                                    # worked in it before, nothing after
    assert T.window_only(back, rows, {1}) == "nothing_else_there"               # its only work was a detour
    page = T.Fact("f2", "page_reached", 2, "pg", "new page")
    assert T.window_only(page, rows) is None                                    # other outcomes: untouched


def test_a_window_flicking_back_to_the_app_in_use_is_no_outcome(store, names):
    """Second verification V2: a desk-sharing tool takes the focus for a moment and gives it back to the browser the
    user was reading in. The browser's window coming back is no outcome (``already_there``), for a new answer and for
    a stored task read back by the demo learner."""
    from yuki.memory import trace_demos as TD

    t0 = NOW
    act(store, t0, "webby", "click", role="Hyperlink", key="k-l1", position="1/List", facts={"page_reached": "pg-a"},
        page_key="pg-a")
    act(store, t0 + 6, "webby", "scroll", resolved="point")
    act(store, t0 + 12, "webby", "keys", resolved="focus", chord="Ctrl+C")
    kvm = act(store, t0 + 13, "kvmshare", "switch", resolved="event", facts={"window_verified": "kvmshare"})
    back = act(store, t0 + 13.5, "webby", "switch", resolved="event", facts={"window_verified": "webby"})
    TL.update(store, now=t0 + 200)
    eid = episode(store, t0 - 10, t0 + 100)
    w = T.TaskWindow(t0 - 10, t0 + 100, [(eid, t0 - 10, t0 + 100)])
    tp = T.TaskPass(store, Worker())
    _rows, _links, pieces, facts = tp.prepare(w)
    back_f = _fid(facts, back, "window_verified")
    checked = T.check({"tasks": [{"pieces": [p.pid for p in pieces], "goal": "You read pages in Webby",
                                  "outcome": back_f}]}, pieces, facts)
    assert checked.drafts[0].outcome is None and any("already_there" in r for r in checked.rejected)
    rows = checked.drafts[0].rows
    assert [int(r["id"]) for r in rows][-2:] == [kvm, back]
    fact = next(f for f in facts[back] if f.kind == "window_verified")
    assert T.window_only(fact, rows) == "already_there"
    # stored before this rule (as two real tasks were): read back, it is not learnable
    tid = tp.write(w, [T.Draft(list(pieces), "You read pages in Webby", fact, [], rows)], None, t0 + 200)[0]
    row = dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (tid,))[0])
    assert row["outcome_fact"] == "window_verified" and T.learnable(row)        # without the store: as stored
    assert T.stored_window_only(store, row) == "already_there" and not T.learnable(row, store)
    assert {d["id"]: d["learnable"] for d in TD.due_tasks(store, t0 + 200, limit=10)}[tid] is False
