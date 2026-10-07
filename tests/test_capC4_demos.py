"""capC4 (report 78 §6.1-6.2, §11 D3/D8; report 100): the demo learner - the user's way learned from what they did.

A task the user did themselves (C2's ``ui_tasks``, its outcome a checked fact) becomes a procedure: code builds the
steps from the trace rows, a stubbed model picks the essence, code checks it. One demo is a trial ("your way, seen
once": a hint, never run alone); two with the same outcome kind on the same target, or one of Yuki's runs that used it
and worked, make a way. Never from unclear, stale, unresolved, Yuki's, rejected or detour evidence. Typing is always a
slot. Long demos run in parts of at most 8 steps, at most 4 parts.

Offline: temp databases, invented apps, names and goals; the model is a stand-in.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_c_practice import Worker, Words, vec
from test_capC2_links import DAY, NOW, store  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory import trace_demos as TD
from yuki.memory import trace_store
from yuki.memory import trace_tasks as T
from yuki.memory.trace_privacy import name_class


# -- building trace rows and tasks ---------------------------------------------------------------------------------

def row(store: Any, at: float, process: str, kind: str, *, source: str = "user", resolved: str = "point",
        role: str | None = None, name: str | None = None, key: str | None = None, chord: str | None = None,
        position: str | None = None, facts: dict[str, Any] | None = None, aid: str | None = None,
        proc_started: float | None = None, typed: int | None = None, page_key: str | None = None,
        file_ext: str | None = None) -> int:
    """One trace row as C1 writes it (a content control's name only as its keyed digest)."""
    cls = name_class(role, (position or "").partition("/")[2] or position) if role else None
    r = {"at": at, "ended_at": at + 0.2, "process": process, "kind": kind, "source": source, "resolved": resolved,
         "role": role, "position": position, "element_key": key, "chord": chord, "typed": typed,
         "name_ciphertext": store._enc(name) if name and cls != "content" else None,
         "name_digest": TD.name_digest(store, process, role or "", name) if name else None,
         "aid_ciphertext": store._enc(aid) if aid else None,
         "aid_digest": store.cipher.digest("ui-aid", process, aid) if aid else None,
         "proc_started": proc_started, "facts": json.dumps(facts) if facts else None, "page_key": page_key,
         "file_ext": file_ext, "outcome": "changed"}
    els = [{"element_key": key, "process": process, "role": role, "name_class": cls, "name": name if cls != "content"
            else None, "at": at}] if key else []
    return trace_store.write_batch(store, [r], els)[0]


def task(store: Any, ids: list[int], goal: str, outcome: str | None, outcome_id: int | None, *,
         target: str | None = None, detour: tuple[int, int] | None = None, verdict: str | None = None,
         family: int | None = None) -> int:
    """One task over the rows ``ids`` (one piece; a detour range inside it), as the task pass writes it."""
    ats = {r["id"]: float(r["at"]) for r in store._query(
        "SELECT id, at FROM ui_actions WHERE id IN (%s)" % ",".join("?" * len(ids)), ids)}
    start, end = min(ats.values()), max(ats.values())

    def op(conn: Any) -> int:
        tid = int(conn.execute(
            "INSERT INTO ui_tasks(window_start, window_end, started_at, ended_at, apps, goal_ciphertext, outcome_fact,"
            " outcome_action, outcome_target, family_id, user_verdict, actions, created_at) VALUES"
            " (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (start - 1, end + 60, start, end, "[]", store._enc(goal), outcome or T.UNCLEAR, outcome_id, target,
             family, verdict, len(ids), end + 60)).lastrowid)
        conn.execute("INSERT INTO ui_task_actions(task_id, first_action, last_action, detour) VALUES (?,?,?,0)",
                     (tid, min(ids), max(ids)))
        if detour:
            conn.execute("INSERT INTO ui_task_actions(task_id, first_action, last_action, detour) VALUES (?,?,?,1)",
                         (tid, detour[0], detour[1]))
        return tid

    return store._write(op)


