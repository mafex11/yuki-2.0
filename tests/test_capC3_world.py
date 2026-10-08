"""capC3 (report 78 §10.3, §12 C3; report 105): the synthetic world the C3 tests share, and the time split.

A temp database built from C1 rows, C2 tasks and families and C4 ways (a stubbed learner), with invented apps
("chatter", "webby"), names and goals. The user sends a message in the team channel four times before the evaluated
task and twice after it:

* T0, T1 (days 0, 1): the quick switcher (Ctrl+K, type, Enter, type, Enter); both learned (a way: 2 demos);
* T2 (day 2): the channel list instead (click a list item, type, Enter); rejected on the Day page AFTER the cut-off;
* T3 (day 3): the quick switcher again: the evaluated task (its start is the cut-off);
* T4 (day 4): an unclear task using a new "Pin" button, plus many clicks on it (future go-tos);
* T5 (day 5): a checked task to another field, learned after the cut-off (a future trial).

Tests here: the time split (nothing at or after the cut-off informs any arm; corrections made since are taken back),
the evaluated task's choice, and the requests (never the segmenter's goals; conversation turns that led to Yuki acting).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from test_c_practice import Words
from test_capC2_links import DAY, NOW, store  # noqa: F401 - the store fixture
from test_capC4_demos import answer, learner, row, task, taskrow
from yuki.eval import c3_arms as A
from yuki.eval import c3_data as D
from yuki.memory import practice as P
from yuki.memory import trace_families as F
from yuki.memory import trace_habits
from yuki.memory import trace_tasks as T

APP, WEB = "chatter", "webby"
GOAL = "You sent a message in the team channel"
REQUEST = "send a message in the team channel"
NAMES = (lambda p: p)   # no program-name lookup in tests


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, tmp_path):
    """Never the user's settings, requests, logs or temp folder: all under the test's own folder."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    monkeypatch.delenv("YUKI_MEMORY_DIR", raising=False)
    import tempfile

    (tmp_path / "temp").mkdir(exist_ok=True)
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "temp"))
    sw = A._switch()
    if sw is not None:
        sw(None)
    yield
    if sw is not None:
        sw(None)


# -- building ----------------------------------------------------------------------------------------------------------


def switcher(store: Any, t0: float, *, field: str = "k-msg") -> list[int]:
    """The quick-switcher route: bring chatter up, Ctrl+K, type the channel, Enter, type the message, Enter (sent)."""
    return [row(store, t0, APP, "switch", resolved="event", facts={"window_verified": APP}),
            row(store, t0 + 2, APP, "keys", resolved="focus", chord="Ctrl+K", role="Edit", key="k-switch",
                position="0/Dialog"),
            row(store, t0 + 4, APP, "type", resolved="focus", role="Edit", key="k-switch", typed=6,
                position="0/Dialog"),
            row(store, t0 + 5, APP, "keys", resolved="focus", chord="Enter", role="Edit", key="k-switch",
                position="0/Dialog", facts={"window_verified": APP}),
            row(store, t0 + 9, APP, "type", resolved="focus", role="Edit", key=field, typed=22, position="1/Pane"),
            row(store, t0 + 10, APP, "keys", resolved="focus", chord="Enter", role="Edit", key=field,
                position="1/Pane", facts={"field_submitted": {"typed": 22}})]


def by_list(store: Any, t0: float) -> list[int]:
    """The channel-list route: bring chatter up, click the channel in the list, type, Enter (sent)."""
    return [row(store, t0, APP, "switch", resolved="event", facts={"window_verified": APP}),
            row(store, t0 + 3, APP, "click", role="ListItem", name="team-channel", key="k-chan", position="2/List"),
            row(store, t0 + 9, APP, "type", resolved="focus", role="Edit", key="k-msg", typed=22, position="1/Pane"),
            row(store, t0 + 10, APP, "keys", resolved="focus", chord="Enter", role="Edit", key="k-msg",
                position="1/Pane", facts={"field_submitted": {"typed": 22}})]


def distractors(store: Any, t0: float) -> None:
    """Other things on screen in the hour before the evaluated task (another app, other buttons)."""
    row(store, t0, WEB, "click", role="Button", name="Reload", key="k-reload")
    row(store, t0 + 30, WEB, "click", role="Button", name="Back", key="k-back")
    row(store, t0 + 60, APP, "click", role="Button", name="Attach", key="k-attach")
    row(store, t0 + 90, APP, "hover", role="Button", name="Settings", key="k-settings")


def learn(store: Any, tid: int, ids: list[int], at: float) -> None:
    learner(store, [answer(ids, slots={ids[2]: "channel", ids[4]: "message"},
                           intent="send a message in a channel")]).learn_task(taskrow(store, tid), now=at)


