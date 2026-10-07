"""capC1b second verification (report 97 "Second verification"): the correction learner takes only what the user's
own presses caused, never Yuki's own slow launch or loading page, never a hover; a run that reached its goal teaches
nothing; a repeat is counted only once the user's route is known; a failed route lowers ways in its own app only.

Offline: temp databases, invented apps and names, a stand-in for the model; the real trace worker (test_trace_worker's
Rig, fakes for every Windows call) where the verifier's scenario needs it. Nothing touches the desktop.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_c_practice import NOW, Worker, store, vec  # noqa: F401 - the store fixture
from test_capC1b_learn import (WORDS, _act, _run, fix_answer, fixer, opening_run, row,  # noqa: F401
                               users_fix)
from test_trace_worker import APP, BROWSER, HWND, WALL0, FakeSink, Rig, button
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory import trace_store
from yuki.memory.trace_privacy import normalise_name

EVENT_OBJECT_SHOW = 0x8002


@pytest.fixture(autouse=True)
def _shell(monkeypatch):
    """Which invented processes are the shell (a path fact in the app; fixed here so no process list is read)."""
    monkeypatch.setattr(L, "_shell_cache", {"explorer": True, "searchhost": True, "chatter": False, "browsr": False,
                                            "browserx": False, "wordmill": False, "quillpad": False,
                                            "fieldnotes": False})


def _row(store: Any, at: float, process: str, kind: str, **cols: Any) -> int:
    """A trace row with any extra columns (``proc_started``, ``ended_at``)."""
    r = {"at": at, "ended_at": at + 0.1, "process": process, "kind": kind, "source": "user", "resolved": "event",
         "outcome": "changed", "during_run": 0, **cols}
    if isinstance(r.get("facts"), dict):
        r["facts"] = json.dumps(r["facts"])
    return trace_store.write_batch(store, [r])[0]


def no_window_run(request_id: str = "n1", at: float = NOW) -> dict[str, Any]:
    """Yuki's opening of Quillpad showed no window yet (an app slow to start): doubtful, the run failed."""
    return _run(request_id, WORDS, [{"tool": "launch_app", "kind": "action", "app": "", "target": 'app "Quillpad"',
                                     "input": {"name": "Quillpad", "no_window": True}, "ok": True, "verdict": None,
                                     "why": "no window yet", "result": "started", "ms": 900.0, "at": at + 1.0}],
                at=at)


# -- V1: Yuki's own slow launch, a loading page, a taught way ----------------------------------------------------------


def test_v1_yukis_slow_window_coming_up_while_the_user_types_elsewhere_is_never_their_fix(tmp_path):
    """The verifier's scenario on the real trace worker: Field Notes' window comes up by itself while the user types
    in the browser. The worker writes a typing row carrying window_verified and a switch row labelled user; neither
    is the user's doing. Nothing is learned and no model is called."""
    rig = Rig(tmp_path)
    store = rig.store
    try:
        rig.fg = rig.under = (HWND[BROWSER], BROWSER)
        rig.feed("fg", rig.clock.t, HWND[BROWSER], BROWSER)
        t0 = WALL0 + rig.clock.t
        P.record_run(store, _run("q1", "open field notes", [
            {"tool": "launch_app", "kind": "action", "app": "browserx", "target": 'app "Field Notes"',
             "input": {"name": "Field Notes"}, "ok": True, "verdict": False,
             "why": "the window in front is the browser's", "result": "started", "ms": 900.0, "at": t0 + 1.0}],
            at=t0))
        rig.clock.t += 6.0
        for _ in range(6):
            rig.feed("char", rig.clock.t, 1, "user", dt=0.15)
        rig.fg = (HWND[APP], APP)
        rig.feed("fg", rig.clock.t + 0.7, HWND[APP], APP, dt=0.7)
        rig.settle()
        kinds = [(r["kind"], r["source"], r["facts"]) for r in rig.rows()]
        assert ("switch", "user") in [(k, s) for k, s, _f in kinds]          # the trace's own labels, as before
        assert any(k == "type" and f and "fieldnotes" in f for k, _s, f in kinds)
        worker = Worker(store)
        assert fixer(worker).run_once(now=t0 + 40) == []                     # nothing reached: waits
        res = fixer(worker).run_once(now=t0 + 700)
        assert [r.result for r in res] == ["the user did not reach what it was for"]
        assert worker.calls == [] and P.live(store, ["procedure"]) == [] and P.live(store, ["pitfall"]) == []
    finally:
        rig.store.close()