def chat_send(store: Any, t0: float, *, source: str = "user", enter_resolved: str = "focus", field: str = "k-msg",
              app: str = "chatter", with_detour: bool = False) -> tuple[list[int], int]:
    """The user opens the chat app, the quick switcher, types a channel, Enter, types a message, Enter (sent)."""
    ids = [row(store, t0, app, "switch", resolved="event", facts={"window_verified": app}),
           row(store, t0 + 2, app, "keys", resolved="focus", chord="Ctrl+K", role="Edit", key="k-switch"),
           row(store, t0 + 4, app, "type", resolved="focus", role="Edit", key="k-switch", typed=6),
           row(store, t0 + 5, app, "keys", resolved="focus", chord="Enter", role="Edit", key="k-switch",
               facts={"window_verified": app})]
    if with_detour:
        ids.append(row(store, t0 + 7, app, "click", role="Button", name="Reply", key="k-reply"))
    ids += [row(store, t0 + 9, app, "type", resolved="focus", role="Edit", key=field, typed=22),
            row(store, t0 + 10, app, "keys", source=source, resolved=enter_resolved, chord="Enter", role="Edit",
                key=field, facts={"field_submitted": {"typed": 22}})]
    return ids, ids[-1]


def answer(ids: list[int], *, slots: dict[int, str] | None = None, intent: str = "send a message in a channel",
           **over: Any) -> dict[str, Any]:
    slots = slots or {}
    return {"teaches": True, "steps": [{"from": f"a{i}", **({"slot": slots[i]} if i in slots else {})} for i in ids],
            "intent": intent, "family": "messaging in a chat app", "why": "the user's own way", **over}


def learner(store: Any, answers: list[Any]) -> L.Learner:
    return L.Learner(Worker(store, answers), embedder=Words())


def demos(store: Any) -> list[P.Playbook]:
    return [o for o in P.live(store, ["procedure"], include_disabled=True) if o.origin == "demo"]


def chat_demo(store: Any, t0: float, **kw: Any) -> tuple[int, list[int]]:
    ids, out = chat_send(store, t0, **kw)
    steps = [i for i in ids if store._query("SELECT kind FROM ui_actions WHERE id = ?", (i,))[0][0] != "click"]
    tid = task(store, ids, "You sent a message in the team channel", "field_submitted", out,
               target=kw.get("field", "k-msg"), detour=(ids[4], ids[4]) if kw.get("with_detour") else None)
    return tid, steps


def taskrow(store: Any, tid: int) -> dict[str, Any]:
    return dict(store._query("SELECT * FROM ui_tasks WHERE id = ?", (tid,))[0])


# -- one demo: a trial ---------------------------------------------------------------------------------------------

def test_one_demo_gives_a_trial_a_hint_only_never_run_alone(store):
    tid, steps = chat_demo(store, NOW)
    lrn = learner(store, [answer(steps, slots={steps[2]: "channel", steps[4]: "message"})])
    res = lrn.learn_task(taskrow(store, tid), now=NOW + 120)
    assert res.result == "learned your way", res.rejected
    [o] = demos(store)
    assert o.state == "candidate" and o.trial and o.label() == P.TRIAL_LABEL == "your way, seen once"
    assert o.confidence < 0.70                                     # under the talker's floor
    parts = o.parts
    assert len(parts) == 1 and len(parts[0]["run"]) == 6 and parts[0]["end"]["kind"] == "field_submitted"
    assert [s["do"] for s in parts[0]["run"]] == ["open_app", "keys", "type", "keys", "type", "keys"]
    assert o.body["sends"] is True                                 # the last Enter sent what was typed
    ev = [e for e in P.ledger(store, o.id) if e.type == "demonstrated"]
    assert len(ev) == 1 and ev[0].run_key.startswith(f"demo:{tid}:")
    # the brain sees it as a hint with its source; the talker is never offered it
    out = P.relevant(store, query_vec=vec("send a message in a channel"), apps_in_play=["chatter"], detail=True)
    [w] = out["ways"]
    assert w["trial"] is True and w["label"] == "your way, seen once" and w["source"] == "learned from what you did"
    from yuki.voice.practice import qualifies

    assert not qualifies(w)
    # and the skill runner refuses it outright
    from yuki.voice.skills import SkillRefused, SkillRunner

    with pytest.raises(SkillRefused, match="seen only once"):
        SkillRunner(object()).run({"steps": [], "sends": True, "to": "x", "what": "y"}, item=w, words=["x y"])
    page = P.workflows(store)
    assert any(c["id"] == o.ref and c["trial"] and c["source"] == "learned from what you did"
               for c in page["proposals"])