def world(store: Any, *, learned: bool = True, future: bool = True) -> dict[str, Any]:
    """The world above. ``learned`` False: no C4 ways (L has only the usual path, the example and the go-tos)."""
    w: dict[str, Any] = {}
    i0 = switcher(store, NOW)
    w["T0"] = task(store, i0, GOAL, "field_submitted", i0[-1], target="k-msg")
    i1 = switcher(store, NOW + DAY)
    w["T1"] = task(store, i1, GOAL, "field_submitted", i1[-1], target="k-msg")
    F.refresh(store, Words(), now=NOW + DAY + 100)
    if learned:
        learn(store, w["T0"], i0, NOW + 120)
        learn(store, w["T1"], i1, NOW + DAY + 120)
    i2 = by_list(store, NOW + 2 * DAY)
    w["T2"] = task(store, i2, GOAL, "field_submitted", i2[-1], target="k-msg")
    distractors(store, NOW + 3 * DAY - 1200)
    i3 = switcher(store, NOW + 3 * DAY)
    w["T3"] = task(store, i3, GOAL, "field_submitted", i3[-1], target="k-msg")
    w["cutoff"] = NOW + 3 * DAY
    w["ids3"] = i3
    F.refresh(store, Words(), now=NOW + 3 * DAY + 100)
    if future:
        i4 = [row(store, NOW + 4 * DAY, APP, "switch", resolved="event", facts={"window_verified": APP})]
        i4 += [row(store, NOW + 4 * DAY + 5 + k, APP, "click", role="Button", name="Pin", key="k-future")
               for k in range(12)]
        w["T4"] = task(store, i4, GOAL, None, None)
        i5 = switcher(store, NOW + 5 * DAY, field="k-msg2")
        w["T5"] = task(store, i5, GOAL, "field_submitted", i5[-1], target="k-msg2")
        F.refresh(store, Words(), now=NOW + 5 * DAY + 100)
        if learned:
            learn(store, w["T5"], i5, NOW + 5 * DAY + 120)
        T.correct_task(store, w["T2"], "not_mine", now=NOW + 5 * DAY + 200)
    trace_habits.update(store, now=NOW + 6 * DAY)
    return w


NOTES_GOAL = "You saved your planner notes file"
NOTES_REQUEST = "save my planner notes file"


def notes(store: Any, t0: float) -> list[int]:
    """Another kind of task: bring the notes app up, type in the page, Ctrl+S, the file saved."""
    return [row(store, t0, "notepadx", "switch", resolved="event", facts={"window_verified": "notepadx"}),
            row(store, t0 + 2, "notepadx", "type", resolved="focus", role="Document", name="Planner", key="k-doc",
                typed=40),
            row(store, t0 + 30, "notepadx", "keys", resolved="focus", chord="Ctrl+S", role="Document", key="k-doc"),
            row(store, t0 + 31, "notepadx", "file_save", resolved="event", file_ext=".txt",
                facts={"file_saved": "fd-notes"})]


def two_kinds(store: Any, *, learned: bool = False) -> dict[str, Any]:
    """The world (no future) plus the notes task on days 0-3 (the last one evaluated): two evaluated requests."""
    w = world(store, learned=learned, future=False)
    for d in range(4):
        ids = notes(store, NOW + d * DAY + 7200)
        w[f"N{d}"] = task(store, ids, NOTES_GOAL, "file_saved", ids[-1], target="fd-notes")
    F.refresh(store, Words(), now=NOW + 3 * DAY + 9000)
    return w


def own_request(at: float = NOW - DAY, text: str = REQUEST) -> D.Request:
    return D.Request(text, at, "you")