def test_v1_a_window_yukis_own_launch_started_is_not_the_users_fix_even_after_their_click(store):
    """Quillpad's process started 0.5 s after Yuki's launch; its window comes up 9 s later just after the user
    clicked something in Chatter. Yuki's own launch (appOpen's rule, from the trace's proc_started): nothing."""
    worker = Worker(store)
    P.record_run(store, no_window_run())
    _row(store, NOW + 8.6, "chatter", "click", resolved="point", role="Button", element_key="k-reply")
    _row(store, NOW + 9.0, "quillpad", "switch", proc_started=NOW + 0.6, facts={"window_verified": "quillpad"})
    res = fixer(worker).run_once(now=NOW + 700)
    assert [r.result for r in res] == ["the user did not reach what it was for"] and worker.calls == []


def test_v1_the_users_own_launch_from_the_shell_is_their_fix(store):
    """The same request; this time the user starts Quillpad themselves from Start (a press in the shell right before
    its process started): that is learned at once."""
    worker = Worker(store)
    P.record_run(store, no_window_run())
    _row(store, NOW + 20.0, "explorer", "click", resolved="point", role="Button", element_key="k-start")
    _row(store, NOW + 21.0, "searchhost", "keys", resolved="focus", chord="Enter")
    sw = _row(store, NOW + 22.0, "quillpad", "switch", proc_started=NOW + 21.5, facts={"window_verified": "quillpad"})
    worker.answers.append({"same_goal": True, "intent": "open Quillpad", "steps": [{"from": f"a{sw}"}]})
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].written.get("fixed")
    assert P.get(store, res[0].written["fixed"][0]).run == [{"do": "open_app", "name": "Quillpad", "app": "quillpad"}]


def test_v1_only_a_row_that_could_bring_the_window_up_reaches_it(store):
    target = {"kind": "window", "names": ["Quillpad"]}
    base = {"id": 1, "at": NOW + 20, "source": "user", "resolved": "focus",
            "facts": json.dumps({"window_verified": "quillpad"})}
    assert not L.reaches(store, target, {**base, "kind": "type", "process": "browsr"})
    assert not L.reaches(store, target, {**base, "kind": "scroll", "process": "browsr"})
    assert not L.reaches(store, target, {**base, "kind": "keys", "process": "browsr"})     # a shortcut elsewhere
    assert L.reaches(store, target, {**base, "kind": "click", "process": "explorer"})       # the taskbar, Start
    assert L.reaches(store, target, {**base, "kind": "keys", "process": "quillpad"})
    sw = {**base, "id": 3, "kind": "switch", "process": "quillpad", "resolved": "event"}
    typing = {"id": 2, "at": NOW + 18.5, "ended_at": NOW + 19.9, "kind": "type", "process": "browsr", "source": "user"}
    alt_tab = {"id": 2, "at": NOW + 19.8, "ended_at": NOW + 19.85, "kind": "keys", "process": "browsr",
               "source": "user"}
    assert not L.reaches(store, target, sw, rows=[typing, sw])          # caused by typing: a window came up by itself
    assert L.reaches(store, target, sw, rows=[alt_tab, sw])             # caused by a shortcut (Alt+Tab)
    assert not L.reaches(store, target, sw, rows=[sw])                  # no input of theirs recorded before it


def _page_run(request_id: str) -> dict[str, Any]:
    return _run(request_id, "open the team roster", [
        _act("open_url", "browsr", {"url": "https://wiki.example.org/team/roster"}, at=NOW + 1.0, verdict=False)])


