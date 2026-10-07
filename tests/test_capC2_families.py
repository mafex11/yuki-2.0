"""capC2 (report 78 §5.4, §6.4; report 98): families, usual paths, and what the brain sees.

Families from the goal's embedding (a stand-in embedder here) plus the longest common subsequence over element keys;
a family of 2 tasks or more gets a usual path (merge and branch, counts and last use, element keys only, detours
dropped). practice.relevant() gains ``path`` (at most 8 steps, worded from kept names only) and ``example`` (one
trimmed past task); the thinker's frame keeps its 2,500-character cap and the pitfalls whole; the tool schemas stay
under 39,000 characters; the talker gets neither.

Offline: temp databases, invented apps, names and goals.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

from test_c_practice import Words, vec
from test_capC2_links import DAY, NOW, act, store  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import trace_families as F
from yuki.memory import trace_tasks as T


def task(store: Any, t0: float, goal: str, steps: list[tuple], *, detour: tuple[int, int] | None = None,
         outcome: str | None = None) -> int:
    """Write the steps (``(process, kind, role, name, key, chord)``) a second apart and one task over them (as the
    pass writes it); ``detour``: indexes of the steps that were a detour; ``outcome``: a fact kind on the last step."""
    ids = []
    for i, (proc, kind, role, name, key, chord) in enumerate(steps):
        last = i == len(steps) - 1
        ids.append(act(store, t0 + i, proc, kind, role=role, name=name, key=key, chord=chord,
                       resolved="focus" if kind == "keys" else "point",
                       facts={outcome: True} if outcome and last else None))
    rows = [r for r in T.window_rows(store, t0 - 1, t0 + len(steps) + 1) if T.kept(r)]
    piece = T.Piece("p1", t0, t0 + len(steps), rows)
    fact = T.Fact("f1", outcome, ids[-1], None, outcome) if outcome else None
    detours = [(ids[detour[0]], ids[detour[1]])] if detour else []
    draft = T.Draft([piece], goal, fact, detours, rows)
    eid = store._write(lambda c: c.execute(
        "INSERT INTO episodes(started_at, ended_at, window_start, window_end, final, text_ciphertext, created_at)"
        " VALUES (?,?,?,?,1,?,?)", (t0 - 1, t0 + 60, t0 - 1, t0 + 60, store._enc("x"), t0 + 60)).lastrowid)
    w = T.TaskWindow(t0 - 1, t0 + 60, [(int(eid), t0 - 1, t0 + 60)])
    return T.TaskPass(store, None).write(w, [draft], None, t0 + 60)[0]


ATTACH = ("chatter", "click", "Button", "Attach", "k-attach", None)
FILE = ("chatter", "click", "ListItem", "weekly-report.pdf", "k-file", None)
SEND = ("chatter", "keys", "Edit", None, "k-edit", "Enter")
REPLY = ("chatter", "click", "Button", "Reply", "k-reply", None)
SWITCHER = ("chatter", "keys", "Edit", None, "k-edit2", "Ctrl+K")
BUILD = ("warp", "command", None, None, None, None)


def three_reports(store: Any) -> list[int]:
    return [task(store, NOW, "You sent the weekly report to the team", [ATTACH, FILE, SEND], outcome="field_submitted"),
            task(store, NOW + DAY, "You sent the weekly report to the team chat",
                 [ATTACH, REPLY, REPLY, FILE, SEND], detour=(1, 2), outcome="field_submitted"),
            task(store, NOW + 2 * DAY, "You sent your weekly report to the team",
                 [SWITCHER, ATTACH, FILE, SEND], outcome="field_submitted")]


def test_families_from_the_goal_and_the_route_with_a_usual_path_and_its_branches(store):
    reports = three_reports(store)
    build = task(store, NOW + 3 * DAY, "You built the planner in the terminal", [BUILD], outcome="command_ok")
    out = F.refresh(store, Words(), now=NOW + 3 * DAY + 100)
    assert out["placed"] == 4 and out["new"] == 2
    fams = {r["id"]: dict(r) for r in store._query("SELECT * FROM ui_families")}
    fam_of = {r["id"]: r["family_id"] for r in store._query("SELECT id, family_id FROM ui_tasks")}
    assert len({fam_of[t] for t in reports}) == 1 and fam_of[build] != fam_of[reports[0]]
    fam = fams[fam_of[reports[0]]]
    assert fam["n_tasks"] == 3 and json.loads(fam["apps"]) == ["chatter"]
    nodes = json.loads(fam["path_keys"])
    usual = [nd["k"] for nd in F.usual(nodes, 3)]
    assert usual == ["k-attach", "k-file", "chatter|keys|Enter"]                    # the detour never in the path
    assert all(nd["n"] == 3 for nd in F.usual(nodes, 3)) and nodes[0]["last"] >= NOW + 2 * DAY
    branches = json.loads(fam["variants_keys"])
    assert branches == [{"after": -1, "keys": ["chatter|keys|Ctrl+K"], "attrs": [{"kind": "keys", "app": "chatter"}],
                         "n": 1, "last": NOW + 2 * DAY}]
    text = json.dumps(fams, default=str)
    assert "Attach" not in text and "weekly" not in text                            # element keys only, label encrypted
    assert store._dec(fam["label_ciphertext"]).startswith("You sent")
    assert F.refresh(store, Words(), now=NOW + 3 * DAY + 200) == {"tasks": 0}       # nothing changed: nothing done


def test_corrections_take_tasks_out_and_a_rename_places_the_task_again(store):
    reports = three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (reports[0],))[0][0]
    T.correct_task(store, reports[1], "not_mine")
    F.refresh(store, Words(), now=NOW + 3 * DAY + 10)
    assert store._query("SELECT n_tasks FROM ui_families WHERE id = ?", (fid,))[0][0] == 2
    assert store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (reports[1],))[0][0] is None
    T.correct_task(store, reports[2], "renamed", "You planned a garden bed in the notes")
    out = F.refresh(store, Words(), now=NOW + 3 * DAY + 20)
    assert out["embedded"] == 1
    assert store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (reports[2],))[0][0] != fid
    T.correct_task(store, reports[0], "not_one")
    F.refresh(store, Words(), now=NOW + 3 * DAY + 30)
    assert not store._query("SELECT 1 FROM ui_families WHERE id = ?", (fid,))      # no task left: gone


def test_lcs_and_merge_branch_helpers():
    assert F.lcs_pairs(list("abcde"), list("xbdy")) == [(1, 1), (3, 2)]
    assert F.lcs_ratio(["a", "b"], ["a", "b", "c", "d"]) == 1.0 and F.lcs_ratio([], ["a"]) == 0.0
    r = lambda *ks: [(k, {"kind": "click", "app": "x"}, float(i)) for i, k in enumerate(ks)]   # noqa: E731
    nodes, branches = F.build_path([r("a", "b", "c"), r("a", "z", "c"), r("a", "b", "c", "d")])
    assert [n["k"] for n in nodes] == ["a", "b", "c"] and [n["n"] for n in nodes] == [3, 2, 3]
    assert {(b["after"], tuple(b["keys"]), b["n"]) for b in branches} == {(0, ("z",), 1), (2, ("d",), 1)}
    assert [n["k"] for n in F.usual(nodes, 3)] == ["a", "b", "c"]


def _brain(store: Any, words: str, **kw: Any) -> dict[str, Any]:
    return P.relevant(store, query_vec=vec(words), apps_in_play=["chatter"], now=NOW + 3 * DAY + 60, **kw)


def test_relevant_gives_the_usual_path_and_one_example_worded_from_kept_names(store):
    three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    out = _brain(store, "send the weekly report to the team")
    path, ex = out["path"], out["example"]
    assert path["tasks"] == 3 and path["family"].startswith("You sent")
    assert path["steps"] == ['in chatter: click Button "Attach"', "in chatter: click an unnamed ListItem",
                             "in chatter: press Enter"]
    assert "weekly-report" not in json.dumps(out)                                   # a content name: never shown
    assert ex["goal"] == "You sent your weekly report to the team" and ex["outcome"] == "you sent what you typed"
    assert ex["steps"][0] == "in chatter: press Ctrl+K" and ex["when"].endswith(("AM", "PM"))
    assert len(path["steps"]) <= F.PATH_STEPS and sum(len(s) + 2 for s in path["steps"]) <= F.PATH_CHARS
    more = _brain(store, "send the weekly report to the team", detail=True)
    assert more["path"]["variants"] == ["1 of the last 3 times, at the start: in chatter: press Ctrl+K"]
    assert _brain(store, "water the tomato plants")["path"] is None                 # nothing near: no path
    assert P.relevant(store, query_vec=vec("send the weekly report"), apps_in_play=["chatter"],
                      go_tos=False).get("path") is None                             # the talker: none
    assert P.relevant(store, query_vec=None, apps_in_play=["chatter"])["path"] is None


def test_a_family_of_one_gives_an_example_but_no_path_and_unclear_may_be_an_example(store):
    task(store, NOW, "You read the seed swap forum thread", [("webby", "click", "Button", "Next", "k-next", None)])
    F.refresh(store, Words(), now=NOW + 100)
    out = P.relevant(store, query_vec=vec("read the seed swap forum thread"), apps_in_play=["webby"], now=NOW + 200)
    assert out["path"] is None
    assert out["example"]["goal"] == "You read the seed swap forum thread"
    assert out["example"]["outcome"] == "not sure it got done"


def test_huge_paths_stay_under_the_caps(store):
    steps = [("chatter", "click", "Button", f"{'N' * 70}{i}", f"k-{i}", None) for i in range(40)]
    task(store, NOW, "You filed the long monthly expense claim", steps, outcome="field_submitted")
    task(store, NOW + DAY, "You filed the long monthly expense claim again", steps, outcome="field_submitted")
    F.refresh(store, Words(), now=NOW + 2 * DAY)
    out = P.relevant(store, query_vec=vec("file the monthly expense claim"), apps_in_play=["chatter"], now=NOW)
    assert 0 < len(out["path"]["steps"]) <= F.PATH_STEPS
    assert all(len(s) <= F.STEP_CHARS for s in out["path"]["steps"])
    assert sum(len(s) + 2 for s in out["path"]["steps"]) <= F.PATH_CHARS
    assert len(out["example"]["steps"]) <= F.EXAMPLE_STEPS and out["example"]["more"] > 0
    detail = P.relevant(store, query_vec=vec("file the monthly expense claim"), apps_in_play=["chatter"], now=NOW,
                        detail=True)
    assert F.PATH_STEPS < len(detail["path"]["steps"]) <= F.DETAIL_PATH_STEPS
    assert sum(len(s) + 2 for s in detail["path"]["steps"]) <= F.DETAIL_PATH_CHARS


def _huge_practice() -> dict[str, Any]:
    pit = {"id": 7, "ref": "pb:7", "kind": "pitfall", "intent": "a pitfall kept whole", "apps": ["chatter"],
           "label": "confidence 0.70", "text": "x " * 300}
    tos = [{"app": f"app{i}", "elements": [{"label": f'Button "{"N" * 60}{j}"', "n": 99} for j in range(10)],
            "chords": [{"chord": f"Ctrl+Shift+{j}", "n": 50} for j in range(8)]} for i in range(6)]
    path = {"family": "F" * 300, "tasks": 9, "steps": [f"in chatter: click Button \"{'S' * 100}{i}\"" for i in range(20)]}
    ex = {"goal": "G" * 300, "when": "today, 2 PM", "outcome": "you sent what you typed",
          "steps": [f"in chatter: press Ctrl+{i}" for i in range(20)]}
    return {"ways": [], "pitfalls": [pit], "go_tos": tos, "path": path, "example": ex}


def test_the_thinkers_frame_keeps_its_cap_and_the_pitfalls_with_path_and_example():
    from yuki.agent import frame as F2

    assert F2.FRAME_CHARS == 2_500
    fr = F2.build({}, practice=_huge_practice())
    assert len(fr.text) <= F2.FRAME_CHARS and "[pb:7]" in fr.text
    small = F2.build({}, practice={"ways": [], "pitfalls": [], "go_tos": [], "path": {
        "family": "You sent the weekly report", "tasks": 3, "steps": ['in chatter: click Button "Attach"',
                                                                       "in chatter: press Enter"]},
        "example": {"goal": "You sent your weekly report", "when": "yesterday, 9 AM", "outcome": "you sent what you typed",
                    "steps": ["in chatter: press Ctrl+K"]}})
    assert ('How the user usually does this (their usual path, from 3 times they did "You sent the weekly report"; '
            'hints, not steps):') in small.text
    assert '1. in chatter: click Button "Attach"' in small.text and "2. in chatter: press Enter" in small.text
    assert 'The last time the user did this (yesterday, 9 AM): "You sent your weekly report" (you sent what you typed):' \
        in small.text
    # the go-tos give way before the path
    both = F2.build({}, practice={**_huge_practice(), "pitfalls": []})
    assert "How the user usually does this" in both.text and len(both.text) <= F2.FRAME_CHARS
    assert F2.path_lines({}) == [] and F2.path_lines({"path": {"steps": []}, "example": {"goal": ""}}) == []


def test_the_schema_guard_and_the_talker_are_untouched():
    from yuki.agent.tools import tool_params
    from yuki.voice import practice as VP

    assert len(json.dumps(tool_params(cacheable=False, screenshot_policy="never"))) < 39_000
    assert VP.LESSONS_CHARS == 400 and VP.MAX_LESSONS == 3 and VP.MAX_SKILLS == 2 and VP.SKILL_CHARS == 520
    root = Path(VP.__file__).resolve().parent
    for name in ("practice.py", "mouth.py", "skills.py"):
        text = (root / name).read_text(encoding="utf-8")
        assert '"path"' not in text and '"example"' not in text
    assert "path" not in inspect.getsource(VP.lessons_block)


def test_memory_client_practice_carries_path_and_example(store):
    from yuki.memory.api import MemoryClient

    three_reports(store)
    F.refresh(store, Words(), now=NOW + 3 * DAY)
    m = MemoryClient(store)
    m._embed = lambda text, wait=None: vec(text)                                   # the stand-in embedder
    got = m.practice("send the weekly report to the team", apps=["chatter"])
    assert got["path"]["steps"][0] == 'in chatter: click Button "Attach"' and got["example"]["goal"]
    assert m.practice("send the weekly report to the team", apps=["chatter"], go_tos=False).get("path") is None
    assert m.practice("send the weekly report", apps=["chatter"], go_tos=False, paths=True)["path"] is not None