class StubThinker:
    """A stand-in for the thinker, plan-only. It follows the frame's usual path (or, failing that, the last
    example) as a model would, mapping each step's words to the snapshot's ids; with no frame it clicks the first
    control and presses Enter. ``jitter``: calls 3-4, 7-8 ... (the second pass of B0 and L when the arms run twice)
    keep only their plan's first step. Records every call."""

    def __init__(self, *, est: float = 0.01, cost: float = 0.01, jitter: bool = False) -> None:
        self.calls: list[dict[str, Any]] = []
        self.est, self.cost, self.jitter = est, cost, jitter

    def estimate(self, chars: int, *, typical: bool = False) -> float:
        return self.est / 2 if typical else self.est

    def plan(self, *, system: str, user: str, tool: dict[str, Any]) -> tuple[dict[str, Any], float]:
        self.calls.append({"system": system, "user": user, "tool": tool})
        snap = user.split("BEGIN_UNTRUSTED_DATA", 1)[1].split("END_UNTRUSTED_DATA", 1)[0]
        wins, els, cur = {}, {}, None
        for line in snap.splitlines():
            m = re.match(r"^(w\d+) (\S+) \(", line)
            if m:
                cur = m.group(1)
                wins[m.group(2)] = cur
                continue
            m = re.match(r"^\s+(e\d+) (.+)$", line)
            if m:
                els.setdefault(m.group(2), m.group(1))
        lines = user.splitlines()
        steps: list[str] = []
        for head, item in (("How the user usually does this", r"^\d+\. (.+)$"), ("The last time the user did this",
                                                                              r"^- (.+)$")):
            if steps:
                break
            on = False
            for ln in lines:
                if ln.startswith(head):
                    on = True
                    continue
                if on:
                    m = re.match(item, ln)
                    if not m:
                        break
                    steps.append(m.group(1))
        out: list[dict[str, Any]] = []
        for s in steps:
            m = re.match(r"^in (\S+): press (.+)$", s)
            if m:
                out.append({"do": "keys", "window": wins.get(m.group(1)), "keys": m.group(2)})
                continue
            m = re.match(r"^bring (\S+) to the front$", s)
            if m:
                out.append({"do": "open_app", "window": wins.get(m.group(1))})
                continue
            m = re.match(r"^in (\S+): (click|type into|paste into|use|choose|select) (.+)$", s)
            if m and m.group(3) in els:
                out.append({"do": "type" if m.group(2) == "type into" else "click", "element": els[m.group(3)]})
        if not out:
            first = next(iter(els.values()), None)
            out = ([{"do": "click", "element": first}] if first else []) + [{"do": "keys", "keys": "Enter"}]
        if self.jitter and ((len(self.calls) - 1) // 2) % 2 == 1 and len(out) > 1:
            out = out[:1]
        return {"steps": out}, self.cost


# -- the time split ------------------------------------------------------------------------------------------------


def test_the_cut_leaves_only_what_memory_held_before_the_evaluated_task(store):
    w = world(store)
    c = w["cutoff"]
    fid = int(store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (w["T3"],))[0][0])
    assert store._query("SELECT max(at) FROM ui_actions")[0][0] > c
    assert any(o.created_at >= c for o in P.live(store, include_disabled=True))      # the future trial is there
    D.cut(store, c, embedder=Words())
    assert store._query("SELECT max(at) FROM ui_actions")[0][0] < c
    assert store._query("SELECT count(*) FROM ui_tasks WHERE created_at >= ? OR started_at >= ?", (c, c))[0][0] == 0
    assert {r[0] for r in store._query("SELECT id FROM ui_tasks")} == {w["T0"], w["T1"], w["T2"]}
    # the rejection made after the cut-off is taken back: T2 is the user's again and back in its family
    t2 = dict(store._query("SELECT user_verdict, family_id FROM ui_tasks WHERE id = ?", (w["T2"],))[0])
    assert t2["user_verdict"] is None and t2["family_id"] == fid
    fam = dict(store._query("SELECT n_tasks, path_keys FROM ui_families WHERE id = ?", (fid,))[0])
    assert fam["n_tasks"] == 3 and "k-future" not in fam["path_keys"]
    assert store._query("SELECT count(*) FROM ui_family_members WHERE ended_at >= ?", (c,))[0][0] == 0
    # practice memory: nothing created, learned or used from the cut-off on; the way of T0/T1 stays a way
    assert store._query("SELECT count(*) FROM playbooks WHERE created_at >= ?", (c,))[0][0] == 0
    assert store._query("SELECT count(*) FROM playbook_events WHERE at >= ?", (c,))[0][0] == 0
    ways = [o for o in P.live(store, ["procedure"]) if o.origin == "demo"]
    assert len(ways) == 1 and not ways[0].trial and ways[0].state == "active"
    # the go-tos are folded again from the rows left: the future "Pin" button is not a go-to
    tallies = trace_habits.app_tallies(store, APP)
    assert "k-future" not in tallies["el"] and tallies["el"]
    assert max(float(v["t"]) for part in ("el", "ch") for v in tallies[part].values()) < c
    assert not store._query("SELECT 1 FROM ui_elements WHERE element_key = 'k-future'")


def test_no_arm_sees_the_future(store):
    w = world(store)
    [inst], _skipped = D.build_instances(store, [own_request()], Words(), names=NAMES)
    assert inst.target_id == w["T3"] and inst.cutoff == w["cutoff"]
    assert "k-future" not in inst.snapshot.keys() and "Pin" not in inst.snapshot.text()
    D.cut(store, inst.cutoff, embedder=Words())
    vec = D.request_vector(store, Words(), REQUEST, inst.snapshot.open_processes())
    stub = StubThinker()
    budget = A.Budget(1.0)
    arms = {"B1": A.example_arm(store, inst, vec=vec, now=inst.cutoff),
            "B0": A.yuki_arm(store, inst, stub, budget, learned=False, vec=vec, now=inst.cutoff),
            "L": A.yuki_arm(store, inst, stub, budget, learned=True, vec=vec, now=inst.cutoff)}
    for res in arms.values():
        assert "k-future" not in res.tokens and "k-msg2" not in res.tokens
    assert all("Pin" not in c["user"] for c in stub.calls)
    # B1 copies the latest task before the cut-off (T2, the list route), as it was
    assert arms["B1"].tokens == D.reference(store, w["T2"]) and arms["B1"].mode == "example"


