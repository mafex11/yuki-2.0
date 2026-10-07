"""capC6 (report 78 §5.4, §12 C6; report 101): the workflow graph's queries (yuki.memory.trace_graph).

Each question on a synthetic database built from C2 rows (tasks written as the task pass writes them, families from
``trace_families.refresh`` with a stand-in embedder, transitions as C2 keeps them): how do I usually do X, what do I do
in <app>, what comes next (after an element, after a task), which apps a task spans, what did I do between T1 and T2,
and the checked evidence. Rejected tasks never shown as the user's, unclear ones never evidence, names only once the
element registry keeps them, every list capped, indexed reads, local time.

Offline: temp databases, invented apps, names and goals.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from test_c_practice import Words, vec
from test_capC2_families import ATTACH, BUILD, FILE, SEND, SWITCHER, task, three_reports
from test_capC2_links import DAY, NOW, act, store  # noqa: F401 - the store fixture
from yuki.memory import trace_families as F
from yuki.memory import trace_graph as G
from yuki.memory import trace_tasks as T


def _family_of(store: Any, tid: int) -> int:
    return int(store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (tid,))[0][0])


def window_tasks(store: Any, t0: float, specs: list[tuple[str, list[tuple], str | None]]) -> list[int]:
    """Several tasks in one episode window (as one task pass writes them), each ``(goal, steps, outcome)``, steps a
    second apart and the tasks a minute apart."""
    drafts, at = [], t0
    for goal, steps, outcome in specs:
        ids = []
        for i, (proc, kind, role, name, key, chord) in enumerate(steps):
            ids.append(act(store, at + i, proc, kind, role=role, name=name, key=key, chord=chord,
                           resolved="focus" if kind == "keys" else "point",
                           facts={outcome: True} if outcome and i == len(steps) - 1 else None))
        rows = [r for r in T.window_rows(store, at - 0.5, at + len(steps)) if T.kept(r)]
        fact = T.Fact("f1", outcome, ids[-1], None, outcome) if outcome else None
        drafts.append(T.Draft([T.Piece("p1", at, at + len(steps), rows)], goal, fact, [], rows))
        at += 60
    w = T.TaskWindow(t0 - 1, at + 60, [])
    return T.TaskPass(store, None).write(w, drafts, None, at + 60)


# -- how do I usually do X -------------------------------------------------------------------------------------------


def test_how_usually_gives_the_nearest_family_with_its_usual_path_branches_counts_and_last_time(store):
    reports = three_reports(store)
    task(store, NOW + 3 * DAY, "You built the planner in the terminal", [BUILD], outcome="command_ok")
    F.refresh(store, Words(), now=NOW + 3 * DAY + 100)
    got = G.how_usually(store, vec("send the weekly report to the team"), now=NOW + 3 * DAY + 200)
    assert got and got[0]["label"].startswith("You sent") and got[0]["tasks"] == 3 and got[0]["of"] == 3
    fam = got[0]
    assert [s["words"] for s in fam["steps"]] == ['in chatter: click Button "Attach"',
                                                  "in chatter: click an unnamed ListItem", "in chatter: press Enter"]
    assert all(s["n"] == 3 for s in fam["steps"]) and fam["apps"] == ["chatter"]
    assert fam["branches"] == [{"words": "in chatter: press Ctrl+K", "n": 1, "where": "at the start"}]
    assert fam["last_at"] >= NOW + 2 * DAY and fam["last_words"].endswith(("AM", "PM"))
    assert fam["latest"]["goal"] == "You sent your weekly report to the team" and fam["latest"]["checked"] is True
    assert fam["latest"]["outcome"] == "typed text sent" and fam["cosine"] >= G.HOW_FLOOR
    assert "weekly-report" not in json.dumps(got)                    # a content control's name: never
    assert G.how_usually(store, vec("water the tomato plants")) == []  # nothing near
    assert G.how_usually(store, None) == []
    # only families spanning an app in play
    assert G.how_usually(store, vec("send the weekly report to the team"), apps=["warp"]) == []
    assert G.how_usually(store, vec("send the weekly report to the team"), apps=["Chatter.exe"])[0]["tasks"] == 3
    assert reports


def test_families_by_recency_and_by_app_capped(store):
    three_reports(store)
    task(store, NOW + 3 * DAY, "You built the planner in the terminal", [BUILD], outcome="command_ok")
    F.refresh(store, Words(), now=NOW + 3 * DAY + 100)
    recent = G.families(store)
    assert [f["label"] for f in recent][0] == "You built the planner in the terminal"
    one = recent[0]
    assert one["tasks"] == 1 and [s["words"] for s in one["steps"]] == ["in warp: run a terminal command"]
    assert one["branches"] == []                                      # a family of one: its route, no branches
    assert [f["tasks"] for f in G.families(store, app="chatter")] == [3]
    assert len(G.families(store, limit=1)) == 1 and len(G.families(store, limit=99)) <= G.MAX_FAMILIES


# -- the user's corrections ------------------------------------------------------------------------------------------


def test_rejected_tasks_are_never_shown_as_the_users_and_never_label_a_family(store):
    reports = three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    fid = _family_of(store, reports[0])
    T.correct_task(store, reports[2], "not_mine")
    out = G.tasks_between(store, NOW - 10, NOW + 4 * DAY)
    assert [t["id"] for t in out["tasks"]] == reports[:2] and out["left_out"] == 1
    fam = G.families(store, app="chatter")[0]
    assert fam["tasks"] == 2 and fam["latest"]["goal"] == "You sent the weekly report to the team chat"
    # a family whose label came from a task the user then rejected never shows that label
    store._write(lambda c: c.execute("UPDATE ui_families SET label_task = ?, label_ciphertext = ? WHERE id = ?",
                                     (reports[2], store._enc("You sent your weekly report to the team"), fid)))
    assert G.families(store, app="chatter")[0]["label"] == ""
    assert G.next_after_task(store, reports[2]) == {} and G.task_apps(store, reports[2]) == []
    # a renamed task shows the user's words, marked
    T.correct_task(store, reports[1], "renamed", "You posted the report for the team")
    t1 = [t for t in G.tasks_between(store, NOW - 10, NOW + 4 * DAY)["tasks"] if t["id"] == reports[1]][0]
    assert t1["goal"] == "You posted the report for the team" and t1["renamed"] is True


# -- what do I do in <app>, names only after promotion ---------------------------------------------------------------


def test_in_app_names_only_what_the_registry_keeps_and_transitions_read_by_the_key(store):
    day0 = NOW
    act(store, day0, "drawer", "menu", role="MenuItem", name="Export", key="k-exp")
    act(store, day0 + 1, "drawer", "click", role="Button", name="Save", key="k-save")
    act(store, day0 + 2, "drawer", "click", role="ListItem", name="holiday-photos.png", key="k-photo")
    store._write(lambda c: c.executemany(
        "INSERT INTO ui_transitions(process, from_key, to_key, n, median_gap_s, gaps, last_at) VALUES (?,?,?,?,?,?,?)",
        [("drawer", "k-exp", "k-save", 9, 2.4, "[]", day0 + 1), ("drawer", "k-save", "k-photo", 4, 75.0, "[]", day0 + 2),
         ("drawer", "k-photo", "k-gone", 7, 1.0, "[]", day0 + 2), ("other", "k-exp", "k-save", 99, 1.0, "[]", day0)]))
    got = G.transitions(store, "drawer")
    assert [(t["from"], t["to"], t["n"]) for t in got] == [("an unnamed MenuItem", 'Button "Save"', 9),
                                                          ('Button "Save"', "an unnamed ListItem", 4)]
    assert got[0]["gap"] == "about 2 s later" and got[1]["gap"] == "about 1 min later"   # k-gone: forgotten, left out
    assert "holiday" not in json.dumps(got)
    # the menu item's name is kept only after 3 distinct days: then it is named
    act(store, day0 + DAY, "drawer", "menu", role="MenuItem", name="Export", key="k-exp")
    assert G.transitions(store, "drawer")[0]["from"] == "an unnamed MenuItem"
    act(store, day0 + 2 * DAY, "drawer", "menu", role="MenuItem", name="Export", key="k-exp")
    assert G.transitions(store, "drawer")[0]["from"] == 'MenuItem "Export"'
    nxt = G.next_after_element(store, "k-exp")
    assert nxt["element"] == 'MenuItem "Export"' and nxt["app"] == "drawer"
    assert nxt["transitions"] == [{"to": 'Button "Save"', "n": 9, "gap": "about 2 s later"}]   # its own app only
    assert G.next_after_element(store, "k-unknown") == {}
    view = G.in_app(store, "Drawer.exe")
    assert view["app"] == "drawer" and len(view["transitions"]) == 2 and view["families"] == []


def test_in_app_carries_the_go_tos_and_shortcuts_without_counting_them_as_served(store):
    from yuki.memory import trace_habits as H

    for d in range(3):
        act(store, NOW + d * DAY, "chatter", "click", role="Button", name="Attach", key="k-attach")
        act(store, NOW + d * DAY + 1, "chatter", "keys", role="Edit", key="k-edit", chord="Ctrl+K", resolved="focus")
    H.update(store, now=NOW + 3 * DAY)
    view = G.in_app(store, "chatter", now=NOW + 3 * DAY)
    assert view["elements"][0]["label"] == 'Button "Attach"' and view["elements"][0]["days"] == 3
    assert view["chords"][0]["chord"] == "Ctrl+K"
    assert H.served_today(store, now=NOW + 3 * DAY) == 0


def test_path_steps_name_a_menu_item_only_once_kept(store):
    menu = ("drawer", "menu", "MenuItem", "Export", "k-exp", None)
    save = ("drawer", "click", "Button", "Save", "k-save", None)
    task(store, NOW, "You exported the drawing", [menu, save], outcome="file_saved")
    task(store, NOW + 10, "You exported the drawing again", [menu, save], outcome="file_saved")
    F.refresh(store, Words(), now=NOW + 200)
    steps = [s["words"] for s in G.families(store, app="drawer")[0]["steps"]]
    assert steps == ["in drawer: choose an unnamed MenuItem", 'in drawer: click Button "Save"']
    act(store, NOW + DAY, "drawer", "menu", role="MenuItem", name="Export", key="k-exp")
    act(store, NOW + 2 * DAY, "drawer", "menu", role="MenuItem", name="Export", key="k-exp")
    steps = [s["words"] for s in G.families(store, app="drawer")[0]["steps"]]
    assert steps[0] == 'in drawer: choose MenuItem "Export"'


# -- what comes next after a task, which apps it spans ---------------------------------------------------------------


def test_what_comes_next_after_a_task_is_the_family_of_the_next_task_in_the_same_stretch(store):
    paste = ("notes", "paste", "Edit", None, "k-note", None)
    first = []
    for d in range(3):
        ids = window_tasks(store, NOW + d * DAY, [("You copied the seed list", [ATTACH, FILE], "selected"),
                                                  ("You pasted the seed list into your notes", [paste], "field_submitted")])
        first.append(ids[0])
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    nxt = G.next_after_task(store, first[0])
    assert nxt["followed_by"] and nxt["followed_by"][0]["label"].startswith("You pasted")
    assert nxt["followed_by"][0]["n"] == 3 and nxt["followed_by"][0]["of"] == 3
    fam = G.families(store, app="chatter")[0]
    assert fam["followed_by"][0]["n"] == 3
    apps = G.task_apps(store, first[0])
    assert apps == [{"app": "chatter", "steps": 2}]


def test_task_apps_lists_each_app_with_its_steps(store):
    tid = task(store, NOW, "You sent the build log to the team", [BUILD, ATTACH, FILE, SEND], outcome="field_submitted")
    assert G.task_apps(store, tid) == [{"app": "chatter", "steps": 3}, {"app": "warp", "steps": 1}]
    assert G.task_apps(store, 999) == []


# -- what did I do between T1 and T2 ---------------------------------------------------------------------------------


def test_tasks_between_gives_goals_outcomes_and_apps_in_local_time_and_is_capped(store):
    a = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND], outcome="field_submitted")
    b = task(store, NOW + 3600, "You read the seed swap forum", [("webby", "click", "Button", "Next", "k-next", None)])
    task(store, NOW + 3 * DAY, "You built the planner", [BUILD], outcome="command_ok")
    out = G.tasks_between(store, NOW - 60, NOW + 2 * 3600, now=NOW + 3 * 3600)
    assert [t["id"] for t in out["tasks"]] == [a, b] and out["more"] == 0 and out["left_out"] == 0
    ta, tb = out["tasks"]
    assert ta["checked"] is True and ta["outcome_phrase"] == "typed text sent" and ta["apps"] == ["chatter"]
    assert ta["outcome_action"] and ta["outcome_at"] == NOW + 2
    assert tb["checked"] is False and tb["outcome"] == "unclear" and tb["outcome_phrase"].startswith("not checked")
    d = datetime.fromtimestamp(NOW)
    assert ta["when"].startswith("today") and ta["when"].endswith(("AM", "PM"))
    assert str(d.hour % 12 or 12) in ta["when"]                       # the PC's local clock
    # a task that began before the range and runs into it is in it
    assert [t["id"] for t in G.tasks_between(store, NOW + 1.5, NOW + 10)["tasks"]] == [a]
    capped = G.tasks_between(store, NOW - 60, NOW + 4 * DAY, limit=1)
    assert len(capped["tasks"]) == 1 and capped["more"] == 2
    assert len(G.tasks_between(store, NOW - 60, NOW + 4 * DAY, limit=500)["tasks"]) <= G.MAX_TASKS


def test_the_range_reads_use_the_indexes(store):
    plan = " ".join(str(r[-1]) for r in store._query(
        "EXPLAIN QUERY PLAN " + G._TASK_SQL + " ORDER BY t.started_at, t.id LIMIT ?", (0.0, 1.0, 0.0, 5)))
    assert "ui_tasks_span" in plan
    plan = " ".join(str(r[-1]) for r in store._query(
        "EXPLAIN QUERY PLAN SELECT from_key, to_key, n FROM ui_transitions WHERE process = ? AND from_key = ?"
        " ORDER BY n DESC LIMIT 5", ("x", "y")))
    assert "USING INDEX" in plan or "PRIMARY KEY" in plan


# -- checked evidence ------------------------------------------------------------------------------------------------


def test_done_evidence_is_only_learnable_tasks_citing_their_real_ids(store):
    sent = task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND], outcome="field_submitted")
    task(store, NOW + 600, "You read the seed swap forum", [("webby", "click", "Button", "Next", "k-next", None)])
    gone = task(store, NOW + 1200, "You saved the planting plan", [("drawer", "click", "Button", "Save", "k-s", None)],
                outcome="file_saved")
    one = task(store, NOW + 1800, "You switched the dark theme on", [SWITCHER], outcome="toggle_state")
    T.correct_task(store, gone, "not_mine")
    T.correct_task(store, one, "not_one")
    ev = G.done_evidence(store, NOW - 60, NOW + 3600)
    assert [e["task"] for e in ev] == [sent]                          # unclear and both rejections: never
    e = ev[0]
    row = store._query("SELECT outcome_action FROM ui_tasks WHERE id = ?", (sent,))[0][0]
    assert e["action"] == row and store._query("SELECT at FROM ui_actions WHERE id = ?", (row,))[0][0] == e["at"]
    assert e["words"] == "you sent what you typed" and e["clock"].endswith(("AM", "PM")) and e["fact"] == "field_submitted"
    assert all(T.learnable(dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (x["task"],))[0])) for x in ev)
    assert G.done_evidence(store, NOW + 100, NOW + 3600) == []         # its outcome lies outside the range
    many = [task(store, NOW + 2 * DAY + k * 10, f"You saved draft number {k} of the plan",
                 [("drawer", "click", "Button", "Save", "k-s", None)], outcome="file_saved") for k in range(20)]
    assert len(G.done_evidence(store, NOW + 2 * DAY - 10, NOW + 3 * DAY, limit=99)) == G.MAX_EVIDENCE
    assert G.done_evidence(store, NOW + 2 * DAY - 10, NOW + 3 * DAY, limit=3)[0]["task"] == many[-1]   # newest first


def test_every_query_is_quiet_on_a_database_without_the_tables():
    class Broken:
        def _query(self, *a: Any, **k: Any) -> Any:
            raise RuntimeError("no such table")

    b = Broken()
    assert G.how_usually(b, vec("x")) == [] and G.families(b) == [] and G.transitions(b, "x") == []
    assert G.next_after_element(b, "k") == {} and G.next_after_task(b, 1) == {} and G.task_apps(b, 1) == []
    assert G.tasks_between(b, 0, 1) == {"tasks": [], "more": 0, "left_out": 0} and G.done_evidence(b, 0, 1) == []


def test_output_caps_hold_on_a_long_family(store):
    steps = [("drawer", "click", "Button", f"Tool {i:02d} " + "x" * 120, f"k-t{i}", None) for i in range(30)]
    task(store, NOW, "You drew the long garden map", steps, outcome="file_saved")
    task(store, NOW + 100, "You drew the long garden map again", steps, outcome="file_saved")
    F.refresh(store, Words(), now=NOW + 300)
    fam = G.families(store, app="drawer")[0]
    assert len(fam["steps"]) == G.MAX_STEPS and all(len(s["words"]) <= G.STEP_CHARS for s in fam["steps"])
    assert len(fam["label"]) <= G.LABEL_CHARS


# -- review fixes (report 101, Review fixes) -------------------------------------------------------------------------

BOLD = ("chatter", "click", "Button", "Bold", "k-bold", None)
PIN = ("chatter", "click", "Button", "Pin", "k-pin", None)
EMOJI = ("chatter", "click", "Button", "Emoji", "k-emoji", None)


def five_routes(store: Any) -> int:
    """Five routes of one family: Bold and Pin taken 2 of 5 times (on the backbone, off the usual path), an Emoji
    taken once after the file."""
    goal = "You sent the weekly report to the team"
    routes = [[ATTACH, BOLD, FILE, SEND, PIN], [ATTACH, BOLD, FILE, SEND, PIN], [ATTACH, FILE, EMOJI, SEND],
              [ATTACH, FILE, SEND], [ATTACH, FILE, SEND]]
    ids = [task(store, NOW + d * DAY, goal, r, outcome="field_submitted") for d, r in enumerate(routes)]
    F.refresh(store, Words(), now=NOW + 5 * DAY + 100)
    return _family_of(store, ids[0])


def test_a_branch_says_after_which_shown_step_never_a_backbone_index(store):
    fid = five_routes(store)
    f = store._query(f"SELECT {G._FAMILY_COLUMNS} FROM ui_families WHERE id = ?", (fid,))[0]
    keys = [nd["k"] for nd in json.loads(f["path_keys"])]
    assert "k-bold" in keys and keys.index("k-bold") < keys.index("k-file")   # the scenario: Bold on the backbone
    fam = G.family_view(store, f)
    words = [s["words"] for s in fam["steps"]]
    assert words == ['in chatter: click Button "Attach"', "in chatter: click an unnamed ListItem",
                     "in chatter: press Enter"]
    emoji = [b for b in fam["branches"] if "Emoji" in b["words"]]
    assert emoji and emoji[0]["where"] == "after step 2"                # after the file (shown step 2), not "3"
    for b in fam["branches"]:                                          # every "after step N" points at a shown step
        if b["where"].startswith("after step "):
            assert 1 <= int(b["where"].split()[-1]) <= len(words)


def test_what_comes_next_after_an_element_is_the_usual_paths_next_step(store):
    five_routes(store)
    nxt = G.next_after_element(store, "k-attach")
    assert [p["next"] for p in nxt["paths"]] == ["in chatter: click an unnamed ListItem"]   # all 5 took it, not Bold
    # after a minority step, the next usual step still
    assert [p["next"] for p in G.next_after_element(store, "k-bold")["paths"]] == ["in chatter: click an unnamed ListItem"]
    # the last usual step has no next usual step (Pin, 2 of 5, is never "what comes next")
    assert G.next_after_element(store, "k-edit")["paths"] == []


def test_a_family_of_one_still_gives_its_routes_next_step(store):
    task(store, NOW, "You pinned the weekly report", [ATTACH, BOLD, PIN], outcome="selected")
    F.refresh(store, Words(), now=NOW + 100)
    assert [p["next"] for p in G.next_after_element(store, "k-attach")["paths"]] == ['in chatter: click Button "Bold"']


def test_done_evidence_for_a_thread_is_never_crowded_out_by_newer_tasks_elsewhere(store):
    from yuki.memory.store import WorkOp

    op = WorkOp("starts", None, title="Garden planner", state="half done", open_items=[], at=NOW - DAY)
    store.apply_work([op])
    wid = int(op.result_id)
    old = task(store, NOW, "You saved the planting plan", [("drawer", "click", "Button", "Save", "k-s", None)],
               outcome="file_saved")
    jid = store.add_journal(NOW + 0.5, None, "Drawer", "The user saved the planting plan.", 6)
    store._write(lambda c: c.execute("UPDATE journal SET work_thread_id = ? WHERE id = ?", (wid, jid)))
    for k in range(G.MAX_EVIDENCE + 2):
        task(store, NOW + 2 * DAY + k * 10, f"You saved draft number {k}",
             [("drawer", "click", "Button", "Save", "k-s", None)], outcome="file_saved")
    assert old not in [e["task"] for e in G.done_evidence(store, NOW - 60, NOW + 3 * DAY)]     # the plain read: cut
    on = G.done_evidence(store, NOW - 60, NOW + 3 * DAY, thread=wid)
    assert [e["task"] for e in on] == [old]
    # a thread merged into another counts as it
    op2 = WorkOp("starts", None, title="Garden", state="half done", open_items=[], at=NOW + 3 * DAY)
    store.apply_work([op2])
    store._write(lambda c: c.execute("UPDATE work_threads SET merged_into = ?, status = 'merged' WHERE id = ?",
                                     (int(op2.result_id), wid)))
    assert [e["task"] for e in G.done_evidence(store, NOW - 60, NOW + 3 * DAY, thread=int(op2.result_id))] == [old]
    assert G.done_evidence(store, NOW - 60, NOW + 3 * DAY, thread=999_999) == []
    plan = " ".join(str(r[-1]) for r in store._query(
        "EXPLAIN QUERY PLAN SELECT 1 FROM journal j WHERE j.work_thread_id IN (1, 2) AND j.at >= 0 AND j.at < 1"))
    assert "journal_work_thread" in plan


def test_in_app_can_skip_the_families_a_caller_already_read(store):
    three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    assert G.in_app(store, "chatter")["families"] and G.in_app(store, "chatter", with_families=False)["families"] == []