def test_v1_a_page_that_finished_loading_under_the_users_scroll_is_not_their_fix(store):
    """Yuki's page was still loading; it reached the address while the user scrolled. The trace puts page_reached on
    the scroll row and writes a page-change row labelled user after it: no press caused it, nothing is learned."""
    from yuki.memory.store import TimelineRow

    worker = Worker(store)
    P.record_run(store, _page_run("p1"))
    key = store.timeline_key("browsr", "wiki.example.org", "/team/roster", None)
    store.save_timeline(TimelineRow(id=None, started_at=NOW + 9, ended_at=NOW + 70, state="present",
                                    process="browsr", app="Browsr", host="wiki.example.org", path="/team/roster",
                                    page_key=key))
    row(store, NOW + 8.5, "browsr", "scroll", facts={"page_reached": key})
    row(store, NOW + 9.0, "browsr", "nav", resolved="event", facts={"page_reached": key}, outcome=None)
    res = fixer(worker).run_once(now=NOW + 700)
    assert [r.result for r in res] == ["the user did not reach what it was for"] and worker.calls == []


def test_v1_a_fix_from_the_trace_never_supersedes_a_way_the_user_taught(store):
    worker = Worker(store)
    taught = P.add_playbook(store, {"kind": "procedure", "intent": "open quillpad", "apps": ["quillpad"],
                                    "origin": "user", "body": {"steps": ["open it from Start"],
                                                               "run": [{"do": "open_app", "name": "Quillpad"}]}},
                            events=[{"type": "taught", "refs": {"via": "test"}}],
                            vector=vec(P.vector_text(store, "open Quillpad", None, ["quillpad"])), now=NOW)
    P.record_run(store, opening_run())
    worker.answers.append(fix_answer(users_fix(store), family=""))
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].written.get("fixed") and res[0].written["fixed"][0] in res[0].written["added"]
    assert taught in [o.id for o in P.live(store, ["procedure"])]
    assert "superseded" not in [e.type for e in P.ledger(store, taught)]


# -- V4: a hover is no change of the control -------------------------------------------------------------------------


def test_v4_a_hover_with_its_tooltip_never_reaches_a_click_target(tmp_path):
    """The verifier's scenario on the real trace worker: Yuki's click on Settings failed; the user only rests the
    pointer on Settings and its tooltip shows (a hover row, outcome changed). Nothing is learned, no model call."""
    rig = Rig(tmp_path)
    store = rig.store
    try:
        t0 = WALL0 + rig.clock.t
        P.record_run(store, _run("q1", "open settings in field notes", [
            {"tool": "click", "kind": "action", "app": "fieldnotes", "target": 'Button "Settings"',
             "input": {"role": "Button", "name": "Settings"}, "ok": False, "verdict": False,
             "why": "not on the screen", "result": "not found", "ms": 300.0, "at": t0 + 1.0}], at=t0))
        rig.clock.t += 20.0
        sink = FakeSink()
        rig.w.sink = sink
        rig.uia.points[(150, 120)] = button(name="Settings")
        sink.anchor_at, sink.anchor_seq, sink.moving, sink.travel = rig.clock.t, 1, True, 200.0
        rig.feed("moving", rig.clock.t)
        rig.clock.t += 0.5
        rig.w.tick()
        rig.feed("out", rig.clock.t + 0.05, EVENT_OBJECT_SHOW, 9, 0, 0, 1)
        rig.clock.t += 1.5
        sink.anchor_at, sink.anchor_seq, sink.moving = rig.clock.t, 2, True
        rig.feed("moving", rig.clock.t)
        rig.cursor = (400, 400)
        rig.settle()
        hover = [r for r in rig.rows() if r["kind"] == "hover"]
        assert hover and hover[0]["outcome"] == "changed"
        h = P.run_header(store, P.run_key("agent", "S1", "q1"))
        steps = P.run_steps(store, h.run_key)
        rows = L.trace_rows(store, h.at - 1, t0 + 600)
        target = L.run_target(store, h, steps, rows)
        assert target["kind"] == "element"
        assert not L.reaches(store, target, dict(hover[0]), rows=rows, h=h, steps=steps)
        assert L.reaches(store, target, {**dict(hover[0]), "kind": "click"}, rows=rows, h=h, steps=steps)
        worker = Worker(store)
        res = fixer(worker).run_once(now=t0 + 700)
        assert [r.result for r in res] == ["the user did not reach what it was for"] and worker.calls == []
    finally:
        rig.store.close()