# -- two demos: a way ----------------------------------------------------------------------------------------------

def test_two_demos_with_the_same_outcome_on_the_same_target_make_a_way(store):
    t1, s1 = chat_demo(store, NOW)
    t2, s2 = chat_demo(store, NOW + DAY)
    ans = lambda s: answer(s, slots={s[2]: "channel", s[4]: "message"})   # noqa: E731
    lrn = learner(store, [ans(s1), ans(s2)])
    lrn.learn_task(taskrow(store, t1), now=NOW + 120)
    lrn.learn_task(taskrow(store, t2), now=NOW + DAY + 120)
    [o] = demos(store)                                             # the second one is a new version of the first
    assert o.state == "active" and not o.trial and o.confidence >= 0.70
    keys = sorted(e.run_key for e in P.ledger(store, o.id) if e.type == "demonstrated")
    assert len(keys) == 2 and keys[0].split(":")[2] == keys[1].split(":")[2]   # the same target
    from yuki.voice.practice import qualifies

    [w] = P.relevant(store, query_vec=vec("send a message in a channel"), apps_in_play=["chatter"],
                     detail=True)["ways"]
    assert qualifies(w) and not w.get("trial") and w["run"]        # the talker may run it now


def test_different_targets_do_not_combine(store):
    t1, s1 = chat_demo(store, NOW, field="k-msg")
    t2, s2 = chat_demo(store, NOW + DAY, field="k-other-field")
    ans = lambda s: answer(s, slots={s[2]: "channel", s[4]: "message"})   # noqa: E731
    lrn = learner(store, [ans(s1), ans(s2)])
    lrn.learn_task(taskrow(store, t1), now=NOW + 120)
    lrn.learn_task(taskrow(store, t2), now=NOW + DAY + 120)
    objs = demos(store)
    assert len(objs) == 2 and all(o.trial and o.state == "candidate" for o in objs)
    # even one ledger holding both targets stays a trial (evaluate groups by target)
    events = [P.Event(NOW, "demonstrated", 1.0, P.demo_key(1, "aaaa")), P.Event(NOW + 1, "demonstrated", 1.0,
                                                                              P.demo_key(2, "bbbb"))]
    got = P.evaluate("procedure", events, created_at=NOW, now=NOW + 10)
    assert got["state"] == "candidate" and got["trial"] and got["demos"] == 1 and got["demo_targets"] == 2
    same = [P.Event(NOW, "demonstrated", 1.0, P.demo_key(1, "aaaa")), P.Event(NOW + 1, "demonstrated", 1.0,
                                                                            P.demo_key(2, "aaaa"))]
    assert P.evaluate("procedure", same, created_at=NOW, now=NOW + 10)["state"] == "active"
    # the same task twice is one demo
    twice = [P.Event(NOW, "demonstrated", 1.0, P.demo_key(1, "aaaa")), P.Event(NOW + 1, "demonstrated", 1.0,
                                                                             P.demo_key(1, "aaaa"))]
    assert P.evaluate("procedure", twice, created_at=NOW, now=NOW + 10)["state"] == "candidate"


