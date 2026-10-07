"""capC1b (report 78 §6.3, report 97): the online correction learner, on action-trace rows built in the test.

A scripted failure: Yuki's opening of an invented app ("Quillpad") brought up another app's window (WordMill); the
user's own trace then shows them opening Quillpad (its window verified). That fix is learned at once (decision D3:
``user_fixed``, active) through the learner's check-and-apply path, and used on the next same request: practice
memory's relevant() shows it, the talker may offer it, and the skill runner runs its steps. Yuki's failed route becomes
a pitfall that lowers rank only; the same route failing again is a strong warning, still never a block. An unrelated
action, a fix that never reaches the target, and Yuki's own or stale rows teach nothing (and cost no model call).

Offline: temp databases, invented apps and names, a word-bag embedder and a stand-in for the model; nothing touches
the desktop, the real memory or the network.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from test_c_practice import NOW, Worker, store, vec  # noqa: F401 - the store fixture
from yuki.memory import practice as P
from yuki.memory import practice_learn as L
from yuki.memory import trace_store
from yuki.memory.trace_privacy import normalise_name

END = NOW + 3.0
WORDS = "open quillpad"


def opening_run(request_id: str = "r1", *, at: float = NOW, verdict: bool | None = False,
                outcome: str = "final") -> dict[str, Any]:
    """Yuki's opening: it asked for Quillpad and WordMill's window came up (not verified)."""
    steps = [{"tool": "launch_app", "kind": "action", "app": "wordmill", "target": 'app "Quillpad"',
              "input": {"name": "Quillpad"}, "ok": True, "verdict": verdict,
              "why": "the window in front is WordMill's, not Quillpad's",
              "result": "started 'Quillpad'; in front: wordmill.exe \"WordMill\"", "ms": 900.0, "at": at + 1.0}]
    return {"source": "agent", "session_id": "S1", "request_id": request_id, "lane": "typed", "at": at,
            "ended_at": at + 3.0, "words": WORDS, "reply": "Opened it.", "outcome": outcome, "steps": steps,
            "wall_s": 3.0}


def row(store: Any, at: float, process: str, kind: str, *, source: str = "user", resolved: str = "point",
        role: str | None = None, name: str | None = None, key: str | None = None, chord: str | None = None,
        facts: dict | None = None, outcome: str | None = "changed", during_run: int = 0, typed: int | None = None,
        position: str | None = None, run_key: str | None = None) -> int:
    r = {"at": at, "ended_at": at + 0.1, "process": process, "kind": kind, "source": source, "resolved": resolved,
         "role": role, "name_ciphertext": store._enc(name) if name else None,
         "name_digest": store.cipher.digest("ui-name", process, role or "", normalise_name(name)) if name else None,
         "element_key": key, "chord": chord, "facts": json.dumps(facts) if facts else None, "outcome": outcome,
         "during_run": during_run, "typed": typed, "position": position, "run_key": run_key}
    els = []
    if key:
        from yuki.memory.trace_privacy import name_class

        els.append({"element_key": key, "process": process, "role": role, "name_class": name_class(role),
                    "name": name, "at": at})
    return trace_store.write_batch(store, [r], els)[0]


def users_fix(store: Any, *, at: float = NOW) -> dict[str, int]:
    """The user's own actions after the failure: Start, the search box, Enter, a chat reply (a detour), then
    Quillpad's window in front (verified). Plus Yuki's own effect (WordMill's window), labelled yuki."""
    ids = {"yuki": row(store, at + 1.2, "wordmill", "switch", source="yuki", resolved="event", during_run=1,
                       facts={"window_verified": "wordmill"})}
    ids["start"] = row(store, at + 20, "explorer", "click", role="Button", name="Start", key="k-start")
    ids["search"] = row(store, at + 22, "searchhost", "type", resolved="focus", role="Edit", key="k-search", typed=8)
    ids["enter"] = row(store, at + 23, "searchhost", "keys", resolved="focus", chord="Enter")
    ids["detour"] = row(store, at + 24, "chatter", "click", role="Button", name="Reply", key="k-reply")
    ids["open"] = row(store, at + 26, "quillpad", "switch", resolved="event", facts={"window_verified": "quillpad"})
    return ids


def fix_answer(ids: dict[str, int], **over: Any) -> dict[str, Any]:
    out = {"same_goal": True,
           "steps": [{"from": f"a{ids['open']}", "do": "Open Quillpad by its own program; its window comes up"}],
           "intent": "open Quillpad", "family": "opening a desktop app",
           "pitfall": {"text": "opening 'Quillpad' by name brought up WordMill's window",
                       "instead": "open Quillpad by its own program and check its window", "from": [1]},
           "why": "the user opened it themselves"}
    out.update(over)
    return out


def fixer(worker: Worker, **kw: Any) -> L.CorrectionLearner:
    return L.CorrectionLearner(L.Learner(worker), **kw)


class Rules:
    def __init__(self, fix: str) -> None:
        self.learning = (("fix_after_failure", fix),)


# -- the scripted failure, learned and used ------------------------------------------------------------------------


def test_a_failed_opening_fixed_by_the_user_is_learned_at_once_and_used_on_the_next_request(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    ids = users_fix(store)
    worker.answers.append(fix_answer(ids, family=""))   # (the word-bag embedder: the family's words would dilute it)
    res = fixer(worker).run_once(now=NOW + 40)                     # well inside the 10-minute look
    assert len(res) == 1 and res[0].result == "learned your fix" and res[0].written.get("fixed")
    call = worker.calls[-1]
    assert call["tool"]["name"] == "record_fix" and call["system"] == L.FIX_PROMPT
    sent = call["user"]
    assert f"a{ids['open']}" in sent and "<- reached it" in sent.split(f"a{ids['open']}")[1].split("\n")[0]
    assert f"a{ids['yuki']} " not in sent                           # Yuki's own row is never shown as the user's
    assert 'Button "Start"' in sent and "typed 8 characters" in sent  # a command name; typed text never
    way = P.get(store, res[0].written["fixed"][0])
    assert way.origin == "fixed" and way.run == [{"do": "open_app", "name": "Quillpad", "app": "quillpad"}]
    assert way.state == "active" and way.confidence == pytest.approx(0.8)    # D3: used at once
    events = P.ledger(store, way.id)
    assert [e.type for e in events if e.type in P.SUCCESS_EVENTS] == ["user_fixed"]
    assert P.learned_way("procedure", events) and P.provenance(store, way)[0] == "user"
    assert (way.body.get("trace") or {}).get("actions") == [ids["open"]]
    # Yuki's failed route: a pitfall with the route, quiet (never on the Playbook page)
    pits = [o for o in P.live(store, ["pitfall"]) if (o.body or {}).get("route")]
    assert len(pits) == 1 and pits[0].body["route"]["step"] == {"do": "open_app", "name": "Quillpad"}
    # looked at once only; the run is now judged failed (the user had to do it)
    assert fixer(worker).run_once(now=NOW + 100) == []
    head = P.run_header(store, P.run_key("agent", "S1", "r1"))
    assert head.signals[L.FIX_KEY]["result"] == "learned your fix" and P.judged_ok(store, head.run_key) is False
    # the next same request: relevant() shows it, the talker may offer it, the frame names it
    found = P.relevant(store, query_vec=vec(P.intent_words(store, WORDS)), apps_in_play=["quillpad"], detail=True)
    item = next(w for w in found["ways"] if w["id"] == way.id)
    assert item["run"] == way.run and item["state"] == "active"
    from yuki.voice.practice import qualifies

    assert qualifies(item), (item["cosine"], item["confidence"], item["state"])   # the talker may run it
    from yuki.agent import frame as task_frame

    fr = task_frame.build({}, practice=found)
    assert f"[pb:{way.id}]" in fr.text and len(fr.text) <= task_frame.FRAME_CHARS


class _Dispatcher:
    def overview_text(self) -> tuple[str, dict, float]:
        return "", {"foreground_hwnd": 0, "windows": []}, 0.0


class FakeHands:
    """The talker's hands as the skill runner uses them for an open_app step (nothing reaches the desktop)."""

    def __init__(self) -> None:
        self._dispatcher = _Dispatcher()
        self.acts: list[tuple[str, dict]] = []

    def _act(self, name: str, args: dict, facts: dict) -> tuple[bool, str, dict]:
        self.acts.append((name, dict(args)))
        return True, f"opened {args.get('name')}: its own window is in front", {**facts, "verified": True}


def test_run_skill_runs_the_learned_fix_as_its_steps(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    worker.answers.append(fix_answer(users_fix(store)))
    fixer(worker).run_once(now=NOW + 40)
    found = P.relevant(store, query_vec=vec(P.intent_words(store, WORDS)), apps_in_play=["quillpad"], detail=True)
    item = next(w for w in found["ways"] if w.get("run"))
    from yuki.voice.skills import SkillRunner

    hands = FakeHands()
    ok, report, facts = SkillRunner(hands).run({"steps": item["run"], "sends": False}, item=item, words=[WORDS])
    assert ok and facts["verified"] is True and hands.acts == [("open_app", {"name": "Quillpad"})]


# -- what is never learned -----------------------------------------------------------------------------------------


def test_an_unrelated_action_after_a_failure_learns_nothing_and_costs_no_call(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    row(store, NOW + 20, "chatter", "click", role="Button", name="Reply", key="k-reply")
    row(store, NOW + 30, "chatter", "keys", resolved="focus", chord="Ctrl+K")
    row(store, NOW + 31, "chatter", "switch", resolved="event", facts={"window_verified": "chatter"})
    assert fixer(worker).run_once(now=NOW + 60) == []               # within the window: waits, nothing written
    res = fixer(worker).run_once(now=END + L.FIX_WINDOW_S + 1)
    assert [r.result for r in res] == ["the user did not reach what it was for"]
    assert worker.calls == [] and P.live(store) == []


def test_a_fix_that_never_reaches_the_target_learns_nothing(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    row(store, NOW + 20, "explorer", "click", role="Button", name="Start", key="k-start")
    row(store, NOW + 22, "searchhost", "type", resolved="focus", role="Edit", key="k-search", typed=8)
    row(store, NOW + 26, "wordmill", "switch", resolved="event", facts={"window_verified": "wordmill"})
    row(store, NOW + 40, "sketchbox", "switch", resolved="event", facts={"window_verified": "sketchbox"})
    res = fixer(worker).run_once(now=END + L.FIX_WINDOW_S + 1)
    assert [r.result for r in res] == ["the user did not reach what it was for"]
    assert worker.calls == [] and P.live(store) == []


def test_yuki_labelled_and_stale_rows_are_never_evidence(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    row(store, NOW + 26, "quillpad", "switch", source="yuki", resolved="event", during_run=0,
        facts={"window_verified": "quillpad"})
    row(store, NOW + 30, "quillpad", "switch", resolved="stale", facts={"window_verified": "quillpad"})
    res = fixer(worker).run_once(now=END + L.FIX_WINDOW_S + 1)
    assert [r.result for r in res] == ["the user did not reach what it was for"] and worker.calls == []


def test_a_step_citing_a_yuki_row_or_one_not_shown_is_refused_and_the_target_action_is_kept(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    ids = users_fix(store)
    worker.answers.append(fix_answer(ids, steps=[{"from": f"a{ids['yuki']}", "do": "Yuki's own window"},
                                                 {"from": "a99999", "do": "an invented action"}]))
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].written.get("fixed")
    assert any("not one of the user's own actions shown" in r for r in res[0].rejected)
    way = P.get(store, res[0].written["fixed"][0])
    assert way.body["trace"]["actions"] == [ids["open"]]           # only the user's action that reached it


def test_a_run_that_worked_by_itself_takes_no_way_from_what_the_user_did(store):
    worker = Worker(store)
    P.record_run(store, opening_run(verdict=True))
    P.set_learned(store, P.run_key("agent", "S1", "r1"), "skipped", "nothing to learn", now=NOW + 5)
    users_fix(store)
    res = fixer(worker).run_once(now=NOW + 40)
    assert [r.result for r in res] == ["it worked by itself: nothing to learn from what the user did"]
    assert worker.calls == [] and P.live(store, ["procedure"]) == []


def test_chatting_elsewhere_during_a_run_that_worked_is_no_takeover_and_teaches_nothing(store):
    worker = Worker(store)
    P.record_run(store, opening_run(verdict=True))
    row(store, NOW + 1.5, "chatter", "type", resolved="focus", role="Edit", key="k-msg", typed=12, during_run=1)
    row(store, NOW + 30, "quillpad", "switch", resolved="event", facts={"window_verified": "quillpad"})
    assert fixer(worker).run_once(now=NOW + 40) == []                # waits for the learner's judgement
    P.set_learned(store, P.run_key("agent", "S1", "r1"), "skipped", "nothing to learn", now=NOW + 95)
    res = fixer(worker).run_once(now=NOW + 100)
    assert [r.result for r in res] == ["it worked by itself: nothing to learn from what the user did"]
    assert worker.calls == [] and P.live(store) == []


def test_the_model_saying_it_is_not_the_same_goal_writes_nothing(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    users_fix(store)
    worker.answers.append({"same_goal": False})
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].result == "not the same goal" and P.live(store) == []


# -- takeover, element targets, the preference, D3 -----------------------------------------------------------------


def saving_run(request_id: str = "r2") -> dict[str, Any]:
    """Yuki clicked Quillpad's Save button; nothing showed (could not tell); the user took over meanwhile."""
    steps = [{"tool": "click", "kind": "action", "app": "quillpad", "target": 'Button "Save"',
              "input": {"role": "Button", "name": "Save"}, "control": "Button|Save", "ok": True, "verdict": None,
              "why": "the report could not tell", "result": "clicked Button 'Save'", "ms": 300.0, "at": NOW + 2.0},
             {"tool": "hotkey", "kind": "action", "app": "quillpad", "target": "keys", "input": {"keys": ["ctrl", "s"]},
              "ok": True, "verdict": True, "why": "verified", "result": "pressed", "ms": 100.0, "at": NOW + 3.0}]
    return {"source": "agent", "session_id": "S1", "request_id": request_id, "lane": "typed", "at": NOW,
            "ended_at": NOW + 8.0, "words": "save my draft", "reply": "Saved.", "outcome": "final", "steps": steps,
            "wall_s": 8.0}


def test_a_takeover_mid_run_reaching_the_same_control_is_learned(store):
    worker = Worker(store)
    P.record_run(store, saving_run())
    menu = row(store, NOW + 4.0, "quillpad", "keys", resolved="focus", chord="Alt+F", during_run=1)
    save = row(store, NOW + 5.0, "quillpad", "click", role="Button", name="Save", key="k-save", during_run=1)
    worker.answers.append({"same_goal": True, "intent": "save the draft in Quillpad",
                           "steps": [{"from": f"a{menu}", "do": "Press Alt+F"},
                                     {"from": f"a{save}", "do": "Click Save"}]})
    res = fixer(worker).run_once(now=NOW + 30)
    way = P.get(store, res[0].written["fixed"][0])
    assert way.run == [{"do": "keys", "keys": ["alt", "f"]}, {"do": "click", "role": "Button", "name": "Save"}]
    assert "[while Yuki's job ran]" in worker.calls[-1]["user"]
    # Yuki's click could not show its effect, but the user's own way clicks that same Save: no failed route
    assert not [o for o in P.live(store, ["pitfall"]) if (o.body or {}).get("route")]


def test_a_content_control_or_typing_without_a_slot_keeps_the_way_a_hint(store):
    worker = Worker(store)
    P.record_run(store, saving_run("r3"))
    item = row(store, NOW + 4.0, "quillpad", "click", role="ListItem", name="Draft for Ravi", key="k-item",
               during_run=1, position="List")
    save = row(store, NOW + 5.0, "quillpad", "click", role="Button", name="Save", key="k-save", during_run=1)
    worker.answers.append({"same_goal": True, "intent": "save the draft in Quillpad",
                           "steps": [{"from": f"a{item}", "do": "Pick the draft"}, {"from": f"a{save}"}]})
    res = fixer(worker).run_once(now=NOW + 30)
    way = P.get(store, res[0].written["fixed"][0])
    assert way.run == [] and len(way.steps) == 2                    # text only: the thinker's hint
    assert "Draft for Ravi" not in worker.calls[-1]["user"]          # a content control's name is never shown


def test_a_preference_from_the_correction_goes_through_the_taught_path(store):
    worker = Worker(store)
    run = opening_run()
    P.record_run(store, run)
    store.add_turn("S1", "r1-next", NOW + 10, "no, not wordmill, the quill one", "", [], "final")
    ids = users_fix(store)
    worker.answers.append(fix_answer(ids, preference={"text": "for notes the user means Quillpad, not WordMill",
                                                      "quote": "not wordmill, the quill one",
                                                      "default": "Quillpad"}))
    res = fixer(worker).run_once(now=NOW + 40)
    habits = P.live(store, ["habit"])
    assert len(habits) == 1 and res[0].written.get("fixed")
    assert any(e.type == "taught" for e in P.ledger(store, habits[0].id))
    page = P.workflows(store)
    assert any(c["id"] == habits[0].ref for c in page["procedures"] + page["proposals"])
    assert P.workflow_edit(store, habits[0].ref, "forget")["ok"]    # undoable on the Playbook page


def test_a_preference_quote_the_user_never_said_is_dropped(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    ids = users_fix(store)
    worker.answers.append(fix_answer(ids, preference={"text": "always WordMill", "quote": "I love wordmill"}))
    res = fixer(worker).run_once(now=NOW + 40)
    assert res[0].written.get("fixed") and P.live(store, ["habit"]) == []


def test_after_confirm_in_d3_makes_the_fix_a_hint_until_confirmed(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    worker.answers.append(fix_answer(users_fix(store)))
    res = fixer(worker, trace_rules=lambda: Rules("after_confirm")).run_once(now=NOW + 40)
    way = P.get(store, res[0].written["fixed"][0])
    assert [e.type for e in P.ledger(store, way.id) if e.type in P.SUCCESS_EVENTS] == ["demonstrated"]
    assert way.state == "candidate"


def test_one_way_per_run_whichever_learner_saw_it_first(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    worker.answers.append(fix_answer(users_fix(store)))
    fixer(worker).run_once(now=NOW + 40)
    assert L._demonstrated_from(store, P.run_key("agent", "S1", "r1"))   # the appOpen recheck skips it


def test_budget_spent_waits_and_a_model_error_is_tried_again(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    ids = users_fix(store)
    learner = L.Learner(worker, postmortem_budget_usd=0.0)
    assert L.CorrectionLearner(learner).run_once(now=NOW + 40) == [] and worker.calls == []
    head = P.run_header(store, P.run_key("agent", "S1", "r1"))
    assert L.FIX_KEY not in head.signals                             # not given up: looked at again later

    class Broken(Worker):
        def _call(self, **kw: Any) -> Any:
            raise RuntimeError("model down")

    broken = Broken(store)
    res = fixer(broken).run_once(now=NOW + 50)
    assert res[0].error and L.FIX_KEY not in P.run_header(store, head.run_key).signals
    worker.answers.append(fix_answer(ids))
    assert fixer(worker).run_once(now=NOW + 80)[0].written.get("fixed")


# -- pitfalls lower rank, never block ------------------------------------------------------------------------------


def _way(store: Any, intent: str, run: list[dict], apps: list[str]) -> int:
    return P.add_playbook(store, {"kind": "procedure", "intent": intent, "apps": apps, "origin": "user",
                                  "body": {"steps": [f"step of {intent}"], "run": run}},
                          events=[{"type": "taught", "refs": {"via": "test"}}], vector=vec(intent), now=NOW)


def test_a_pitfall_lowers_the_rank_of_a_way_taking_its_route_and_never_blocks_it(store):
    failed = [{"do": "open_app", "name": "Quillpad"}]
    a = _way(store, "open quillpad notes", failed, ["quillpad"])
    b = _way(store, "open quillpad editor", [{"do": "open_app", "name": "Quillpad", "app": "quillpad"}], ["quillpad"])
    q = vec("open quillpad notes")
    before = [w["id"] for w in P.relevant(store, query_vec=q, apps_in_play=["quillpad"])["ways"]]
    assert before[0] == a
    P.add_playbook(store, {"kind": "pitfall", "intent": "open quillpad", "apps": ["quillpad"],
                           "body": {"text": "opening by name showed WordMill", "route":
                                    {"app": "quillpad", "step": failed[0], "element_keys": []}}},
                   events=[{"type": "observed", "run_key": "agent:S1:x"}], vector=vec("open quillpad"), now=NOW)
    found = P.relevant(store, query_vec=q, apps_in_play=["quillpad"])
    order = [w["id"] for w in found["ways"]]
    assert order == [b, a]                                          # lower, still there
    assert next(w for w in found["ways"] if w["id"] == a).get("lowered") is True


def test_the_same_route_failing_again_is_a_strong_warning_still_never_a_block(store):
    worker = Worker(store)
    P.record_run(store, opening_run("r1"))
    worker.answers.append(fix_answer(users_fix(store)))
    fixer(worker).run_once(now=NOW + 40)
    later = NOW + 3600.0
    P.record_run(store, opening_run("r9", at=later))
    worker.answers.append(fix_answer(users_fix(store, at=later)))
    res = fixer(worker).run_once(now=later + 40)
    assert res[0].written.get("repeated")
    pits = [o for o in P.live(store, ["pitfall"]) if (o.body or {}).get("route")]
    assert len(pits) == 1 and pits[0].n_observed == 2
    found = P.relevant(store, query_vec=vec(P.intent_words(store, WORDS)), apps_in_play=["wordmill", "quillpad"])
    pit = next(p for p in found["pitfalls"] if p["id"] == pits[0].id)
    assert pit["warning"] == "strong" and pit["repeats"] == 2
    assert found["ways"]                                            # the way is still offered
    from yuki.agent import frame as task_frame

    text = task_frame.build({}, practice=found).text
    assert "STRONG WARNING" in text and "still allowed" in text


# -- the practice worker's pass and the doctor -----------------------------------------------------------------------


def test_the_practice_workers_trace_pass_learns_waits_in_a_game_and_writes_health(store):
    worker = Worker(store)
    P.record_run(store, opening_run())
    ids = users_fix(store)
    full = {"fullscreen": True}
    pw = L.PracticeWorker(store, worker, live_fn=lambda: full, idle_fn=lambda: 0.0, clock=lambda: NOW + 40)
    out = pw.trace_pass(NOW + 40)
    assert out.get("fix_waiting") == "fullscreen" and worker.calls == []
    full["fullscreen"] = False
    worker.answers.append(fix_answer(ids))
    out = pw.trace_pass(NOW + 41)                                   # the user is busy: no idle wait for this one
    assert out["fixed"][0]["result"] == "learned your fix"
    health = json.loads(store.nudge_state("trace:learn"))
    assert health["fixes"] == 1 and health["pitfalls"] == 1
    from yuki.core.doctor import _trace_learn_words

    words = _trace_learn_words(store.nudge_state("trace:learn"), {"hung_skipped": 3})
    assert "learned 1 way from your own fixes" in words and "apps that stopped answering" in words