# -- V5: the repeat waits for the user's route -----------------------------------------------------------------------


def _save_pitfall(store: Any) -> int:
    return P.add_playbook(store, {"kind": "pitfall", "intent": "save the draft in quillpad", "apps": ["quillpad"],
                                  "body": {"text": "the Save click did nothing", "route": {
                                      "app": "quillpad", "step": {"do": "click", "role": "Button", "name": "Save"},
                                      "element_keys": [], "run_step": 1}}},
                          events=[{"type": "observed", "run_key": "agent:S1:old"}], vector=vec("save draft"), now=NOW)


def test_v5_the_user_clicking_the_very_control_that_failed_is_no_repeat_and_their_way_is_not_lowered(store):
    worker = Worker(store)
    pit = _save_pitfall(store)
    P.record_run(store, _run("s2", "save my draft", [
        _act("click", "quillpad", {"role": "Button", "name": "Save"}, at=NOW + 2.0, ok=False, verdict=False)]))
    save = row(store, NOW + 20.0, "quillpad", "click", role="Button", name="Save", key="k-save")
    worker.answers.append({"same_goal": True, "intent": "save the draft in Quillpad", "steps": [{"from": f"a{save}"}]})
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].written.get("fixed") and not res[0].written.get("repeated")
    assert P.get(store, pit).n_observed == 1                          # no repeat: the user's route takes that step
    way = res[0].written["fixed"][0]
    found = P.relevant(store, query_vec=vec("save the draft in quillpad"), apps_in_play=["quillpad"])
    item = next(w for w in found["ways"] if w["id"] == way)
    assert not item.get("lowered")
    assert next(p for p in found["pitfalls"] if p["id"] == pit).get("warning") != "strong"


def test_v5_the_same_route_failing_again_with_no_fix_of_the_user_still_adds_to_its_pitfall(store):
    worker = Worker(store)
    pit = _save_pitfall(store)
    P.record_run(store, _run("s3", "save my draft", [
        _act("click", "quillpad", {"role": "Button", "name": "Save"}, at=NOW + 2.0, ok=False, verdict=False)]))
    assert fixer(worker).run_once(now=NOW + 40) == []                 # not yet: the user's route is not known
    assert P.get(store, pit).n_observed == 1
    res = fixer(worker).run_once(now=NOW + 700)
    assert [r.result for r in res] == ["the user did not reach what it was for"]
    assert P.get(store, pit).n_observed == 2


# -- V6: a failed route lowers ways in its own app only ----------------------------------------------------------------


def test_v6_a_route_that_failed_in_one_app_never_lowers_a_way_in_another(store):
    send = {"do": "click", "role": "Button", "name": "Send"}
    mine = P.add_playbook(store, {"kind": "procedure", "intent": "send the report", "apps": ["quillpad"],
                                  "origin": "user", "body": {"steps": ["click Send"], "run": [send]}},
                          events=[{"type": "taught", "refs": {"via": "test"}}], vector=vec("send the report"), now=NOW)
    chat = P.add_playbook(store, {"kind": "procedure", "intent": "send the report in chatter", "apps": ["chatter"],
                                  "origin": "user", "body": {"steps": ["click Send"], "run": [send]}},
                          events=[{"type": "taught", "refs": {"via": "test"}}], vector=vec("send the report"), now=NOW)
    P.add_playbook(store, {"kind": "pitfall", "intent": "send in chatter", "apps": ["chatter"],
                           "body": {"text": "Send did nothing", "route": {"app": "chatter", "step": send,
                                                                          "element_keys": []}}},
                   events=[{"type": "observed", "run_key": "agent:S1:x"}], vector=vec("send chatter"), now=NOW)
    found = P.relevant(store, query_vec=vec("send the report"), apps_in_play=["quillpad", "chatter"])
    by_id = {w["id"]: w for w in found["ways"]}
    assert not by_id[mine].get("lowered") and by_id[chat].get("lowered") is True