def test_a_trial_that_one_of_yukis_runs_used_and_verified_is_a_way(store):
    tid, steps = chat_demo(store, NOW)
    learner(store, [answer(steps, slots={steps[2]: "channel", steps[4]: "message"})]).learn_task(
        taskrow(store, tid), now=NOW + 120)
    [o] = demos(store)
    # a run that only showed the same thing (unverified) is no proof
    P.add_events(store, [{"playbook_id": o.id, "type": "observed", "run_key": "agent:S1:r1", "at": NOW + 200}],
                 now=NOW + 200)
    assert P.get(store, o.id).state == "candidate"
    # a run of Yuki's that cited it and worked (the learner's ran_ok) makes it a way
    P.add_events(store, [{"playbook_id": o.id, "type": "ran_ok", "run_key": "agent:S1:r2", "at": NOW + 300}],
                 now=NOW + 300)
    o = P.get(store, o.id)
    assert o.state == "active" and not o.trial and o.label() != P.TRIAL_LABEL


# -- zero ways or trials from bad evidence -------------------------------------------------------------------------

def test_nothing_from_unclear_or_rejected_tasks_and_no_model_call(store):
    ids, out = chat_send(store, NOW)
    unclear = task(store, ids, "You did something in the chat", None, None)
    rejected = task(store, ids, "You sent a message", "field_submitted", out, target="k-msg", verdict="not_mine")
    worker = Worker(store, [])
    lrn = L.Learner(worker, embedder=Words())
    for tid in (unclear, rejected):
        res = lrn.learn_task(taskrow(store, tid), now=NOW + 120)
        assert res.result.startswith("not learnable") and not res.written
    got = L.demo_pass(lrn, NOW + 120)
    assert worker.calls == [] and demos(store) == [] and "demos" not in got
    assert TD.load_state(store)["cursor"] >= rejected               # passed for good


@pytest.mark.parametrize("bad", ["yuki", "stale", "unresolved", "detour"])
def test_nothing_from_yukis_stale_unresolved_or_detour_rows(store, bad):
    if bad == "detour":
        ids, out = chat_send(store, NOW, with_detour=True)
        tid = task(store, ids, "You sent a message in the team channel", "field_submitted", out, target="k-msg",
                   detour=(ids[4], ids[4]))
        cite = ids                                                  # the model cites the detour's click too
    else:
        kw = {"yuki": {"source": "yuki"}, "stale": {"enter_resolved": "stale"},
              "unresolved": {"enter_resolved": "unresolved"}}[bad]
        ids, out = chat_send(store, NOW)
        # the step the model cites (the message's Enter) is the bad row; the task's outcome is on another, good row
        good_out = row(store, NOW + 12, "chatter", "keys", resolved="focus", chord="Enter", role="Edit", key="k-msg",
                       facts={"field_submitted": {"typed": 1}})
        store._write(lambda c: c.execute("UPDATE ui_actions SET source = ?, resolved = ? WHERE id = ?",
                                         (kw.get("source", "user"), kw.get("enter_resolved", "focus"), out)))
        ids.append(good_out)
        tid = task(store, ids, "You sent a message in the team channel", "field_submitted", good_out, target="k-msg")
        cite = ids
    worker = Worker(store, [answer(cite)])
    res = L.Learner(worker, embedder=Words()).learn_task(taskrow(store, tid), now=NOW + 120)
    assert demos(store) == [] and not res.written.get("demo"), res
    assert any("nothing is learned" in r or "not one of the user's actions shown" in r for r in res.rejected)


def test_nothing_when_the_checked_outcome_rests_on_a_bad_row(store):
    ids, out = chat_send(store, NOW, enter_resolved="stale")
    tid = task(store, ids, "You sent a message in the team channel", "field_submitted", out, target="k-msg")
    res = learner(store, [answer(ids)]).learn_task(taskrow(store, tid), now=NOW + 120)
    assert demos(store) == [] and "checked outcome" in res.rejected[0]