def test_the_evaluated_task_is_the_latest_with_an_earlier_one_to_the_same_checked_end(store):
    w = world(store, future=False)
    fid = D.match_family(store, Words(), REQUEST)
    assert fid is not None
    assert int(D.pick_target(store, fid)["id"]) == w["T3"]
    # a conversation's request needs a later task of the user's: none after day 3
    assert D.pick_target(store, fid, after=NOW + 3 * DAY + 50) is None
    assert int(D.pick_target(store, fid, after=NOW + 2 * DAY + 50)["id"]) == w["T3"]
    # a task to another field has no earlier one with the same end: never evaluated
    i5 = switcher(store, NOW + 5 * DAY, field="k-msg2")
    t5 = task(store, i5, GOAL, "field_submitted", i5[-1], target="k-msg2")
    F.refresh(store, Words(), now=NOW + 5 * DAY + 100)
    assert int(D.pick_target(store, fid)["id"]) == w["T3"] != t5
    # the earlier task must have been known (labelled) before the evaluated one started
    store._write(lambda c: c.execute("UPDATE ui_tasks SET created_at = ? WHERE id IN (?, ?, ?)",
                                     (NOW + 3 * DAY + 999, w["T0"], w["T1"], w["T2"])))
    assert D.pick_target(store, fid) is None


def test_the_reference_is_the_users_own_route_and_the_snapshot_holds_its_controls_in_a_fixed_order(store):
    w = world(store, future=False)
    ref = D.reference(store, w["T3"])
    assert ref == [f"{APP}|switch", f"{APP}|keys|Ctrl+K", "k-switch", f"{APP}|keys|Enter", "k-msg",
                   f"{APP}|keys|Enter"]
    snap = D.snapshot(store, dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (w["T3"],))[0]),
                      w["cutoff"], names=NAMES)
    assert {"k-switch", "k-msg", "k-attach", "k-settings", "k-reload", "k-back"} <= snap.keys()
    assert [x.process for x in snap.windows] == [APP, WEB] and snap.windows[0].front
    labels = [e.label for e in snap.elements if e.window == "w1"]
    assert labels == sorted(labels, key=str.casefold)                     # never in the order they were used
    assert "team-channel" not in snap.text()                              # a content control's name: never


def test_requests_are_the_users_own_or_turns_that_led_to_yuki_acting_never_goals(store, tmp_path):
    assert D.add_requests([REQUEST, "  send a  message in the team channel ", "open my notes"], now=NOW) == 2
    assert D.requests_path().is_relative_to(tmp_path)                     # the test's own LOCALAPPDATA
    D.mark_steps_shown(now=NOW + 10)
    D.add_requests(["written after seeing steps"], now=NOW + 20)
    own = D.own_requests()
    assert [r.text for r in own] == [REQUEST, "open my notes", "written after seeing steps"]
    assert [r.late for r in own] == [False, False, True]
    logs = tmp_path / "logs"
    logs.mkdir()
    lines = [{"type": "user_message", "session": "s1", "turn": 1, "ts": NOW, "text": "pin the planner note"},
             {"type": "memory_acting", "session": "s1", "turn": 1, "state": "begin"},
             {"type": "user_message", "session": "s1", "turn": 2, "ts": NOW + 5, "text": "what time is it"},
             {"type": "user_message", "session": "s1", "turn": 3, "ts": NOW + 9, "text": "pin the planner note"},
             {"type": "memory_acting", "session": "s1", "turn": 3, "state": "begin"}]
    (logs / "20261002-100000-worker.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\nnot json\n",
                                                      encoding="utf-8")
    conv = D.conversation_requests(logs)
    assert [(r.text, r.origin, r.at) for r in conv] == [("pin the planner note", "conversation", NOW)]
    # the segmenter's goals never become requests: the late request is left out, the goal is nowhere
    world(store, future=False)
    insts, skipped = D.build_instances(store, [*own, *conv], Words(), names=NAMES)
    assert all(i.request.text != GOAL for i in insts)
    assert any(s["why"] == "written after planned steps were shown" for s in skipped)
    assert Path(D.requests_path()).exists()