def test_a_send_step_comes_only_from_the_users_own_trace(store):
    ids, out = chat_send(store, NOW, source="yuki")               # Yuki's own Enter sent it
    tid = task(store, ids, "You sent a message in the team channel", "field_submitted", out, target="k-msg")
    res = learner(store, [answer(ids)]).learn_task(taskrow(store, tid), now=NOW + 120)
    assert demos(store) == []
    assert res.rejected                                              # never the user's way, never a send it may repeat


# -- typing is a slot (D8) -----------------------------------------------------------------------------------------

def test_never_fixed_typed_text(store):
    tid, steps = chat_demo(store, NOW)
    # the model names no slot for the first typing: it is still a slot (the request's words), never text
    learner(store, [answer(steps, slots={steps[4]: "message"})]).learn_task(taskrow(store, tid), now=NOW + 120)
    [o] = demos(store)
    types = [s for p in o.parts for s in p["run"] if s["do"] == "type"]
    # no slot named: the request's own words (run_text shows "<what to type, in their words>"); never a text field
    assert len(types) == 2 and all(set(s) <= {"do", "slot", "expect"} for s in types)
    assert [s.get("slot", "") for s in types] == ["", "message"]
    blob = json.dumps(o.body)
    assert "text" not in {k for p in o.parts for s in p["run"] for k in s}
    assert P.clean_run([{"do": "type", "text": "hello there", "slot": "message"}]) == [{"do": "type",
                                                                                       "slot": "message"}]
    assert "hello" not in blob


# -- parts ---------------------------------------------------------------------------------------------------------

def long_demo(store: Any, t0: float, *, apps: list[str], per_app: int, state_at_switch: bool = True) -> tuple[int,
                                                                                                              list[int]]:
    """``per_app`` named clicks in each app, an app switch between them (its window verified), a file saved at the
    end (the outcome)."""
    ids, t = [], t0
    for k, app in enumerate(apps):
        ids.append(row(store, t, app, "switch", resolved="event",
                       facts={"window_verified": app} if state_at_switch else None))
        t += 1
        for i in range(per_app):
            ids.append(row(store, t, app, "click", role="Button", name=f"Step {k}-{i}", key=f"k-{app}-{i}"))
            t += 1
    out = row(store, t, apps[-1], "keys", resolved="focus", chord="Ctrl+S", role="Document", key="k-doc",
              facts={"file_saved": "d-file"}, file_ext=".txt")
    ids.append(out)
    tid = task(store, ids, "You copied the figures into the notes", "file_saved", out, target="d-file")
    return tid, ids


def test_long_demos_run_in_parts_of_at_most_8_steps_each_ending_on_a_checked_state(store):
    tid, ids = long_demo(store, NOW, apps=["sheets", "notesy"], per_app=5)       # 2 + 10 + 1 = 13 steps
    learner(store, [answer(ids, intent="copy the figures into my notes")]).learn_task(taskrow(store, tid),
                                                                                      now=NOW + 300)
    [o] = demos(store)
    parts = o.parts
    assert 2 <= len(parts) <= P.MAX_PARTS and all(1 <= len(p["run"]) <= 8 for p in parts)
    assert sum(len(p["run"]) for p in parts) == 13
    assert parts[0]["end"]["kind"] == "window_verified" and parts[-1]["end"]["kind"] == "file_saved"
    assert parts[0]["run"][-1]["do"] == "open_app" or parts[1]["run"][0]["do"] == "open_app"
    assert "run" not in o.body                                       # more than one part: never one flat run
    detail = o.detail()
    assert detail["parts"] == parts and len(detail["run"]) == 13
    assert len(o.steps) <= P.MAX_PROC_STEPS and o.steps[0].startswith("Part 1: ")


def test_more_than_4_parts_or_no_checked_point_stays_a_hint(store):
    tid, ids = long_demo(store, NOW, apps=["a1", "a2", "a3", "a4", "a5"], per_app=7)   # 41 steps: > 4 parts
    res = learner(store, [answer(ids, intent="move the numbers across five apps")]).learn_task(
        taskrow(store, tid), now=NOW + 300)
    [o] = demos(store)
    assert o.parts == [] and o.run == [] and o.detail()["run"] == []
    assert any("hint" in r for r in res.rejected)
    tid2, ids2 = long_demo(store, NOW + DAY, apps=["solo"], per_app=10)          # 12 steps, one app: no cut point
    res2 = learner(store, [answer(ids2, intent="fill in the long form")]).learn_task(taskrow(store, tid2),
                                                                                    now=NOW + DAY + 300)
    assert any("no app switch or link point" in r for r in res2.rejected)
    # an app switch with no checked state there is no cut point either
    tid3, ids3 = long_demo(store, NOW + 2 * DAY, apps=["left", "right"], per_app=5, state_at_switch=False)
    res3 = learner(store, [answer(ids3, intent="copy across two windows")]).learn_task(taskrow(store, tid3),
                                                                                      now=NOW + 2 * DAY + 300)
    assert any("hint" in r for r in res3.rejected)


def test_clean_parts_keeps_at_most_4_parts_of_at_most_8_steps():
    step = {"do": "keys", "keys": ["enter"]}
    end = {"kind": "window_verified", "app": "x"}
    assert P.clean_parts([{"run": [step] * 8, "end": end}] * 4)
    assert P.clean_parts([{"run": [step] * 9, "end": end}]) == []
    assert P.clean_parts([{"run": [step], "end": end}] * 5) == []
    assert P.clean_parts([{"run": [step], "end": {"kind": "made_up"}}]) == []
    assert P.MAX_RUN_STEPS == 8 and P.MAX_PARTS == 4


# -- pick, aid, command steps by code -------------------------------------------------------------------------------

def test_a_content_click_becomes_a_pick_by_digest_or_slot_and_a_command_is_the_thinkers(store):
    t0 = NOW
    ids = [row(store, t0, "tunes", "switch", resolved="event", facts={"window_verified": "tunes"}),
           row(store, t0 + 2, "tunes", "click", role="ListItem", name="Rainy Mornings", position="List",
               key="k-pl", facts={"selected": True})]
    tid = task(store, ids, "You played your usual playlist", "selected", ids[-1], target="k-pl")
    learner(store, [answer(ids, intent="play my usual playlist")]).learn_task(taskrow(store, tid), now=NOW + 60)
    [o] = demos(store)
    pick = o.parts[0]["run"][-1]
    assert pick["do"] == "pick" and pick["in"] == {"role": "List"} and pick["role"] == "ListItem"
    assert pick["item"] == TD.name_digest(store, "tunes", "ListItem", "Rainy Mornings")[:P.PICK_DIGEST_CHARS]
    assert "Rainy" not in json.dumps(o.body)                        # the item's name is never kept
    # with a slot the item is named by the request
    step = P.demo_step({"kind": "click", "role": "ListItem", "position": "List", "process": "tunes",
                        "name_digest": "ab" * 32}, None, slot="playlist")
    assert step == {"do": "pick", "role": "ListItem", "app": "tunes", "in": {"role": "List"}, "slot": "playlist"}
    # a Warp command: a step for the thinker's terminal tool only
    cmd = P.demo_step({"kind": "command", "process": "warp"}, None, slot="build command")
    assert cmd == {"do": "command", "slot": "build command", "app": "warp"}
    assert P.clean_run([cmd]) == [] and P.runnable_parts([{"run": [cmd], "end": {"kind": "command_ok"}}]) is False


def test_warp_commands_make_parts_the_skill_runner_never_runs(store):
    ids = [row(store, NOW, "warp", "switch", resolved="event", facts={"window_verified": "warp"}),
           row(store, NOW + 3, "warp", "command", resolved="app_data", facts={"command_ok": True})]
    tid = task(store, ids, "You built the planner in the terminal", "command_ok", ids[-1], target="warp")
    learner(store, [answer(ids, slots={ids[-1]: "build command"}, intent="build the planner")]).learn_task(
        taskrow(store, tid), now=NOW + 60)
    [o] = demos(store)
    assert any(s["do"] == "command" for p in o.parts for s in p["run"])
    assert o.detail()["run"] == [] and "run" not in o.body        # the talker gets no steps to run
    from yuki.voice.skills import SkillRefused, SkillRunner, parse_steps

    item = {"ref": o.ref, "id": o.id, "intent": o.intent, "apps": o.apps, **o.detail(), "trial": False}
    with pytest.raises(SkillRefused, match="terminal command"):   # even once it is a way
        SkillRunner(object()).run({"steps": []}, item=item, words=["build the planner"])
    with pytest.raises(SkillRefused):
        parse_steps([{"do": "command"}])


def test_an_automation_id_only_where_that_apps_ids_are_proven(store):
    def click(at: float, started: float, aid: str, key: str = "k-save", app: str = "editr") -> dict[str, Any]:
        rid = row(store, at, app, "click", role="Button", name="Save", key=key, aid=aid, proc_started=started)
        return dict(store._query(f"SELECT {TD.COLUMNS} FROM ui_actions WHERE id = ?", (rid,))[0])

    one = click(NOW, 1000.0, "SaveBtn")
    assert TD.proven_aid(store, one) is None                         # one session: not proven
    two = click(NOW + DAY, 2000.0, "SaveBtn")
    assert TD.proven_aid(store, two) == "SaveBtn"
    assert P.demo_step(two, "Save", aid="SaveBtn") == {"do": "click", "role": "Button", "name": "Save",
                                                       "aid": "SaveBtn"}
    # an element of that app seen with two different ids across sessions: the app's ids are not proven
    click(NOW, 1000.0, "Row_1", key="k-row")
    click(NOW + DAY, 2000.0, "Row_7", key="k-row")
    assert TD.proven_aid(store, two) is None


# -- the Day page's corrections, the pass, families, the page, the doctor --------------------------------------------

def test_a_rejection_on_the_day_page_withdraws_the_demo_and_an_undo_learns_it_again(store):
    tid, steps = chat_demo(store, NOW)
    ans = answer(steps, slots={steps[2]: "channel", steps[4]: "message"})
    worker = Worker(store, [ans, ans])
    lrn = L.Learner(worker, embedder=Words())
    L.demo_pass(lrn, NOW + 120)
    [o] = demos(store)
    T.correct_task(store, tid, "not_mine")
    got = L.demo_pass(lrn, NOW + 200)
    assert got["demo_sync"]["withdrawn"] == 1 and got["demo_sync"]["retired"] == 1
    assert demos(store) == [] and P.get(store, o.id).valid_to is not None
    T.undo_correction(store, tid)
    got = L.demo_pass(lrn, NOW + 300)
    assert got["demo_sync"]["redo"] == 1 and len(demos(store)) == 1 and len(worker.calls) == 2


def test_forgetting_the_hour_takes_the_way_learned_from_it_and_undo_brings_it_back(store):
    tid, steps = chat_demo(store, NOW)
    ans = answer(steps, slots={steps[2]: "channel", steps[4]: "message"})
    worker = Worker(store, [ans, ans])
    lrn = L.Learner(worker, embedder=Words())
    L.demo_pass(lrn, NOW + 120)
    assert len(demos(store)) == 1
    gone = trace_store.forget(store, since=NOW - 1, until=NOW + 60, now=NOW + 150)
    assert gone["tasks"] == 1
    got = L.demo_pass(lrn, NOW + 200)
    assert got["demo_sync"]["withdrawn"] == 1 and demos(store) == []
    trace_store.undo_forget(store, gone["diary_id"], now=NOW + 250)
    got = L.demo_pass(lrn, NOW + 300)
    assert got["demo_sync"]["redo"] == 1 and len(demos(store)) == 1


def test_the_pass_waits_in_the_gate_and_spends_from_the_shared_pool(store):
    tid, steps = chat_demo(store, NOW)
    worker = Worker(store, [answer(steps)])
    lrn = L.Learner(worker, embedder=Words())
    got = L.demo_pass(lrn, NOW + 120, blocked=lambda: "call")
    assert got["demos_waiting"] == "call" and worker.calls == []
    got = L.demo_pass(lrn, NOW + 120, blocked=lambda: None)
    assert len(worker.calls) == 1 and worker.calls[0]["tool"]["name"] == "record_demo"
    kinds = [r[0] for r in store._query("SELECT kind FROM portrait_runs")]
    assert kinds == [L.DEMO_KIND] and L.pool_of(L.DEMO_KIND) == "shared"
    health = json.loads(store.nudge_state(TD.HEALTH_KEY))
    assert health["trials"] == 1 and health["learned_today"] == 1
    from yuki.core.doctor import _trace_demos_words

    words = _trace_demos_words(json.dumps(health), NOW + 130)
    assert "learned 1 way from what you did" in words and "seen only once" in words and "pb:" not in words


def test_the_practice_worker_runs_the_demo_pass_only_inside_its_gate(store, monkeypatch):
    tid, steps = chat_demo(store, NOW)
    worker = Worker(store, [answer(steps)])
    pw = L.PracticeWorker(store, worker, embedder=Words(), clock=lambda: NOW + 120)
    monkeypatch.setattr(pw, "blocked", lambda need_idle=True: "busy")
    pw.task_pass(NOW + 120)
    assert worker.calls == []
    monkeypatch.setattr(pw, "blocked", lambda need_idle=True: None)
    out = pw.task_pass(NOW + 120)
    assert out.get("demos") and len(demos(store)) == 1


def test_a_family_links_to_its_way_and_relevant_prefers_the_way_over_the_path(store):
    from yuki.memory import trace_families as F

    t1, s1 = chat_demo(store, NOW)
    t2, s2 = chat_demo(store, NOW + DAY)
    F.refresh(store, Words(), now=NOW + DAY + 50)
    fid = store._query("SELECT family_id FROM ui_tasks WHERE id = ?", (t1,))[0][0]
    assert fid is not None
    ans = lambda s: answer(s, slots={s[2]: "channel", s[4]: "message"})   # noqa: E731
    lrn = learner(store, [ans(s1), ans(s2)])
    lrn.learn_task(taskrow(store, t1), now=NOW + DAY + 100)
    q = vec("You sent a message in the team channel")
    trial = P.relevant(store, query_vec=q, apps_in_play=["chatter"])
    assert trial["ways"][0].get("trial") and trial["path"] is not None   # a trial is a hint beside the path
    lrn.learn_task(taskrow(store, t2), now=NOW + DAY + 200)
    [o] = demos(store)
    assert store._query("SELECT playbook_id FROM ui_families WHERE id = ?", (fid,))[0][0] == o.id
    out = P.relevant(store, query_vec=q, apps_in_play=["chatter"])
    assert out["ways"][0]["id"] == o.id and not out["ways"][0].get("trial")
    assert out["path"] is None                                      # the way says it: the path gives way


def test_the_playbook_page_shows_the_source_and_your_way_seen_once():
    from yuki.ui.web_data import ORIGIN_WORDS, procedure_view, proposal_view

    card = {"id": "pb:4", "kind": "procedure", "title": "send a message in a channel", "steps": ["Open chatter"],
            "origin": "demo", "since": NOW, "apps": ["chatter"], "evidence": {"seen": 0}}
    prop = proposal_view(card)
    assert prop["basis"].startswith("Your way, seen once") and "learned from what you did" in prop["basis"]
    assert procedure_view(card)["learned"].startswith("Learned from what you did") and ORIGIN_WORDS["demo"]
